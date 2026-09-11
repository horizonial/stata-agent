"""toolkit 直接单测：工具注册结构 / handler 错误路径 / enabled 动态暴露。"""

from __future__ import annotations

import tempfile
import pathlib
import json
import os

import pytest

from stata_agent.toolkit import ToolContext, _atomic_write_pair, default_tools, err, ok
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.events.schema import EVENT_IDEA, ACTOR_AGENT, Event
from stata_agent.harness.tool_enforcer import ToolEnforcer
from stata_agent.tools.fake_executor import FakeExecutor, default_test_contract

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


def test_result_contract_controls_evidence_gate(tmp_path):
    store = _store(tmp_path)
    ctx = ToolContext(idea="ui", store=store, executor=FakeExecutor(store))
    tool = default_tools()["run_stata"]
    contract = default_test_contract().model_dump()
    verified = tool.handler({"code": "display 1", "result_contract": contract}, ctx)
    assert verified["ok"] is True
    assert verified["data"]["evidence_ready"] is True
    assert verified["data"]["signed_cards"] == 3
    exploratory = tool.handler({"code": "display 2"}, ctx)
    assert exploratory["ok"] is True
    assert exploratory["data"]["evidence_ready"] is False
    assert exploratory["data"]["signed_cards"] == 0
    assert "contract_missing" in exploratory["data"]["verification"]["failed_codes"]
    assert exploratory["data"]["verification"]["suggestion"]

    enforcer = ToolEnforcer({"run_stata": tool})
    malformed = dict(contract, unexpected="not allowed")
    denied = enforcer.execute("run_stata", {"code": "display 3", "result_contract": malformed}, ctx)
    assert denied["ok"] is False
    assert denied["error"]["type"] == "permission_denied"
    store.close()


def test_write_draft_emits_docx_and_evidence_manifest(tmp_path):
    store = _store(tmp_path)
    ctx = ToolContext(
        idea="ui",
        store=store,
        executor=FakeExecutor(store, run_root=tmp_path / "fake-runs"),
        run_root=tmp_path / "deliveries",
    )
    tools = default_tools()
    contract = default_test_contract().model_dump()
    result = tools["run_stata"].handler(
        {"code": "display 1", "result_contract": contract},
        ctx,
    )
    assert result["ok"] is True and result["data"]["evidence_ready"] is True

    delivered = tools["write_draft"].handler({}, ctx)

    assert delivered["ok"] is True
    data = delivered["data"]
    draft = pathlib.Path(data["draft_path"])
    manifest_path = pathlib.Path(data["manifest_path"])
    assert draft.is_file() and manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["kind"] == "evidence-manifest.v1"
    assert manifest["delivery_digest"] == data["delivery_digest"]
    assert all("path" not in json.dumps(card) for card in manifest["cards"])
    store.close()


def test_atomic_delivery_rollback_preserves_previous_pair(tmp_path, monkeypatch):
    docx = tmp_path / "draft.docx"
    manifest = tmp_path / "draft.evidence.json"
    docx.write_bytes(b"old-docx")
    manifest.write_bytes(b"old-manifest")
    original_replace = os.replace

    def fail_manifest_commit(source, destination):
        if pathlib.Path(destination) == manifest and pathlib.Path(source).suffix == ".tmp":
            raise OSError("injected commit failure")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_manifest_commit)
    with pytest.raises(OSError):
        _atomic_write_pair(((docx, b"new-docx"), (manifest, b"new-manifest")))
    assert docx.read_bytes() == b"old-docx"
    assert manifest.read_bytes() == b"old-manifest"
