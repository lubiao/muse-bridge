"""Worker integration tests: fake transports, real SQLite, no remote accounts."""

from types import SimpleNamespace

import pytest

import muse_feishu_bridge.bridge as bridge_module
from muse_feishu_bridge.bridge import Bridge, split_text
from muse_feishu_bridge.config import Config
from muse_feishu_bridge.feishu import FeishuError
from muse_feishu_bridge.muse import AuthExpired, DeliveryUnknown, MuseError
from muse_feishu_bridge.state import State


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def harness(tmp_path):
    states = []

    def create(*, bound=True, baseline=100, chunk_chars=100, max_chars=20000):
        clock = Clock()
        directory = tmp_path / str(len(states))
        config = Config(path=directory / "config.json", app_id="offline-app",
                        secrets_file=directory / "unused-secrets.json", data_dir=directory / "state",
                        outbound_chunk_chars=chunk_chars, max_message_chars=max_chars)
        state = State(config.data_dir, clock=clock)
        states.append(state)
        if bound:
            assert state.bind(state.new_pairing_token(), "ou_owner", "oc_private")
        if baseline is not None:
            state.put("baseline_seq", baseline)
        muse = SimpleNamespace(send_text=None, history=None, watch=None)
        feishu = SimpleNamespace(send_text=None)
        bridge = Bridge(config, state, muse, feishu)
        return SimpleNamespace(bridge=bridge, state=state, muse=muse, feishu=feishu, clock=clock)

    yield create
    for state in states:
        state.close()


def incoming(message_id="om_message", text="do the task", **changes):
    message = {"chat_type": "p2p", "sender_open_id": "ou_owner", "chat_id": "oc_private",
               "message_id": message_id, "text": text}
    message.update(changes)
    return message


def completed(message_id="m_reply", seq=101, text="Muse answer", **changes):
    event = {"message_id": message_id, "seq": seq, "text": text,
             "role": "assistant", "completed": True, "is_thread": False}
    event.update(changes)
    return event


def outgoing_rows(state):
    return [dict(row) for row in state.db.execute("SELECT * FROM outbox ORDER BY rowid")]


def run_one_incoming(h, behavior):
    calls = []

    def send(text, request_id):
        calls.append((text, request_id))
        h.bridge.stop_event.set()
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    h.muse.send_text = send
    h.bridge.baseline_ready.set()
    h.bridge._incoming()
    return calls


def test_owner_binding_and_exact_private_chat_are_required_before_enqueuing(harness):
    h = harness(bound=False)
    h.bridge.receive(incoming(text="task before binding"))
    assert h.state.counts()["inbox"] == {}
    token = h.state.new_pairing_token()
    h.bridge.receive(incoming("bind", "/bind " + token))
    assert h.state.owner() == {"open_id": "ou_owner", "chat_id": "oc_private"}
    assert len(outgoing_rows(h.state)) == 1
    for changes in ({"sender_open_id": "ou_other"}, {"chat_id": "oc_other"},
                    {"chat_type": "group"}, {"sender_open_id": None}, {"message_id": "x" * 257}):
        rejected = incoming("rejected", "/status")
        rejected.update(changes)
        h.bridge.receive(rejected)
    h.bridge.receive(incoming("valid", "real task"))
    h.bridge.receive(incoming("valid", "duplicate changed content"))
    assert h.state.counts()["inbox"] == {"pending": 1}
    assert h.state.claim_incoming()["text"] == "real task"
    assert len(outgoing_rows(h.state)) == 1


def test_status_and_oversized_text_stay_local_without_becoming_muse_tasks(harness):
    h = harness(max_chars=20)
    h.bridge.receive(incoming("status", "/status"))
    h.bridge.receive(incoming("too-long", "长" * 21))
    assert h.state.counts()["inbox"] == {}
    rows = outgoing_rows(h.state)
    assert [row["id"] for row in rows] == ["status:status", "oversize:too-long"]
    assert {row["chat_id"] for row in rows} == {"oc_private"}


