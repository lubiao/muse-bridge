"""Offline checks of durable submission and notification boundaries."""

import json

import pytest

from muse_qq_bridge.state import State


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    state = State(tmp_path / "state", clock=clock)
    yield state, clock
    state.close()


def bind_owner(state):
    token = state.new_pairing_token()
    assert state.bind(token, "ou_owner", "oc_private")


def assistant(message_id, seq):
    return {"message_id": message_id, "seq": seq, "role": "assistant", "completed": True}


def test_pairing_requires_valid_unexpired_token_and_freezes_both_owner_and_chat(store):
    state, clock = store
    token = state.new_pairing_token(lifetime=10)
    assert token not in json.dumps(state.get("pairing"))
    assert not state.bind("incorrect", "ou_intruder", "oc_intruder")
    assert not state.bind(token, "", "oc_private")
    clock.now += 11
    assert not state.bind(token, "ou_owner", "oc_private")
    token = state.new_pairing_token()
    assert state.bind(token, "ou_owner", "oc_private")
    assert state.get("pairing") == {}
    assert state.is_owner("ou_owner", "oc_private")
    assert not state.is_owner("ou_owner", "oc_other")
    assert not state.is_owner("ou_intruder", "oc_private")
    assert not state.bind(token, "ou_intruder", "oc_private")
    assert state.owner() == {"open_id": "ou_owner", "chat_id": "oc_private"}


def test_duplicate_incoming_event_cannot_replace_original_text_or_request_id(store):
    state, _ = store
    assert state.enqueue_incoming("om_same", "oc_private", "original task")
    assert not state.enqueue_incoming("om_same", "oc_intruder", "replacement task")
    row = state.claim_incoming()
    assert row["text"] == "original task"
    assert row["chat_id"] == "oc_private"
    assert row["request_id"]
    state.incoming_result(row["id"], "sent")
    assert not state.enqueue_incoming("om_same", "oc_private", "original task")
    assert state.claim_incoming() is None


def test_same_timestamp_incoming_tasks_keep_arrival_order_not_message_id_order(store):
    state, _ = store
    state.enqueue_incoming("z-arrived-first", "oc_private", "first")
    state.enqueue_incoming("a-arrived-second", "oc_private", "second")
    first = state.claim_incoming()
    assert first["text"] == "first"
    state.incoming_result(first["id"], "sent")
    assert state.claim_incoming()["text"] == "second"


def test_restart_never_replays_muse_task_that_might_already_have_been_submitted(tmp_path):
    clock = Clock()
    path = tmp_path / "restart"
    before = State(path, clock=clock)
    before.enqueue_incoming("om_uncertain", "oc_private", "perform a real task")
    original = before.claim_incoming()
    before.enqueue_incoming("om_waiting", "oc_private", "next task")
    before.close()  # Simulate termination after Muse received a task but before local acknowledgement.
    after = State(path, clock=clock)
    try:
        assert after.recover() == 1
        uncertain = after.db.execute("SELECT * FROM inbox WHERE id='om_uncertain'").fetchone()
        assert uncertain["state"] == "unknown"
        assert uncertain["error"] == "restart_during_send"
        assert uncertain["request_id"] == original["request_id"]
        assert not after.enqueue_incoming("om_uncertain", "oc_private", "perform a real task")
        next_task = after.claim_incoming()
        assert next_task["id"] == "om_waiting"
        after.incoming_result(next_task["id"], "sent")
        assert after.claim_incoming() is None
        assert after.recover() == 0
    finally:
        after.close()


def test_restart_reuses_outgoing_key_and_original_uuid_window(tmp_path):
    clock = Clock()
    path = tmp_path / "outbox-restart"
    before = State(path, clock=clock)
    before.queue_text("muse:message:0", "oc_private", "one notification")
    original = before.claim_outgoing()
    before.close()
    clock.now += 20
    after = State(path, clock=clock)
    try:
        assert after.recover() == 0  # Only interrupted Muse submissions count here.
        replay = after.claim_outgoing()
        assert (replay["id"], replay["chat_id"], replay["text"]) == (
            original["id"], original["chat_id"], original["text"])
        assert replay["first_attempt"] == 1000.0
        assert replay["attempts"] == 1
        after.outgoing_sent(replay["id"], "om_remote")
        assert after.claim_outgoing() is None
    finally:
        after.close()


@pytest.mark.parametrize("elapsed, allowed", [(3499.9, True), (3500, False), (3600, False)])
def test_retry_never_crosses_the_safety_margin_of_feishu_uuid_window(store, elapsed, allowed):
    state, clock = store
    state.queue_text("stable-key", "oc_private", "notification")
    first = state.claim_outgoing()
    state.outgoing_failed(first["id"], True, "delivery_uncertain", delay=0)
    clock.now += elapsed
    retry = state.claim_outgoing()
    assert (retry is not None) is allowed
    if allowed:
        assert retry["id"] == first["id"]
        assert retry["first_attempt"] == 1000.0
    else:
        assert state.counts()["outbox"] == {"unknown": 1}
        assert state.claim_outgoing() is None


