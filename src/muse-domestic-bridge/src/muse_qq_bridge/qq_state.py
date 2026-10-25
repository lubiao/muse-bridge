# SPDX-License-Identifier: Apache-2.0
"""Owner-bound QQ intake and conservative durable delivery stages."""
import json
import time
import uuid
from .state import State
from .files import filename
from .transport import attachment_name


class QQState(State):
    def initialize(self, credentials, baseline):
        with self.lock, self.db:
            expected = {'open_id': credentials['owner_id'], 'chat_id': credentials['owner_id']}
            if self.owner() and self.owner() != expected:
                raise ValueError('owner_change_requires_new_state')
            self._put('owner', expected)
            if self.get('baseline_seq') is None:
                self._put('baseline_seq', baseline)
                self._put('attachment_baseline_seq', baseline)
                self._put('enabled_at', time.time())

    def ingest(self, message, credentials, max_chars=20000):
        owner = credentials['owner_id']
        if (message.get('post_type') != 'message' or message.get('message_type') != 'private'
                or str(message.get('user_id')) != owner or str(message.get('user_id')) == credentials['bot_id']
                or str(message.get('self_id')) != credentials['bot_id'] or message.get('group_id')):
            return False
        items = message.get('message')
        mid = str(message.get('message_id') or '')
        if not isinstance(items, list) or not mid or len(mid) > 128:
            return False
        created = message.get('time')
        if isinstance(created, (int, float)) and created < int(self.get('enabled_at', 0)):
            return False
        with self.lock, self.db:
            if not self.is_owner(owner, owner):
                raise ValueError('owner_not_initialized')
            if self.get('qq-received:' + mid):
                return False
            # Combine text segments into one turn; file segments remain ordered
            # independent jobs with IDs stable across event/history replay.
            text = ''.join((i.get('data') or {}).get('text', '') for i in items if i.get('type') == 'text')
            images = [{'file_key':{'message_id':mid,'index':index},'filename':'image','size':None}
                for index,item in enumerate(items[:100]) if item.get('type') == 'image']
            if images:
                self._insert('qq:' + mid + ':images', owner, '', 'file', {'media_kind':'image','images':images,'caption':text})
            elif text.strip() == '/status':
                counts = self.counts()
                unknown = sum(counts[t].get('unknown', 0) for t in ('inbox', 'outbox'))
                self._queue_text('status:qq:' + mid, owner,
                    f'QQ 桥接运行中。单文件上限 200 MiB；待核对 {unknown} 条。')
            elif text.strip():
                if len(text) > max_chars:
                    self._queue_text('oversize:qq:' + mid, owner, '这条文字太长，请分成几条发送。')
                else:
                    self._insert('qq:' + mid + ':text', owner, text, 'text', {})
            unsupported = False
            for index, item in enumerate(items[:100]):
                if item.get('type') == 'file':
                    file = item.get('data') or {}
                    size = file.get('file_size', file.get('size'))
                    try:
                        size = int(size) if size is not None else None
                    except (TypeError, ValueError):
                        size = -1
                    file_id = file.get('file_id')
                    if not isinstance(file_id, str) or not file_id:
                        unsupported = True
                        continue
                    self._insert('qq:' + mid + ':' + str(index), owner, '', 'file', {
                        'filename': filename(attachment_name(file)), 'size': size,
                        'file_key': {'file_id': file_id, 'size': size}})
                elif item.get('type') not in ('text', 'reply', 'image'):
                    unsupported = True
            if unsupported:
                self._queue_text('unsupported:qq:' + mid, owner,
                    '目前支持文字、图片和普通文件附件。语音或视频请先以文件附件发送。')
            self._put('qq-received:' + mid, True)
            self._put('last_qq_incoming_at', time.time())
            return True

    def _insert(self, key, owner, text, kind, payload):
        self.db.execute("INSERT OR IGNORE INTO inbox(id,chat_id,text,request_id,created,kind,payload) VALUES (?,?,?,?,?,?,?)",
            (key, owner, text, str(uuid.uuid5(uuid.NAMESPACE_URL, 'muse-qq:' + key)),
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
            self.db.execute("UPDATE outbox SET state='sending',first_attempt=COALESCE(first_attempt,?) WHERE id=?", (self.clock(), key))

    def outgoing_unknown(self, key):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET state='unknown',error='delivery_unknown' WHERE id=?", (key,))

    def recover(self):
        with self.lock, self.db:
            count = sum(self.db.execute(f"SELECT count(*) FROM {t} WHERE state='sending'").fetchone()[0] for t in ('inbox','outbox'))
            for table in ('inbox','outbox'):
                self.db.execute(f"UPDATE {table} SET state='unknown',error='restart_during_send' WHERE state='sending'")
                self.db.execute(f"UPDATE {table} SET state='pending' WHERE state='preparing'")
            return count

    def retained_media(self):
        with self.lock:
            return {json.loads(row['payload'])['qq_file']['path'] for row in self.db.execute(
                "SELECT payload FROM outbox WHERE kind='file' AND (state IN ('pending','preparing','sending') OR (state='unknown' AND created>?))",
                (self.clock()-7*86400,)) if json.loads(row['payload']).get('qq_file')}