@pytest.mark.parametrize("text,limit", [("a" * 100 + "\n" + "b" * 150, 100),
                                       ("你好🙂\n" * 130, 100), ("x" * 1205, 100)])
def test_chunks_are_bounded_and_lossless_even_when_newline_is_at_the_boundary(text, limit):
    pieces = split_text(text, limit)
    assert "".join(pieces) == text
    assert all(0 < len(piece) <= limit for piece in pieces)


def test_muse_main_chat_only_and_proactive_messages_are_durably_forwarded_once(harness):
    h = harness()
    for changes in ({"role": "user"}, {"completed": False}, {"is_thread": True}, {"text": ""}):
        h.bridge.receive_muse(completed(**changes))
    assert outgoing_rows(h.state) == []
    text = "你好🙂" * 103
    # A proactive Muse reply may carry a reply relationship and must still be forwarded.
    h.bridge.receive_muse(completed(text=text, reply_to_message_id="earlier-task"))
    h.bridge.receive_muse(completed(text=text, reply_to_message_id="earlier-task"))
    h.bridge.receive_muse(completed("historical", seq=100, text="before baseline"))
    rows = outgoing_rows(h.state)
    assert "".join(row["text"] for row in rows) == text
    assert {row["chat_id"] for row in rows} == {"oc_private"}
    assert len(rows) == len(split_text(text, 100))


def test_acknowledged_incoming_task_is_sent_once_with_persisted_request_key(harness):
    h = harness()
    h.bridge.receive(incoming())
    saved = h.state.db.execute("SELECT request_id FROM inbox").fetchone()[0]
    calls = run_one_incoming(h, {"status": "accepted"})
    assert calls == [("do the task", saved)]
    assert h.state.counts()["inbox"] == {"sent": 1}
    assert h.state.claim_incoming() is None


@pytest.mark.parametrize("result", [DeliveryUnknown("lost_ack", "send"),
                                    {"status": "unconfirmed"},
                                    RuntimeError("PRIVATE_TRANSPORT_BODY")])
def test_uncertain_submission_is_not_automatically_replayed(harness, result, caplog):
    h = harness()
    h.bridge.receive(incoming())
    calls = run_one_incoming(h, result)
    assert len(calls) == 1
    assert h.state.counts()["inbox"] == {"unknown": 1}
    assert h.state.claim_incoming() is None
    assert h.state.recover() == 0
    assert len(outgoing_rows(h.state)) == 1
    assert "PRIVATE_TRANSPORT_BODY" not in caplog.text
    assert "PRIVATE_TRANSPORT_BODY" not in outgoing_rows(h.state)[0]["text"]


def test_sender_auth_failure_keeps_task_and_baseline_gate_for_retry_after_backoff(harness):
    h = harness()
    h.bridge.receive(incoming())
    run_one_incoming(h, AuthExpired("authentication_rejected", "auth", retryable=False))
    assert h.state.counts()["inbox"] == {"pending": 1}
    assert h.bridge.baseline_ready.is_set()
    assert h.state.get("baseline_seq") == 100
    assert len(outgoing_rows(h.state)) == 1
    h.bridge.stop_event.clear()
    run_one_incoming(h, {"status": "accepted"})
    assert h.state.counts()["inbox"] == {"sent": 1}


