from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stata_agent.events.schema import (
    ACTOR_ORCH,
    EVENT_AGENT_STEP,
    EVENT_APPROVAL_REQ,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    EVENT_USER,
    Event,
)
from stata_agent.storage.sqlite_store import SQLiteStore
import stata_agent.ui as ui


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    return TestClient(ui.app)


def test_workspace_create_switch_and_legacy_default(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/workspaces")
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "ui"

    created = client.post("/api/workspaces", json={"name": "最低工资与就业"})
    assert created.status_code == 200
    workspace_id = created.json()["workspace"]["id"]
    assert workspace_id != "ui"

    switched = client.get("/api/state", params={"ws": workspace_id})
    assert switched.status_code == 200
    assert switched.json()["workspace_id"] == workspace_id

    # Existing clients can omit ws and continue to read the ui ledger.
    legacy = client.get("/api/state")
    assert legacy.status_code == 200
    assert legacy.json()["workspace_id"] == "ui"

    assert client.get("/api/state", params={"ws": "../outside"}).status_code == 422
    assert client.get("/api/state", params={"ws": "not-registered"}).status_code == 404

    greeted = client.post("/api/chat", params={"ws": workspace_id}, json={"text": "你好"})
    assert greeted.status_code == 200
    assert greeted.json()["state"]["workspace_id"] == workspace_id


def test_trace_filter_and_cursor(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="seed", takeover=True)
    try:
        store.append(Event(idea_id="ui", event_type=EVENT_USER, actor="user", payload={"text": "检查主回归"}))
        store.append(Event(idea_id="ui", event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                           operation_id="op-run-1", payload={"run_id": "run-1", "side_effect": "read"}))
        store.append(Event(idea_id="ui", event_type=EVENT_TOOL_CALL, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                           operation_id="op-run-1", payload={"run_id": "run-1", "call_id": "call-1"}))
        store.append(Event(idea_id="ui", event_type=EVENT_TOOL_RESULT, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                           operation_id="op-run-1", payload={"run_id": "run-1", "call_id": "call-1", "rc": 0}))
        store.append(Event(idea_id="ui", event_type=EVENT_RUN_SUCCEEDED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                           operation_id="op-run-1", phase="ESTIMATION",
                           payload={"run_id": "run-1", "summary": "运行成功"}))
        store.append(Event(idea_id="ui", event_type=EVENT_APPROVAL_REQ, actor="agent", phase="ESTIMATION", payload={"request_id": "approval-1", "act": "request_run", "reason": "需要确认主回归"}))
    finally:
        store.close()

    first = client.get("/api/trace", params={"limit": 2})
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    body = first.json()
    assert [row["seq"] for row in body["items"]] == sorted([row["seq"] for row in body["items"]], reverse=True)
    assert body["items"][0]["type"] == EVENT_APPROVAL_REQ
    assert body["next_before_seq"] is not None

    older = client.get("/api/trace", params={"limit": 2, "before_seq": body["next_before_seq"]})
    assert older.status_code == 200
    assert all(row["seq"] < body["next_before_seq"] for row in older.json()["items"])

    searched = client.get("/api/trace", params={"search": "approval-1"})
    assert searched.status_code == 200
    assert len(searched.json()["items"]) == 1
    assert searched.json()["items"][0]["object"] == "approval-1"


def test_trace_activity_groups_by_correlation_and_paginates_groups(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="seed", takeover=True)
    try:
        store.append(Event(idea_id="ui", event_type=EVENT_USER, actor="user", source="user",
                           correlation_id="corr-old", payload={"text": "旧请求"}))
        store.append(Event(idea_id="ui", event_type=EVENT_AGENT_STEP, actor="agent", source="agent",
                           correlation_id="corr-old", payload={"reply": "已完成", "terminal_reason": "model_stop"}))
        store.append(Event(idea_id="ui", event_type=EVENT_USER, actor="user", source="user",
                           correlation_id="corr-new", payload={"text": "重新跑 Stata"}))
        store.append(Event(idea_id="ui", event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                           correlation_id="corr-new", operation_id="op-new", payload={"run_id": "run-new"}))
    finally:
        store.close()

    first = client.get("/api/trace/activity", params={"limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert body["schema"] == "stata-agent.trace-activity.v1"
    assert body["total_groups"] == 2
    assert body["items"][0]["title"] == "重新跑 Stata"
    assert body["items"][0]["steps"][0]["label"] == "Stata 运行 1"
    assert body["next_before_seq"] is not None
    assert body["items"][0]["technical_ids"]["correlation_id"] == "corr-new"

    older = client.get("/api/trace/activity", params={"limit": 1, "before_seq": body["next_before_seq"]})
    assert older.status_code == 200
    assert older.json()["items"][0]["title"] == "旧请求"
    assert older.json()["total_groups"] == 2
    assert client.get("/api/trace/activity", params={"category": "run"}).json()["total_groups"] == 1


def test_trace_activity_rejects_invalid_query_with_stable_code(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/trace/activity", params={"limit": 0})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "trace_query_invalid"
    response = client.get("/api/trace/activity", params={"before_seq": "not-a-seq"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "trace_query_invalid"
    response = client.get("/api/trace/activity", params={"category": "not-a-category"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "trace_query_invalid"


def test_v4_static_structure_and_safe_dom_rendering():
    root = Path(ui.__file__).with_name("webui")
    index = (root / "index.html").read_text(encoding="utf-8")
    styles = (root / "styles.css").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")

    assert "Stata 研究助手" in index
    assert "workspace-sidebar" in index
    assert "view-root" in index
    assert "phase-track" not in index
    assert "inspector" not in index
    assert "trace-drawer" not in index
    assert "grid-template-columns: var(--sidebar-width) minmax(0, 1fr)" in styles
    assert "/api/workspaces" in script
    assert "/api/trace" in script
    assert "/api/trace/activity" in script
    assert "data-trace-technical" in script
    assert "traceExpandedTechnical" in script
    assert "stata-agent.trace-filter" in script
    assert "writeStorage(\"stata-agent.trace-filter\"" in script
    assert "活动时间线" in script and "技术审计" in script
    assert "innerHTML" not in script
    assert "trace-drawer" not in script