def test_retried_first_chunk_blocks_later_chunks_until_its_next_attempt(store):
    state, clock = store
    state.queue_text("muse:reply:0", "oc_private", "first")
    state.queue_text("muse:reply:1", "oc_private", "second")
    first = state.claim_outgoing()
    state.outgoing_failed(first["id"], True, "rate_limit", delay=30)
    assert state.claim_outgoing() is None
    clock.now += 30
    assert state.claim_outgoing()["id"] == first["id"]
    state.outgoing_sent(first["id"], "om_first")
    assert state.claim_outgoing()["text"] == "second"


def test_same_timestamp_twelve_chunks_keep_numeric_emission_order(store):
    state, _ = store
    bind_owner(state)
    state.put("baseline_seq", 100)
    chunks = [f"piece {index}" for index in range(12)]
    assert state.record_muse(assistant("long-reply", 101), chunks)
    delivered = []
    while row := state.claim_outgoing():
        delivered.append(row["text"])
        state.outgoing_sent(row["id"], "remote:" + row["id"])
    assert delivered == chunks


def test_late_history_backfill_is_filtered_against_initial_baseline_not_latest_seen(store):
    state, _ = store
    bind_owner(state)
    state.put("baseline_seq", 100)
    assert state.record_muse(assistant("newer", 130), ["newer message"])
    assert state.record_muse(assistant("late-older", 120), ["arrived late through history"])
    assert not state.record_muse(assistant("old-history", 100), ["before onboarding"])
    assert not state.record_muse(assistant("newer", 130), ["duplicated live/history event"])
    assert state.get("baseline_seq") == 100
    assert state.get("last_muse_seq") == 130
    delivered = []
    while row := state.claim_outgoing():
        delivered.append(row["text"])
        state.outgoing_sent(row["id"], "remote:" + row["id"])
    assert delivered == ["newer message", "arrived late through history"]


def test_muse_deduplication_and_queued_chunks_survive_restart(tmp_path):
    path = tmp_path / "muse-restart"
    first = State(path)
    bind_owner(first)
    first.put("baseline_seq", 5)
    assert first.record_muse(assistant("completed-reply", 8), ["A", "B"])
    first.close()
    second = State(path)
    try:
        assert not second.record_muse(assistant("completed-reply", 8), ["A", "B"])
        assert second.counts()["outbox"] == {"pending": 2}
        assert second.record_muse(assistant("late-reply", 7), ["late"])
        assert second.get("last_muse_seq") == 8
    finally:
        second.close()


def test_notifications_require_both_bound_owner_and_initialized_baseline(store):
    state, _ = store
    assert not state.record_muse(assistant("unbound", 1), ["private text"])
    bind_owner(state)
    assert not state.record_muse(assistant("no-baseline", 2), ["private text"])
    assert state.counts()["outbox"] == {}


def test_duplicate_outbox_key_never_changes_payload_after_an_uncertain_delivery(store):
    state, _ = store
    state.queue_text("stable", "oc_private", "original")
    state.queue_text("stable", "oc_other", "replacement")
    row = state.claim_outgoing()
    assert (row["chat_id"], row["text"]) == ("oc_private", "original")


def test_completed_event_and_every_chunk_commit_together_or_can_be_recovered(store, monkeypatch):
    state, _ = store
    bind_owner(state)
    state.put("baseline_seq", 10)
    enqueue = state._queue_text
    attempts = []

    def interrupted(key, chat, text):
        attempts.append(key)
        if len(attempts) == 2:
            raise OSError("simulated disk failure before second chunk")
        enqueue(key, chat, text)

    monkeypatch.setattr(state, "_queue_text", interrupted)
    with pytest.raises(OSError, match="simulated disk failure"):
        state.record_muse(assistant("atomic-reply", 11), ["first", "second"])
    assert state.counts()["outbox"] == {}
    assert state.db.execute("SELECT count(*) FROM muse_events WHERE id='atomic-reply'").fetchone()[0] == 0
    assert state.get("last_muse_seq") is None
    monkeypatch.setattr(state, "_queue_text", enqueue)
    assert state.record_muse(assistant("atomic-reply", 11), ["first", "second"])
    assert state.counts()["outbox"] == {"pending": 2}


def test_expired_uncertain_outbox_item_does_not_permanently_block_later_notifications(store):
    state, clock = store
    state.queue_text("uncertain", "oc_private", "already might be delivered")
    row = state.claim_outgoing()
    state.outgoing_failed(row["id"], True, "transport", delay=0)
    state.queue_text("later", "oc_private", "a new notification")
    clock.now += 3500
    assert state.claim_outgoing() is None
    assert state.counts()["outbox"] == {"pending": 1, "unknown": 1}
    assert state.claim_outgoing()["id"] == "later"
