"""Regression coverage for ledger FSM, uncertain recovery, and writer gates."""

from __future__ import annotations

import pytest
from io import BytesIO
from docx import Document

from stata_agent.domain.models import Claim, RunRecord
from stata_agent.domain.reducers import IllegalEventSequence, empty
from stata_agent.events.append import WriterNotPermitted
from stata_agent.events.schema import (
    ACTOR_EVIDENCE,
    ACTOR_ORCH,
    ACTOR_VALIDATOR,
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_RETRACT,
    EVENT_CLAIM_SIGNED,
    EVENT_IDEA,
    EVENT_RESTORED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    Event,
)
from stata_agent.harness.recovery import reconcile_uncertain
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.storage.store import StaleWrite
from stata_agent.tools.evidence_signer import sign_run_numeric_cards
from stata_agent.tools.fake_executor import FakeExecutor
from stata_agent.tools.executor import StataExecutor, TransportUncertainError
from stata_agent.writer.draft_multi import draft_from_ledger


def _run_chain(*, terminal=EVENT_RUN_SUCCEEDED, run_id="r1", op="op1", attempt=1, provenance=None):
    return [
        Event(
            idea_id="i1", event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            operation_id=op, attempt_id=attempt, fingerprint=f"fp-{op}",
            payload={"run_id": run_id, "side_effect": "write", "semantic_input_hash": f"fp-{op}"},
        ),
        Event(
            idea_id="i1", event_type=EVENT_TOOL_CALL, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            operation_id=op, payload={"run_id": run_id, "call_id": f"call-{op}"},
        ),
        Event(
            idea_id="i1", event_type=EVENT_TOOL_RESULT, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            operation_id=op, payload={"run_id": run_id, "call_id": f"call-{op}", "rc": 0},
        ),
        Event(
            idea_id="i1", event_type=terminal, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            operation_id=op, attempt_id=attempt,
            payload={"run_id": run_id, "machine": {"coef": -2.0, "N": 10},
                     "provenance": provenance or {}},
        ),
    ]


def test_timeout_commits_requested_call_result_and_uncertain(monkeypatch, tmp_path):
    class TimeoutSession:
        calls = 0

        def __init__(self):
            self.closed = False

        def call(self, _code):
            type(self).calls += 1
            raise TimeoutError("transport timeout")

        def close(self):
            self.closed = True

    monkeypatch.setattr("stata_agent.tools.executor.StataSession", TimeoutSession)
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    executor = StataExecutor(store, run_root=tmp_path / "runs")

    with pytest.raises(TransportUncertainError):
        executor.execute("display 1", idea="i1", side_effect="write")

    events = list(store.scan("i1"))
    assert [event.event_type for event in events] == [
        EVENT_RUN_REQ, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, EVENT_RUN_UNCERTAIN,
    ]
    assert all(event.operation_id == "op-" + events[0].payload["run_id"] for event in events)
    assert events[2].payload["transport_error"] is True
    assert store.project("i1").runs[events[0].payload["run_id"]].status == "uncertain"
    assert TimeoutSession.calls == 1  # transport uncertainty is never auto-retried
    store.close()


