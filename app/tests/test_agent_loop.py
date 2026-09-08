"""通用 agent loop：聊天默认、工具自主选择、护栏拦未知工具、ask_user 停。"""

from __future__ import annotations

from stata_agent.events.schema import EVENT_IDEA, ACTOR_AGENT, Event
from stata_agent.harness.agent_loop import run_loop
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import ToolContext, default_tools
from stata_agent.tools.fake_executor import FakeExecutor


class FakeChatProvider:
    """脚本化：每次 chat 返回指定 {content, tool_calls}。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
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
                                          "arguments": {"code": "sysuse auto, clear"}}]},
        {"content": "主回归已跑完，系数 -238.9。", "tool_calls": None},
    ])
    ctx = ToolContext(idea="ui", store=s, executor=FakeExecutor(s))
    res = run_loop(s, prov, default_tools(), ctx, user_text="用 DID 跑主回归")
    assert res.reply == "主回归已跑完，系数 -238.9。"
    assert res.tool_calls == 1
    # 工具执行写进了事件（tool.invoked + tool.done + 证据链）
    kinds = [e.event_type for e in s.scan("ui")]
    assert "tool.invoked" in kinds and "tool.done" in kinds
    # 证据自动签了卡
    assert len(s.project("ui").cards) >= 1
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
