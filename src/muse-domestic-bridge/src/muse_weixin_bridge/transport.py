# SPDX-License-Identifier: Apache-2.0
# Tencent iLink protocol adaptation; Copyright (C) 2026 Tencent.
# Modified Python implementation; see licenses/openclaw-weixin-MIT.txt.
"""Tencent iLink HTTPS transport. Never logs server responses or credentials."""
from __future__ import annotations

import base64
import hashlib
import secrets
import re
import time
from urllib.parse import urlencode
import requests

from . import protocol as p
from .config import Secrets
from .files import MAX_FILE_BYTES, FileError, filename


class ChannelError(Exception):
    def __init__(self, category, *, retryable=False, code=None):
        self.category = category
        self.retryable = retryable
        self.code = code
        self.retry_after_seconds = None
        super().__init__(category)


class CipherReader:
    """Stream bounded writes; a bytes body would sendall the entire attachment
    under the socket's short connect timeout on a slow cross-border route."""
    def __init__(self, data, timeout=600):
        self.data = memoryview(data)
        self.offset = 0
        self.deadline = time.monotonic() + timeout

    def __len__(self):
        return len(self.data)

    def tell(self):
        return self.offset

    def read(self, size=65536):
        if time.monotonic() > self.deadline:
            raise ChannelError('cdn_upload_deadline', retryable=True)
        size = 65536 if size is None or size < 0 else min(size, 65536)
        chunk = self.data[self.offset:self.offset + size].tobytes()
        self.offset += len(chunk)
        return chunk


class WeixinClient:
    def __init__(self, credentials_path, state):
        self.credentials = Secrets(credentials_path).read()
        for key in ('token', 'user_id', 'bot_id', 'base_url'):
            if not isinstance(self.credentials.get(key), str) or not self.credentials[key]:
                raise ChannelError('credentials_invalid')
        p.checked_url(self.credentials['base_url'])
        self.state = state

    def api(self, endpoint, body, timeout=40):
        try:
            return p.call('ilink/bot/' + endpoint,
                dict(body, base_info={'channel_version': p.VERSION, 'bot_agent': 'MuseBridge/1.0'}),
                token=self.credentials['token'], base=self.credentials['base_url'], timeout=timeout)
        except p.ProbeError as error:
            raise ChannelError(error.kind, code=error.code,
                retryable=error.kind == 'network' or (error.kind == 'http' and (error.code or 0) >= 500)) from None

    def get_updates(self, cursor):
        return self.api('getupdates', {'get_updates_buf': cursor}, timeout=40)

    def send_item(self, chat, item, request_id):
        if chat != self.credentials['user_id']:
            raise ChannelError('recipient_rejected')
        context = self.state.get('weixin_context', {})
        if not context.get('token'):
            raise ChannelError('context_missing')
        reply = self.api('sendmessage', {'msg': {
            'from_user_id': '', 'to_user_id': chat, 'client_id': request_id,
            'message_type': 2, 'message_state': 2, 'context_token': context['token'], 'item_list': [item]}})
        # No documented server-side deduplication contract. Callers never replay
        # an ambiguous send, even with the same client_id.
        return str(reply.get('message_id') or request_id)

    def send_text(self, chat, text, request_id):
        return self.send_item(chat, {'type': 1, 'text_item': {'text': text}}, request_id)

    def send_file(self, chat, media, request_id):
        return self.send_item(chat, media, request_id)

    def upload_file(self, name, data, *, image=False):
        if not data:
            raise FileError('empty_attachment')
        if len(data) > MAX_FILE_BYTES:
            raise FileError('file_too_large')
        key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
        cipher = p.encrypt(data, key)
        result = self.api('getuploadurl', {'filekey': filekey, 'media_type': 1 if image else 3,
            'to_user_id': self.credentials['user_id'], 'rawsize': len(data),
            'rawfilemd5': hashlib.md5(data).hexdigest(), 'filesize': len(cipher),
            'no_need_thumb': True, 'aeskey': key.hex()})
        url = result.get('upload_full_url')
        if not url:
            if not result.get('upload_param'):
                raise ChannelError('upload_url_missing')
            url = p.CDN + '/upload?' + urlencode({'encrypted_query_param': result['upload_param'], 'filekey': filekey})
        p.checked_url(url, cdn=True)
        session = requests.Session()
        session.trust_env = False
        try:
            with session.post(url, data=CipherReader(cipher), headers={'Content-Type': 'application/octet-stream'},
                    timeout=(30, 180), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ChannelError('cdn_upload_http', code=response.status_code, retryable=response.status_code >= 500)
                param = response.headers.get('x-encrypted-param')
                if not param:
                    raise ChannelError('upload_receipt_missing', retryable=True)
        except requests.RequestException:
            raise ChannelError('cdn_upload_network', retryable=True) from None
        finally:
            session.close()
        media={'encrypt_query_param':param,'aes_key':base64.b64encode(key.hex().encode()).decode(),'encrypt_type':1}
        if image:
            return {'type':2,'image_item':{'media':media,'mid_size':len(cipher)}}
        return {'type':4,'file_item':{'file_name':filename(name),'len':str(len(data)),'media':media}}

    def upload_image(self, name, data):
        from .images import prepare_image
        data,name=prepare_image(data,name)
        return self.upload_file(name,data,image=True)

    def send_image(self, chat, media, request_id):
        return self.send_item(chat,media,request_id)

    def download_file(self, message_id, item):
        return self._download_media(item)

    def download_image(self, message_id, item):
        from .images import MAX_IMAGE_BYTES
        media = dict(item.get('media') or {})
        if item.get('aeskey'):
            key = item['aeskey']
            if not isinstance(key,str) or not re.fullmatch(r'[0-9a-fA-F]{32}',key):
                raise FileError('invalid_encrypted_attachment')
            media['aes_key'] = base64.b64encode(bytes.fromhex(key)).decode('ascii')
        return self._download_media({'media':media}, limit=MAX_IMAGE_BYTES, allow_plain=True)

    def _download_media(self, item, limit=MAX_FILE_BYTES, allow_plain=False):
        try:
            size = int(item['len']) if item.get('len') is not None else None
        except (ValueError, TypeError):
            raise FileError('invalid_file_size') from None
        if size is not None and not 0 < size <= limit:
            raise FileError('file_too_large' if size else 'empty_attachment')
        media = item.get('media') or {}
        url = media.get('full_url')
        if not url:
            if not media.get('encrypt_query_param'):
                raise FileError('download_reference_missing')
            url = p.CDN + '/download?' + urlencode({'encrypted_query_param': media['encrypt_query_param']})
        p.checked_url(url, cdn=True)
        session = requests.Session()
        session.trust_env = False
        try:
            with session.get(url, timeout=(10, 60), stream=True, allow_redirects=False) as response:
                cipher = p.bounded_response(response, limit + 16)
            key = media.get('aes_key', '')
            plain = cipher if allow_plain and not key else p.decrypt(cipher, key)
        except requests.RequestException:
            raise ChannelError('cdn_download_network', retryable=True) from None
        except p.ProbeError as error:
            raise ChannelError(error.kind, code=error.code, retryable=error.kind == 'http' and (error.code or 0) >= 500) from None
        except (ValueError, TypeError):
            raise FileError('invalid_encrypted_attachment') from None
        finally:
            session.close()
        if not plain or len(plain) > limit:
            raise FileError('file_too_large' if plain else 'empty_attachment')
        if size is not None and len(plain) != size:
            raise FileError('file_size_changed')
        return plain
