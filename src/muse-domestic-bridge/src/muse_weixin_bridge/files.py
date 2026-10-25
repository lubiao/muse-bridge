# SPDX-License-Identifier: Apache-2.0
"""Bounded private attachment cache; never fetch arbitrary message URLs."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import threading
import time

from .config import private_directory

MAX_FILE_BYTES = 200 * 1024 * 1024
INLINE_FILE_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = 1024 * 1024 * 1024


class FileError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def filename(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return "attachment.bin"
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', value).strip(' .')
    if not value:
        return "attachment.bin"
    if len(value) > 180:
        suffix = Path(value).suffix[:20]
        value = value[:160] + suffix
    return value


def workspace_path(value: str) -> str:
    if not isinstance(value, str):
        raise FileError("unsupported_file_path")
    path = value.removeprefix("sandbox://")
    if (not path.startswith("workspace/") or '\\' in path or '\x00' in path
            or ':' in path or any(part in ('', '.', '..') for part in path.split('/'))
            or len(path) > 2048):
        raise FileError("unsupported_file_path")
    return str(PurePosixPath(path))


class FileCache:
    def __init__(self, directory: Path):
        self.directory = directory / 'attachments'
        private_directory(self.directory)
        self.lock = threading.RLock()

    def path(self, key: str) -> Path:
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + '.bin')

    def read(self, key: str) -> bytes | None:
        path = self.path(key)
        if not path.exists():
            return None
        if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            raise FileError("invalid_attachment_cache")
        return path.read_bytes()

    def write(self, key: str, data: bytes) -> None:
        with self.lock:
            self._write(key, data)

    def _write(self, key: str, data: bytes) -> None:
        if not isinstance(data, bytes) or not data:
            raise FileError("empty_attachment")
        if len(data) > MAX_FILE_BYTES:
            raise FileError("file_too_large")
        used = sum(p.stat().st_size for p in self.directory.glob('*.bin') if p.is_file())
        if used + len(data) > MAX_CACHE_BYTES:
            raise FileError("attachment_cache_full")
        descriptor, temporary = tempfile.mkstemp(prefix='part-', suffix='.tmp', dir=self.directory)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != 'nt':
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path(key))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def cleanup(self, retained_keys: set[str], now: float | None = None) -> int:
        with self.lock:
            return self._cleanup(retained_keys, now)

    def _cleanup(self, retained_keys: set[str], now: float | None = None) -> int:
        retained = {self.path(key).name for key in retained_keys}
        now = time.time() if now is None else now
        removed = 0
        for path in self.directory.iterdir():
            owned = re.fullmatch(r'[0-9a-f]{64}\.bin|part-[A-Za-z0-9_-]+\.tmp', path.name)
            if not owned or path.is_symlink() or not path.is_file() or path.name in retained:
                continue
            if now - path.stat().st_mtime >= 3600:
                path.unlink()
                removed += 1
        return removed
