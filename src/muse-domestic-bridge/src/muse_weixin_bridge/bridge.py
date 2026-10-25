# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from contextlib import nullcontext
import logging
import mimetypes
from pathlib import Path
import threading
import time
import uuid

from .config import Config, write_private_json
from .transport import ChannelError as FeishuError
from .muse import AuthExpired, DeliveryUnknown, MuseError
from .state import State
from .files import FileCache, FileError, MAX_FILE_BYTES, INLINE_FILE_BYTES
from .incoming_images import prepare_images

LOGGER = logging.getLogger(__name__)


def split_text(text: str, limit: int) -> list[str]:
    pieces = []
    while len(text) > limit:
        end = text.rfind("\n", 0, limit)
        end = end + 1 if end >= limit // 2 else limit
        pieces.append(text[:end])
        text = text[end:]
    if text:
        pieces.append(text)
    return pieces


class Bridge:
    def __init__(self, config: Config, state: State, muse, feishu):
        self.config, self.state, self.muse, self.feishu = config, state, muse, feishu
        self.stop_event = threading.Event()
        self.baseline_ready = threading.Event()
        self.threads: list[threading.Thread] = []
        self.health = {"running": True, "muse": "starting", "last_incoming_at": None,
                       "last_muse_event_at": None, "last_feishu_delivery_at": None}
        self.health_lock = threading.Lock()
        self.auth_episode = None
        self.files = FileCache(config.data_dir)
        self.file_transfer_lock = threading.Lock()

    def receive(self, message: dict) -> None:
        # Called on the SDK event loop: only a bounded local SQLite transaction.
        if message.get("chat_type") != "p2p":
            return
        sender, chat = message.get("sender_open_id"), message.get("chat_id")
        message_id, text = message.get("message_id"), message.get("text")
        is_file = message.get('kind') == 'file'
        if not all(isinstance(value, str) and value for value in (sender, chat, message_id)):
            return
        if not isinstance(text, str) or (not text and not is_file):
            return
        if len(message_id) > 256:
            return
        if not self.state.owner():
            if text.startswith("/bind ") and self.state.bind(text[6:].strip(), sender, chat):
                self.state.queue_text("bind:" + message_id, chat,
                    "已绑定。现在发给我的文字会进入你原来的 Muse 主聊天，Muse 回复和主动提醒会回到这里。")
            return
        if not self.state.is_owner(sender, chat):
            return
        with self.health_lock:
            self.health["last_incoming_at"] = time.time()
        if is_file:
            payload = message.get('file')
            if isinstance(payload, dict) and isinstance(payload.get('file_key'), str):
                self.state.enqueue_file(message_id, chat, payload)
            return
        if text.strip() == "/status":
            counts = self.state.counts()
            waiting = counts["inbox"].get("pending", 0)
            unknown = counts["inbox"].get("unknown", 0) + counts["outbox"].get("unknown", 0)
            self.state.queue_text("status:" + message_id, chat,
                f"桥接进程运行中。等待转交：{waiting}；需要核对的投递：{unknown}；Muse 状态：{self.health['muse']}。")
            return
        if len(text) > self.config.max_message_chars:
            self.state.queue_text("oversize:" + message_id, chat, "这条文字太长，请分成几条发送。")
            return
        self.state.enqueue_incoming(message_id, chat, text)

    def receive_muse(self, event: dict) -> None:
        if event.get("is_thread") is True or event.get("role") != "assistant" or not event.get("completed"):
            return
        text = event.get("text")
        if event.get('kind') == 'file' and isinstance(event.get('file'), dict):
            self.state.record_muse(event, [])
            return
        if not isinstance(text, str) or not text:
            return
        with self.health_lock:
            self.health["last_muse_event_at"] = time.time()
        self.state.record_muse(event, split_text(text, self.config.outbound_chunk_chars))

    def notify_owner(self, key: str, text: str):
        owner = self.state.owner()
        if owner:
            self.state.queue_text("notice:" + key, owner["chat_id"], text)

    def authentication_failed(self):
        with self.health_lock:
            self.health["muse"] = "需要更新登录信息"
            if self.auth_episode is None:
                self.auth_episode = uuid.uuid4().hex
            episode = self.auth_episode
        self.notify_owner("muse-auth:" + episode,
            "Muse 登录信息已失效，消息已保留在队列中，需要在桥接服务更新登录信息。")

    def authenticated(self, description):
        with self.health_lock:
            self.auth_episode = None
            self.health["muse"] = description

    def _watch(self):
        while not self.stop_event.is_set():
            try:
                history = self.muse.history(limit=self.config.history_limit)
                if self.state.get('image_baseline_seq') is None:
                    self.state.put('image_baseline_seq', max((int(event.get('seq') or 0) for event in history), default=0))
                if self.state.get('attachment_baseline_seq') is None:
                    # Upgrades must not dump old attachments into the private chat.
                    self.state.put('attachment_baseline_seq', max((int(event.get('seq') or 0) for event in history), default=0))
                if self.state.get("baseline_seq") is None:
                    baseline = max((int(event.get("seq") or 0) for event in history), default=0)
                    self.state.put("baseline_seq", baseline)
                else:
                    for event in history:
                        self.receive_muse(event)
                self.baseline_ready.set()
                self.authenticated("连接已校验")
                self.muse.watch(self.stop_event)
            except AuthExpired:
                self.baseline_ready.clear()
                self.authentication_failed()
                self.stop_event.wait(60)
            except Exception as error:
                self.health["muse"] = "正在重新连接"
                LOGGER.warning("muse_watch_interrupted type=%s", type(error).__name__)
                self.stop_event.wait(10)

    def _incoming(self):
        while not self.stop_event.is_set():
            if not self.baseline_ready.wait(1):
                continue
            row = self.state.claim_incoming()
            if row is None:
                self.stop_event.wait(0.5)
                continue
            submitting = row['kind'] != 'file'
            data = None
            try:
                with self.file_transfer_lock if row['kind'] == 'file' else nullcontext():
                    if row['kind'] == 'file':
                        payload = json.loads(row['payload'])
                        if payload.get('media_kind') == 'image':
                            refs = prepare_images(self, row, payload)
                            self.state.incoming_submitting(row['id'])
                            submitting = True
                            answer = self.muse.send_image_references(refs, payload.get('caption', ''), row['request_id'])
                        else:
                            size = payload.get('size')
                            if isinstance(size, int) and (size < 0 or size > MAX_FILE_BYTES):
                                raise FileError('file_too_large')
                            item = payload.get('muse_file_ref')
                            data = None
                            mime_type = mimetypes.guess_type(payload['filename'])[0] or 'application/octet-stream'
                            if not item:
                                cache_key = 'inbox:' + row['id']
                                data = self.files.read(cache_key)
                                if data is None:
                                    data = self.feishu.download_file(row['id'], payload['file_key'])
                                    self.files.write(cache_key, data)
                                if len(data) > INLINE_FILE_BYTES:
                                    item = self.muse.prepare_file_reference(payload['filename'], mime_type, data, row['request_id'])
                                    payload['muse_file_ref'] = item
                                    self.state.file_payload('inbox', row['id'], payload)
                            self.state.incoming_submitting(row['id'])
                            submitting = True
                            if item:
                                answer = self.muse.send_file_reference(item, row['request_id'])
                            else:
                                answer = self.muse.send_file(payload['filename'], mime_type, data, row['request_id'])
                    else:
                        answer = self.muse.send_text(row["text"], row["request_id"])
                    if answer.get("status") != "accepted":
                        raise DeliveryUnknown("unconfirmed_ack", "send")
                    if row['kind'] == 'file' and payload.get('media_kind') == 'image':
                        payload['muse_image_ack'] = {key:answer[key] for key in ('message_id','status') if key in answer}
                        self.state.file_payload('inbox', row['id'], payload)
                    self.state.incoming_result(row["id"], "sent")
                    self.authenticated("最近提交成功")
                    data = None
            except FileError as error:
                self.state.incoming_result(row['id'], 'failed', error.code)
                self.file_failed('in:' + row['id'], error.code)
            except FeishuError as error:
                # Resource reads happen before any Muse submission.
                self.state.incoming_result(row['id'], 'pending' if error.retryable else 'failed', error.category)
                if not error.retryable:
                    self.file_failed('in:' + row['id'], error.category)
                self.stop_event.wait(error.retry_after_seconds or 10)
            except AuthExpired:
                self.state.incoming_result(row["id"], "pending", "authentication_required")
                self.authentication_failed()
                self.stop_event.wait(60)
                # This error is only raised before a write; a later connection
                # rereads the private credential file and may safely retry.
            except DeliveryUnknown:
                self.state.incoming_result(row["id"], "unknown", "delivery_unknown")
                self.notify_owner("unknown:" + row["id"],
                    "刚才一条消息的提交结果暂时无法确认。为避免执行两次，我没有自动重发；请在 Muse 主聊天核对。")
            except MuseError as error:
                retryable = bool(getattr(error, "retryable", False))
                self.state.incoming_result(row["id"], "pending" if retryable else "failed", "muse_transport_error")
                if not retryable and row['kind'] == 'file':
                    self.file_failed('in:' + row['id'], 'muse_transport_error')
                self.stop_event.wait(10)
            except Exception as error:
                self.state.incoming_result(row["id"], "unknown" if submitting else "pending", "unexpected_send_error")
                LOGGER.warning("muse_send_interrupted type=%s", type(error).__name__)
                if submitting:
                    self.notify_owner("unknown:" + row["id"], "一条消息的提交结果需要在 Muse 主聊天核对，未自动重复提交。")
                else:
                    self.stop_event.wait(10)

    def _outgoing(self):
        while not self.stop_event.is_set():
            row = self.state.claim_outgoing()
            if row is None:
                self.stop_event.wait(0.5)
                continue
            try:
                if row['kind'] == 'file':
                    payload = json.loads(row['payload'])
                    if not payload.get('feishu_file_key'):
                        cache_key = 'outbox:' + row['id']
                        data = self.files.read(cache_key)
                        if data is None:
                            data = self.muse.read_file(payload['path'], payload.get('size'))
                            self.files.write(cache_key, data)
                        payload['feishu_file_key'] = self.feishu.upload_file(payload['filename'], data)
                        self.state.file_payload('outbox', row['id'], payload)
                    self.state.outgoing_submitting(row['id'])
                    remote_id = self.feishu.send_file(row['chat_id'], payload['feishu_file_key'], row['id'])
                else:
                    remote_id = self.feishu.send_text(row["chat_id"], row["text"], row["id"])
                self.state.outgoing_sent(row["id"], remote_id)
                with self.health_lock:
                    self.health["last_feishu_delivery_at"] = time.time()
            except FileError as error:
                self.state.outgoing_failed(row['id'], False, error.code)
                self.file_failed('out:' + row['id'], error.code)
            except AuthExpired:
                self.state.outgoing_failed(row['id'], True, 'authentication_required', 60)
                self.authentication_failed()
            except MuseError as error:
                self.state.outgoing_failed(row['id'], error.retryable, error.code, 15)
                if not error.retryable:
                    self.file_failed('out:' + row['id'], error.code)
            except FeishuError as error:
                delay = error.retry_after_seconds or min(180, 5 * 2 ** min(row["attempts"], 5))
                self.state.outgoing_failed(row["id"], error.retryable, error.category, delay)
                LOGGER.warning("feishu_send_interrupted category=%s retryable=%s", error.category, error.retryable)
                if not error.retryable and row['kind'] == 'file':
                    self.file_failed('out:' + row['id'], error.category)
            except Exception as error:
                # Same persisted Feishu UUID can be retried in its one-hour window.
                self.state.outgoing_failed(row["id"], True, "unexpected_transport_error", 15)
                LOGGER.warning("feishu_send_interrupted type=%s", type(error).__name__)

    def file_failed(self, key: str, code: str):
        if code == 'file_too_large':
            text = f'这份文件未转发：目前单个文件上限为 {MAX_FILE_BYTES // (1024 * 1024)} MiB，请缩小后重发。'
        elif code == 'attachment_cache_full':
            text = '这份文件未转发：附件缓存暂满，请稍后重发。'
        else:
            text = '这份文件未转发成功，请稍后重发；如果重复出现，需要检查桥接附件权限或文件状态。'
        self.notify_owner('file:' + key, text)

    def _health(self):
        next_cleanup = 0
        while not self.stop_event.is_set():
            with self.health_lock:
                snapshot = dict(self.health)
            snapshot.update({"updated_at": time.time(), "owner_bound": bool(self.state.owner()),
                             "queues": self.state.counts()})
            write_private_json(self.config.data_dir / "health.json", snapshot)
            if time.monotonic() >= next_cleanup:
                try:
                    self.files.cleanup(self.state.retained_files())
                except OSError:
                    LOGGER.warning('attachment_cache_cleanup_failed')
                next_cleanup = time.monotonic() + 60
            self.stop_event.wait(5)

    def start(self):
        interrupted = self.state.recover()
        if interrupted:
            self.notify_owner("restart-unknown:" + uuid.uuid4().hex, "服务已恢复。上次中断时有消息的提交结果未确认，未自动重复执行，请在 Muse 核对。")
        for name, target in (("muse-watch", self._watch), ("muse-send", self._incoming),
                             ("feishu-send", self._outgoing), ("health", self._health)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stop_event.set()
        deadline = time.monotonic() + 8
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))

