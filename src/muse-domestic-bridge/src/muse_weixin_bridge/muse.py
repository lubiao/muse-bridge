# SPDX-License-Identifier: Apache-2.0
# Authentication/Gateway adaptations: Copyright (c) 2026 Nik Ships (MIT).
# Modifications: bounded transport, durable bridge integration and file references.
# See THIRD_PARTY_NOTICES.md and licenses/muse-cli-MIT.txt.
"""Muse's existing main chat, with bounded I/O and no credential files.

``watch`` emits completed assistant messages, including proactive messages. It
also re-reads the latest 500 history records every 15 seconds. The application
must establish its startup baseline and durably deduplicate/filter these events;
starting this watcher must not itself authorize forwarding old conversations.
History's ``after_seq`` is a local filter, not an undocumented server cursor.
An outage exceeding the requested history window requires explicit recovery.
"""
from __future__ import annotations

from collections import OrderedDict
import base64
from collections.abc import Callable
import hashlib
import json
import os
import queue
import re
from select import select
import secrets
import threading
import time
from typing import Any
from urllib.parse import quote
from .files import FileError, MAX_FILE_BYTES, INLINE_FILE_BYTES, filename, workspace_path
from .noise_framing import Decoder as _FrameDecoder, encode as _encode_frames

from curl_cffi import CurlECode, CurlError, CurlInfo, CurlWsFlag
from curl_cffi.requests import WebSocket
from noise.connection import Keypair, NoiseConnection
from muse_cli.gateway import (
    AuthError as _UpstreamAuthError,
    Gateway,
    GatewayError as _UpstreamGatewayError,
    GATEWAY_HOST,
    fetch_access_token,
    fetch_hatch_token,
    fetch_session_info,
    ServiceRequest, ServiceResponse, ServiceFrame,
)

CONNECT_TIMEOUT = 20.0
CALL_TIMEOUT = 30.0
RECEIVE_TIMEOUT = 45.0
HISTORY_POLL_SECONDS = 15.0
HISTORY_WINDOW = 500
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024


class MuseError(RuntimeError):
    """A safe error: neither its message nor its attributes contain service text."""

    def __init__(self, code: str, phase: str, *, retryable: bool = True,
                 http_status: int | None = None, error_type: str | None = None):
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.http_status = http_status
        self.error_type = error_type
        super().__init__(f"Muse {code} during {phase}")


class AuthExpired(MuseError):
    """Authentication was rejected; stop and obtain fresh user credentials."""


class DeliveryUnknown(MuseError):
    """Submission may have reached Muse. Never automatically resend it."""


class _Cancelled(Exception):
    pass


class _Stop:
    def __init__(self, *events: threading.Event | None):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)


def _safe_error(error: Exception, phase: str) -> MuseError:
    if isinstance(error, MuseError):
        return error
    status = None
    if isinstance(error, _UpstreamGatewayError):
        status = error.status if isinstance(error.status, int) else None
    elif isinstance(error, _UpstreamAuthError):
        # Upstream exception strings include private response bodies. Extract
        # only the fixed endpoint and numeric status; never retain that string.
        match = re.match(r"(api/session|auth/check|hatch/token) -> (\d{3})", str(error))
        if match:
            phase, status = match.group(1), int(match.group(2))
    kind = type(error).__name__
    if status in (401, 403):
        return AuthExpired("authentication_rejected", phase, retryable=False,
                           http_status=status, error_type=kind)
    if isinstance(error, TimeoutError):
        return MuseError("timeout", phase, error_type=kind)
    return MuseError("connection_or_protocol_error", phase, http_status=status,
                     error_type=kind)


