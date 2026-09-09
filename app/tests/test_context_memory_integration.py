"""Context/Memory V2 integration guards owned by the UI/loop surface."""

from __future__ import annotations

import sys
import types
import json
import threading
import time
from dataclasses import dataclass

import pytest

import stata_agent.ui as ui
from stata_agent.events.schema import ACTOR_AGENT, EVENT_IDEA, Event
from stata_agent.harness.agent_loop import run_loop
from stata_agent.harness.memory_scheduler import MemoryExtractionScheduler
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


def test_loop_uses_context_assembler(tmp_path, monkeypatch):
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
    assert Assembler.calls and Assembler.calls[0]["ctx"] is ctx
    store.close()


def test_context_budget_failure_happens_before_provider_io(tmp_path):
    from stata_agent.harness.context_assembler import ContextBudget

    store = _store(tmp_path)
    provider = _Provider([{"content": "should not run", "tool_calls": None}])
    ctx = ToolContext(
        idea="ui",
        store=store,
        context_budget=ContextBudget(max_input_tokens=8, reserve_output_tokens=4),
    )

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


def test_provider_overflow_uses_context_summarizer_once(tmp_path):
    store = _store(tmp_path)

    class Summary:
        provider_name = "local"
        prompt_version = "test-summary-v1"

        def __init__(self):
            self.requests = []

        def summarize(self, request):
            self.requests.append(request)
            source_id = request.sources[0].source_id
            return {
                "objective": {"text": "保留当前研究目标", "source_ids": [source_id]},
                "constraints": [],
                "decisions": [],
                "open_items": [],
            }

    summary = Summary()
    provider = _Provider([
        RuntimeError("context_length_exceeded"),
        {"content": "retried", "tool_calls": None},
    ])
    result = run_loop(
        store,
        provider,
        {},
        ToolContext(idea="ui", store=store, compaction_summarizer=summary),
        user_text="hello",
    )

    assert result.reply == "retried"
    assert len(summary.requests) == 1
    boundary = [event for event in store.scan("ui") if event.event_type == "compaction.boundary"][-1]
    assert boundary.payload["summary_mode"] == "model_validated"
    assert boundary.payload["summary"]["objective"] == "保留当前研究目标"
    store.close()


def test_default_features_make_no_extra_provider_call(tmp_path, monkeypatch):
    monkeypatch.delenv("STATA_AGENT_COMPACTION_SUMMARY", raising=False)
    monkeypatch.delenv("STATA_AGENT_MEMORY_EXTRACTION", raising=False)
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    monkeypatch.setattr(ui, "_executor", lambda _store: None)
    monkeypatch.setattr(ui, "_rag", lambda: None)
    monkeypatch.setattr(ui, "_memory", lambda: None)
    scheduler = MemoryExtractionScheduler(max_pending=1)
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", scheduler)

    class Provider:
        provider = "local"

        def __init__(self):
            self.calls = 0

        def chat(self, _messages, *, tools=None):
            self.calls += 1
            return {"content": "ok", "tool_calls": None}

    provider = Provider()
    monkeypatch.setattr(ui, "_provider", lambda: provider)
    reply, ask, _ = ui._run_chat_sync("ui", "hello", "interactive")

    assert (reply, ask) == ("ok", None)
    assert provider.calls == 1
    assert scheduler.pending == 0
    scheduler.shutdown(wait=True)


def test_feature_switches_and_privacy_adapter_fail_closed(monkeypatch):
    from stata_agent.config import compaction_summary_mode, memory_extraction_mode

    env = {}
    assert compaction_summary_mode(environ=env) == "deterministic"
    assert memory_extraction_mode(environ=env) == "off"
    env.update({
        "STATA_AGENT_COMPACTION_SUMMARY": "provider",
        "STATA_AGENT_MEMORY_EXTRACTION": "provider",
    })
    assert compaction_summary_mode(environ=env) == "provider"
    assert memory_extraction_mode(environ=env) == "provider"
    env["STATA_AGENT_MEMORY_EXTRACTION"] = "unsafe"
    assert memory_extraction_mode(environ=env) == "off"

    class Remote:
        provider = "deepseek"

        def __init__(self):
            self.calls = 0

        def chat(self, _messages, **_kwargs):
            self.calls += 1
            return {"content": "unexpected"}

    remote = Remote()
    adapter = ui._PrivacyChatAdapter(remote, privacy_mode="local_strict")
    with pytest.raises(Exception):
        adapter.chat([{"role": "user", "content": "private C:/data.csv"}])
    assert remote.calls == 0


