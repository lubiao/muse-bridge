# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from .config import Config, write_private_json
from .feishu import FeishuError, MEDIA_TYPES

# Feishu sends containers that Python's built-in table does not know, and an
# unknown extension would make Muse see application/octet-stream.
for _ext, _type in MEDIA_TYPES.items():
    mimetypes.add_type(_type, _ext)
from .muse import AuthExpired, DeliveryUnknown, MuseError
from .state import State
from .files import FileCache, FileError, MAX_FILE_BYTES, INLINE_FILE_BYTES
from .incoming_images import prepare_images
from . import reader
from . import video_frames

LOGGER = logging.getLogger(__name__)

# Muse keeps UI-only widget references inline in the assistant's text, e.g.
# "...就不打扰[[hatch_widget:widget-<uuid>]]". They address widgets in Muse's own
# client and render as literal noise in a chat app, so drop just the wrapper.
_WIDGET_REFERENCE = re.compile(r"\[\[hatch_[a-z_]*widget[^\]]*\]\]", re.IGNORECASE)


def strip_client_markup(text: str) -> str:
    if not isinstance(text, str):
        return text
    cleaned = _WIDGET_REFERENCE.sub("", text)
    if not cleaned.strip():
        return text
    # Removing a reference leaves the space that separated it from the prose.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.rstrip(" \t")


