"""Task 3: append preflight and checkpoint recovery health probes."""

from __future__ import annotations

import json

import pytest

from stata_agent.events.schema import ACTOR_AGENT, ACTOR_ORCH, EVENT_COMPACTION, EVENT_IDEA, EVENT_TOOL_INVOKED, EVENT_TOOL_DONE, EVENT_USER, Event
from stata_agent.harness.compaction import (
    COMPACTION_VERSION,
    CompactionValidationError,
    checkpoint_health,
    compact,
    validate_boundary,
)
from stata_agent.storage.sqlite_store import SQLiteStore


def _event(idea: str, event_type: str, payload: dict) -> Event:
    return Event(idea_id=idea, event_type=event_type, actor=ACTOR_AGENT, source=ACTOR_AGENT, payload=payload)


def _seed(store: SQLiteStore) -> None:
    store.append(_event("i1", EVENT_IDEA, {"question": "q"}))
    store.append(_event("i1", EVENT_USER, {"text": "continue"}))


def _summary(*, evidence_refs: list[str] | None = None, research_state: str | None = None) -> dict:
    return {
        "objective": "q",
        "constraints": [],
        "decisions": [],
        "open_items": [],
        "research_state": research_state or (
            "phase=; current_spec=; current_family=; sample_sig=; "
            "claims=0; cards=0; runs=0; evidence_refs=0"
        ),
        "evidence_refs": evidence_refs or [],
    }


def test_preflight_rejects_fidelity_mismatch_before_append(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="health-test")
    _seed(store)
    before = len(list(store.scan("i1")))
    candidate = Event(
        idea_id="i1",
        event_type=EVENT_COMPACTION,
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        seq=3,
        payload={
            "version": COMPACTION_VERSION,
            "from_seq": 1,
            "to_seq": 2,
            "previous_boundary_seq": None,
            "summary": _summary(research_state="phase=CORRUPTED"),
            "retained_from_seq": 3,
            "reason": "test",
        },
    )
    with pytest.raises(CompactionValidationError, match="research_state"):
        validate_boundary(candidate, events=list(store.scan("i1")))
    assert len(list(store.scan("i1"))) == before
    store.close()


def test_preflight_does_not_accept_future_evidence_ref(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="health-test")
    _seed(store)
    store.append(_event("i1", EVENT_USER, {"text": "future"}))
    candidate = Event(
        idea_id="i1",
        event_type=EVENT_COMPACTION,
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        seq=4,
        payload={
            "version": COMPACTION_VERSION,
            "from_seq": 1,
            "to_seq": 2,
            "summary": _summary(evidence_refs=["seq:3"]),
            "retained_from_seq": 2,
        },
    )
    with pytest.raises(CompactionValidationError, match="evidence"):
        validate_boundary(candidate, events=list(store.scan("i1")))
    store.close()


def test_preflight_rejects_tail_start_inside_tool_pair(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="health-test")
    _seed(store)
    store.append(Event(
        idea_id="i1", event_type=EVENT_TOOL_INVOKED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
        payload={"tool": "inspect", "call_id": "c1"},
    ))
    store.append(Event(
        idea_id="i1", event_type=EVENT_TOOL_DONE, actor=ACTOR_ORCH, source=ACTOR_ORCH,
        payload={"tool": "inspect", "call_id": "c1"},
    ))
    candidate = Event(
        idea_id="i1",
        event_type=EVENT_COMPACTION,
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        seq=5,
        payload={
            "version": COMPACTION_VERSION,
            "from_seq": 1,
            "to_seq": 4,
            "summary": _summary(),
            "retained_from_seq": 4,
        },
    )
    with pytest.raises(CompactionValidationError, match="retained"):
        validate_boundary(candidate, events=list(store.scan("i1")))
    store.close()


@pytest.mark.parametrize(
    ("event_type", "payload", "message"),
    [
        (EVENT_TOOL_DONE, {"tool": "inspect", "call_id": "missing"}, "tool_terminal_without_start"),
        ("approval.rejected", {"request_id": "missing"}, "approval_terminal_identity_mismatch"),
        ("run.failed", {"operation_id": "missing", "run_id": "run"}, "semantic unit"),
    ],
)
def test_compaction_does_not_close_dangling_semantic_units(tmp_path, event_type, payload, message):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="health-test")
    _seed(store)
    compact(store, "i1")
    if event_type == "run.failed":
        # SQLite's reducer correctly rejects a terminal run without its
        # matching run.requested event.  Exercise the compaction preflight
        # directly with that malformed replay input instead of weakening the
        # append invariant merely to manufacture corrupt ledger state.
        malformed = _event("i1", event_type, payload).model_copy(update={"seq": 4})
        candidate = Event(
            idea_id="i1",
            event_type=EVENT_COMPACTION,
            actor=ACTOR_ORCH,
            source=ACTOR_ORCH,
            seq=5,
            payload={
                "version": COMPACTION_VERSION,
                "from_seq": 4,
                "to_seq": 4,
                "summary": _summary(),
                "retained_from_seq": 5,
            },
        )
        with pytest.raises(CompactionValidationError, match=message):
            validate_boundary(candidate, events=[*store.scan("i1"), malformed])
        assert len([event for event in store.scan("i1") if event.event_type == EVENT_COMPACTION]) == 1
        store.close()
        return
    store.append(_event("i1", event_type, payload))
    with pytest.raises(ValueError, match="安全压缩"):
        compact(store, "i1")
    assert len([event for event in store.scan("i1") if event.event_type == EVENT_COMPACTION]) == 1
    store.close()


def test_checkpoint_health_is_stable_after_sqlite_reopen(tmp_path):
    path = tmp_path / "ledger.db"
    store = SQLiteStore(str(path), writer_id="health-test")
    _seed(store)
    compact(store, "i1")
    assert checkpoint_health(store, "i1").ok
    store.close()

    reopened = SQLiteStore(str(path), writer_id="health-reopen", takeover=True)
    result = checkpoint_health(reopened, "i1")
    assert result.ok
    assert result.boundary_seq is not None
    assert result.expected_projection is not None
    assert result.recovered_projection is not None
    reopened.close()


def test_corrupt_latest_boundary_fails_closed_without_new_boundary(tmp_path):
    path = tmp_path / "ledger.db"
    store = SQLiteStore(str(path), writer_id="health-test")
    _seed(store)
    compact(store, "i1")
    count = len([event for event in store.scan("i1") if event.event_type == EVENT_COMPACTION])
    row = store.connection.execute(
        "SELECT event_id,payload FROM events WHERE idea_id=? AND event_type=? ORDER BY seq DESC LIMIT 1",
        ("i1", EVENT_COMPACTION),
    ).fetchone()
    payload = json.loads(row["payload"])
    payload["summary"]["research_state"] = "phase=CORRUPTED"
    store.connection.execute(
        "UPDATE events SET payload=? WHERE event_id=?",
        (json.dumps(payload, ensure_ascii=False), row["event_id"]),
    )
    store.connection.commit()
    assert not checkpoint_health(store, "i1").ok
    with pytest.raises(CompactionValidationError, match="历史 compaction"):
        compact(store, "i1")
    assert len([event for event in store.scan("i1") if event.event_type == EVENT_COMPACTION]) == count
    store.close()