@pytest.mark.parametrize("recovered_by", ["sender", "watch"])
def test_auth_alert_deduplicates_one_outage_but_not_a_new_outage_after_recovery(harness, recovered_by):
    h = harness()
    h.bridge.authentication_failed()
    first_alert = h.state.claim_outgoing()
    assert first_alert["chat_id"] == "oc_private"
    assert first_alert["id"].startswith("notice:muse-auth:")
    h.state.outgoing_sent(first_alert["id"], "om_first_auth_alert")
    for _ in range(3):
        h.bridge.authentication_failed()
    assert h.state.counts()["outbox"] == {"sent": 1}

    if recovered_by == "sender":
        h.bridge.receive(incoming("after-renewal", "resume task"))
        run_one_incoming(h, {"status": "accepted"})
    else:
        h.muse.history = lambda **kwargs: []
        h.muse.watch = lambda stop: stop.set()
        h.bridge._watch()
    assert h.bridge.auth_episode is None

    h.bridge.authentication_failed()
    h.bridge.authentication_failed()
    rows = outgoing_rows(h.state)
    assert len(rows) == 2
    assert rows[1]["id"].startswith("notice:muse-auth:")
    assert rows[1]["id"] != first_alert["id"]
    assert rows[1]["chat_id"] == "oc_private"
    assert h.state.counts()["outbox"] == {"pending": 1, "sent": 1}


def test_definite_nonretryable_muse_rejection_is_not_resubmitted(harness):
    h = harness()
    h.bridge.receive(incoming())
    run_one_incoming(h, MuseError("invalid_request", "send", retryable=False))
    assert h.state.counts()["inbox"] == {"failed": 1}
    assert h.state.claim_incoming() is None


def test_initial_baseline_uses_maximum_history_sequence_without_dumping_old_text(harness):
    h = harness(baseline=None)
    history_calls = []

    def history(*, limit):
        history_calls.append(limit)
        return [completed("older", 6), completed("latest", 12), completed("unordered", 9)]

    h.muse.history = history
    h.muse.watch = lambda stop: stop.set()
    h.bridge._watch()
    assert h.state.get("baseline_seq") == 12
    assert h.bridge.baseline_ready.is_set()
    assert history_calls == [h.bridge.config.history_limit]
    assert outgoing_rows(h.state) == []


def test_watch_reconnect_backfills_late_messages_without_moving_initial_baseline(harness):
    h = harness(baseline=100)
    h.bridge.receive_muse(completed("already-delivered", 130, "live"))
    h.muse.history = lambda **kwargs: [completed("already-delivered", 130, "live"),
                                      completed("late-message", 120, "history backfill"),
                                      completed("old-message", 99, "pre-baseline")]
    h.muse.watch = lambda stop: stop.set()
    h.bridge._watch()
    assert h.state.get("baseline_seq") == 100
    assert h.state.get("last_muse_seq") == 130
    assert [row["text"] for row in outgoing_rows(h.state)] == ["live", "history backfill"]


def test_watch_auth_failure_clears_gate_but_recovers_using_new_history_check(harness, monkeypatch):
    h = harness(baseline=100)
    attempts, waits = [], []

    def history(**kwargs):
        attempts.append("history")
        if len(attempts) == 1:
            raise AuthExpired("authentication_rejected", "history", retryable=False)
        return [completed("after-renewal", 101, "after renewal")]

    def wait(delay):
        waits.append(delay)
        assert not h.bridge.baseline_ready.is_set()
        assert h.state.get("baseline_seq") == 100
        return False

    h.muse.history = history
    h.muse.watch = lambda stop: stop.set()
    monkeypatch.setattr(h.bridge.stop_event, "wait", wait)
    h.bridge._watch()
    assert attempts == ["history", "history"]
    assert waits == [60]
    assert h.bridge.baseline_ready.is_set()
    assert h.state.get("baseline_seq") == 100
    assert any(row["text"] == "after renewal" for row in outgoing_rows(h.state))


def test_uncertain_feishu_delivery_retries_same_key_and_text_within_window(harness):
    h = harness()
    h.state.queue_text("muse:reply:0", "oc_private", "fixed payload")
    calls = []

    def send(chat, text, key):
        calls.append((chat, text, key))
        h.bridge.stop_event.set()
        if len(calls) == 1:
            raise FeishuError("transport", retryable=True, delivery_uncertain=True,
                              retry_after_seconds=5)
        return "om_delivered"

    h.feishu.send_text = send
    h.bridge._outgoing()
    assert h.state.counts()["outbox"] == {"pending": 1}
    h.clock.now += 5
    h.bridge.stop_event.clear()
    h.bridge._outgoing()
    assert calls == [("oc_private", "fixed payload", "muse:reply:0")] * 2
    assert h.state.counts()["outbox"] == {"sent": 1}
    assert outgoing_rows(h.state)[0]["remote_id"] == "om_delivered"