def test_invalid_claim_is_rejected_before_commit(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    event = Event(
        idea_id="i1", event_type=EVENT_CLAIM_SIGNED, actor=ACTOR_EVIDENCE, source=ACTOR_EVIDENCE,
        payload={"claim": {"claim_id": "c1", "statement": "x", "cards": ["missing"],
                           "written_by": ACTOR_EVIDENCE}},
    )
    with pytest.raises(IllegalEventSequence):
        store.append(event)
    assert list(store.scan("i1")) == []
    store.close()


def test_append_many_is_all_or_nothing(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    events = [
        Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_ORCH, source=ACTOR_ORCH),
        Event(
            idea_id="i1", event_type=EVENT_CARD_SIGNED, actor=ACTOR_VALIDATOR, source=ACTOR_VALIDATOR,
            payload={"card": {"card_id": "card-1", "signed_by": ACTOR_VALIDATOR}},
        ),
        Event(
            idea_id="i1", event_type=EVENT_CLAIM_SIGNED, actor=ACTOR_EVIDENCE, source=ACTOR_EVIDENCE,
            payload={"claim": {"claim_id": "c1", "statement": "x", "cards": ["ghost"],
                               "written_by": ACTOR_EVIDENCE}},
        ),
    ]
    with pytest.raises(IllegalEventSequence):
        store.append_many(events)
    assert list(store.scan("i1")) == []
    store.close()


def test_source_and_payload_role_spoofing_is_rejected(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    with pytest.raises(WriterNotPermitted):
        store.append(Event(
            idea_id="i1", event_type=EVENT_CARD_SIGNED,
            actor=ACTOR_VALIDATOR, source=ACTOR_VALIDATOR,
            payload={"card": {"card_id": "c", "signed_by": "agent"}},
        ))
    with pytest.raises(WriterNotPermitted):
        store.append(Event(
            idea_id="i1", event_type=EVENT_CLAIM_SIGNED,
            actor=ACTOR_EVIDENCE, source=ACTOR_EVIDENCE,
            payload={"claim": {"claim_id": "c", "statement": "x", "cards": [],
                               "written_by": ACTOR_VALIDATOR}},
        ))
    store.close()


def test_forged_succeeded_run_cannot_be_signed(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    forged = {"kind": "real", "executor": "other", "attested": True,
              "do_file": "x.do", "command_hash": "h", "env_sig": {"version": "18"}}
    store.append_many(_run_chain(provenance=forged))
    with pytest.raises(ValueError, match="provenance"):
        sign_run_numeric_cards(store, "r1")
    assert store.project("i1").cards == {}
    store.close()


def test_uncertain_reconcile_is_findable_and_idempotent(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    store.append_many(_run_chain(terminal=EVENT_RUN_UNCERTAIN))
    first = reconcile_uncertain(store, "i1")
    second = reconcile_uncertain(store, "i1")
    assert first == second == ["op1 -> rerun"]
    restored = list(store.scan("i1", event_types={EVENT_RESTORED}))
    assert len(restored) == 1
    assert store.project("i1").pending["op1"] == "uncertain"
    store.close()


def test_terminal_run_id_or_attempt_mismatch_is_rejected_without_pollution(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    chain = _run_chain(attempt=2)
    store.append_many(chain[:3])
    bad_id = chain[3].model_copy(update={"payload": {"run_id": "other"}})
    with pytest.raises(IllegalEventSequence):
        store.append(bad_id)
    bad_attempt = chain[3].model_copy(update={"attempt_id": 3})
    with pytest.raises(IllegalEventSequence):
        store.append(bad_attempt)
    assert len(list(store.scan("i1"))) == 3
    store.append(chain[3])
    assert store.project("i1").runs["r1"].status == "succeeded"
    store.close()


def test_retracted_and_missing_evidence_never_produce_draft(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    fake = FakeExecutor(store)
    fake.execute("display 1", idea="i1", run_id="r1")
    sign_run_numeric_cards(store, "r1", claim_statement="不可再见的结论")
    store.append(Event(
        idea_id="i1", event_type=EVENT_CLAIM_RETRACT, actor=ACTOR_EVIDENCE, source=ACTOR_EVIDENCE,
        payload={"claim_id": "claim-r1", "written_by": ACTOR_EVIDENCE},
    ))
    data = draft_from_ledger(store.project("i1"))
    doc = Document(BytesIO(data))
    assert not any("不可再见的结论" in paragraph.text for paragraph in doc.paragraphs)
    store.close()

    missing = empty("i1")
    missing.claims["c-missing"] = Claim(
        claim_id="c-missing", statement="缺卡结论", cards=["ghost"], written_by=ACTOR_EVIDENCE,
    )
    with pytest.raises(ValueError, match="缺 EvidenceCard"):
        draft_from_ledger(missing)

    raw = empty("i1")
    raw.runs["r-raw"] = RunRecord(
        run_id="r-raw", operation_id="op-raw", status="succeeded",
        machine={"coef": 1.0, "N": 10}, provenance={},
    )
    with pytest.raises(ValueError, match="EvidenceCard"):
        draft_from_ledger(raw)


def test_expired_lease_is_fenced(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    store._conn.execute("UPDATE writer_lease SET expires_at=0")
    store._conn.commit()
    with pytest.raises(StaleWrite):
        store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_ORCH, source=ACTOR_ORCH))
    store.close()
