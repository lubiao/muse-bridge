# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from contextlib import nullcontext
import logging
import threading
import time
import uuid

from .bridge import Bridge
from .config import write_private_json
from .files import FileError, MAX_FILE_BYTES
from .muse import AuthExpired, MuseError
from .transport import ChannelError

LOG = logging.getLogger(__name__)


class WeixinBridge(Bridge):
    def _poll(self):
        try:
            self.feishu.api('msg/notifystart', {})
        except ChannelError:
            LOG.warning('weixin_start_notice_failed')
        failures = 0
        while not self.stop_event.is_set():
            if not self.baseline_ready.wait(1):
                continue
            self.health['weixin_poll_activity'] = time.time()
            try:
                result = self.feishu.get_updates(self.state.get('weixin_cursor', ''))
                self.state.ingest(result, self.feishu.credentials['user_id'], self.config.max_message_chars)
                self.health['weixin'] = 'polling'
                failures = 0
            except ChannelError as error:
                failures += 1
                self.health['weixin'] = 'login_required' if error.code == -14 else 'reconnecting'
                LOG.warning('weixin_poll_failed category=%s code=%s', error.category, error.code)
                # Official client pauses expired account sessions for one hour.
                self.stop_event.wait(3600 if error.code == -14 else min(60, 2 ** min(failures, 6)))

    def _outgoing(self):
        while not self.stop_event.is_set():
            row = self.state.claim_outgoing()
            if row is None:
                self.stop_event.wait(0.5)
                continue
            submitting = False
            context_version = self.state.get('weixin_context', {}).get('version', 0)
            try:
                with self.file_transfer_lock if row['kind'] == 'file' else nullcontext():
                    if row['kind'] == 'file':
                        payload = json.loads(row['payload'])
                        media = payload.get('weixin_media')
                        if media is None:
                            data = self.files.read('outbox:' + row['id'])
                            if data is None:
                                data = self.muse.read_file(payload['path'], payload.get('size'))
                                self.files.write('outbox:' + row['id'], data)
                            upload = self.feishu.upload_image if payload.get('media_kind') == 'image' else self.feishu.upload_file
                            media = upload(payload['filename'], data)
                            payload['weixin_media'] = media
                            self.state.file_payload('outbox', row['id'], payload)
                        # Drop the large buffer before waiting for chat delivery.
                        data = None
                    context_version = self.state.get('weixin_context', {}).get('version', 0)
                    if not self.state.get('weixin_context', {}).get('token'):
                        self.state.outgoing_blocked(row['id'], context_version)
                        continue
                    self.state.outgoing_submitting(row['id'])
                    submitting = True
                    client_id = 'muse-wx-' + uuid.uuid5(uuid.NAMESPACE_URL, row['id']).hex
                    if row['kind'] == 'file':
                        send = self.feishu.send_image if payload.get('media_kind') == 'image' else self.feishu.send_file
                        remote_id = send(row['chat_id'], media, client_id)
                    else:
                        remote_id = self.feishu.send_text(row['chat_id'], row['text'], client_id)
                    self.state.outgoing_sent(row['id'], remote_id)
                    self.health['last_weixin_delivery_at'] = time.time()
                    self.health['weixin'] = 'last_delivery_accepted'
            except ChannelError as error:
                if submitting:
                    if error.category == 'api':
                        # Explicit rejection is safe to retry with the next
                        # owner interaction; network ambiguity is not.
                        self.state.outgoing_blocked(row['id'], context_version)
                        self.health['weixin'] = 'delivery_rejected_waiting_for_user_message'
                    else:
                        self.state.outgoing_unknown(row['id'])
                        self.health['weixin'] = 'delivery_requires_check'
                else:
                    self.state.outgoing_failed(row['id'], error.retryable, error.category,
                        min(180, 5 * 2 ** min(row['attempts'], 5)))
                    if not error.retryable and row['kind'] == 'file':
                        self.file_failed('out:' + row['id'], error.category)
                LOG.warning('weixin_delivery_failed category=%s code=%s submitted=%s',
                    error.category, error.code, submitting)
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
            except Exception as error:
                if submitting:
                    self.state.outgoing_unknown(row['id'])
                else:
                    self.state.outgoing_failed(row['id'], True, 'preparation_interrupted', 30)
                LOG.warning('weixin_delivery_interrupted type=%s', type(error).__name__)

    def _health(self):
        next_cleanup = 0
        while not self.stop_event.is_set():
            snapshot = dict(self.health)
            snapshot.pop('last_feishu_delivery_at', None)
            snapshot.update(updated_at=time.time(), owner_bound=bool(self.state.owner()),
                queues=self.state.counts(), max_file_bytes=MAX_FILE_BYTES,
                last_poll=self.state.get('last_weixin_poll_at'),
                last_incoming=self.state.get('last_weixin_incoming_at'),
                context_updated=self.state.get('weixin_context', {}).get('updated'))
            write_private_json(self.config.data_dir / 'health.json', snapshot)
            if time.monotonic() >= next_cleanup:
                self.files.cleanup(self.state.retained_files())
                next_cleanup = time.monotonic() + 60
            self.stop_event.wait(5)

    def start(self):
        super().start()
        thread = threading.Thread(target=self._poll, name='weixin-poll', daemon=True)
        self.threads.append(thread)
        thread.start()
