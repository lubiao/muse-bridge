"""Offline regressions for delivery ambiguity, actual framing, and proactive chat.

No test contacts Muse or reads credentials. Protobuf stream fixtures exercise
the module's real parser; fake gateways replace only transport boundaries.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest
from muse_cli.gateway import ServiceFrame
from muse_feishu_bridge import muse


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("This test must not access a real network or account")
    for name in ("fetch_session_info", "fetch_access_token", "fetch_hatch_token"):
        monkeypatch.setattr(muse, name, forbidden)
    monkeypatch.setattr(muse.WebSocket, "connect", forbidden)


def no_credentials():
    raise AssertionError("Tests must not consult a credential provider")


class FakeGateway:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error
        self.calls = []
        self.closed = False

    def call_json(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


class FakeStream(FakeGateway):
    def __init__(self, frames=(), stop=None):
        super().__init__()
        self.frames, self.stop = list(frames), stop

    def _open(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return 1

    def _read_frame(self):
        if self.frames:
            return self.frames.pop(0)
        if self.stop is not None:
            while not self.stop.is_set():
                time.sleep(0.005)
            raise muse._Cancelled()
        raise ConnectionError("private-service-response-must-not-leak")


def stream_frames(records):
    response = ServiceFrame(stream_id=1)
    response.response.status = 200
    body = ServiceFrame(stream_id=1)
    body.body_chunk.data = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
    return [response, body]


def live(name, message_id, seq, **payload):
    return {"type": "event", "event": name,
            "payload": {"message_id": message_id, "message_seq": seq, **payload}}


def historical(seq, role, text, **payload):
    return {"seq": seq, "event_name": "message." + role,
            "payload": {"message_id": "id" + str(seq), "display_text": text,
                        "status": "completed", **payload}}


def test_send_existing_main_chat_ack_is_not_submitted_twice(monkeypatch):
    transport = FakeGateway({"message_id": "user-ack"})
    client = muse.MainChatClient(no_credentials, lambda event: None)
    monkeypatch.setattr(client, "_connect", lambda stop_event=None: transport)
    first = client.send_text("hello", "request-1")
    assert client.send_text("hello", "request-1") == first
    assert first == {"request_id": "request-1", "status": "accepted", "message_id": "user-ack"}
    assert len(transport.calls) == 1
    method, request = transport.calls[0]
    assert method == "chat.stream"
    assert request["body"]["items"] == [{"type": "text", "text": "hello"}]
    assert "session_id" not in request["body"]
    assert "capabilities" not in request["body"]
    assert transport.closed


def file_widget(seq=200, **file_changes):
    file={'path':'sandbox://workspace/your_files/sample.docx','label':'sample.docx',
          'mime':'application/octet-stream','byte_len':5,'missing':False}
    file.update(file_changes)
    return {'seq':seq,'event_name':'delta.presentation','payload':{'message_id':'file-message',
        'id':'widget-1','seq':seq,'role':'assistant','kind':'file','is_thread':False,'data':{'file':file}}}


def test_file_submission_carries_raw_bytes_and_keeps_original_main_chat(monkeypatch):
    import base64
    gateway=FakeGateway({'message_id':'file-ack'})
    client=muse.MainChatClient(no_credentials,lambda event:None)
    monkeypatch.setattr(client,'_connect',lambda *args:gateway)
    data=bytes(range(256))
    assert client.send_file('sample.docx','application/octet-stream',data,'file-request')['status']=='accepted'
    client.send_file('sample.docx','application/octet-stream',data,'file-request')
    assert len(gateway.calls)==1
    body=gateway.calls[0][1]['body']
    assert 'session_id' not in body
    assert base64.b64decode(body['items'][0]['data_base64'])==data


def test_history_normalizes_file_widgets_without_requiring_text_completion(monkeypatch):
    gateway=FakeGateway({'chat_events':[file_widget()]})
    client=muse.MainChatClient(no_credentials,lambda event:None)
    monkeypatch.setattr(client,'_connect',lambda *args:gateway)
    row=client.history()[0]
    assert row['kind']=='file' and row['event_id']=='file:file-message:widget-1'
    assert row['seq']==200 and row['completed'] is True
    assert row['file']['size']==5


def test_live_file_widget_matches_history_identity():
    record=file_widget()
    live_record={'type':'event','event':'delta.presentation','payload':record['payload']}
    a=muse._normalized_file(record,source='history')
    b=muse._normalized_file(live_record,source='live')
    assert (a['event_id'],a['seq'])==(b['event_id'],b['seq'])
    live_record['payload']['is_thread']=True
    assert muse._normalized_file(live_record,source='live') is None


def test_file_read_chunks_decodes_binary_and_checks_eof(monkeypatch):
    import base64
    data=bytes(range(256))*1200
    class Chunked(FakeGateway):
        def call_json(self,method,**kwargs):
            assert method=='fs.read'
            body=kwargs['body'];offset=body['offset'];raw=data[offset:offset+body['len']]
            self.calls.append(body)
            return {'offset':offset,'len':len(raw),'eof':offset+len(raw)==len(data),'data_base64':base64.b64encode(raw).decode()}
    gateway=Chunked();client=muse.MainChatClient(no_credentials,lambda event:None)
    monkeypatch.setattr(client,'_connect',lambda *args:gateway)
    assert client.read_file('sandbox://workspace/your_files/test.bin',len(data))==data
    assert len(gateway.calls)==2 and gateway.closed


def test_large_upload_is_chunked_without_agent_execution_and_reference_is_small(monkeypatch):
    import base64,hashlib
    data=bytes(range(256))*9000
    class Upload(FakeGateway):
        def __init__(self):super().__init__();self.raw=bytearray()
        def call_json(self,method,**kwargs):
            self.calls.append((method,kwargs))
            if method=='fs.stat':return {'kind':'file','size':len(self.raw)}
            assert method=='fs.write'
            body=kwargs['body'];chunk=base64.b64decode(body['data_base64'])
            assert len(chunk)<=256*1024
            if body['overwrite']:
                assert not body['append'];self.raw.clear()
            else:assert body['append']
            self.raw.extend(chunk)
            return {'bytes_written':len(chunk),'append':body['append']}
    upload=Upload();client=muse.MainChatClient(no_credentials,lambda event:None)
    monkeypatch.setattr(client,'_connect',lambda *args:upload)
    item=client.prepare_file_reference('large.zip','application/zip',data,'stable-large-request')
    assert bytes(upload.raw)==data
    assert item['type']=='file_ref' and item['bytes_len']==len(data)
    assert item['sha256']==hashlib.sha256(data).hexdigest()
    assert upload.closed and all(method!='chat.stream' for method,_ in upload.calls)
    submission=FakeGateway({'message_id':'large-ack'})
    monkeypatch.setattr(client,'_connect',lambda *args:submission)
    client.send_file_reference(item,'stable-large-request')
    assert len(json.dumps(submission.calls[0][1]['body']))<1024
    assert submission.calls[0][1]['body']['items'][0]['path']==item['path']
    assert 'session_id' not in submission.calls[0][1]['body']


def test_upload_timeout_is_retryable_and_starts_again_with_overwrite(monkeypatch):
    import base64
    data=b'x'*600000;offsets=[]
    class Upload(FakeGateway):
        def call_json(self,method,**kwargs):
            if method=='fs.stat':return {'kind':'file','size':len(data)}
            body=kwargs['body'];offsets.append(body['overwrite'])
            if len(offsets)==2:raise TimeoutError('do-not-leak')
            return {'bytes_written':len(base64.b64decode(body['data_base64'])),'append':body['append']}
    client=muse.MainChatClient(no_credentials,lambda event:None)
    monkeypatch.setattr(client,'_connect',lambda *args:Upload())
    with pytest.raises(muse.MuseError) as caught:client.prepare_file_reference('x.zip','application/zip',data,'retry-upload')
    assert not isinstance(caught.value,muse.DeliveryUnknown) and caught.value.retryable
    client.prepare_file_reference('x.zip','application/zip',data,'retry-upload')
    assert offsets[:3]==[True,False,True]


@pytest.mark.parametrize('path',['/etc/passwd','workspace/../secret','https://example.com/a','sandbox://workspace/a/../../b','workspace/a\\b'])
def test_file_read_rejects_non_workspace_paths_before_authentication(path):
    from muse_feishu_bridge.files import FileError
    client=muse.MainChatClient(no_credentials,lambda event:None)
    with pytest.raises(FileError):client.read_file(path)


@pytest.mark.parametrize(("response_body", "accepted"), [
    (b'{"message_id":"ack-user"}\n', True),
    (b'{"result":{"message_id":"ack-user"}}\n', True),
    (b'{"type":"ack"}\n{"message_id":"ack-user"}\n', False),
    (b'{"type":"event","event":"message.user","payload":{"message_id":"echo-user"}}\n', False),
])
def test_real_gateway_json_parser_accepts_one_record_but_not_ambiguous_echo(monkeypatch, response_body, accepted):
    # Exercise the installed upstream request/call_json implementation with real
    # protobuf response frames. No Gateway constructor or network is involved.
    gateway = object.__new__(muse._BoundedGateway)
    gateway.ws = SimpleNamespace(deadline=None, terminate=lambda: None)
    frame = ServiceFrame(stream_id=1)
    frame.response.status = 200
    frame.response.body = response_body
    frame.response.end_body = True
    writes = []
    def open_stream(method, path_params=None, body=None, query=None):
        writes.append((method, body))
        return 1
    monkeypatch.setattr(gateway, "_open", open_stream)
    monkeypatch.setattr(gateway, "_read_frame", lambda: frame)
    client = muse.MainChatClient(no_credentials, lambda event: None)
    monkeypatch.setattr(client, "_connect", lambda stop_event=None: gateway)
    if accepted:
        assert client.send_text("hello", "raw-ack")["message_id"] == "ack-user"
    else:
        with pytest.raises(muse.DeliveryUnknown):
            client.send_text("hello", "raw-ack")
        with pytest.raises(muse.DeliveryUnknown):
            client.send_text("hello", "raw-ack")
    assert len(writes) == 1


@pytest.mark.parametrize("transport_error", [
    TimeoutError("private-token-bearing-url"),
    muse._UpstreamGatewayError(401, "private-auth-response"),
    muse._UpstreamGatewayError(-1, "non-JSON response: private-NDJSON-echo"),
])
def test_unknown_submission_including_late_auth_is_never_resent(monkeypatch, transport_error):
    transport = FakeGateway(error=transport_error)
    client = muse.MainChatClient(no_credentials, lambda event: None)
    monkeypatch.setattr(client, "_connect", lambda stop_event=None: transport)
    for _ in range(2):
        with pytest.raises(muse.DeliveryUnknown) as raised:
            client.send_text("hello", "request-unknown")
        assert not raised.value.retryable
        assert "private" not in str(raised.value)
    assert len(transport.calls) == 1
    assert transport.closed


def test_authentication_failure_before_submission_is_explicit_and_safe(monkeypatch):
    safe = muse._safe_error(muse._UpstreamAuthError("hatch/token -> 401 private-response"), "connect")
    assert isinstance(safe, muse.AuthExpired)
    assert (safe.phase, safe.http_status, safe.retryable) == ("hatch/token", 401, False)
    assert "private" not in str(safe)
    client = muse.MainChatClient(no_credentials, lambda event: None)
    def rejected(stop_event=None):
        raise safe
    monkeypatch.setattr(client, "_connect", rejected)
    with pytest.raises(muse.AuthExpired):
        client.send_text("hello", "not-submitted")
    assert "not-submitted" not in client._submissions


def test_history_keeps_self_referential_proactive_messages_and_completed_users(monkeypatch):
    transport = FakeGateway({"chat_events": [
        historical(9, "assistant", "old"),
        historical(11, "user", "input"),
        historical(12, "assistant", "reminder", reply_to_message_id="id12"),
        historical(13, "assistant", "side chat", is_thread=True),
        historical(14, "assistant", "still writing", status=None),
    ]})
    client = muse.MainChatClient(no_credentials, lambda event: None)
    monkeypatch.setattr(client, "_connect", lambda stop_event=None: transport)
    rows = client.history(after_seq=10, limit=500)
    assert [(row["seq"], row["role"]) for row in rows] == [(11, "user"), (12, "assistant")]
    assert rows[-1]["reply_to_message_id"] == rows[-1]["message_id"]
    assert all(row["completed"] and row["source"] == "history" for row in rows)
    assert transport.calls[0][1]["body"] == {"limit": 500}
    assert transport.closed


@pytest.mark.parametrize("encoded_transcript", [False, True])
def test_stream_handles_structured_transcript_missing_final_text_and_proactive_push(encoded_transcript):
    transcript = {"messages": [
        {"id": "r0", "role": "assistant", "content": [{"type": "text", "text": "old"}]},
        {"id": "r1", "role": "assistant", "content": [{"type": "text", "text": "FINAL"}]},
    ]}
    if encoded_transcript:
        transcript = json.dumps(transcript)
    records = [
        live("delta.text_append", "r1", 20, text="draft"),
        live("delta.message_done", "r1", 20, transcript=transcript),
        live("delta.text_append", "r2", 21, text="KEEP"),
        live("delta.message_done", "r2", 21),
        live("message.assistant", "r3", 22, display_text="REMIND", status="completed",
             reply_to_message_id="r3"),
        live("delta.message_done", "r4", 23, display_text="OTHER CHAT", is_thread=True),
    ]
    transport = FakeStream(stream_frames(records))
    output = queue.Queue()
    client = muse.MainChatClient(no_credentials, lambda event: None)
    client._stream(transport, muse._Stop(), output)
    rows, errors = [], []
    while not output.empty():
        value = output.get_nowait()
        (rows if isinstance(value, dict) else errors).append(value)
    assert [row["text"] for row in rows] == ["FINAL", "KEEP", "REMIND"]
    assert rows[-1]["reply_to_message_id"] == "r3"
    assert all(row["source"] == "live" and row["completed"] for row in rows)
    assert len(errors) == 1 and isinstance(errors[0], muse.MuseError)
    assert "private" not in str(errors[0])
    assert transport.closed


def test_watch_callback_runs_serially_and_reader_closes_its_connection(monkeypatch):
    stop = threading.Event()
    received, callback_threads, transports = [], [], []
    def receive(row):
        received.append(row)
        callback_threads.append(threading.get_ident())
        stop.set()
    client = muse.MainChatClient(no_credentials, receive)
    def connect(stop_event=None):
        transport = FakeStream(stream_frames([live("delta.message_done", "r", 20, content="answer")]), stop_event)
        transports.append(transport)
        return transport
    monkeypatch.setattr(client, "_connect", connect)
    monkeypatch.setattr(client, "_history", lambda *args: [])
    client.watch(stop)
    assert [row["text"] for row in received] == ["answer"]
    assert callback_threads == [threading.get_ident()]
    assert transports[0].closed


@pytest.mark.parametrize("failure", [
    muse.AuthExpired("authentication_rejected", "subscribe", retryable=False, http_status=401),
    muse.MuseError("schema_change", "subscribe", retryable=False),
])
def test_watch_propagates_authentication_and_nonretryable_failures(monkeypatch, failure):
    client = muse.MainChatClient(no_credentials, lambda event: None)
    monkeypatch.setattr(client, "_connect", lambda stop_event=None: FakeGateway())
    monkeypatch.setattr(client, "_history", lambda *args: [])
    def stream(gateway, stop, output):
        gateway.close()
        output.put(failure)
    monkeypatch.setattr(client, "_stream", stream)
    with pytest.raises(type(failure)) as raised:
        client.watch(threading.Event())
    assert raised.value is failure


def test_nonblocking_receive_deadline_exits_without_network(monkeypatch):
    class FakeCurl:
        def getinfo(self, key):
            return 0
    class NoData(muse._BoundedWebSocket):
        def __init__(self):
            self.stop = muse._Stop()
            self.deadline = time.monotonic() + 0.025
        @property
        def curl(self):
            return FakeCurl()
        def recv_fragment(self):
            raise muse.CurlError("again", muse.CurlECode.AGAIN)
    def wait(*args):
        time.sleep(min(args[-1], 0.005))
        return [], [], []
    monkeypatch.setattr(muse, "select", wait)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        NoData().recv()
    assert time.monotonic() - start < 0.5


def test_live_high_sequence_does_not_hide_later_low_sequence_history(monkeypatch):
    stop = threading.Event()
    received = []
    def receive(row):
        received.append(row)
        if len(received) == 2:
            stop.set()
    client = muse.MainChatClient(no_credentials, receive)
    monkeypatch.setattr(client, "_connect", lambda stop_event=None: FakeStream(
        stream_frames([live("delta.message_done", "newer", 100, content="newer completion")]), stop_event))
    history_calls = []
    def history(after_seq, limit, stop_event=None):
        history_calls.append((after_seq, limit))
        return [{"message_id": "older", "seq": 90, "role": "assistant",
                 "text": "late older completion", "completed": True, "source": "history"}]
    monkeypatch.setattr(client, "_history", history)
    client.watch(stop)
    assert [(row["seq"], row["source"]) for row in received] == [(100, "live"), (90, "history")]
    assert history_calls == [(0, 500)]
