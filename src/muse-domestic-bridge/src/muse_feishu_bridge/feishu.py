# SPDX-License-Identifier: Apache-2.0
"""Official Feishu SDK transport for the Muse bridge.

The callback must only authenticate/validate and durably enqueue the supplied
dictionary. It runs on the SDK event loop and must return within three seconds.
Do not call Muse or send network requests from that callback. Raising from it
causes the SDK to return a failed acknowledgement so Feishu can redeliver.

Verified against lark-oapi==1.7.5. Its public WebSocket API has no stop method;
run ``start()`` on the main thread in a supervised, dedicated process. Its
endpoint-discovery request also has no configurable timeout. REST sends below
have their own 15-second timeout. SDK auto-reconnect remains enabled.
"""

from __future__ import annotations

import json
import io
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, TypedDict

import lark_oapi as lark
import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
    CreateFileRequest, CreateFileRequestBody, GetMessageResourceRequest)
from lark_oapi.api.im.v1 import CreateImageRequest,CreateImageRequestBody
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest, CreateMessageReactionRequestBody, Emoji)
from lark_oapi.core.model import RequestOption
from lark_oapi.core.token import verify
from lark_oapi.core.http.transport import _build_header, _build_url
from types import SimpleNamespace
from .files import FileError, MAX_FILE_BYTES, filename
from . import markdown as _markdown
from lark_oapi.core.exception import (
    AccessDeniedException,
    NoAuthorizationException,
    ObtainAccessTokenException,
)
from lark_oapi.ws.exception import ClientException

LOGGER = logging.getLogger(__name__)
_SEND_TIMEOUT_SECONDS = 15.0

# Message types the bridge relays. ``audio`` is a voice note and ``media`` a
# video; both reach Muse through the generic file channel, since its image
# channel only accepts PNG/JPEG.
_ACCEPTED_TYPES = ("text", "file", "image", "post", "audio", "media")

# The reaction the bridge puts on a message it has accepted. Feishu's own set of
# emoji types includes "OK"; it reads as a tick in both mobile and desktop clients.
_ACK_EMOJI = "OK"

# Python's mimetypes table misses the containers Feishu actually sends.
MEDIA_TYPES = {
    ".opus": "audio/opus",
    ".amr": "audio/amr",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".silk": "audio/silk",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}
_IDEMPOTENCY_NAMESPACE = uuid.UUID("70546bf5-c82d-4082-9e5b-d319ed818821")
_RATE_LIMIT_CODES = {230020, 99991400}
_TEMPORARY_CODES = {1500, 1503, 5000, 230049}
_PERMISSION_CODES = {
    230002, 230006, 230013, 230018, 230027, 230029, 230035,
    230036, 230038, 230053, 99991401, 99991662, 99991672, 99991673,
    99991676, 99991679,
}
_AUTH_CODES = {99991543, 99991661, 99991663, 99991664, 99991665, 99991668, 99991671}


class IncomingMessage(TypedDict):
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    text: str
    created_at_ms: int


class FeishuError(RuntimeError):
    """Log-safe error; callers may retry only when ``retryable`` is true.

    ``delivery_uncertain`` means the send may already have reached Feishu. Reuse
    the original key and text for retries; never generate a new key on timeout.
    Feishu's UUID deduplication window is one hour, not permanent exactly-once.
    Raw response messages, headers, URLs and original exceptions are excluded.
    """

    def __init__(
        self,
        category: str,
        *,
        retryable: bool = False,
        code: int | None = None,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        delivery_uncertain: bool = False,
    ) -> None:
        self.category = category
        self.retryable = retryable
        self.code = code
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.delivery_uncertain = delivery_uncertain
        super().__init__(
            f"Feishu {category} error "
            f"(http_status={http_status}, code={code}, retryable={retryable})"
        )


