# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid

from .config import private_directory


class State:
    def __init__(self, directory: Path, clock=time.time):
        private_directory(directory)
        self.path = directory / "bridge.sqlite3"
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, text TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', request_id TEXT NOT NULL,
                created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, text TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL,
                first_attempt REAL, next_attempt REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                remote_id TEXT);
            CREATE TABLE IF NOT EXISTS muse_events (
                id TEXT PRIMARY KEY, seq INTEGER NOT NULL, observed REAL NOT NULL);
        """)
        # Additive migration preserves the existing owner, queues and dedup IDs.
        for table in ("inbox", "outbox"):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, declaration in (("kind", "TEXT NOT NULL DEFAULT 'text'"),
                                      ("payload", "TEXT NOT NULL DEFAULT '{}'")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key: str, value):
        with self.lock, self.db:
            self._put(key, value)

    def _put(self, key: str, value):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    def owner(self):
        return self.get("owner")

    def new_pairing_token(self, lifetime=1800) -> str:
        token = secrets.token_hex(12)
        self.put("pairing", {"hash": hashlib.sha256(token.encode()).hexdigest(),
                             "expires": self.clock() + lifetime})
        return token

    def bind(self, token: str, sender: str, chat: str) -> bool:
        if not sender or not chat:
            return False
        with self.lock, self.db:
            owner = self.owner()
            if owner:
                return owner == {"open_id": sender, "chat_id": chat}
            expected = self.get("pairing", {})
            digest = hashlib.sha256(token.encode()).hexdigest()
            if self.clock() > expected.get("expires", 0) or not hmac.compare_digest(expected.get("hash", ""), digest):
                return False
            self._put("owner", {"open_id": sender, "chat_id": chat})
            self._put("pairing", {})
            return True

    def is_owner(self, sender: str, chat: str) -> bool:
        return self.owner() == {"open_id": sender, "chat_id": chat}

    def enqueue_incoming(self, message_id: str, chat_id: str, text: str) -> bool:
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "muse-feishu:" + message_id))
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO inbox(id,chat_id,text,request_id,created) VALUES (?,?,?,?,?)",
                (message_id, chat_id, text, request_id, self.clock()))
            return cursor.rowcount == 1

    def claim_incoming(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM inbox WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            stage = "preparing" if row["kind"] == "file" else "sending"
            self.db.execute("UPDATE inbox SET state=?,attempts=attempts+1 WHERE id=?", (stage, row["id"]))
            return dict(row)

    def enqueue_file(self, message_id: str, chat_id: str, payload: dict) -> bool:
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO inbox(id,chat_id,text,request_id,created,kind,payload) VALUES (?,?,?,?,?,'file',?)",
                (message_id, chat_id, '', str(uuid.uuid5(uuid.NAMESPACE_URL, 'muse-feishu:' + message_id)),
                 self.clock(), json.dumps(payload, ensure_ascii=False)))
            return cursor.rowcount == 1

    def file_payload(self, table: str, key: str, payload: dict):
        if table not in ('inbox', 'outbox'):
            raise ValueError('invalid_queue')
        with self.lock, self.db:
            self.db.execute(f"UPDATE {table} SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), key))

    def incoming_submitting(self, key: str):
        with self.lock, self.db:
            self.db.execute("UPDATE inbox SET state='sending' WHERE id=? AND state='preparing'", (key,))

    def retained_files(self) -> set[str]:
        with self.lock:
            keys = set()
            for table in ('inbox', 'outbox'):
                for row in self.db.execute(
                    f"SELECT id,state,created FROM {table} WHERE kind='file' AND state IN ('pending','preparing','sending','unknown')"):
                    if row['state'] != 'unknown' or self.clock() - row['created'] < 7 * 86400:
                        keys.add(table + ':' + row['id'])
            return keys

    def incoming_result(self, message_id: str, state: str, error=None):
        if state not in ("pending", "sent", "unknown", "failed"):
            raise ValueError("invalid_incoming_state")
        with self.lock, self.db:
            self.db.execute("UPDATE inbox SET state=?,error=? WHERE id=?", (state, error, message_id))

    def recover(self) -> int:
        """A crash after submission must not execute the same Muse task twice."""
        with self.lock, self.db:
            count = self.db.execute("SELECT count(*) FROM inbox WHERE state='sending'").fetchone()[0]
            self.db.execute("UPDATE inbox SET state='unknown',error='restart_during_send' WHERE state='sending'")
            self.db.execute("UPDATE inbox SET state='pending' WHERE state='preparing'")
            # Feishu retries reuse a persisted key within its one-hour UUID window.
            self.db.execute("UPDATE outbox SET state='pending' WHERE state='sending'")
            return count

    def queue_text(self, key: str, chat: str, text: str):
        with self.lock, self.db:
            self._queue_text(key, chat, text)

    def _queue_text(self, key: str, chat: str, text: str):
        self.db.execute("INSERT OR IGNORE INTO outbox(id,chat_id,text,created) VALUES (?,?,?,?)",
                        (key, chat, text, self.clock()))

    def record_muse(self, event: dict, chunks: list[str]) -> bool:
        message_id, seq = event.get("event_id") or event.get("message_id"), int(event.get("seq") or 0)
        if not message_id or event.get("role") != "assistant" or not event.get("completed"):
            return False
        with self.lock, self.db:
            fresh = self.db.execute("INSERT OR IGNORE INTO muse_events VALUES (?,?,?)",
                                    (message_id, seq, self.clock())).rowcount
            if not fresh:
                return False
            owner = self.owner()
            baseline = self.get("baseline_seq")
            if event.get('kind') == 'file':
                baseline = self.get('image_baseline_seq') if event.get('file', {}).get('media_kind') == 'image' else self.get('attachment_baseline_seq')
            # Initial onboarding starts at the current conversation, not its old history.
            if not owner or baseline is None or (seq and seq <= baseline):
                return False
            if event.get('kind') == 'file':
                self.db.execute("INSERT OR IGNORE INTO outbox(id,chat_id,text,created,kind,payload) VALUES (?,?,?,?,'file',?)",
                    ('muse-file:' + message_id, owner['chat_id'], '', self.clock(), json.dumps(event['file'], ensure_ascii=False)))
            else:
                for index, text in enumerate(chunks):
                    self._queue_text(f"muse:{message_id}:{index}", owner["chat_id"], text)
            self._put("last_muse_seq", max(seq, self.get("last_muse_seq", 0)))
            return True

    def claim_outgoing(self):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM outbox WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            if row["next_attempt"] > self.clock():
                return None
            first_attempt = row["first_attempt"]
            if first_attempt is not None and self.clock() - first_attempt >= 3500:
                self.db.execute("UPDATE outbox SET state='unknown',error='idempotency_window_expired' WHERE id=?",
                                (row["id"],))
                return None
            self.db.execute(
                "UPDATE outbox SET state='sending',attempts=attempts+1,first_attempt=COALESCE(first_attempt,?) WHERE id=?",
                (None if row['kind'] == 'file' else self.clock(), row["id"]))
            return dict(row)

    def outgoing_submitting(self, key: str):
        with self.lock, self.db:
            self.db.execute('UPDATE outbox SET first_attempt=COALESCE(first_attempt,?) WHERE id=?', (self.clock(), key))

    def outgoing_sent(self, key: str, remote_id: str):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET state='sent',remote_id=?,error=NULL WHERE id=?", (remote_id, key))

    def outgoing_failed(self, key: str, retryable: bool, error: str, delay=10):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET state=?,error=?,next_attempt=? WHERE id=?",
                            ("pending" if retryable else "failed", error, self.clock() + delay, key))

    def counts(self) -> dict:
        with self.lock:
            result = {}
            for table in ("inbox", "outbox"):
                result[table] = {row[0]: row[1] for row in self.db.execute(f"SELECT state,count(*) FROM {table} GROUP BY state")}
            return result