def test_memory_extraction_response_does_not_wait_for_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("STATA_AGENT_MEMORY_EXTRACTION", "provider")
    monkeypatch.delenv("STATA_AGENT_COMPACTION_SUMMARY", raising=False)
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    monkeypatch.setattr(ui, "_executor", lambda _store: None)
    monkeypatch.setattr(ui, "_rag", lambda: None)
    monkeypatch.setattr(ui, "_memory", lambda: None)
    scheduler = MemoryExtractionScheduler(max_pending=1)
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", scheduler)
    started = threading.Event()
    release = threading.Event()

    def slow_job(**_kwargs):
        started.set()
        release.wait(2)

    monkeypatch.setattr(ui, "_run_memory_extraction_job", slow_job)

    class Provider:
        provider = "local"

        def chat(self, _messages, *, tools=None):
            return {"content": "ok", "tool_calls": None}

    monkeypatch.setattr(ui, "_provider", Provider)
    begin = time.monotonic()
    reply, ask, _ = ui._run_chat_sync("ui", "以后默认使用中文", "interactive")
    elapsed = time.monotonic() - begin

    assert (reply, ask) == ("ok", None)
    assert elapsed < 1.0
    assert started.wait(1.0)
    release.set()
    scheduler.shutdown(wait=True, timeout=1.0)


def test_enabled_extraction_opens_fresh_store_and_memory(tmp_path, monkeypatch):
    from stata_agent.memory.memstore import MemoryStore

    monkeypatch.setenv("STATA_AGENT_MEMORY_EXTRACTION", "provider")
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    monkeypatch.setattr(ui, "_executor", lambda _store: None)
    monkeypatch.setattr(ui, "_rag", lambda: None)
    scheduler = MemoryExtractionScheduler(max_pending=1)
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", scheduler)
    instances = []

    def new_memory():
        memory = MemoryStore(tmp_path / "memory.json")
        instances.append(memory)
        return memory

    monkeypatch.setattr(ui, "_memory", new_memory)

    class Provider:
        provider = "local"

        def __init__(self):
            self.calls = []

        def chat(self, messages, *, json_mode=False, tools=None):
            self.calls.append((json_mode, messages))
            if json_mode:
                return {"content": json.dumps({"candidates": []})}
            return {"content": "ok", "tool_calls": None}

    provider = Provider()
    monkeypatch.setattr(ui, "_provider", lambda: provider)
    reply, ask, _ = ui._run_chat_sync("ui", "以后默认使用中文回答", "interactive")
    assert (reply, ask) == ("ok", None)

    scheduler.shutdown(wait=True, timeout=1.0)
    assert len(instances) == 2
    assert instances[0] is not instances[1]
    assert any(json_mode for json_mode, _messages in provider.calls)
    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="verify", takeover=True)
    try:
        event_types = [event.event_type for event in store.scan("ui")]
    finally:
        store.close()
    assert event_types[-2:] == [
        "memory.extraction.requested",
        "memory.extraction.noop",
    ]


def test_memory_candidate_review_is_workspace_isolated_and_ledgered(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from stata_agent.memory.memstore import MemoryStore

    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    memory = MemoryStore(tmp_path / "memory.json", workspace_id=ui._workspace_id("ui"))
    accepted = memory.add_candidate("以后默认使用中文", source_ids=["seq:1"])
    rejected = memory.add_candidate("始终保留完整日志", source_ids=["seq:2"])
    other = memory.add_candidate("另一个工作区的约束", workspace_id="sha256:other", source_ids=["seq:3"])
    client = TestClient(ui.app)

    listed = client.get("/api/memory/candidates", params={"ws": "ui"})
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()["items"]} == {accepted["id"], rejected["id"]}

    accepted_response = client.post(
        f"/api/memory/candidates/{accepted['id']}/accept",
        params={"ws": "ui"},
        json={"note": "用户确认"},
    )
    assert accepted_response.status_code == 200
    assert accepted_response.json()["status"] == "accepted"
    assert client.get("/api/memory/candidates", params={"ws": "ui"}).json()["items"] == [rejected]

    rejected_response = client.post(
        f"/api/memory/candidates/{rejected['id']}/reject",
        params={"ws": "ui"},
        json={"note": "暂不采用"},
    )
    assert rejected_response.status_code == 200
    assert client.post(
        f"/api/memory/candidates/{other['id']}/accept", params={"ws": "ui"}
    ).status_code == 404

    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="audit", takeover=True)
    try:
        events = list(store.scan("ui"))
    finally:
        store.close()
    assert [event.event_type for event in events[-2:]] == [
        "memory.candidate.accepted",
        "memory.candidate.rejected",
    ]
    assert all("provider" not in event.payload for event in events[-2:])


def test_memory_scheduler_shutdown_and_resume_are_bounded():
    scheduler = MemoryExtractionScheduler(max_pending=1, shutdown_timeout=0.1)
    scheduler.shutdown(wait=True)
    assert not scheduler.accepting
    assert not scheduler.submit(lambda: None, key="closed")
    scheduler.resume()
    done = threading.Event()
    assert scheduler.submit(lambda: done.set(), key="resumed")
    assert done.wait(1.0)
    scheduler.shutdown(wait=True)


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