def _parse_post(content: dict) -> tuple[str, list[dict]]:
    """Split a ``post`` (rich text) body into its words and its images.

    The wire shape is ``{title, content: [[block, ...], ...]}`` where each block
    is ``{"tag": "text"|"img"|"a"|"at"|"media"|"code_block", ...}``. Older
    payloads wrap the whole thing per locale, so both shapes are accepted.
    """
    if not isinstance(content, dict):
        return "", []
    block = content
    for locale in ("zh_cn", "en_us", "ja_jp"):
        candidate = content.get(locale)
        if isinstance(candidate, dict):
            block = candidate
            break
    lines: list[str] = []
    title = block.get("title")
    if isinstance(title, str) and title.strip():
        lines.append(title.strip())
    images: list[dict] = []
    paragraphs = block.get("content")
    if not isinstance(paragraphs, list):
        return "\n".join(lines), images
    for paragraph in paragraphs:
        if not isinstance(paragraph, list):
            continue
        words: list[str] = []
        for item in paragraph:
            if not isinstance(item, dict):
                continue
            tag = item.get("tag")
            if tag == "text":
                value = item.get("text")
                if isinstance(value, str):
                    words.append(value)
            elif tag == "a":
                value = item.get("text") or item.get("href")
                if isinstance(value, str):
                    words.append(value)
            elif tag == "at":
                name = item.get("user_name") or item.get("user_id")
                if isinstance(name, str):
                    words.append("@" + name)
            elif tag in ("img", "media"):
                key = item.get("image_key") or item.get("file_key")
                if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,512}", key):
                    images.append({"file_key": key,
                                   "filename": filename(item.get("file_name") or "image"),
                                   "size": None})
                words.append("[图片]")
            elif tag == "code_block":
                value = item.get("text")
                if isinstance(value, str):
                    words.append(value)
        row = "".join(words).strip()
        if row:
            lines.append(row)
    return "\n".join(lines), images


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _response_error(response: Any) -> FeishuError:
    raw = getattr(response, "raw", None)
    status = _as_int(getattr(raw, "status_code", None))
    code = _as_int(getattr(response, "code", None))
    headers = getattr(raw, "headers", None) or {}
    retry_after = None
    for name, value in headers.items():
        if str(name).lower() == "retry-after":
            try:
                retry_after = min(3600.0, max(0.0, float(value)))
            except (TypeError, ValueError, OverflowError):
                pass
            break
    options = {"code": code, "http_status": status}
    if status == 429 or code in _RATE_LIMIT_CODES:
        return FeishuError(
            "rate_limit", retryable=True,
            retry_after_seconds=retry_after, **options,
        )
    if status in {408, 425} or (status is not None and status >= 500):
        return FeishuError("temporary", retryable=True, **options)
    if code in _TEMPORARY_CODES:
        return FeishuError("temporary", retryable=True, **options)
    if status == 401 or code in _AUTH_CODES:
        return FeishuError("authentication", **options)
    if status == 403 or code in _PERMISSION_CODES:
        return FeishuError("permission", **options)
    if code == 99991403:
        return FeishuError("monthly_quota", **options)
    if code in {230022, 230028}:
        return FeishuError("content_rejected", **options)
    if status is None or code is None:
        return FeishuError("protocol", retryable=True, delivery_uncertain=True, **options)
    return FeishuError("invalid_request", **options)