def test_start_recovers_ambiguous_muse_send_before_starting_any_worker(harness, monkeypatch):
    h = harness()
    h.state.enqueue_incoming("interrupted", "oc_private", "task")
    h.state.claim_incoming()
    started = []

    class RecordedThread:
        def __init__(self, *, name, target, daemon):
            self.name = name
            assert daemon

        def start(self):
            assert h.state.counts()["inbox"] == {"unknown": 1}
            started.append(self.name)

        def join(self, timeout):
            pass

    monkeypatch.setattr(bridge_module.threading, "Thread", RecordedThread)
    h.bridge.start()
    assert set(started) == {"muse-watch", "muse-send", "feishu-send", "health"}
    assert h.state.claim_incoming() is None
    notices = outgoing_rows(h.state)
    assert len(notices) == 1
    assert notices[0]["id"].startswith("notice:restart-unknown:")
    h.bridge.stop()


def attachment(message_id='attachment-1',seq=101):
    return completed(message_id,seq,text='',kind='file',event_id='file:'+message_id,
        file={'path':'sandbox://workspace/your_files/sample.docx','filename':'sample.docx','size':5,'mime_type':'application/octet-stream'})


def incoming_file(**changes):
    return incoming(text='',kind='file',file={'filename':'input.docx','file_key':'file_fixture','size':5},**changes)


def test_file_owner_filter_precedes_all_downloads_and_local_enqueue(harness):
    h=harness()
    h.bridge.receive(incoming_file(sender_open_id='stranger'))
    h.bridge.receive(incoming_file(chat_id='other-chat'))
    assert h.state.counts()['inbox']=={}
    h.bridge.receive(incoming_file())
    h.bridge.receive(incoming_file())
    assert h.state.counts()['inbox']=={'pending':1}
    assert h.state.claim_incoming()['kind']=='file'


def test_file_download_failure_retries_before_submission_and_then_forwards_cached_bytes(harness):
    h=harness();h.bridge.receive(incoming_file());calls=[]
    def download(*args):
        calls.append('download');h.bridge.stop_event.set()
        if calls.count('download')==1:raise FeishuError('transport',retryable=True)
        return b'12345'
    def send(name,mime,data,key):
        calls.append('send');assert data==b'12345'
        h.bridge.stop_event.set();return {'status':'accepted'}
    h.feishu.download_file=download;h.muse.send_file=send;h.bridge.baseline_ready.set()
    h.bridge._incoming()
    assert h.state.counts()['inbox']=={'pending':1}
    h.bridge.stop_event.clear();h.bridge._incoming()
    assert calls==['download','download','send']
    assert h.state.counts()['inbox']=={'sent':1}


def test_file_ambiguous_muse_submission_stays_unknown_after_restart(harness):
    h=harness();h.bridge.receive(incoming_file())
    h.feishu.download_file=lambda *args:b'12345'
    def send(*args):
        h.bridge.stop_event.set();raise DeliveryUnknown('lost_ack','send')
    h.muse.send_file=send;h.bridge.baseline_ready.set();h.bridge._incoming()
    assert h.state.counts()['inbox']=={'unknown':1}
    h.state.recover();assert h.state.claim_incoming() is None


def test_upgrade_skips_historical_files_then_deduplicates_new_file_widgets(harness):
    h=harness()
    h.muse.history=lambda **kwargs:[attachment('old-file',110)]
    h.muse.watch=lambda stop:stop.set();h.bridge._watch()
    assert h.state.get('attachment_baseline_seq')==110
    assert outgoing_rows(h.state)==[]
    h.bridge.receive_muse(attachment('new-file',111))
    h.bridge.receive_muse(attachment('new-file',111))
    h.bridge.receive_muse(attachment('late-file',109))
    assert len(outgoing_rows(h.state))==1
    assert outgoing_rows(h.state)[0]['kind']=='file'


