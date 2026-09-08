"""toolkit 直接单测：工具注册结构 / handler 错误路径 / enabled 动态暴露。"""

from __future__ import annotations

import tempfile
import pathlib

import pytest

from stata_agent.toolkit import ToolContext, default_tools, err, ok
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.events.schema import EVENT_IDEA, ACTOR_AGENT, Event

REQUIRED = {"inspect_dataset", "run_stata", "run_do_file", "read_artifact",
            "write_artifact", "search_literature", "fetch_source", "update_research_plan",
            "verify_result", "ask_user", "write_draft"}


def _store(tmp_path):
    s = SQLiteStore(str(tmp_path / "l.db"), writer_id="t", takeover=True)
    s.append(Event(idea_id="ui", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                   payload={"question": "x"}))
    return s


def test_default_tools_registered_and_contract():
    tools = default_tools()
    assert REQUIRED <= set(tools)
    for t in tools.values():
        schema = t.input_schema
        assert schema.get("type") == "object"
        assert schema.get("additionalProperties") is False
        assert t.permission in {"safe", "read", "write", "execute", "network", "external", "destructive"}
        oa = t.as_openai()
        assert oa["type"] == "function"
        assert oa["function"]["name"] == t.name
        assert "description" in oa["function"] and oa["function"]["description"]


def test_enabled_dynamic_exposure():
    tools = default_tools()
    no_exec = ToolContext(executor=None, network_available=False)
    assert tools["run_stata"].enabled(no_exec) is False
    assert tools["read_artifact"].enabled(no_exec) is True   # 恒可用
    assert tools["fetch_source"].enabled(no_exec) is False   # 无网络
    with_exec = ToolContext(executor=object(), network_available=True)
    assert tools["run_stata"].enabled(with_exec) is True
    assert tools["fetch_source"].enabled(with_exec) is True


def test_handlers_error_without_dependencies(tmp_path):
    s = _store(tmp_path)
    ctx = ToolContext(idea="ui", store=s, executor=None, rag=None, memory=None)
    t = default_tools()
    assert t["run_stata"].handler({}, ctx)["ok"] is False            # 缺 code
    assert t["run_stata"].handler({"code": "x"}, ctx)["ok"] is False  # 无 executor
    assert t["search_literature"].handler({"query": "q"}, ctx)["ok"] is False  # 无 rag
    assert t["write_draft"].handler({}, ctx)["ok"] is False          # 无结果
    assert t["verify_result"].handler({"run_id": "r1"}, ctx)["ok"] is False  # run 不存在
    s.close()


def test_ask_user_returns_ask():
    t = default_tools()
    r = t["ask_user"].handler({"question": "数据在哪？"}, ToolContext())
    assert r["ok"] is True and r["data"]["ask"] == "数据在哪？"


def test_ok_err_shape():
    assert ok({"a": 1}) == {"ok": True, "data": {"a": 1}}
    assert err("x", type="t", retryable=True, suggestion="y") == {
        "ok": False, "error": {"type": "t", "message": "x", "retryable": True, "suggestion": "y"}}
