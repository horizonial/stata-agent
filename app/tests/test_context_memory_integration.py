"""Context/Memory V2 integration guards owned by the UI/loop surface."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import stata_agent.ui as ui
from stata_agent.events.schema import ACTOR_AGENT, EVENT_IDEA, Event
from stata_agent.harness.agent_loop import run_loop
from stata_agent.runner import approve
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import Tool, ToolContext, ok


class _Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _store(tmp_path):
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"), writer_id="integration")
    store.append(Event(idea_id="ui", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "x"}))
    return store


@dataclass(frozen=True)
class _Budget:
    max_input_tokens: int
    reserve_output_tokens: int = 0


def test_loop_uses_context_assembler_and_records_manifest(tmp_path, monkeypatch):
    module = types.ModuleType("stata_agent.harness.context_assembler")

    @dataclass(frozen=True)
    class Item:
        layer: str = "recent"
        source_ids: tuple[str, ...] = ("seq:2",)
        estimated_tokens: int = 3
        truncated: bool = False

    @dataclass
    class Assembled:
        messages: list[dict]
        manifest: list[Item]
        estimated_tokens: int = 3
        compacted_through_seq: int | None = None

    class Assembler:
        calls = []

        def assemble(self, **kwargs):
            self.calls.append(kwargs)
            return Assembled([
                {"role": "system", "content": kwargs["system"]},
                {"role": "user", "content": kwargs["user_text"]},
            ], [Item()])

    module.ContextAssembler = Assembler
    module.ContextBudgetExceeded = RuntimeError
    monkeypatch.setitem(sys.modules, "stata_agent.harness.context_assembler", module)
    store = _store(tmp_path)
    provider = _Provider([{"content": "ok", "tool_calls": None}])
    ctx = ToolContext(idea="ui", store=store, workspace_id="sha256:test")

    result = run_loop(store, provider, {}, ctx, user_text="hello")

    assert result.reply == "ok"
    assert len(provider.calls) == 1
    assembled = [event for event in store.scan("ui") if event.event_type == "context.assembled"]
    assert assembled and assembled[-1].payload["items"][0]["source_ids"] == ["seq:2"]
    assert assembled[-1].payload["estimated_tokens"] == 3
    store.close()


def test_context_budget_failure_happens_before_provider_io(tmp_path):
    store = _store(tmp_path)
    provider = _Provider([{"content": "should not run", "tool_calls": None}])
    ctx = ToolContext(idea="ui", store=store, context_budget=_Budget(max_input_tokens=8, reserve_output_tokens=4))

    result = run_loop(store, provider, {}, ctx, user_text="这是一条超过预算的当前用户消息" * 4)

    assert provider.calls == []
    assert result.terminal_reason == "context_budget"
    assert any(event.event_type == "budget.limit" for event in store.scan("ui"))
    store.close()


def test_provider_overflow_compacts_and_retries_once(tmp_path):
    store = _store(tmp_path)
    provider = _Provider([
        RuntimeError("context_length_exceeded"),
        {"content": "retried", "tool_calls": None},
    ])

    result = run_loop(store, provider, {}, ToolContext(idea="ui", store=store), user_text="hello")

    assert result.reply == "retried"
    assert len(provider.calls) == 2
    assert len([event for event in store.scan("ui") if event.event_type == "compaction.boundary"]) == 1
    store.close()


def test_later_overflow_retry_keeps_complete_tool_pair(tmp_path):
    store = _store(tmp_path)
    provider = _Provider([
        {"content": None, "tool_calls": [{"id": "call-1", "name": "ping", "arguments": {}}]},
        RuntimeError("maximum context length reached"),
        {"content": "kept", "tool_calls": None},
    ])
    ping = Tool(
        name="ping",
        description="ping",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda _args, _ctx: ok({"pong": True}),
        permission="safe",
    )

    result = run_loop(store, provider, {"ping": ping}, ToolContext(idea="ui", store=store), user_text="hello")

    assert result.reply == "kept"
    assert len(provider.calls) == 3
    retry_messages = provider.calls[-1]
    assert any(message.get("role") == "assistant" and message.get("tool_calls") for message in retry_messages)
    assert any(message.get("role") == "tool" and "pong" in str(message.get("content")) for message in retry_messages)
    store.close()


def test_workspace_identity_is_stable_and_project_specific(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    first = ui._workspace_id("alpha")
    second = ui._workspace_id("alpha")
    other = ui._workspace_id("beta")

    assert first == second
    assert first.startswith("sha256:")
    assert first != other


def test_context_budget_type_is_optional_for_legacy_callers():
    assert ToolContext(idea="legacy").workspace_id is None
    assert ToolContext(idea="legacy").context_budget is None


def test_approval_note_passes_workspace_and_provenance_to_v2_store(tmp_path):
    store = _store(tmp_path)
    request_seq = store.append(Event(
        idea_id="ui",
        event_type="approval.requested",
        actor="agent",
        source="agent",
        payload={"request_id": "apr-1", "act": "propose_spec"},
    ))

    class Memory:
        def __init__(self):
            self.calls = []

        def add(self, text, *, kind, workspace_id, confidence, source_ids, scope="project"):
            self.calls.append({
                "text": text,
                "kind": kind,
                "workspace_id": workspace_id,
                "confidence": confidence,
                "source_ids": source_ids,
                "scope": scope,
            })

    memory = Memory()
    approve(store, "apr-1", decision="approve", note="保留固定效应", idea="ui",
            memory=memory, workspace_id="sha256:project")

    assert memory.calls == [{
        "text": "研究决定：保留固定效应",
        "kind": "decision",
        "workspace_id": "sha256:project",
        "confidence": "explicit",
        "source_ids": ("apr-1", str(request_seq + 1)),
        "scope": "project",
    }]
    store.close()