class _BoundedWebSocket(WebSocket):
    """Bound each receive/send on the owning thread, including Noise handshake.

    curl_cffi's regular recv retries EAGAIN forever. Using its nonblocking
    fragment API with select/deadlines avoids that loop and avoids closing a
    libcurl handle concurrently from a watchdog thread.
    """

    def __init__(self, stop: _Stop):
        super().__init__()
        self.stop = stop
        self.deadline: float | None = None

    def _check(self, deadline: float) -> None:
        if self.stop.is_set():
            raise _Cancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError("Muse websocket I/O deadline reached")

    def recv(self) -> tuple[bytes, int]:
        deadline = self.deadline or time.monotonic() + RECEIVE_TIMEOUT
        descriptor = self.curl.getinfo(CurlInfo.ACTIVESOCKET)
        chunks: list[bytes] = []
        total = 0
        while True:
            self._check(deadline)
            try:
                chunk, frame = self.recv_fragment()
            except CurlError as error:
                if error.code != CurlECode.AGAIN:
                    raise
                select([descriptor], [], [], min(0.25, max(0.0, deadline - time.monotonic())))
                continue
            if frame.flags & CurlWsFlag.CLOSE:
                raise ConnectionError("Muse websocket closed")
            if frame.flags & (CurlWsFlag.PING | CurlWsFlag.PONG):
                continue
            total += len(chunk)
            if total > MAX_FRAME_BYTES:
                raise MuseError("frame_too_large", "receive", retryable=False)
            chunks.append(chunk)
            if frame.bytesleft == 0 and not frame.flags & CurlWsFlag.CONT:
                return b"".join(chunks), int(frame.flags)

    def send(self, payload: str | bytes, flags: int = CurlWsFlag.BINARY) -> int:
        deadline = self.deadline or time.monotonic() + CALL_TIMEOUT
        if isinstance(payload, str):
            payload = payload.encode()
        descriptor = self.curl.getinfo(CurlInfo.ACTIVESOCKET)
        offset = 0
        while offset < len(payload):
            self._check(deadline)
            try:
                offset += self.curl.ws_send(payload[offset:], flags)
            except CurlError as error:
                if error.code != CurlECode.AGAIN:
                    raise
                select([], [descriptor], [], min(0.25, max(0.0, deadline - time.monotonic())))
        return offset


class _BoundedGateway(Gateway):
    """Upstream Gateway framing/RPC with a bounded websocket transport.

    This mirrors only upstream's authentication/Noise initialization so that
    its *initial* handshake reads are bounded too. RPC and protobuf framing
    continue to use the installed muse-cli implementation.
    """

    def __init__(self, cookie: str, stop: _Stop):
        self.ws: _BoundedWebSocket | None = None
        try:
            if stop.is_set():
                raise _Cancelled()
            self.vm_id = fetch_session_info(cookie)["vm_id"]
            if stop.is_set():
                raise _Cancelled()
            access_token = fetch_access_token(cookie)
            if stop.is_set():
                raise _Cancelled()
            hatch_token = fetch_hatch_token(cookie, access_token, self.vm_id)
            if stop.is_set():
                raise _Cancelled()
            address = (f"wss://{GATEWAY_HOST}/v1/noise?vm_id={quote(self.vm_id, safe='')}"
                       f"&auth_token={quote(hatch_token, safe='')}")
            self.ws = _BoundedWebSocket(stop)
            # Preserve upstream's explicit TLS verification. Do not follow a
            # redirect carrying a token-bearing gateway URL to another host.
            self.ws.connect(address, impersonate="chrome", timeout=CONNECT_TIMEOUT,
                            verify=True, allow_redirects=False)
            self.ws.deadline = time.monotonic() + CONNECT_TIMEOUT
            noise = NoiseConnection.from_name(b"Noise_XX_25519_AESGCM_SHA256")
            noise.set_as_initiator()
            noise.set_keypair_from_private_bytes(Keypair.STATIC, os.urandom(32))
            noise.start_handshake()
            self.ws.send_bytes(bytes(noise.write_message(b"")))
            message, _ = self.ws.recv()
            noise.read_message(message)
            self.ws.send_bytes(bytes(noise.write_message(b"")))
            if not noise.handshake_finished:
                raise MuseError("handshake_incomplete", "connect")
            self.noise = noise
            self.stream = 1
            self._send_lock = threading.Lock()
            self._recv_lock = threading.Lock()
            self._frame_decoder = _FrameDecoder()
            self.ws.deadline = None
        except BaseException:
            self.close()
            raise

    def request(self, method: str, path_params=None, body=None, query=None, timeout=CALL_TIMEOUT):
        self.ws.deadline = time.monotonic() + timeout
        try:
            return super().request(method, path_params, body, query, timeout)
        finally:
            self.ws.deadline = None

    def _send_envelope(self, service_id, frame_bytes):
        outer = ServiceRequest(service=service_id, payload=frame_bytes)
        frames = _encode_frames(outer.SerializeToString())
        with self._send_lock:
            for frame in frames:
                self.ws.send_bytes(bytes(self.noise.encrypt(frame)))

    def _read_frame(self):
        with self._recv_lock:
            while True:
                data, _flags = self.ws.recv()
                outer = self._frame_decoder.decode(bytes(self.noise.decrypt(bytes(data))))
                if outer is None:
                    continue
                response = ServiceResponse(); response.ParseFromString(outer)
                frame = ServiceFrame(); frame.ParseFromString(response.payload)
                return frame

    def close(self) -> None:
        # Called by the connection's owner, after its reader has stopped.
        if self.ws is not None:
            try:
                self.ws.terminate()
            except Exception:
                pass


