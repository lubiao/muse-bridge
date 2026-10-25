# SPDX-License-Identifier: Apache-2.0
"""Authenticated loopback OneBot API; large attachments use local files."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from urllib.parse import urlsplit
import requests

from .config import Secrets
from .files import FileError, MAX_FILE_BYTES, filename
from .images import prepare_image


class ChannelError(Exception):
    def __init__(self, category, *, retryable=False, code=None):
        self.category, self.retryable, self.code = category, retryable, code
        self.retry_after_seconds = None
        super().__init__(category)


def attachment_name(data):
    return data.get('name') or data.get('file_name') or data.get('file')


def read_download(path, roots, expected_size=None, limit=MAX_FILE_BYTES):
    """Open every path component without following symlinks. The process can
    traverse QQ's parents but has ACL read access to downloaded files alone."""
    candidate = Path(path)
    if not candidate.is_absolute() or '..' in candidate.parts:
        raise FileError('download_path_rejected')
    if not any(candidate.is_relative_to(Path(root)) and candidate != Path(root) for root in roots):
        raise FileError('download_path_rejected')
    directory_flags = getattr(os, 'O_PATH', os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open('/', directory_flags)
    try:
        for part in candidate.parts[1:-1]:
            following = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        file = os.open(candidate.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise FileError('download_path_rejected')
            if not 0 < info.st_size <= limit:
                raise FileError('file_too_large' if info.st_size else 'empty_attachment')
            if expected_size is not None and info.st_size != expected_size:
                raise FileError('file_size_changed')
            data = stream.read(limit + 1)
            if len(data) != info.st_size:
                raise FileError('file_size_changed')
            return data
    except OSError:
        raise FileError('download_file_inaccessible') from None
    finally:
        os.close(descriptor)


class QQClient:
    def __init__(self, credentials_path, state, *, shared_dir, download_roots):
        self.credentials = Secrets(credentials_path).read()
        self.state = state
        self.shared_dir = Path(shared_dir)
        self.download_roots = download_roots
        for key in ('api_token', 'owner_id', 'bot_id'):
            if not isinstance(self.credentials.get(key), str) or not self.credentials[key]:
                raise ChannelError('credentials_invalid')
        if not all(self.credentials[k].isdecimal() for k in ('owner_id', 'bot_id')):
            raise ChannelError('credentials_invalid')

    def api(self, action, body=None, timeout=30):
        if not re.fullmatch('[a-z_]+', action):
            raise ChannelError('endpoint_rejected')
        session = requests.Session()
        session.trust_env = False
        try:
            with session.post('http://127.0.0.1:3300/' + action, json=body or {},
                    headers={'Authorization': 'Bearer ' + self.credentials['api_token']},
                    timeout=(5, timeout), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ChannelError('http', code=response.status_code, retryable=response.status_code >= 500)
                chunks, total = [], 0
                deadline = time.monotonic() + timeout
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > 2 * 1024 * 1024 or time.monotonic() > deadline:
                        raise ChannelError('response_limit')
                    chunks.append(chunk)
                result = json.loads(b''.join(chunks))
            if not isinstance(result, dict):
                raise ChannelError('response_invalid')
            if result.get('status') != 'ok' or result.get('retcode') not in (None, 0):
                code = result.get('retcode')
                raise ChannelError('onebot_rejected', code=code if isinstance(code, int) else None)
            return result.get('data')
        except requests.RequestException:
            raise ChannelError('network', retryable=True) from None
        except (ValueError, TypeError):
            raise ChannelError('response_invalid') from None
        finally:
            session.close()

    def check_identity(self):
        result = self.api('get_login_info')
        if str((result or {}).get('user_id')) != self.credentials['bot_id']:
            raise ChannelError('bot_identity_changed')
        return True

    def send_text(self, chat, text, request_id):
        self._recipient(chat)
        result = self.api('send_private_msg', {'user_id': chat, 'message': [{'type': 'text', 'data': {'text': text}}]}, timeout=60)
        if not isinstance(result, dict) or not result.get('message_id'):
            raise ChannelError('send_receipt_missing')
        return str(result['message_id'])

    def upload_file(self, name, data):
        if not data or len(data) > MAX_FILE_BYTES:
            raise FileError('file_too_large' if data else 'empty_attachment')
        name = filename(name)
        key = hashlib.sha256(name.encode() + b'\x00' + hashlib.sha256(data).digest()).hexdigest()
        path = self.shared_dir / (key + '.bin')
        if not path.exists():
            used = sum(p.stat().st_size for p in self.shared_dir.glob('*.bin') if p.is_file())
            if used + len(data) > 1024 * 1024 * 1024:
                raise FileError('attachment_cache_full')
            fd, temporary = tempfile.mkstemp(prefix='part-', suffix='.tmp', dir=self.shared_dir)
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o640)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if path.is_symlink() or path.stat().st_size != len(data):
            raise FileError('invalid_attachment_cache')
        return {'path': str(path), 'name': name, 'size': len(data)}

    def upload_image(self, name, data):
        data, name = prepare_image(data, name)
        return self.upload_file(name, data)

    def _staged_path(self, item):
        path = Path(item['path'])
        if path.parent != self.shared_dir or not re.fullmatch(r'[0-9a-f]{64}\.bin', path.name) or path.is_symlink():
            raise FileError('upload_path_rejected')
        if not path.exists() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
            raise FileError('invalid_attachment_cache')
        return path

    def send_image(self, chat, item, request_id):
        self._recipient(chat)
        path = self._staged_path(item)
        result = self.api('send_private_msg', {'user_id': chat, 'message': [
            {'type': 'image', 'data': {'file': path.as_uri()}}]}, timeout=120)
        if not isinstance(result, dict) or not result.get('message_id'):
            raise ChannelError('send_receipt_missing')
        return str(result['message_id'])

    def send_file(self, chat, item, request_id):
        self._recipient(chat)
        path = self._staged_path(item)
        result = self.api('upload_private_file', {'user_id': chat, 'file': str(path),
            'name': filename(item['name']), 'upload_file': True}, timeout=600)
        if not isinstance(result, dict) or not result.get('file_id'):
            raise ChannelError('send_receipt_missing')
        # This API's receipt is a file ID, not a OneBot message ID.
        return 'file:' + str(result['file_id'])

    def download_file(self, message_id, item):
        try:
            size = int(item['size']) if item.get('size') is not None else None
        except (TypeError, ValueError):
            raise FileError('invalid_file_size') from None
        if size is not None and not 0 < size <= MAX_FILE_BYTES:
            raise FileError('file_too_large' if size else 'empty_attachment')
        file_id = item.get('file_id')
        if not isinstance(file_id, str) or not file_id or len(file_id) > 4096:
            raise FileError('invalid_file_id')
        result = self.api('get_file', {'file_id': file_id}, timeout=600)
        if not isinstance(result, dict) or not isinstance(result.get('file'), str):
            raise FileError('download_path_missing')
        return read_download(result['file'], self.download_roots, size)

    def download_image(self, message_id, item):
        from .images import MAX_IMAGE_BYTES
        mid, index = item.get('message_id'), item.get('index')
        if not isinstance(mid, str) or not re.fullmatch(r'-?\d{1,32}', mid) or not isinstance(index, int) or not 0 <= index < 100:
            raise FileError('invalid_image_reference')
        message = self.api('get_msg', {'message_id':mid})
        if (not isinstance(message,dict) or message.get('message_type') != 'private'
                or str((message.get('sender') or {}).get('user_id')) != self.credentials['owner_id']):
            raise FileError('image_sender_rejected')
        segments = message.get('message', [])
        if not isinstance(segments,list) or index >= len(segments) or segments[index].get('type') != 'image':
            raise FileError('invalid_image_reference')
        data = segments[index].get('data') or {}
        file_id = data.get('file_id') or data.get('file')
        if not isinstance(file_id,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,512}',file_id):
            raise FileError('invalid_image_reference')
        result = self.api('get_image', {'file':file_id}, timeout=120)
        if not isinstance(result,dict) or not isinstance(result.get('file'),str):
            raise FileError('download_path_missing')
        # QQ forces downloaded pictures to 0600, overriding inherited ACLs.
        # Use the original QQ CDN reference instead of broadening OS access.
        for url in (result.get('url'), data.get('url')):
            if isinstance(url,str) and url.startswith('https://'):
                return self._download_image_url(url, MAX_IMAGE_BYTES)
        return read_download(result['file'], self.download_roots, limit=MAX_IMAGE_BYTES)

    def _download_image_url(self, url, limit):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme=='https' and parsed.hostname in (
                'multimedia.nt.qq.com.cn','multimedia.nt.qq.com','gchat.qpic.cn')
                and parsed.port in (None,443) and not parsed.username and not parsed.password and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise FileError('image_url_rejected')
        session = requests.Session();session.trust_env=False
        try:
            with session.get(url,stream=True,timeout=(10,60),allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ChannelError('image_download_http',code=response.status_code,retryable=response.status_code>=500)
                length = response.headers.get('Content-Length')
                if length is not None and int(length)>limit:
                    raise FileError('image_size_limit')
                chunks,total,deadline = [],0,time.monotonic()+120
                for chunk in response.iter_content(65536):
                    total+=len(chunk)
                    if total>limit:raise FileError('image_size_limit')
                    if time.monotonic()>deadline:raise ChannelError('image_download_deadline',retryable=True)
                    chunks.append(chunk)
                if not total:raise FileError('empty_attachment')
                return b''.join(chunks)
        except requests.RequestException:
            raise ChannelError('image_download_network',retryable=True) from None
        except ValueError:
            raise FileError('invalid_image_response') from None
        finally:
            session.close()

    def _recipient(self, chat):
        if chat != self.credentials['owner_id']:
            raise ChannelError('recipient_rejected')

    def cleanup(self, retained_paths):
        for path in self.shared_dir.iterdir():
            if (not re.fullmatch(r'[0-9a-f]{64}\.bin|part-[A-Za-z0-9_-]+\.tmp', path.name)
                    or path.is_symlink() or not path.is_file() or str(path) in retained_paths):
                continue
            if time.time() - path.stat().st_mtime >= 3600:
                path.unlink()