def test_file_outbox_restart_reuses_uploaded_key_and_does_not_download_again(harness):
    h=harness();h.state.put('attachment_baseline_seq',100)
    h.bridge.receive_muse(attachment());calls=[]
    h.muse.read_file=lambda *args:(calls.append('read') or b'12345')
    h.feishu.upload_file=lambda *args:(calls.append('upload') or 'file_uploaded')
    def send(chat,key,stable_key):
        calls.append(('send',key,stable_key));h.bridge.stop_event.set()
        if sum(isinstance(c,tuple) for c in calls)==1:
            raise FeishuError('transport',retryable=True,delivery_uncertain=True,retry_after_seconds=5)
        return 'om_file_sent'
    h.feishu.send_file=send;h.bridge._outgoing()
    h.state.recover();h.clock.now+=5;h.bridge.stop_event.clear();h.bridge._outgoing()
    assert calls[:2]==['read','upload']
    assert calls[2]==calls[3]
    assert h.state.counts()['outbox']=={'sent':1}


def test_oversized_file_is_not_read_or_submitted_and_owner_gets_limit_notice(harness,monkeypatch):
    from muse_feishu_bridge.files import MAX_FILE_BYTES
    h=harness();message=incoming_file();message['file']['size']=MAX_FILE_BYTES+1
    h.bridge.receive(message);h.bridge.baseline_ready.set()
    monkeypatch.setattr(h.bridge.stop_event,'wait',lambda delay:h.bridge.stop_event.set())
    h.bridge._incoming()
    assert h.state.counts()['inbox']=={'failed':1}
    assert '30 MiB' in outgoing_rows(h.state)[0]['text']


def test_large_upload_is_preparing_then_persisted_reference_precedes_submission(harness):
    import hashlib,json
    from muse_feishu_bridge.files import INLINE_FILE_BYTES
    h=harness();data=b'x'*(INLINE_FILE_BYTES+1);message=incoming_file()
    message['file']['size']=len(data);h.bridge.receive(message)
    h.feishu.download_file=lambda *args:data
    def prepare(name,mime,raw,key):
        assert h.state.db.execute('SELECT state FROM inbox').fetchone()[0]=='preparing'
        assert raw==data
        return {'type':'file_ref','path':'workspace/user/files/large.zip','filename':name,
                'mime_type':mime,'bytes_len':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    def send(item,key):
        saved=h.state.db.execute('SELECT state,payload FROM inbox').fetchone()
        assert saved['state']=='sending' and json.loads(saved['payload'])['muse_file_ref']==item
        h.bridge.stop_event.set();return {'status':'accepted'}
    h.muse.prepare_file_reference=prepare;h.muse.send_file_reference=send
    h.bridge.baseline_ready.set();h.bridge._incoming()
    assert h.state.counts()['inbox']=={'sent':1}


def test_resuming_prepared_reference_needs_no_redownload_or_reupload(harness):
    import hashlib
    h=harness();message=incoming_file()
    message['file']['muse_file_ref']={'type':'file_ref','path':'workspace/user/files/x.zip','filename':'x.zip',
        'mime_type':'application/zip','bytes_len':5,'sha256':hashlib.sha256(b'12345').hexdigest()}
    h.bridge.receive(message)
    def forbidden(*args):raise AssertionError('must reuse persisted reference')
    h.feishu.download_file=forbidden;h.muse.prepare_file_reference=forbidden
    def send(*args):h.bridge.stop_event.set();return {'status':'accepted'}
    h.muse.send_file_reference=send;h.bridge.baseline_ready.set();h.bridge._incoming()
    assert h.state.counts()['inbox']=={'sent':1}
