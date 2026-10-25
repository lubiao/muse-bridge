# SPDX-License-Identifier: Apache-2.0
"""Separate durable cursor, owner and queues; no sharing with Feishu state."""
import json
import time
from .state import State
from .files import filename


class WeixinState(State):
    def initialize(self, credentials, baseline):
        with self.lock, self.db:
            expected = {'open_id': credentials['user_id'], 'chat_id': credentials['user_id']}
            if self.owner() and self.owner() != expected:
                raise ValueError('owner_change_requires_new_state')
            self._put('owner', expected)
            if self.get('baseline_seq') is None:
                self._put('baseline_seq', baseline)
                self._put('attachment_baseline_seq', baseline)
                self._put('weixin_cursor', credentials.get('cursor', ''))
                self._put('weixin_context', {'token': credentials.get('context_token'),
                    'updated': credentials.get('context_updated', 0), 'version': 1})
                self._put('enabled_at', time.time())

    def ingest(self, result, user_id, max_chars=20000):
        # Cursor and every accepted message commit together. Replayed pages
        # cannot re-submit a Muse task; context updates also persist atomically.
        with self.lock, self.db:
            if self.owner() != {'open_id': user_id, 'chat_id': user_id}:
                raise ValueError('owner_not_initialized')
            for msg in result.get('msgs', []):
                if (msg.get('from_user_id') != user_id or msg.get('message_type') != 1
                        or msg.get('group_id') or msg.get('message_state') not in (None, 2)):
                    continue
                mid = str(msg.get('message_id') or '')
                if not mid or len(mid) > 128:
                    continue
                # Discard the pre-onboarding backlog, even if the cursor was old.
                created = msg.get('create_time_ms')
                if isinstance(created, (int, float)) and created / 1000 < self.get('enabled_at', 0):
                    continue
                seen_key = 'wx-received:' + mid
                if self.get(seen_key):
                    continue
                context = self.get('weixin_context', {})
                if msg.get('context_token'):
                    version = context.get('version', 0) + 1
                    self._put('weixin_context', {'token': msg['context_token'], 'updated': time.time(), 'version': version})
                    self._refresh_blocked_media()
                    self.db.execute("UPDATE outbox SET state='pending',next_attempt=0,error=NULL WHERE state='blocked'")
                items = (msg.get('item_list') or [])[:100]
                images = [item['image_item'] for item in items if item.get('type') == 2 and isinstance(item.get('image_item'), dict)]
                caption = ''.join((item.get('text_item') or {}).get('text', '') for item in items if item.get('type') == 1)
                if images:
                    self._insert_incoming('wx:' + mid + ':images', user_id, '', 'file', {'media_kind':'image',
                        'caption':caption,'images':[{'file_key':image,'filename':'image','size':None} for image in images]})
                for index, item in enumerate(items):
                    key = 'wx:' + mid + ':' + str(index)
                    if item.get('type') == 1:
                        if images:
                            continue
                        text = (item.get('text_item') or {}).get('text')
                        if not isinstance(text, str) or not text.strip():
                            continue
                        if text.strip() == '/status':
                            counts = self.counts()
                            unknown = counts['inbox'].get('unknown', 0) + counts['outbox'].get('unknown', 0)
                            blocked = counts['outbox'].get('blocked', 0)
                            self._queue_text('status:' + key, user_id,
                                f'微信桥接运行中。单文件上限 200 MiB；待核对 {unknown} 条；待会话恢复 {blocked} 条。')
                        elif len(text) > max_chars:
                            self._queue_text('oversize:' + key, user_id, '这条文字太长，请分成几条发送。')
                        else:
                            self._insert_incoming(key, user_id, text, 'text', {})
                    elif item.get('type') == 4 and isinstance(item.get('file_item'), dict):
                        file = item['file_item']
                        try:
                            size = int(file['len']) if file.get('len') is not None else None
                        except (TypeError, ValueError):
                            size = -1
                        self._insert_incoming(key, user_id, '', 'file', {
                            'filename': filename(file.get('file_name')), 'file_key': file, 'size': size})
                    elif item.get('type') != 2 or not isinstance(item.get('image_item'), dict):
                        self._queue_text('unsupported:' + key, user_id,
                            '目前支持文字、图片和普通文件附件。语音或视频请先以文件附件发送。')
                self._put(seen_key, True)
                self._put('last_weixin_incoming_at', time.time())
            if result.get('get_updates_buf'):
                self._put('weixin_cursor', result['get_updates_buf'])
            self._put('last_weixin_poll_at', time.time())

    def _insert_incoming(self, key, user, text, kind, payload):
        import uuid
        self.db.execute("INSERT OR IGNORE INTO inbox(id,chat_id,text,request_id,created,kind,payload) VALUES (?,?,?,?,?,?,?)",
            (key, user, text, str(uuid.uuid5(uuid.NAMESPACE_URL, 'muse-weixin:' + key)),
             self.clock(), kind, json.dumps(payload, ensure_ascii=False)))

    def claim_outgoing(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM outbox WHERE state='pending' AND next_attempt<=? ORDER BY rowid LIMIT 1",
                (self.clock(),)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE outbox SET state='preparing',attempts=attempts+1 WHERE id=?", (row['id'],))
            return dict(row)

    def outgoing_submitting(self, key):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET state='sending',first_attempt=COALESCE(first_attempt,?) WHERE id=?",
                (self.clock(), key))

    def outgoing_unknown(self, key):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET state='unknown',error='delivery_unknown' WHERE id=?", (key,))

    def outgoing_blocked(self, key, context_version):
        with self.lock, self.db:
            newer = self.get('weixin_context', {}).get('version', 0) > context_version
            if newer:
                self._refresh_blocked_media(key)
            self.db.execute("UPDATE outbox SET state=?,error='weixin_rejected',next_attempt=0 WHERE id=?",
                ('pending' if newer else 'blocked', key))

    def _refresh_blocked_media(self, key=None):
        # A rejected file may have an expired CDN receipt as well as an old
        # context. Keep the original source/cache but acquire a fresh receipt.
        query = "SELECT id,payload FROM outbox WHERE kind='file' AND "
        query += "id=?" if key is not None else "state='blocked'"
        for row in self.db.execute(query, (key,) if key is not None else ()):
            payload = json.loads(row['payload'])
            payload.pop('weixin_media', None)
            self.db.execute("UPDATE outbox SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row['id']))

    def recover(self):
        with self.lock, self.db:
            count = sum(self.db.execute(f"SELECT count(*) FROM {table} WHERE state='sending'").fetchone()[0]
                for table in ('inbox', 'outbox'))
            for table in ('inbox', 'outbox'):
                self.db.execute(f"UPDATE {table} SET state='unknown',error='restart_during_send' WHERE state='sending'")
                self.db.execute(f"UPDATE {table} SET state='pending' WHERE state='preparing'")
            return count

    def retained_files(self):
        with self.lock:
            keys = super().retained_files()
            for row in self.db.execute("SELECT id FROM outbox WHERE kind='file' AND state='blocked'"):
                keys.add('outbox:' + row['id'])
            return keys