class FeishuClient:
    """Single-chat text adapter with a short, caller-owned enqueue callback.

    Construction performs no network I/O. Send calls are serialized because SDK
    request/token state is shared. This adapter does not implement an inbox,
    authorization allowlist, delivery retry loop, or permanent deduplication;
    those belong to the bridge's durable queue layer.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: Callable[[IncomingMessage], None],
    ) -> None:
        if not isinstance(app_id, str) or not app_id.strip():
            raise ValueError("Feishu app_id is required")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise ValueError("Feishu app_secret is required")
        if not callable(on_message):
            raise TypeError("on_message must be a fast enqueue callable")
        self._on_message = on_message
        self._send_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._started = False
        # SDK diagnostics can include endpoint URLs, exception details or payloads.
        # Our own errors expose only stable categories and numeric status codes.
        self._api = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain("https://open.feishu.cn")
            .timeout(_SEND_TIMEOUT_SECONDS)
            .log_level(lark.LogLevel.CRITICAL)
            .build()
        )
        self._file_api = (lark.Client.builder().app_id(app_id).app_secret(app_secret)
            .domain('https://open.feishu.cn').timeout(300).log_level(lark.LogLevel.CRITICAL).build())
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message)
            .build()
        )
        self._ws = lark.ws.Client(
            app_id,
            app_secret,
            domain="https://open.feishu.cn",
            event_handler=handler,
            log_level=lark.LogLevel.CRITICAL,
            auto_reconnect=True,
        )

    def start(self) -> None:
        """Block on official SDK event processing; call once on the main thread."""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Feishu start() must run on the main thread")
        with self._start_lock:
            if self._started:
                raise RuntimeError("Feishu start() may only be called once")
            self._started = True
        try:
            self._ws.start()
        except ClientException as exc:
            raise FeishuError("configuration", code=_as_int(exc.code)) from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise FeishuError("connection", retryable=True) from None

    def stop(self) -> None:
        """The pinned SDK has no public graceful-stop API."""
        raise NotImplementedError(
            "lark-oapi 1.7.5 has no public stop API; stop the dedicated bridge process"
        )

    def _handle_message(self, data: Any) -> None:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if message is None or sender is None:
            return
        if (
            getattr(message, "chat_type", None) != "p2p"
            or getattr(message, "message_type", None) not in _ACCEPTED_TYPES
            or getattr(sender, "sender_type", None) != "user"
        ):
            return
        try:
            content = json.loads(getattr(message, "content", "") or "")
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring malformed Feishu text content")
            return
        if not isinstance(content, dict):
            return
        text = content.get("text")
        message_type = getattr(message, 'message_type', None)
        is_file = message_type == 'file'
        is_image = message_type == 'image'
        is_post = message_type == 'post'
        # Voice notes and videos ride the file channel: Muse's image channel only
        # takes PNG/JPEG, but its file channel passes any media type through.
        is_media = message_type in ('audio', 'media')
        media_kind = message_type
        post_images: list[dict] = []
        if is_post:
            # A "rich text" post is how the Feishu client sends a photo with a
            # caption, or several photos at once. Dropping it silently lost the
            # whole message, so gather its words and its images here.
            text, post_images = _parse_post(content)
            if not text.strip() and not post_images:
                return
        if is_image:
            key = content.get('image_key')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', key):
                return
            text = ''
        if is_file:
            key = content.get('file_key')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', key):
                return
            text = ''
        if is_media:
            # Feishu names the key differently per media type: audio carries
            # file_key, video carries file_key too but may add image_key for the
            # poster frame. Both are downloaded through the same resource API.
            key = content.get('file_key')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', key):
                return
            text = '' if not isinstance(text, str) else text
            supplied = content.get('file_name')
            # Feishu often omits a name for voice notes; the extension decides
            # which MIME type Muse is told, so it must not be empty.
            if not isinstance(supplied, str) or not supplied.strip():
                supplied = 'voice.opus' if media_kind == 'audio' else 'video.mp4'
            elif '.' not in supplied:
                supplied += '.opus' if media_kind == 'audio' else '.mp4'
        sender_id = getattr(sender, "sender_id", None)
        identifiers = {
            "message_id": getattr(message, "message_id", None),
            "chat_id": getattr(message, "chat_id", None),
            "sender_open_id": getattr(sender_id, "open_id", None),
        }
        created_at = _as_int(getattr(message, "create_time", None))
        if (
            not isinstance(text, str)
            or (not is_file and not is_image and not is_media and not text.strip())
            or created_at is None
            or created_at < 0
            or any(not isinstance(value, str) or not value for value in identifiers.values())
        ):
            return
        incoming: IncomingMessage = {
            **identifiers,
            "chat_type": "p2p",
            "text": text,
            "created_at_ms": created_at,
        }
        if is_file:
            incoming.update(kind='file', file={'file_key':key,
                'filename':filename(content.get('file_name')), 'size':_as_int(content.get('file_size'))})
        if is_image:
            incoming.update(kind='file', file={'media_kind':'image','images':[
                {'file_key':key,'filename':'image','size':None}],'caption':''})
        if is_media:
            duration = _as_int(content.get('duration'))
            incoming.update(kind='file', file={
                'file_key': key,
                'filename': filename(supplied),
                'size': _as_int(content.get('file_size')),
                'media_kind': media_kind,
                'duration_ms': duration,
            })
        if is_post and post_images:
            # Keep the caption with the pictures so Muse sees one message, not two.
            incoming.update(kind='file', file={'media_kind':'image','images':post_images,
                                               'caption':text})
            incoming['text'] = text
        try:
            self._on_message(incoming)
        except Exception:
            # Preserve a failure ACK for retry without exposing callback details
            # to the SDK's logger. The caller must deduplicate message_id.
            LOGGER.warning("Feishu inbox enqueue failed; requesting redelivery")
            raise RuntimeError("Feishu inbox enqueue failed") from None

    def send_text(self, chat_id: str, text: str, idempotency_key: str) -> str:
        """Send once; return message_id or raise a log-safe FeishuError.

        Repeated calls for the same chat/key derive the same UUID. The durable
        outbox must keep the same text for a key and retry within one hour.
        """
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise ValueError("chat_id is required")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a nonempty string")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required")
        content = json.dumps({"text": text}, ensure_ascii=False)
        msg_type = "text"
        # Muse answers in Markdown; a text message would show the syntax literally.
        # Cards render it, so only structured replies switch message type — short
        # conversational lines keep the plain bubble they had before.
        if _markdown.looks_like_markdown(text):
            content = _markdown.card_content(text)
            msg_type = "interactive"
        stable_uuid = str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, chat_id + "\0" + idempotency_key))
        # Account for the nested JSON string being escaped again on the wire.
        # ASCII escaping gives a conservative bound for Unicode content too.
        size_bound = json.dumps({
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": content,
            "uuid": stable_uuid,
        }, ensure_ascii=True).encode("utf-8")
        if len(size_bound) > 140_000:
            raise ValueError("text is too large; split it into stable outbox chunks")
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .uuid(stable_uuid)
                .build()
            )
            .build()
        )
        try:
            with self._send_lock:
                response = self._api.im.v1.message.create(request)
        except (requests.Timeout, requests.ConnectionError):
            raise FeishuError("transport", retryable=True, delivery_uncertain=True) from None
        except (NoAuthorizationException, AccessDeniedException):
            raise FeishuError("authentication") from None
        except ObtainAccessTokenException as exc:
            # Obtaining a token happens before message transmission.
            code = _as_int(exc.code)
            if code in _RATE_LIMIT_CODES:
                raise FeishuError("rate_limit", retryable=True, code=code) from None
            if code in _TEMPORARY_CODES:
                raise FeishuError("temporary", retryable=True, code=code) from None
            raise FeishuError("authentication", code=code) from None
        except Exception:
            # Includes non-JSON responses; delivery may have succeeded upstream.
            raise FeishuError("protocol", retryable=True, delivery_uncertain=True) from None
        status = _as_int(getattr(getattr(response, "raw", None), "status_code", None))
        if status is None or not 200 <= status < 300 or not response.success():
            raise _response_error(response)
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not isinstance(message_id, str) or not message_id:
            raise FeishuError("protocol", retryable=True, delivery_uncertain=True)
        return message_id

    def _perform(self, operation, *, uncertain=False):
        try:
            with self._send_lock:
                response = operation()
        except (requests.Timeout, requests.ConnectionError):
            raise FeishuError('transport', retryable=True, delivery_uncertain=uncertain) from None
        except (NoAuthorizationException, AccessDeniedException):
            raise FeishuError('authentication') from None
        except ObtainAccessTokenException as error:
            code = _as_int(error.code)
            raise FeishuError('rate_limit' if code in _RATE_LIMIT_CODES else 'authentication',
                code=code, retryable=code in _RATE_LIMIT_CODES | _TEMPORARY_CODES) from None
        except Exception:
            raise FeishuError('protocol', retryable=True, delivery_uncertain=uncertain) from None
        status = _as_int(getattr(getattr(response, 'raw', None), 'status_code', None))
        if status is None or not 200 <= status < 300 or not response.success():
            raise _response_error(response)
        return response

    def acknowledge(self, message_id: str) -> bool:
        """React to the owner's message so a slow turn is visibly received.

        Best effort by design: a missing reaction scope or a rate limit must never
        interrupt relaying, so failures are swallowed and reported as False.
        """
        if not isinstance(message_id, str) or not message_id:
            return False
        try:
            body = (CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(_ACK_EMOJI).build())
                    .build())
            request = (CreateMessageReactionRequest.builder()
                       .message_id(message_id).request_body(body).build())
            response = self._perform(
                lambda: self._api.im.v1.message_reaction.create(request))
            return getattr(response, 'code', None) == 0
        except Exception:
            # A reaction is a courtesy; never let it fail a message.
            return False

    def download_image(self, message_id: str, image_key: str) -> bytes:
        from .images import MAX_IMAGE_BYTES
        return self._download_resource(message_id, image_key, 'image', MAX_IMAGE_BYTES)

    def download_file(self, message_id: str, file_key: str) -> bytes:
        return self._download_resource(message_id, file_key, 'file', MAX_FILE_BYTES)

    def _download_resource(self, message_id: str, file_key: str, kind: str, limit: int) -> bytes:
        if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,512}', value)
                   for value in (message_id, file_key)):
            raise FileError('invalid_file_reference')
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(kind).build()
        option = RequestOption()
        try:
            with self._send_lock:
                verify(self._api._config, request, option)
                headers = _build_header(request, option, self._api._config)
            url = _build_url('https://open.feishu.cn', request.uri, request.paths)
            # SDK get() buffers the whole resource. Stream the identical signed
            # request ourselves to enforce the cap before buffering a large file.
            with requests.get(url, headers=headers, params=request.queries, stream=True,
                              timeout=(10, 30), allow_redirects=False) as response:
                if 200 <= response.status_code < 300:
                    length = _as_int(response.headers.get('Content-Length'))
                    if length is not None and length > limit:
                        raise FileError('file_too_large')
                    pieces, total = [], 0
                    deadline = time.monotonic() + 180
                    for chunk in response.iter_content(65536):
                        if time.monotonic() > deadline:
                            raise FeishuError('transport', retryable=True)
                        total += len(chunk)
                        if total > limit:
                            raise FileError('file_too_large')
                        pieces.append(chunk)
                    if not total:
                        raise FileError('empty_attachment')
                    return b''.join(pieces)
                data = bytearray()
                for chunk in response.iter_content(4096):
                    data.extend(chunk)
                    if len(data) > 16384:
                        break
                try:
                    code = json.loads(data).get('code')
                except (ValueError, AttributeError):
                    code = None
                raise _response_error(SimpleNamespace(raw=response, code=code))
        except (FeishuError, FileError):
            raise
        except (requests.Timeout, requests.ConnectionError):
            raise FeishuError('transport', retryable=True) from None
        except (NoAuthorizationException, AccessDeniedException, ObtainAccessTokenException):
            raise FeishuError('authentication') from None
        except Exception:
            raise FeishuError('protocol', retryable=True) from None

    def upload_file(self, name: str, data: bytes) -> str:
        if not isinstance(data, bytes) or not data:
            raise FileError('empty_attachment')
        if len(data) > MAX_FILE_BYTES:
            raise FileError('file_too_large')
        request = CreateFileRequest.builder().build()
        option = RequestOption()
        form = MultipartEncoder({'file_type':'stream', 'file_name':filename(name),
            'file':(filename(name), io.BytesIO(data), 'application/octet-stream')})
        deadline = time.monotonic() + 180
        def check_progress(monitor):
            if time.monotonic() >= deadline:
                raise FeishuError('transport', retryable=True)
        body = MultipartEncoderMonitor(form, check_progress)
        try:
            with self._send_lock:
                verify(self._file_api._config, request, option)
                headers = _build_header(request, option, self._file_api._config)
                headers['Content-Type'] = form.content_type
                # Explicit file part and no nullable form fields. This exact
                # streaming form is boundary-tested with a complete 30 MiB file.
                with requests.post('https://open.feishu.cn/open-apis/im/v1/files',
                        headers=headers, data=body, timeout=(10,30), allow_redirects=False) as response:
                    payload = response.json()
                    if not 200 <= response.status_code < 300 or payload.get('code') != 0:
                        raise _response_error(SimpleNamespace(raw=response,code=payload.get('code')))
                    key = payload.get('data',{}).get('file_key')
        except FeishuError:
            raise
        except (requests.Timeout, requests.ConnectionError):
            raise FeishuError('transport', retryable=True) from None
        except (NoAuthorizationException, AccessDeniedException, ObtainAccessTokenException):
            raise FeishuError('authentication') from None
        except Exception:
            raise FeishuError('protocol', retryable=True) from None
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', key):
            raise FeishuError('protocol', retryable=True)
        return key

    def send_file(self, chat_id: str, file_key: str, idempotency_key: str) -> str:
        if not all(isinstance(value, str) and value for value in (chat_id, file_key, idempotency_key)):
            raise ValueError('file_send_parameters_missing')
        stable_uuid = str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, chat_id + '\0' + idempotency_key))
        request = CreateMessageRequest.builder().receive_id_type('chat_id').request_body(
            CreateMessageRequestBody.builder().receive_id(chat_id).msg_type('file')
            .content(json.dumps({'file_key':file_key})).uuid(stable_uuid).build()).build()
        response = self._perform(lambda: self._api.im.v1.message.create(request), uncertain=True)
        mid = getattr(getattr(response, 'data', None), 'message_id', None)
        if not isinstance(mid, str) or not mid:
            raise FeishuError('protocol', retryable=True, delivery_uncertain=True)
        return mid

    def upload_image(self, name: str, data: bytes) -> str:
        from .images import prepare_image
        data,name=prepare_image(data,name)
        request=CreateImageRequest.builder().request_body(CreateImageRequestBody.builder()
            .image_type('message').image(io.BytesIO(data)).build()).build()
        response=self._perform(lambda:self._file_api.im.v1.image.create(request))
        key=getattr(getattr(response,'data',None),'image_key',None)
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}',key):
            raise FeishuError('protocol',retryable=True)
        return key

    def send_image(self, chat_id: str, image_key: str, idempotency_key: str) -> str:
        if not all(isinstance(x,str) and x for x in (chat_id,image_key,idempotency_key)):
            raise ValueError('image_send_parameters_missing')
        stable_uuid=str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE,chat_id+'\0'+idempotency_key))
        request=CreateMessageRequest.builder().receive_id_type('chat_id').request_body(
            CreateMessageRequestBody.builder().receive_id(chat_id).msg_type('image')
            .content(json.dumps({'image_key':image_key})).uuid(stable_uuid).build()).build()
        response=self._perform(lambda:self._api.im.v1.message.create(request),uncertain=True)
        mid=getattr(getattr(response,'data',None),'message_id',None)
        if not isinstance(mid,str) or not mid:
            raise FeishuError('protocol',retryable=True,delivery_uncertain=True)
        return mid