def split_text(text: str, limit: int) -> list[str]:
    text = strip_client_markup(text)
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
        self.reader = None

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
        # Acknowledge every accepted message before any slow work. Media
        # downloads and a full Muse turn can take a while, and without a receipt
        # an unanswered message is indistinguishable from a lost one.
        # Fire-and-forget on a thread so the SDK event loop is never blocked.
        self._acknowledge_async(message_id)
        if is_file:
            payload = message.get('file')
            if isinstance(payload, dict) and (isinstance(payload.get('file_key'), str) or payload.get('media_kind') == 'image'):
                self.state.enqueue_file(message_id, chat, payload)
            return
        if text.strip() == "/status":
            counts = self.state.counts()
            waiting = counts["inbox"].get("pending", 0)
            unknown = counts["inbox"].get("unknown", 0) + counts["outbox"].get("unknown", 0)
            self.state.queue_text("status:" + message_id, chat,
                f"桥接进程运行中。等待转交：{waiting}；需要核对的投递：{unknown}；Muse 状态：{self.health['muse']}。")
            return
        if text.strip().startswith("/read"):
            self._read_feishu(message_id, chat, text.strip())
            return
        if len(text) > self.config.max_message_chars:
            self.state.queue_text("oversize:" + message_id, chat, "这条文字太长，请分成几条发送。")
            return
        self.state.enqueue_incoming(message_id, chat, text)

    def _send_video(self, row, payload: dict) -> dict:
        """Forward a clip as still frames, falling back to the file itself.

        Muse cannot look inside an ``.mp4``, so the representative frames are sent
        through the image channel it can read. When extraction is unavailable or
        fails, the original file is forwarded instead: a relayed video is worse
        than stills but far better than a dropped message.
        """
        data = self.files.read('inbox:' + row['id'])
        if data is None:
            data = self.feishu.download_file(row['id'], payload['file_key'])
            self.files.write('inbox:' + row['id'], data)
        size = payload.get('size')
        if isinstance(size, int) and (size < 0 or size > MAX_FILE_BYTES):
            raise FileError('file_too_large')

        frames: list[tuple[bytes, str]] = []
        with tempfile.TemporaryDirectory(dir=str(self.config.data_dir)) as workspace:
            source = Path(workspace) / (payload.get('filename') or 'video.mp4')
            source.write_bytes(data)
            frames = video_frames.extract_frames(source)
            seconds = video_frames.duration_seconds(source)
        if seconds is None:
            seconds = 0.0

        if not frames:
            # Fall back to the raw attachment, which is what this bridge did before.
            self.state.incoming_submitting(row['id'])
            item = self.muse.prepare_file_reference(payload['filename'],
                                                    'video/mp4', data, row['request_id'])
            return self.muse.send_file_reference(item, row['request_id'])

        refs = []
        for index, (frame, name) in enumerate(frames):
            # Frames come back in start→end order but may be fewer than requested,
            # so spread the labels across the actual count rather than assuming 3.
            ratio = (index / max(1, len(frames) - 1)) if len(frames) > 1 else 0.5
            offset = seconds * (0.1 + 0.8 * ratio)
            refs.append(self.muse.prepare_file_reference(
                name, 'image/jpeg', frame,
                row['request_id'] + ':frame:' + str(index)))
            payload.setdefault('frame_notes', []).append(
                video_frames.describe_offset(index, len(frames), offset))
        payload['muse_frame_refs'] = len(refs)
        self.state.file_payload('inbox', row['id'], payload)

        caption = "（用户发来一段视频，共 %s 秒。下面是它的 %d 张画面：%s。原视频已另存。）" % (
            int(round(seconds)) if seconds else '?', len(refs),
            '、'.join(payload.get('frame_notes') or []))
        self.state.incoming_submitting(row['id'])
        return self.muse.send_image_references(refs, caption, row['request_id'])

    def _acknowledge_async(self, message_id: str) -> None:
        """Put the receipt reaction on a worker thread.

        The SDK event loop must return within three seconds and the reaction is a
        network call, so it cannot run inline. Failures are already swallowed by
        FeishuClient.acknowledge.
        """
        def run() -> None:
            try:
                self.feishu.acknowledge(message_id)
            except Exception:
                pass

        threading.Thread(target=run, name="feishu-ack", daemon=True).start()

    def _read_feishu(self, message_id: str, chat: str, command: str) -> None:
        """``/read [N] [问题]`` pulls the Feishu timeline into a Muse turn.

        Runs on the SDK event loop, so it only reads a bounded number of messages
        and hands the result to the normal outbox; the Muse call happens on the
        worker thread like any other message.
        """
        parts = command.split(None, 2)
        count, question = 20, ""
        if len(parts) > 1:
            if parts[1].isdigit():
                count = max(1, min(int(parts[1]), reader.MAX_MESSAGES))
                if len(parts) > 2:
                    question = parts[2].strip()
            else:
                question = " ".join(parts[1:]).strip()
        try:
            rows = self.reader.recent_messages(count, chat_id=chat)
        except reader.FeishuReadError as error:
            reason = str(error)
            hint = ("读取失败：应用缺少消息读取权限，或这个会话不在应用的可用范围内。"
                    if "permission_missing" in reason else
                    "读取失败：" + reason)
            self.state.queue_text("readfail:" + message_id, chat, hint)
            return
        if not rows:
            self.state.queue_text("readempty:" + message_id, chat, "没有读到任何消息。")
            return
        transcript = reader.format_messages(
            rows, header=f"以下是飞书会话里最近 {len(rows)} 条消息（只读快照，时间按本机时区）：")
        prompt = ("这是我从飞书同步给你的上下文，请只基于这些内容回答。\n\n"
                  + transcript + "\n\n")
        prompt += ("我的问题：" + question) if question else \
                  "请概括上面的对话：谁在找我、有哪些待办或需要我回复的事。"
        # Reuse the normal inbound path so the answer comes back the same way.
        self.state.enqueue_incoming(message_id, chat, prompt)

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
            try:
                if row['kind'] == 'file':
                    payload = json.loads(row['payload'])
                    if payload.get('media_kind') == 'image':
                        refs = prepare_images(self, row, payload)
                        self.state.incoming_submitting(row['id'])
                        submitting = True
                        answer = self.muse.send_image_references(refs, payload.get('caption', ''), row['request_id'])
                    elif payload.get('media_kind') == 'media' and not payload.get('muse_file_ref'):
                        # A video is opaque to Muse; its frames are not. Send the
                        # stills first and keep the clip as the original record.
                        answer = self._send_video(row, payload)
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
                    image = payload.get('media_kind') == 'image'
                    remote_key = 'feishu_image_key' if image else 'feishu_file_key'
                    if not payload.get(remote_key):
                        cache_key = 'outbox:' + row['id']
                        data = self.files.read(cache_key)
                        if data is None:
                            data = self.muse.read_file(payload['path'], payload.get('size'))
                            self.files.write(cache_key, data)
                        upload = self.feishu.upload_image if image else self.feishu.upload_file
                        payload[remote_key] = upload(payload['filename'], data)
                        self.state.file_payload('outbox', row['id'], payload)
                    self.state.outgoing_submitting(row['id'])
                    send = self.feishu.send_image if image else self.feishu.send_file
                    remote_id = send(row['chat_id'], payload[remote_key], row['id'])
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

