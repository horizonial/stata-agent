"""通用 agent loop：聊天默认、工具自主选择、护栏拦未知工具、ask_user 停。"""

from __future__ import annotations

import time

from stata_agent.events.schema import (
    ACTOR_AGENT,
    EVENT_IDEA,
    EVENT_PROVIDER_TURN_COMPLETED,
    EVENT_PROVIDER_TURN_STARTED,
    Event,
)
from stata_agent.harness.agent_loop import run_loop
from stata_agent.harness.tool_enforcer import ToolEnforcer
from stata_agent.privacy.modes import sanitize_messages
from stata_agent.providers.protocol import ProviderError
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import Tool, ToolContext, default_tools, ok
from stata_agent.tools.fake_executor import FakeExecutor, default_test_contract


class FakeChatProvider:
    """脚本化：每次 chat 返回指定 {content, tool_calls}。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.tool_snapshots = []

    def chat(self, messages, tools=None):
        self.calls += 1
        self.tool_snapshots.append(tools)
        return self.responses.pop(0)


def _store(tmp_path):
    s = SQLiteStore(str(tmp_path / "l.db"), writer_id="t")
    s.append(Event(idea_id="ui", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                   payload={"question": "x"}))
    return s


def test_plain_chat_returns_text_no_tools(tmp_path):
    s = _store(tmp_path)
    prov = FakeChatProvider([{"content": "你好，有什么可以帮你？", "tool_calls": None}])
    ctx = ToolContext(idea="ui", store=s)
    res = run_loop(s, prov, default_tools(), ctx, user_text="你好")
    assert res.reply == "你好，有什么可以帮你？"
    assert res.tool_calls == 0
    s.close()


def test_research_calls_run_stata_tool(tmp_path):
    s = _store(tmp_path)
    prov = FakeChatProvider([
        {"content": None, "tool_calls": [{"id": "c1", "name": "run_stata",
                                          "arguments": {"code": "sysuse auto, clear",
                                                        "result_contract": default_test_contract().model_dump()}}]},
        {"content": "主回归已跑完，系数 -238.9。", "tool_calls": None},
    ])
    ctx = ToolContext(idea="ui", request_id="req-loop-1", store=s, executor=FakeExecutor(s))
    res = run_loop(s, prov, default_tools(), ctx, user_text="用 DID 跑主回归")
    assert res.reply == "主回归已跑完，系数 -238.9。"
    assert res.tool_calls == 1
    # 工具执行写进了事件（tool.invoked + tool.done + 证据链）
    correlated = [event for event in s.scan("ui") if event.event_type in {
        "tool.invoked", "tool.done", "run.requested", "run.succeeded", "tool.call", "tool.result",
    }]
    assert correlated and {event.correlation_id for event in correlated} == {"req-loop-1"}
    kinds = [e.event_type for e in s.scan("ui")]
    assert "tool.invoked" in kinds and "tool.done" in kinds
    lifecycle = [event for event in s.scan("ui") if event.event_type in {
        EVENT_PROVIDER_TURN_STARTED, EVENT_PROVIDER_TURN_COMPLETED,
    }]
    assert [event.event_type for event in lifecycle] == [
        EVENT_PROVIDER_TURN_STARTED, EVENT_PROVIDER_TURN_COMPLETED,
        EVENT_PROVIDER_TURN_STARTED, EVENT_PROVIDER_TURN_COMPLETED,
    ]
    assert {event.correlation_id for event in lifecycle} == {"req-loop-1"}
    assert [event.payload["turn_index"] for event in lifecycle] == [1, 1, 2, 2]
    assert all("messages" not in event.payload and "response" not in event.payload for event in lifecycle)
    # 证据自动签了卡
    assert len(s.project("ui").cards) >= 1
    evidence_events = [event for event in s.scan("ui") if event.event_type in {"evidence.card_signed", "claim.signed"}]
    assert evidence_events and {event.correlation_id for event in evidence_events} == {"req-loop-1"}
    s.close()


def test_guard_blocks_unknown_tool(tmp_path):
    s = _store(tmp_path)
    prov = FakeChatProvider([
        {"content": None, "tool_calls": [{"id": "c1", "name": "drop_all_tables",
                                          "arguments": {}}]},
        {"content": "好的。", "tool_calls": None},
    ])
    ctx = ToolContext(idea="ui", store=s)
    res = run_loop(s, prov, default_tools(), ctx, user_text="删库")
    # 未知工具被护栏拒，模型收到错误后回复
    results = [e for e in s.scan("ui") if e.event_type == "tool.done"]
    assert results and results[0].payload["ok"] is False
    assert "未知工具" in results[0].payload["result"]["error"]["message"]
    s.close()


def test_ask_user_stops_loop(tmp_path):
    s = _store(tmp_path)
    prov = FakeChatProvider([
        {"content": None, "tool_calls": [{"id": "c1", "name": "ask_user",
                                          "arguments": {"question": "数据文件在哪？"}}]},
    ])
    ctx = ToolContext(idea="ui", store=s)
    res = run_loop(s, prov, default_tools(), ctx, user_text="跑一下")
    assert res.ask == "数据文件在哪？"
    assert res.reply is None
    s.close()


def test_done_phase_denies_execute_and_records_terminal(tmp_path):
    s = _store(tmp_path)
    prov = FakeChatProvider([{
        "content": None,
        "tool_calls": [{"id": "c1", "name": "run_stata", "arguments": {"code": "sysuse auto"}}],
    }, {"content": "已拒绝", "tool_calls": None}])
    ctx = ToolContext(idea="ui", store=s, executor=FakeExecutor(s), phase="DONE")
    res = run_loop(s, prov, default_tools(), ctx, user_text="不要再运行")
    assert res.tool_calls == 1
    done = [event for event in s.scan("ui") if event.event_type == "tool.done"][-1]
    assert done.payload["ok"] is False
    assert "DONE" in done.payload["result"]["error"]["message"]
    assert not any(event.event_type == "run.requested" for event in s.scan("ui"))
    s.close()


def test_enforcer_rejects_path_escape_and_local_urls(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    tool = Tool(
        name="write",
        description="write",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args, ctx: ok({"written": args["path"]}),
        permission="write",
    )
    ledger = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="path-test")
    ctx = ToolContext(idea="ui", store=ledger, run_root=root, phase="WRITING")
    enforcer = ToolEnforcer({"write": tool})
    assert "工作区根目录" in (enforcer.validate("write", {"path": str(tmp_path / "escape.txt")}, ctx) or "")
    assert "绝对路径" in (enforcer.validate("write", {"path": "relative.txt"}, ctx) or "")
    network_ctx = ToolContext(run_root=root, phase="WRITING", privacy_mode="approved_remote", network_available=True)
    fetch = default_tools()["fetch_source"]
    assert "scheme" in (ToolEnforcer({"fetch_source": fetch}).validate(
        "fetch_source", {"url": "file:///etc/passwd"}, network_ctx
    ) or "")
    from stata_agent.harness.tool_enforcer import validate_url

    assert "loopback" in (validate_url("http://localhost:8080/x") or "")
    assert "private" in (validate_url("http://192.168.1.3/x") or "")
    ledger.close()


def test_single_response_tool_calls_are_budgeted(tmp_path):
    s = _store(tmp_path)
    calls_seen = []
    ping = Tool(
        name="ping",
        description="ping",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda args, ctx: calls_seen.append(1) or ok({"pong": True}),
        permission="safe",
    )
    prov = FakeChatProvider([{
        "content": None,
        "tool_calls": [{"id": str(i), "name": "ping", "arguments": {}} for i in range(100)],
    }])
    res = run_loop(s, prov, {"ping": ping}, ToolContext(idea="ui", store=s),
                   user_text="ping", max_tool_calls=7)
    assert res.tool_calls == 7 and len(calls_seen) == 7
    events = list(s.scan("ui"))
    budget = [event for event in events if event.event_type == "budget.limit"]
    assert budget and budget[-1].payload["kind"] == "tool_calls"
    assert events[-1].event_type == "agent_step"
    s.close()


def test_tool_timeout_is_enforced(tmp_path):
    def slow(args, ctx):
        time.sleep(0.2)
        return ok({"done": True})

    tool = Tool(
        name="slow",
        description="slow",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=slow,
        permission="safe",
        timeout_seconds=0.01,
    )
    result = ToolEnforcer({"slow": tool}).execute("slow", {}, ToolContext())
    assert result["ok"] is False and result["error"]["type"] == "timeout"


def test_max_steps_writes_budget_and_terminal_agent_step(tmp_path):
    s = _store(tmp_path)
    ping = Tool(
        name="ping",
        description="ping",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda args, ctx: ok({"pong": True}),
        permission="safe",
    )
    prov = FakeChatProvider([
        {"content": None, "tool_calls": [{"id": "1", "name": "ping", "arguments": {}}]},
    ])
    res = run_loop(s, prov, {"ping": ping}, ToolContext(idea="ui", store=s),
                   user_text="one step", max_steps=1)
    assert res.tool_calls == 1
    events = list(s.scan("ui"))
    assert any(event.event_type == "budget.limit" and event.payload["kind"] == "max_steps" for event in events)
    assert events[-1].event_type == "agent_step"
    s.close()


def test_provider_failure_is_observed_without_sensitive_payload(tmp_path):
    s = _store(tmp_path)

    class FailingProvider(FakeChatProvider):
        def chat(self, messages, tools=None):
            self.calls += 1
            raise ProviderError("auth", provider="deepseek", detail="Bearer SECRET_SENTINEL")

    prov = FailingProvider([])
    ctx = ToolContext(idea="ui", request_id="req-provider-failure", store=s)
    result = run_loop(s, prov, default_tools(), ctx, user_text="检查模型")

    assert result.terminal_reason == "provider_error"
    events = list(s.scan("ui"))
    failed = [event for event in events if event.event_type == "provider.turn.failed"]
    assert len(failed) == 1
    assert failed[0].correlation_id == "req-provider-failure"
    assert failed[0].payload["error_code"] == "auth"
    assert failed[0].payload["retryable"] is False
    assert "SECRET_SENTINEL" not in str(failed[0].payload)
    assert "messages" not in failed[0].payload and "response" not in failed[0].payload
    s.close()


def test_last_provider_turn_is_reserved_for_tool_result_finalization(tmp_path):
    s = _store(tmp_path)
    ping = Tool(
        name="ping",
        description="ping",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda args, ctx: ok({"pong": True}),
        permission="safe",
    )
    prov = FakeChatProvider([
        {"content": None, "tool_calls": [{"id": "1", "name": "ping", "arguments": {}}]},
        {"content": "工具结果已确认。", "tool_calls": None},
    ])

    res = run_loop(
        s,
        prov,
        {"ping": ping},
        ToolContext(idea="ui", store=s),
        user_text="run and summarize",
        max_steps=2,
    )

    assert res.reply == "工具结果已确认。"
    assert res.terminal_reason == "model_stop"
    assert res.tool_calls == 1
    assert prov.calls == 2
    assert prov.tool_snapshots[0]
    assert prov.tool_snapshots[1] == []
    assert not any(event.event_type == "budget.limit" for event in s.scan("ui"))
    s.close()


def test_current_user_event_is_not_sent_twice(tmp_path):
    s = _store(tmp_path)
    current = "same current message"
    s.append(Event(idea_id="ui", event_type="user.message", actor="user", source="user",
                   payload={"text": current}))

    class Capture(FakeChatProvider):
        def __init__(self):
            super().__init__([{"content": "ok", "tool_calls": None}])
            self.messages = None

        def chat(self, messages, tools=None):
            self.messages = messages
            return super().chat(messages, tools)

    provider = Capture()
    run_loop(s, provider, {}, ToolContext(idea="ui", store=s), user_text=current)
    assert [m for m in provider.messages if m["role"] == "user"].count({"role": "user", "content": current}) == 1
    s.close()


def test_mixed_sanitized_does_not_send_credentials():
    messages = [
        {"role": "user", "content": "API_KEY=TOP_SECRET_VALUE password=hunter2"},
        {"role": "tool", "content": "local file C:\\Users\\alice\\secret.dta"},
    ]
    safe = sanitize_messages(messages, mode="mixed_sanitized", provider="deepseek")
    rendered = str(safe)
    assert "TOP_SECRET_VALUE" not in rendered
    assert "hunter2" not in rendered
    assert "C:\\Users\\alice\\secret.dta" not in rendered
    assert "sanitized" in rendered
