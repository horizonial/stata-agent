"""Focused regression tests for Context/Compaction V2."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stata_agent.events.schema import (
    ACTOR_AGENT,
    ACTOR_ORCH,
    EVENT_CONTEXT_ASSEMBLED,
    EVENT_IDEA,
    EVENT_TOOL_INVOKED,
    EVENT_USER,
    Event,
)
from stata_agent.harness.compaction import (
    COMPACTION_VERSION,
    CompactionValidationError,
    compact,
    normalize_boundary_payload,
)
from stata_agent.harness.context_assembler import (
    ContextAssembler,
    ContextBudget,
    ContextBudgetExceeded,
)
from stata_agent.storage.sqlite_store import SQLiteStore


def _event(idea: str, event_type: str, payload: dict) -> Event:
    return Event(idea_id=idea, event_type=event_type, actor=ACTOR_AGENT, source=ACTOR_AGENT, payload=payload)


def test_context_budget_deduplicates_recorded_current_and_records_safe_manifest(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="context-test")
    store.append(_event("i1", EVENT_IDEA, {"question": "DID"}))
    store.append(_event("i1", EVENT_USER, {"text": "继续分析"}))
    ctx = SimpleNamespace(idea="i1", memory=None)
    result = ContextAssembler().assemble(
        store=store,
        ctx=ctx,
        user_text="继续分析",
        system="安全系统指令",
        budget=ContextBudget(max_input_tokens=500, reserve_output_tokens=100),
    )

    user_messages = [message for message in result.messages if message.get("role") == "user"]
    assert [message.get("content") for message in user_messages].count("继续分析") == 1
    assert result.estimated_tokens <= 400
    assert {item.layer for item in result.manifest} >= {"system", "research_state", "current_user"}
    telemetry = [event for event in store.scan("i1") if event.event_type == EVENT_CONTEXT_ASSEMBLED]
    assert len(telemetry) == 1
    payload = telemetry[0].payload
    assert "source_ids" in payload and "content" not in payload and "text" not in payload
    store.close()


def test_context_tail_never_exposes_dangling_tool_invocation(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="context-test")
    store.append(_event("i1", EVENT_IDEA, {"question": "q"}))
    store.append(_event("i1", EVENT_USER, {"text": "run it"}))
    store.append(Event(
        idea_id="i1",
        event_type=EVENT_TOOL_INVOKED,
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        payload={"tool": "run_stata", "args": {"code": "summarize x"}, "call_id": "c1"},
    ))
    store.append(Event(
        idea_id="i1",
        event_type="tool.done",
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        payload={"tool": "run_stata", "call_id": "c1", "result": {"ok": True}},
    ))
    result = ContextAssembler().assemble(
        store=store,
        ctx=SimpleNamespace(idea="i1", memory=None),
        user_text="下一步",
        system="system",
        budget=ContextBudget(max_input_tokens=600, reserve_output_tokens=100, recent_tail_tokens=300),
    )
    assistants = [message for message in result.messages if message.get("tool_calls")]
    tools = [message for message in result.messages if message.get("role") == "tool"]
    assert len(assistants) == len(tools) == 1
    assert assistants[0]["tool_calls"][0]["id"] == tools[0]["tool_call_id"]
    store.close()


def test_compaction_v2_is_monotonic_and_pending_tool_stays_uncompacted(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="compact-test")
    store.append(_event("i1", EVENT_IDEA, {"question": "q"}))
    store.append(_event("i1", EVENT_USER, {"text": "first"}))
    first = compact(store, "i1")
    first_boundary = [event for event in store.scan("i1") if event.event_type == "compaction.boundary"][-1]
    payload = first_boundary.payload
    assert payload["version"] == COMPACTION_VERSION
    assert set(payload["summary"]) == {
        "objective", "constraints", "decisions", "open_items", "research_state", "evidence_refs"
    }

    store.append(_event("i1", EVENT_USER, {"text": "second"}))
    second = compact(store, "i1")
    second_boundary = [event for event in store.scan("i1") if event.event_type == "compaction.boundary"][-1]
    assert second["from_seq"] == first["to_seq"] + 1
    assert second_boundary.payload["previous_boundary_seq"] == first["seq"]
    assert second["to_seq"] > first["to_seq"]

    # An invocation without a result is never included in a compaction range.
    store.append(Event(
        idea_id="i1", event_type=EVENT_TOOL_INVOKED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
        payload={"tool": "run_stata", "call_id": "dangling"},
    ))
    with pytest.raises(ValueError, match="安全压缩"):
        compact(store, "i1")
    # The unresolved invocation remains in the immutable ledger and no unsafe
    # boundary is appended.
    assert len([event for event in store.scan("i1") if event.event_type == "compaction.boundary"]) == 2
    store.close()


def test_v1_summary_remains_readable_and_invalid_v2_refs_fail():
    old = normalize_boundary_payload({"from_seq": 1, "to_seq": 2, "summary": "legacy"})
    assert old["version"] == 1 and old["summary"] == "legacy"
    with pytest.raises(CompactionValidationError):
        normalize_boundary_payload({
            "version": 2,
            "from_seq": 1,
            "to_seq": 2,
            "summary": {"objective": "", "constraints": [], "decisions": [], "open_items": [], "research_state": "", "evidence_refs": []},
            "retained_from_seq": 0,
        })


def test_system_and_current_user_are_non_droppable(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="budget-test")
    with pytest.raises(ContextBudgetExceeded):
        ContextAssembler().assemble(
            store=store,
            ctx=SimpleNamespace(idea="i1", memory=None),
            user_text="current user message",
            system="safety instruction",
            budget=ContextBudget(max_input_tokens=8, reserve_output_tokens=0, chars_per_token=1),
        )
    store.close()
