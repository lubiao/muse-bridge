"""Offline boundary checks using the pinned, real Feishu SDK models.

All credentials below are placeholders. Network transports are blocked for
every test, and message creation is replaced with an in-process response.
"""

import json
import logging
import threading

import lark_oapi as lark
import pytest
import requests
import websockets
from lark_oapi.api.im.v1 import CreateMessageResponse, P2ImMessageReceiveV1
from lark_oapi.core.exception import ObtainAccessTokenException
from lark_oapi.core.model import RawResponse
from lark_oapi.ws.exception import ClientException

from muse_feishu_bridge.feishu import FeishuClient, FeishuError


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network calls are forbidden in Feishu adapter tests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(websockets, "connect", forbidden)


@pytest.fixture
def adapter():
    received = []
    client = FeishuClient("cli_offline_test", "offline-placeholder-secret", received.append)
    return client, received


def event_payload(**message_changes):
    message = {
        "message_id": "om_incoming",
        "chat_id": "oc_private",
        "chat_type": "p2p",
        "message_type": "text",
        "content": json.dumps({"text": "你好，Muse\n完成后通知我"}, ensure_ascii=False),
        "create_time": "1792512345678",
    }
    message.update(message_changes)
    return {
        "schema": "2.0",
        "header": {
            "event_id": "ev_must_not_be_used_for_deduplication",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "message": message,
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_allowed"},
            },
        },
    }


def sdk_response(code=0, status=200, message_id="om_sent", headers=None):
    response = CreateMessageResponse({
        "code": code,
        "msg": "This raw server message must never be logged",
        "data": {"message_id": message_id},
    })
    response.raw = RawResponse()
    response.raw.status_code = status
    response.raw.headers = headers or {}
    return response


def mock_send(monkeypatch, client, result=None, error=None):
    sent = []

    def create(request):
        sent.append(request)
        if error is not None:
            raise error
        return result if result is not None else sdk_response()

    monkeypatch.setattr(client._api.im.v1.message, "create", create)
    return sent


def test_actual_sdk_construction_is_offline_and_sets_bounded_rest_timeout(adapter):
    client, _ = adapter
    assert isinstance(client._ws, lark.ws.Client)
    assert client._api._config.timeout == 15.0
    assert client._api._config.domain == "https://open.feishu.cn"
    assert client._file_api._config.timeout == 300
    assert logging.getLogger("Lark").level == logging.CRITICAL


def test_file_upload_uses_explicit_multipart_filename_and_binary_content_type(adapter,monkeypatch):
    from muse_feishu_bridge import feishu
    client,_=adapter;seen=[]
    monkeypatch.setattr(feishu,'verify',lambda *args:None)
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def json(self):return {'code':0,'data':{'file_key':'file_v3_uploaded'}}
    def post(url,**kwargs):
        assert url=='https://open.feishu.cn/open-apis/im/v1/files'
        assert kwargs['allow_redirects'] is False
        wire=kwargs['data'].to_string();seen.append(wire)
        return Response()
    monkeypatch.setattr(feishu.requests,'post',post)
    assert client.upload_file('sample.zip',b'\x00binary\xff')=='file_v3_uploaded'
    assert b'filename="sample.zip"' in seen[0]
    assert b'Content-Type: application/octet-stream' in seen[0]
    assert b'\x00binary\xff' in seen[0]


def test_real_sdk_event_normalizes_only_the_required_envelope(adapter):
    client, received = adapter
    client._handle_message(P2ImMessageReceiveV1(event_payload()))
    assert received == [{
        "message_id": "om_incoming",
        "chat_id": "oc_private",
        "chat_type": "p2p",
        "sender_open_id": "ou_allowed",
        "text": "你好，Muse\n完成后通知我",
        "created_at_ms": 1792512345678,
    }]


def test_file_callback_only_enqueues_references_without_network(adapter):
    client, received = adapter
    client._handle_message(P2ImMessageReceiveV1(event_payload(message_type='file',
        content=json.dumps({'file_key':'file_v3_fixture', 'file_name':'研究.docx', 'file_size':905}))))
    assert received[0]['kind'] == 'file'
    assert received[0]['text'] == ''
    assert received[0]['file'] == {'file_key':'file_v3_fixture','filename':'研究.docx','size':905}


