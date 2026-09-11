from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

import stata_agent.ui as ui
from stata_agent.application import RequestControlRegistry
from stata_agent.events.schema import (
    ACTOR_AGENT,
    ACTOR_ORCH,
    EVENT_APPROVAL_REQ,
    EVENT_TOOL_DONE,
    EVENT_TOOL_INVOKED,
    Event,
)
from stata_agent.storage.sqlite_store import SQLiteStore


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    return TestClient(ui.app)


def _sse_rows(response) -> list[dict]:
    async def collect():
        return [item.decode() if isinstance(item, bytes) else item async for item in response.body_iterator]

    payload = "".join(asyncio.run(collect()))
    return [json.loads(part.split("data: ", 1)[1]) for part in payload.strip().split("\n\n")]


def test_conversation_projects_tool_lifecycle_as_one_independent_card():
    events = [
        Event(idea_id="ui", event_type=EVENT_TOOL_INVOKED, actor=ACTOR_ORCH,
              payload={"tool": "run_stata", "args": {"code": "secret"}}),
        Event(idea_id="ui", event_type=EVENT_TOOL_DONE, actor=ACTOR_ORCH,
              payload={"tool": "run_stata", "ok": False,
                       "result": {"error": "拒绝：超出运行目录"}}),
    ]

    messages = ui._conversation(events)

    assert len(messages) == 1
    assert messages[0]["kind"] == "tool"
    assert messages[0]["tool_name"] == "run_stata"
    assert messages[0]["status"] == "failed"
    assert messages[0]["error"] == "拒绝：超出运行目录"
    assert messages[0]["args_keys"] == ["code"]
    assert "secret" not in json.dumps(messages)


def test_sse_tool_events_are_structured_and_not_assistant_text(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")

    def fake_run(*args, on_event=None, **kwargs):
        on_event({"type": "tool_started", "name": "run_stata"})
        on_event({"type": "text_delta", "text": "结果"})
        on_event({"type": "tool_completed", "name": "run_stata", "ok": False})
        return "结果", None, {"events": 3}

    monkeypatch.setattr(ui, "_run_chat_sync", fake_run)
    response = asyncio.run(ui.chat_stream(ui.ChatIn(text="hello")))
    rows = _sse_rows(response)

    assert [row["type"] for row in rows] == ["start", "tool_started", "token", "tool_completed", "done"]
    started, completed = rows[1], rows[3]
    assert started["tool_id"] == completed["tool_id"]
    assert started["status"] == "running"
    assert completed["status"] == "failed"
    assert completed["ok"] is False
    assert completed["error"]["code"] == "tool_failed"
    assert "run_stata" not in rows[2]["text"]


def test_stop_is_request_scoped_and_idempotent(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")
    monkeypatch.setattr(ui, "_REQUEST_CONTROL_REGISTRY", RequestControlRegistry())
    import threading

    event = threading.Event()
    ui._register_request_control("req-1", "ui", event)
    first = ui.control_stop(ui.StopIn(request_id="req-1"))
    second = ui.control_stop(ui.StopIn(request_id="req-1"))
    assert first["status"] == second["status"] == "cancelling"
    assert first["cancel_requested"] is True
    assert event.is_set()

    ui._finish_request_control("req-1", status="cancelled")
    terminal = ui.control_stop(ui.StopIn(request_id="req-1"))
    assert terminal["status"] == "cancelled"
    assert terminal["cancel_requested"] is True


def test_approval_modify_audits_user_steering_and_decision(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="seed", takeover=True)
    try:
        store.append(Event(idea_id="ui", event_type=EVENT_APPROVAL_REQ, actor=ACTOR_AGENT,
                           payload={"request_id": "approval-1", "act": "request_run",
                                    "reason": "请确认主回归"}))
    finally:
        store.close()

    response = client.post(
        "/api/approvals/approval-1/decision",
        json={"decision": "modify", "note": "改用城市和年份固定效应"},
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "modified"
    assert response.json()["state"]["pending_approvals"] == []

    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="check", takeover=True)
    try:
        rows = list(store.scan("ui"))
    finally:
        store.close()
    assert [row.event_type for row in rows[-3:]] == ["user.message", "steering", "approval.granted"]
    assert rows[-1].payload["decision"] == "modify"
    assert rows[-1].payload["note"] == "改用城市和年份固定效应"
    assert client.get("/api/approvals", params={"status": "all"}).json()["items"][0]["status"] == "modified"


def test_api_errors_have_one_shape_and_static_ui_contract(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/state", params={"ws": "not-registered"})
    body = response.json()
    assert response.status_code == 404
    assert body["ok"] is False
    assert body["error"]["code"] == "http_404"
    assert body["error"]["message"] == body["detail"]

    root = Path(ui.__file__).with_name("webui")
    script = (root / "app.js").read_text(encoding="utf-8")
    assert "md-table" in script
    assert "tool-card" in script
    assert 'action: "stop"' in script
    assert "innerHTML" not in script
    assert "eval(" not in script
    assert "document.write" not in script
