"""Task 1/2 quality gates for complete request budgets and proactive compaction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stata_agent.events.schema import ACTOR_AGENT, EVENT_IDEA, EVENT_USER, Event
from stata_agent.harness.agent_loop import run_loop
from stata_agent.harness.context_assembler import (
    CONTEXT_BUDGET_SNAPSHOT_VERSION,
    ContextAssembler,
    ContextBudget,
    ContextBudgetExceeded,
    build_budget_snapshot,
)
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import ToolContext


class _Provider:
    provider = "local"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.calls.append(messages)
        return {"content": "ok", "tool_calls": None}


def _event(idea: str, event_type: str, payload: dict) -> Event:
    return Event(idea_id=idea, event_type=event_type, actor=ACTOR_AGENT, source=ACTOR_AGENT, payload=payload)


def test_budget_snapshot_accounts_for_complete_request_components() -> None:
    messages = [
        {"role": "system", "content": "safe"},
        {"role": "user", "content": "run"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "pong"},
    ]
    schemas = [{"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}]
    budget = ContextBudget(
        max_input_tokens=500,
        reserve_output_tokens=100,
        compaction_buffer_tokens=50,
        soft_limit_tokens=300,
        chars_per_token=2,
    )

    snapshot = build_budget_snapshot(messages, tool_schemas=schemas, budget=budget)

    assert snapshot.version == CONTEXT_BUDGET_SNAPSHOT_VERSION
    assert snapshot.estimated_input_tokens == (
        snapshot.message_tokens
        + snapshot.chat_framing_tokens
        + snapshot.tool_schema_tokens
        + snapshot.tool_call_framing_tokens
    )
    assert snapshot.tool_schema_tokens > 0
    assert snapshot.tool_call_framing_tokens > 0
    assert snapshot.hard_limit_tokens == 400
    assert snapshot.soft_limit_tokens == 300
    assert snapshot.counter_source == "deterministic_json_chars_v1"
    assert snapshot.counter_metadata["tool_schema_count"] == 1


def test_tool_schema_overflow_fails_before_provider_io() -> None:
    schema = {"type": "function", "function": {"name": "large", "description": "x" * 200}}
    with pytest.raises(ContextBudgetExceeded):
        ContextAssembler().assemble(
            store=SimpleNamespace(scan=lambda _idea: [], project=lambda _idea: None),
            ctx=SimpleNamespace(idea="i1", memory=None),
            user_text="hi",
            system="safe",
            budget=ContextBudget(max_input_tokens=80, reserve_output_tokens=0, chars_per_token=1),
            tool_schemas=[schema],
        )


def test_soft_pressure_compacts_once_before_provider_io(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="quality-test")
    store.append(_event("i1", EVENT_IDEA, {"question": "q"}))
    for index in range(24):
        store.append(_event("i1", EVENT_USER, {"text": f"历史约束 {index} 固定效应"}))

    provider = _Provider()
    budget = ContextBudget(
        max_input_tokens=900,
        reserve_output_tokens=0,
        hard_limit_tokens=700,
        soft_limit_tokens=120,
        recent_tail_tokens=300,
        memory_tokens=0,
    )
    result = run_loop(
        store,
        provider,
        {},
        ToolContext(idea="i1", store=store, context_budget=budget),
        user_text="继续",
        max_steps=1,
    )

    assert result.reply == "ok"
    assert len(provider.calls) == 1
    assert len([event for event in store.scan("i1") if event.event_type == "compaction.boundary"]) == 1
    telemetry = [event for event in store.scan("i1") if event.event_type == "context.assembled"]
    assert telemetry
    assert all("prompt" not in event.payload and "content" not in event.payload for event in telemetry)
    store.close()