def _integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _transcript_text(value: Any, message_id: str | None) -> str | None:
    if isinstance(value, str):
        # Match the successful probe's support for transcripts serialized inside
        # a JSON string. Decode only the recognizable transcript envelope; normal
        # assistant prose or a literal JSON answer must remain text.
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            return value
        if not isinstance(decoded, dict) or not isinstance(decoded.get("messages"), list):
            return value
        value = decoded
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        return None
    messages = [message for message in value["messages"]
                if isinstance(message, dict) and message.get("role") == "assistant"]
    if message_id:
        identified = [message for message in messages if message.get("id") == message_id]
        messages = identified or [message for message in messages if not message.get("id")]
    if not messages or not isinstance(messages[-1].get("content"), list):
        return None
    return "\n".join(item["text"] for item in messages[-1]["content"]
                     if isinstance(item, dict) and item.get("type") == "text"
                     and isinstance(item.get("text"), str))


def _final_text(payload: dict, message_id: str | None) -> str | None:
    if isinstance(payload.get("display_text"), str):
        return payload["display_text"]
    text = _transcript_text(payload.get("transcript"), message_id)
    if text is not None:
        return text
    return payload.get("content") if isinstance(payload.get("content"), str) else None


def _identity(event: dict, payload: dict) -> tuple[str | None, int | None]:
    message_id = payload.get("message_id") or event.get("message_id") or payload.get("id")
    if not isinstance(message_id, str):
        message_id = None
    sequence = _integer(payload.get("message_seq"))
    if sequence is None:
        sequence = _integer(event.get("message_seq"))
    return message_id, sequence


def _normalized(event: dict, *, source: str, text: str | None = None,
                force_completed: bool = False) -> dict | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    # A main-chat subscription can carry other clients' status/snippets.
    # Exclude only explicitly marked side-chat message events.
    if payload.get("is_thread", event.get("is_thread")) is True:
        return None
    name = event.get("event_name") or event.get("event")
    role = "user" if name == "message.user" or payload.get("role") == "user" else "assistant"
    message_id, sequence = _identity(event, payload)
    if source == "history":
        sequence = _integer(event.get("seq")) if _integer(event.get("seq")) is not None else sequence
    if text is None:
        text = _final_text(payload, message_id)
    if not isinstance(text, str) or not text:
        return None
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise MuseError("message_too_large", "normalize", retryable=False)
    completed = role == "user" or force_completed or payload.get("status") == "completed"
    if not completed or (not message_id and sequence is None):
        return None
    result = dict(message_id=message_id, seq=sequence, role=role, text=text,
                  completed=True, source=source)
    for key in ("reply_to_message_id", "session_id", "is_thread"):
        value = payload.get(key, event.get(key))
        if isinstance(value, (str, bool)):
            result[key] = value
    event_seq = _integer(event.get("seq"))
    if source == "live" and event_seq is not None:
        result["event_seq"] = event_seq
    return result


def _normalized_file(event: dict, *, source: str) -> dict | None:
    payload = event.get('payload')
    if not isinstance(payload, dict) or payload.get('kind') != 'file':
        return None
    if payload.get('is_thread', event.get('is_thread')) is True or payload.get('role') != 'assistant':
        return None
    data = payload.get('data')
    file = data.get('file') if isinstance(data, dict) else None
    if not isinstance(file, dict) or file.get('missing') is True:
        return None
    mid, seq = _identity(event, payload)
    seq = _integer(event.get('seq')) or _integer(payload.get('seq')) or seq
    widget_id = payload.get('id')
    if not mid or seq is None or seq < 1 or not isinstance(widget_id, str) or not widget_id:
        return None
    path = file.get('path')
    if not isinstance(path, str) or len(path) > 2048:
        return None
    # Keep rejected path/size metadata so the worker can tell the owner why it failed.
    return dict(message_id=mid, event_id='file:' + mid + ':' + widget_id, seq=seq,
                role='assistant', kind='file', text='', completed=True, source=source,
                is_thread=False, file={'path':path, 'filename':filename(file.get('label')),
                    'mime_type':file.get('mime') if isinstance(file.get('mime'), str) else 'application/octet-stream',
                    'size':_integer(file.get('byte_len'))})