def test_file_send_uuid_and_uploaded_key_stay_identical_after_timeout(adapter, monkeypatch):
    client, _ = adapter
    sent = mock_send(monkeypatch, client)
    client.send_file('oc_private', 'file_v3_fixture', 'persisted-file-row')
    client.send_file('oc_private', 'file_v3_fixture', 'persisted-file-row')
    wire = json.loads(lark.JSON.marshal(sent[0].body))
    assert wire['msg_type'] == 'file'
    assert json.loads(wire['content']) == {'file_key':'file_v3_fixture'}
    assert sent[0].body.uuid == sent[1].body.uuid


@pytest.mark.parametrize('declared', [None, '40000000'])
def test_file_download_caps_stream_and_never_follows_redirects(adapter, monkeypatch, declared):
    from muse_feishu_bridge import feishu
    from muse_feishu_bridge.files import FileError, MAX_FILE_BYTES
    client, _ = adapter
    monkeypatch.setattr(feishu, 'verify', lambda *args: None)
    closed=[]
    class Response:
        status_code=200
        headers={} if declared is None else {'Content-Length':declared}
        def __enter__(self):return self
        def __exit__(self,*args):closed.append(True)
        def iter_content(self,size):
            for _ in range(MAX_FILE_BYTES//65536+1):yield b'x'*65536
    def get(url, **kwargs):
        assert url.startswith('https://open.feishu.cn/open-apis/im/v1/messages/om_test/resources/')
        assert kwargs['stream'] is True and kwargs['allow_redirects'] is False
        return Response()
    monkeypatch.setattr(feishu.requests,'get',get)
    with pytest.raises(FileError, match='file_too_large'):
        client.download_file('om_test','file_fixture')
    assert closed == [True]


@pytest.mark.parametrize("changes", [
    {"chat_type": "group"},
    {"message_type": "image"},
    {"content": "{invalid-json"},
    {"content": "[]"},
    {"content": '{"text": 123}'},
    {"content": '{"text": "  "}'},
    {"create_time": None},
    {"create_time": "not-a-timestamp"},
    {"create_time": -1},
    {"message_id": ""},
    {"chat_id": None},
])
def test_unsupported_or_malformed_events_are_not_queued(adapter, changes):
    client, received = adapter
    client._handle_message(P2ImMessageReceiveV1(event_payload(**changes)))
    assert received == []


def test_messages_from_bots_are_not_forwarded(adapter):
    client, received = adapter
    payload = event_payload()
    payload["event"]["sender"]["sender_type"] = "app"
    client._handle_message(P2ImMessageReceiveV1(payload))
    assert received == []


def test_missing_sender_open_id_is_not_queued(adapter):
    client, received = adapter
    payload = event_payload()
    payload["event"]["sender"]["sender_id"] = {}
    client._handle_message(P2ImMessageReceiveV1(payload))
    assert received == []


def test_enqueue_failure_propagates_for_redelivery_without_raw_details(adapter, caplog):
    client, _ = adapter

    def failed_enqueue(message):
        raise ValueError("sensitive-payload-and-secret-placeholder")

    client._on_message = failed_enqueue
    with pytest.raises(RuntimeError, match="Feishu inbox enqueue failed") as caught:
        client._handle_message(P2ImMessageReceiveV1(event_payload()))
    assert caught.value.__suppress_context__ is True
    assert "sensitive-payload" not in str(caught.value)
    assert "sensitive-payload" not in caplog.text


def test_actual_sdk_request_serialization_and_stable_uuid(adapter, monkeypatch):
    client, _ = adapter
    sent = mock_send(monkeypatch, client)
    text = '中文 "引号" \\路径\n第二行'
    assert client.send_text("oc_private", text, "durable-outbox-row-7") == "om_sent"
    assert client.send_text("oc_private", text, "durable-outbox-row-7") == "om_sent"
    client.send_text("oc_private", "different message", "durable-outbox-row-8")
    wire_body = json.loads(lark.JSON.marshal(sent[0].body))
    assert dict(sent[0].queries) == {"receive_id_type": "chat_id"}
    assert wire_body["receive_id"] == "oc_private"
    assert wire_body["msg_type"] == "text"
    assert json.loads(wire_body["content"]) == {"text": text}
    assert len(wire_body["uuid"]) == 36
    assert sent[0].body.uuid == sent[1].body.uuid
    assert sent[2].body.uuid != sent[0].body.uuid


def test_uuid_survives_a_process_client_restart(adapter, monkeypatch):
    client, _ = adapter
    first = mock_send(monkeypatch, client)
    client.send_text("oc_private", "same text", "persisted-row-1")
    restarted = FeishuClient("cli_offline_test", "offline-placeholder-secret", lambda _: None)
    second = mock_send(monkeypatch, restarted)
    restarted.send_text("oc_private", "same text", "persisted-row-1")
    assert first[0].body.uuid == second[0].body.uuid


@pytest.mark.parametrize("code,status,category,retryable", [
    (230020, 400, "rate_limit", True),
    (99991400, 429, "rate_limit", True),
    (230049, 400, "temporary", True),
    (5000, 200, "temporary", True),
    (0, 503, "temporary", True),
    (230013, 400, "permission", False),
    (99991672, 400, "permission", False),
    (99991403, 400, "monthly_quota", False),
    (99991663, 400, "authentication", False),
    (230022, 400, "content_rejected", False),
    (230001, 400, "invalid_request", False),
    (987654321, 200, "invalid_request", False),
])
def test_http_and_business_errors_are_classified(
    adapter, monkeypatch, caplog, code, status, category, retryable,
):
    client, _ = adapter
    mock_send(monkeypatch, client, result=sdk_response(code, status))
    with pytest.raises(FeishuError) as caught:
        client.send_text("oc_private", "private message body", "outbox-row")
    error = caught.value
    assert (error.category, error.retryable) == (category, retryable)
    assert (error.code, error.http_status) == (code, status)
    assert "raw server message" not in str(error) + caplog.text
    assert "private message body" not in str(error) + caplog.text


def test_rate_limit_preserves_numeric_retry_after(adapter, monkeypatch):
    client, _ = adapter
    mock_send(monkeypatch, client, result=sdk_response(
        99991400, 429, headers={"Retry-After": "15"},
    ))
    with pytest.raises(FeishuError) as caught:
        client.send_text("oc_private", "text", "row")
    assert caught.value.retry_after_seconds == 15


@pytest.mark.parametrize("transport_error", [
    requests.Timeout("secret-placeholder"),
    requests.ConnectionError("secret-placeholder"),
])
def test_transport_failures_preserve_delivery_uncertainty(adapter, monkeypatch, transport_error):
    client, _ = adapter
    mock_send(monkeypatch, client, error=transport_error)
    with pytest.raises(FeishuError) as caught:
        client.send_text("oc_private", "text", "row")
    assert caught.value.retryable is True
    assert caught.value.delivery_uncertain is True
    assert "secret-placeholder" not in str(caught.value)


def test_missing_response_message_id_is_uncertain(adapter, monkeypatch):
    client, _ = adapter
    mock_send(monkeypatch, client, result=sdk_response(message_id=None))
    with pytest.raises(FeishuError) as caught:
        client.send_text("oc_private", "text", "row")
    assert caught.value.category == "protocol"
    assert caught.value.retryable and caught.value.delivery_uncertain


@pytest.mark.parametrize("code,category,retryable", [
    (10003, "authentication", False),
    (99991400, "rate_limit", True),
    (5000, "temporary", True),
])
def test_sdk_token_acquisition_failures_are_sanitized(
    adapter, monkeypatch, code, category, retryable,
):
    client, _ = adapter
    mock_send(monkeypatch, client, error=ObtainAccessTokenException(
        "token acquisition", code, "secret-placeholder",
    ))
    with pytest.raises(FeishuError) as caught:
        client.send_text("oc_private", "text", "row")
    assert caught.value.category == category
    assert caught.value.retryable is retryable
    assert caught.value.delivery_uncertain is False
    assert "secret-placeholder" not in str(caught.value)


def test_nested_json_request_size_is_checked_before_transport(adapter, monkeypatch):
    client, _ = adapter
    sent = mock_send(monkeypatch, client)
    # The second escaping layer is what makes this exceed the wire-body budget.
    with pytest.raises(ValueError, match="too large"):
        client.send_text("oc_private", '"' * 40_000, "row")
    assert sent == []


def test_start_sanitizes_sdk_auth_failure_and_is_single_use(adapter, monkeypatch):
    client, _ = adapter

    def failed_start():
        raise ClientException(10003, "secret-placeholder")

    monkeypatch.setattr(client._ws, "start", failed_start)
    with pytest.raises(FeishuError) as caught:
        client.start()
    assert caught.value.category == "configuration"
    assert caught.value.code == 10003
    assert "secret-placeholder" not in str(caught.value)
    with pytest.raises(RuntimeError, match="once"):
        client.start()


def test_start_rejects_background_thread_before_touching_sdk(adapter, monkeypatch):
    client, _ = adapter
    calls = []
    monkeypatch.setattr(client._ws, "start", lambda: calls.append("start"))
    errors = []

    def run():
        try:
            client.start()
        except RuntimeError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == ["Feishu start() must run on the main thread"]
    assert calls == []


def test_stop_explicitly_reports_sdk_limitation(adapter):
    client, _ = adapter
    with pytest.raises(NotImplementedError, match="no public stop API"):
        client.stop()


def test_native_image_send_uses_image_type_and_stable_uuid(adapter,monkeypatch):
    client,_=adapter
    sent=mock_send(monkeypatch,client)
    client.send_image('oc_private','img_key','native-image')
    client.send_image('oc_private','img_key','native-image')
    assert len(sent)==2
    assert sent[0].request_body.msg_type=='image'
    assert json.loads(sent[0].request_body.content)=={'image_key':'img_key'}
    assert sent[0].request_body.uuid==sent[1].request_body.uuid


def test_webp_upload_uses_native_image_endpoint_and_png_bytes(adapter,monkeypatch):
    import io
    from PIL import Image
    client,_=adapter
    raw=io.BytesIO();Image.new('RGB',(7,9),'red').save(raw,format='WEBP')
    calls=[]
    def upload(request):
        calls.append(request)
        result=sdk_response();result.data.image_key='img_native'
        return result
    monkeypatch.setattr(client._file_api.im.v1.image,'create',upload)
    assert client.upload_image('avatar.webp',raw.getvalue())=='img_native'
    assert calls[0].request_body.image_type=='message'
    assert calls[0].request_body.image.getvalue().startswith(b'\x89PNG')


def test_native_image_callback_enqueues_image_reference_without_network(adapter):
    client,received=adapter
    client._handle_message(P2ImMessageReceiveV1(event_payload(message_type='image',content=json.dumps({'image_key':'img_v3_test'}))))
    assert received[0]['file']=={'media_kind':'image','images':[{'file_key':'img_v3_test','filename':'image','size':None}],'caption':''}


def test_image_download_uses_message_resource_image_query_and_image_cap(adapter,monkeypatch):
    from muse_feishu_bridge import feishu
    client,_=adapter;calls=[]
    monkeypatch.setattr(feishu,'verify',lambda *a:None)
    class Response:
        status_code=200;headers={'Content-Length':'10000001'}
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_content(self,*a):pytest.fail('oversized body must not be read')
    def get(url,**kw):calls.append((url,kw));return Response()
    monkeypatch.setattr(feishu.requests,'get',get)
    from muse_feishu_bridge.files import FileError
    with pytest.raises(FileError):client.download_image('om_test','img_v3_test')
    assert dict(calls[0][1]['params'])['type']=='image' and calls[0][1]['allow_redirects'] is False