def _normalized_presentations(event: dict, *, source: str) -> list[dict]:
    payload = event.get('payload')
    if not isinstance(payload, dict) or payload.get('kind') != 'image':
        row = _normalized_file(event, source=source)
        return [row] if row is not None else []
    data = payload.get('data')
    images = data.get('images') if isinstance(data, dict) else None
    if not isinstance(images, list):
        images = [{'path': payload['image_path']}] if isinstance(payload.get('image_path'), str) else []
    result = []
    for index, image in enumerate(images[:100]):
        if not isinstance(image, dict):
            continue
        proxy = dict(event, payload=dict(payload, kind='file', data={'file': image}))
        row = _normalized_file(proxy, source=source)
        if row is not None:
            row['event_id'] = 'image:' + row['message_id'] + ':' + payload['id'] + ':' + str(index)
            row['file']['media_kind'] = 'image'
            result.append(row)
    return result


class MainChatClient:
    """Each send/history call owns a fresh connection; watch owns another.

    ``on_event`` is invoked serially on the thread running ``watch``. Callback
    failures stop the watcher. A request_id is local correlation, not a claim
    that Muse supports server-side idempotency. Accepted/unknown attempts are
    remembered in memory; durable submission state belongs to the application.
    """

    def __init__(self, cookie_provider: Callable[[], str], on_event: Callable[[dict], None]):
        self.cookie_provider = cookie_provider
        self.on_event = on_event
        self._send_lock = threading.Lock()
        self._node_id = "weixin-bridge-" + secrets.token_hex(8)
        self._submissions: OrderedDict[str, dict | None] = OrderedDict()

    def _connect(self, stop_event=None) -> Gateway:
        try:
            cookie = self.cookie_provider()
            if not isinstance(cookie, str) or not cookie.strip():
                raise AuthExpired("credentials_missing", "connect", retryable=False)
            return _BoundedGateway(cookie, _Stop(stop_event))
        except _Cancelled:
            raise
        except Exception as error:
            raise _safe_error(error, "connect") from None

    def send_text(self, text: str, request_id: str) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must not be empty")
        return self._send_items([{"type":"text", "text":text}], request_id)

    def send_file(self, name: str, mime_type: str, data: bytes, request_id: str) -> dict:
        if not isinstance(data, bytes) or not data:
            raise FileError('empty_attachment')
        if len(data) > MAX_FILE_BYTES:
            raise FileError('file_too_large')
        if len(data) > INLINE_FILE_BYTES:
            item = self.prepare_file_reference(name, mime_type, data, request_id)
            return self.send_file_reference(item, request_id)
        mime_type = mime_type if isinstance(mime_type, str) and re.fullmatch(r'[\w.+-]+/[\w.+-]+', mime_type) else 'application/octet-stream'
        return self._send_items([{'type':'file', 'filename':filename(name),
            'mime_type':mime_type, 'data_base64':base64.b64encode(data).decode('ascii')}], request_id)

    def prepare_file_reference(self, name: str, mime_type: str, data: bytes, request_id: str) -> dict:
        """Upload only, without invoking the agent. A full restart overwrites
        this job's dedicated path, so an uncertain append is never replayed.
        The caller persists this reference before marking chat submission begun.
        """
        if not isinstance(data, bytes) or not data:
            raise FileError('empty_attachment')
        if len(data) > MAX_FILE_BYTES:
            raise FileError('file_too_large')
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise ValueError('invalid_file_request_id')
        name = filename(name)
        identity = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        path = workspace_path('workspace/user/files/weixin-' + identity + '-' + name)
        gateway = self._connect()
        try:
            deadline = time.monotonic() + 600
            for offset in range(0, len(data), 1024 * 1024):
                if time.monotonic() >= deadline:
                    raise MuseError('timeout', 'file_write')
                chunk = data[offset:offset + 1024 * 1024]
                reply = gateway.call_json('fs.write', body={'path':path,
                    'data_base64':base64.b64encode(chunk).decode('ascii'),
                    'overwrite':offset == 0, 'append':offset != 0, 'create_parent':offset == 0},
                    timeout=min(CALL_TIMEOUT, max(0.1, deadline-time.monotonic())))
                if (not isinstance(reply, dict) or reply.get('bytes_written') != len(chunk)
                        or reply.get('append') is not (offset != 0)):
                    raise FileError('invalid_file_write_response')
            reply = gateway.call_json('fs.stat', body={'path':path}, timeout=CALL_TIMEOUT)
            if not isinstance(reply, dict) or reply.get('kind') != 'file' or reply.get('size') != len(data):
                raise FileError('file_size_changed')
            return {'type':'file_ref', 'path':path, 'filename':name,
                'mime_type':mime_type if isinstance(mime_type, str) and re.fullmatch(r'[\w.+-]+/[\w.+-]+', mime_type) else 'application/octet-stream',
                'bytes_len':len(data), 'sha256':hashlib.sha256(data).hexdigest()}
        except FileError:
            raise
        except Exception as error:
            # Filesystem upload never executes an agent turn. Retrying the
            # entire upload is safe, even after a lost fs.write acknowledgement.
            raise _safe_error(error, 'file_write') from None
        finally:
            gateway.close()

    def _file_reference_item(self, item: dict) -> dict:
        if (not isinstance(item, dict) or item.get('type') != 'file_ref'
                or not isinstance(item.get('bytes_len'), int) or isinstance(item.get('bytes_len'), bool)
                or not 0 < item['bytes_len'] <= MAX_FILE_BYTES
                or not isinstance(item.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise FileError('invalid_file_reference')
        checked = {key:item[key] for key in ('type','filename','mime_type','bytes_len','sha256')}
        checked['path'] = workspace_path(item.get('path'))
        checked['filename'] = filename(checked['filename'])
        return checked

    def send_file_reference(self, item: dict, request_id: str) -> dict:
        return self._send_items([self._file_reference_item(item)], request_id)

    def send_image_references(self, images: list[dict], text: str, request_id: str) -> dict:
        if not isinstance(images, list) or not 1 <= len(images) <= 10 or not isinstance(text, str) or len(text) > 20000:
            raise FileError('invalid_image_reference')
        items = [self._file_reference_item(item) for item in images]
        if any(item['mime_type'] not in ('image/png','image/jpeg') or item['bytes_len'] > 10_000_000 for item in items):
            raise FileError('invalid_image_reference')
        if sum(item['bytes_len'] for item in items) > 30_000_000:
            raise FileError('image_batch_size_limit')
        if text.strip():
            items.append({'type':'text','text':text})
        return self._send_items(items, request_id)

    def _send_items(self, items: list[dict], request_id: str) -> dict:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise ValueError("request_id must be a nonempty string of at most 256 characters")
        with self._send_lock:
            if request_id in self._submissions:
                prior = self._submissions[request_id]
                if prior is None:
                    raise DeliveryUnknown("previous_submission_unknown", "send", retryable=False)
                return dict(prior)
            gateway = self._connect()
            # Record before any transport write. Any subsequent exception is
            # conservatively unknown and must not cause a second submission.
            self._submissions[request_id] = None
            while len(self._submissions) > 2048:
                self._submissions.popitem(last=False)
            try:
                acknowledgement = gateway.call_json("chat.stream", body={
                    "items": items,
                    "node_id": self._node_id,
                }, timeout=CALL_TIMEOUT)
                if (not isinstance(acknowledgement, dict)
                        or not isinstance(acknowledgement.get("message_id"), str)
                        or not acknowledgement["message_id"]):
                    raise DeliveryUnknown("acknowledgement_missing", "send", retryable=False)
                result = dict(request_id=request_id, status="accepted",
                              message_id=acknowledgement["message_id"])
                for key in ("reply_to_message_id", "session_id"):
                    if isinstance(acknowledgement.get(key), str):
                        result[key] = acknowledgement[key]
                self._submissions[request_id] = result
                return dict(result)
            except DeliveryUnknown:
                raise
            except Exception as error:
                safe = _safe_error(error, "send")
                # Authentication rejection during _connect is safe to retry
                # after reauthentication. Once submission starts, preserve an
                # unknown outcome durably even if the later error is auth-related.
                raise DeliveryUnknown("submission_outcome_unknown", "send", retryable=False,
                                      http_status=safe.http_status, error_type=safe.error_type) from None
            finally:
                gateway.close()

    def read_file(self, path: str, expected_size: int | None = None) -> bytes:
        path = workspace_path(path)
        if expected_size is not None and (expected_size < 0 or expected_size > MAX_FILE_BYTES):
            raise FileError('file_too_large')
        gateway = self._connect()
        try:
            parts, offset = [], 0
            deadline = time.monotonic() + 600
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MuseError('timeout', 'file_read')
                response = gateway.call_json('fs.read', body={'path':path, 'offset':offset,
                    'len':min(1024 * 1024, MAX_FILE_BYTES + 1 - offset)}, timeout=min(CALL_TIMEOUT, remaining))
                if not isinstance(response, dict) or response.get('offset') != offset or not isinstance(response.get('eof'), bool):
                    raise FileError('invalid_file_response')
                try:
                    chunk = base64.b64decode(response['data_base64'], validate=True)
                except (KeyError, ValueError, TypeError):
                    raise FileError('invalid_file_response') from None
                if len(chunk) > 1024 * 1024 or response.get('len') != len(chunk):
                    raise FileError('invalid_file_response')
                offset += len(chunk)
                if offset > MAX_FILE_BYTES:
                    raise FileError('file_too_large')
                parts.append(chunk)
                if response['eof']:
                    break
                if not chunk:
                    raise FileError('invalid_file_response')
            if not offset:
                raise FileError('empty_attachment')
            if expected_size is not None and offset != expected_size:
                raise FileError('file_size_changed')
            return b''.join(parts)
        except FileError:
            raise
        except Exception as error:
            raise _safe_error(error, 'file_read') from None
        finally:
            gateway.close()

    def history(self, after_seq: int = 0, limit: int = HISTORY_WINDOW) -> list[dict]:
        if not isinstance(after_seq, int) or after_seq < 0 or not isinstance(limit, int) or limit < 1:
            raise ValueError("after_seq must be nonnegative and limit must be positive")
        return self._history(after_seq, limit)

    def _history(self, after_seq: int, limit: int, stop_event=None) -> list[dict]:
        gateway = self._connect(stop_event)
        try:
            response = gateway.call_json("chat.history", body={"limit": limit}, timeout=CALL_TIMEOUT)
            if not isinstance(response, dict) or not isinstance(response.get("chat_events"), list):
                raise MuseError("history_shape_changed", "history", retryable=False)
            rows = []
            for event in response["chat_events"]:
                if not isinstance(event, dict):
                    continue
                if event.get('event_name') == 'delta.presentation':
                    for row in _normalized_presentations(event, source='history'):
                        if row['seq'] > after_seq:
                            rows.append(row)
                    continue
                elif event.get('event_name') in ('message.user', 'message.assistant'):
                    row = _normalized(event, source='history')
                else:
                    continue
                if row is not None and row["seq"] is not None and row["seq"] > after_seq:
                    rows.append(row)
            return sorted(rows, key=lambda row: row["seq"])
        except _Cancelled:
            raise
        except Exception as error:
            raise _safe_error(error, "history") from None
        finally:
            gateway.close()

    def _stream(self, gateway: Gateway, stop: _Stop, output: queue.Queue) -> None:
        def put(value: Any) -> None:
            while not stop.is_set():
                try:
                    output.put(value, timeout=0.25)
                    return
                except queue.Full:
                    pass

        buffer = b""
        messages: OrderedDict[str, str] = OrderedDict()
        try:
            stream_id = gateway._open("chat.subscribe", body={"capabilities": ["chat_cancel", "delta_stream"]})
            confirmed = False
            while not stop.is_set():
                frame = gateway._read_frame()
                if frame.stream_id != stream_id:
                    continue
                kind = frame.WhichOneof("kind")
                ended = False
                if kind == "response":
                    if not 200 <= frame.response.status < 300:
                        raise _UpstreamGatewayError(frame.response.status, "subscription rejected")
                    confirmed = True
                    buffer += bytes(frame.response.body)
                    ended = frame.response.end_body
                elif kind == "body_chunk" and confirmed:
                    buffer += bytes(frame.body_chunk.data)
                    ended = frame.body_chunk.end_body
                elif kind == "reset":
                    raise ConnectionError("Muse subscription reset")
                if len(buffer) > MAX_EVENT_BYTES:
                    raise MuseError("event_too_large", "subscribe", retryable=False)
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get("type") != "event":
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or payload.get("role") == "user" or payload.get("is_thread") is True:
                        continue
                    name = event.get("event")
                    if name == 'delta.presentation':
                        for row in _normalized_presentations(event, source='live'):
                            put(row)
                        continue
                    if name not in ("delta.text_append", "delta.message_done", "message.assistant"):
                        continue
                    message_id, sequence = _identity(event, payload)
                    key = message_id or (f"seq:{sequence}" if sequence is not None else None)
                    if key is None:
                        continue
                    prior = messages.get(key, messages.get(f"seq:{sequence}", ""))
                    if name == "delta.text_append":
                        text = payload.get("text")
                        if isinstance(text, str):
                            prior += text
                        if len(prior.encode("utf-8")) > MAX_TEXT_BYTES:
                            raise MuseError("message_too_large", "subscribe", retryable=False)
                        messages[key] = prior
                        if sequence is not None:
                            messages[f"seq:{sequence}"] = prior
                    else:
                        final = _final_text(payload, message_id)
                        row = _normalized(event, source="live", text=final if final is not None else prior,
                                          force_completed=name == "delta.message_done")
                        if row is not None and row["role"] == "assistant":
                            put(row)
                        messages.pop(key, None)
                        messages.pop(f"seq:{sequence}", None)
                    while len(messages) > 2048:
                        messages.popitem(last=False)
                if ended:
                    raise ConnectionError("Muse subscription ended")
        except _Cancelled:
            pass
        except Exception as error:
            if not stop.is_set():
                put(_safe_error(error, "subscribe"))
        finally:
            gateway.close()

    def watch(self, stop_event: threading.Event) -> None:
        """Run until stopped; AuthExpired and callback errors stop explicitly.

        Initial/periodic history uses after_seq=0 intentionally. The application
        must compare against its stored baseline before delivering anything.
        Only this watch thread invokes on_event; its reader only queues data.
        """
        output: queue.Queue = queue.Queue(maxsize=256)
        connection_stop = threading.Event()
        reader: threading.Thread | None = None
        seen: OrderedDict[tuple[Any, ...], None] = OrderedDict()
        next_connect = next_poll = 0.0
        backoff = 1.0

        def emit(row: dict) -> None:
            if row["role"] != "assistant":
                return
            key = (row.get('event_id') or row.get("message_id") or row.get("seq"), hashlib.sha256(row["text"].encode()).digest())
            if key in seen:
                return
            try:
                self.on_event(dict(row))
            except Exception:
                raise MuseError("callback_failed", "dispatch", retryable=False) from None
            seen[key] = None
            while len(seen) > 4096:
                seen.popitem(last=False)

        try:
            while not stop_event.is_set():
                now = time.monotonic()
                if reader is None and now >= next_connect:
                    connection_stop = threading.Event()
                    stop = _Stop(stop_event, connection_stop)
                    try:
                        gateway = self._connect(stop)
                    except _Cancelled:
                        break
                    except AuthExpired:
                        raise
                    except MuseError as error:
                        if not error.retryable:
                            raise
                        next_connect = time.monotonic() + backoff
                        backoff = min(30.0, backoff * 2)
                    else:
                        reader = threading.Thread(target=self._stream, args=(gateway, stop, output), daemon=True,
                                                  name="muse-main-chat-reader")
                        reader.start()
                try:
                    item = output.get(timeout=0.25)
                except queue.Empty:
                    item = None
                if isinstance(item, AuthExpired):
                    raise item
                if isinstance(item, MuseError):
                    if not item.retryable:
                        raise item
                    connection_stop.set()
                    if reader is not None:
                        reader.join(timeout=1)
                    reader = None
                    next_connect = time.monotonic() + backoff
                    backoff = min(30.0, backoff * 2)
                elif isinstance(item, dict):
                    emit(item)
                    backoff = 1.0
                if not stop_event.is_set() and time.monotonic() >= next_poll:
                    try:
                        for row in self._history(0, HISTORY_WINDOW, stop_event):
                            emit(row)
                    except _Cancelled:
                        break
                    except AuthExpired:
                        raise
                    except MuseError as error:
                        if not error.retryable:
                            raise
                    next_poll = time.monotonic() + HISTORY_POLL_SECONDS
        finally:
            connection_stop.set()
            if reader is not None:
                # Bounded recv checks cancellation every 250 ms. The owning
                # reader closes its own libcurl handle, avoiding close/read races.
                reader.join(timeout=2)
