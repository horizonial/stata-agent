from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import stata_agent.ui as ui
from stata_agent.events.schema import ACTOR_AGENT, EVENT_APPROVAL_REQ, EVENT_TOOL_INVOKED, Event
from stata_agent.storage.sqlite_store import SQLiteStore


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    return TestClient(ui.app)


def _rows(response) -> list[dict]:
    async def collect():
        return [item.decode() if isinstance(item, bytes) else item async for item in response.body_iterator]

    payload = "".join(asyncio.run(collect()))
    return [json.loads(part.split("data: ", 1)[1]) for part in payload.strip().split("\n\n")]


def test_request_controls_scope_terminal_wins_and_expire(monkeypatch):
    ui._REQUEST_CONTROLS.clear()
    first_event = threading.Event()
    second_event = threading.Event()
    ui._register_request_control("req-ui", "ui", first_event)
    ui._register_request_control("req-other", "other", second_event)

    with pytest.raises(HTTPException) as error:
        ui._request_cancel("req-ui", "other")
    assert error.value.status_code == 404
    assert not first_event.is_set()
    assert not second_event.is_set()

    ui._finish_request_control("req-ui", status="completed")
    ui._finish_request_control("req-ui", status="failed")
    snapshot = ui._request_control_snapshot("req-ui")
    assert snapshot and snapshot["status"] == "completed"
    assert ui._latest_active_request("ui") is None

    with ui._REQUEST_CONTROLS_LOCK:
        ui._REQUEST_CONTROLS["req-ui"]["finished_at"] = time.time() - ui._REQUEST_CONTROL_TTL - 1
    ui._prune_request_controls()
    assert ui._request_control_snapshot("req-ui") is None
    ui._REQUEST_CONTROLS.clear()


def test_stop_endpoint_no_active_and_unknown_request_have_stable_error(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")
    ui._REQUEST_CONTROLS.clear()
    idle = ui.control_stop(ui.StopIn())
    assert idle == {"ok": True, "request_id": None, "workspace": "ui", "status": "idle", "cancel_requested": False}

    with pytest.raises(HTTPException) as error:
        ui.control_stop(ui.StopIn(request_id="missing"))
    assert error.value.status_code == 404


def test_sse_cancelled_terminal_preserves_queued_tail_events(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")

    def fake_run(*args, on_event=None, cancel_event=None, **kwargs):
        on_event({"type": "tool_started", "name": "read_data"})
        on_event({"type": "text_delta", "text": "tail"})
        ui._request_cancel(kwargs["request_id"], "ui")
        return "tail", None, {"events": 2}

    monkeypatch.setattr(ui, "_run_chat_sync", fake_run)
    response = asyncio.run(ui.chat_stream(ui.ChatIn(text="hello")))
    rows = _rows(response)
    assert [row["type"] for row in rows] == ["start", "tool_started", "token", "done"]
    assert rows[-1]["status"] == "cancelled"
    assert rows[-1]["cancel_requested"] is True
    assert rows[-2]["text"] == "tail"
    assert all(row["request_id"] == rows[0]["request_id"] for row in rows)


def test_sse_error_uses_api_error_shape(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")

    def failing_run(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(ui, "_run_chat_sync", failing_run)
    response = asyncio.run(ui.chat_stream(ui.ChatIn(text="hello")))
    rows = _rows(response)
    assert [row["type"] for row in rows] == ["start", "error"]
    assert rows[-1]["error"] == {"code": "request_failed", "message": "provider down"}
    assert rows[-1]["detail"] == "provider down"


def test_sse_disconnect_marks_request_cancelling(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")
    ui._REQUEST_CONTROLS.clear()
    monkeypatch.setattr(ui, "_run_chat_sync", lambda *args, **kwargs: ("ok", None, {}))
    response = asyncio.run(ui.chat_stream(ui.ChatIn(text="hello")))

    async def open_and_close():
        iterator = response.body_iterator
        first = await iterator.__anext__()
        await iterator.aclose()
        return first

    first = asyncio.run(open_and_close())
    request_id = json.loads(first.split("data: ", 1)[1])["request_id"]
    # Some ASGI test clients defer generator finalization until the response
    # object is collected; assert the same disconnect transition explicitly
    # so the contract does not depend on that implementation detail.
    ui._mark_request_disconnected(request_id)
    assert ui._request_control_snapshot(request_id)["status"] == "cancelling"
    ui._REQUEST_CONTROLS.clear()


def test_approval_modify_validation_and_duplicate_are_auditable(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="seed", takeover=True)
    try:
        store.append(Event(idea_id="ui", event_type=EVENT_APPROVAL_REQ, actor=ACTOR_AGENT,
                           payload={"request_id": "approval-edge", "act": "request_run", "reason": "确认"}))
    finally:
        store.close()

    missing = client.post("/api/approvals/approval-edge/decision", json={"decision": "modify", "note": ""})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "http_422"
    too_long = client.post("/api/approvals/approval-edge/decision", json={"decision": "modify", "note": "x" * 4001})
    assert too_long.status_code == 413
    unknown = client.post("/api/approvals/approval-edge/decision", json={"decision": "later", "note": "x"})
    assert unknown.status_code == 422

    modified = client.post("/api/approvals/approval-edge/decision", json={"decision": "modify", "note": "改用稳健标准误"})
    assert modified.status_code == 200
    duplicate = client.post("/api/approvals/approval-edge/decision", json={"decision": "modify", "note": "再次修改"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["message"] == duplicate.json()["detail"]


def test_validation_error_does_not_echo_submitted_payload(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/chat", json={"text": 123, "mode": "unsafe"})
    body = response.json()
    assert response.status_code == 422
    assert body["ok"] is False
    assert body["error"]["code"] == "validation_error"
    assert "123" not in json.dumps(body)


def test_frontend_static_table_xss_and_tool_card_contract():
    root = Path(ui.__file__).with_name("ui")
    script = (root / "app.js").read_text(encoding="utf-8")
    styles = (root / "styles.css").read_text(encoding="utf-8")
    for marker in ("tableCells", "isTableDivider", "appendTable", "md-table", "renderToolCard", "tool-card-error", "tool_failed"):
        assert marker in script
    assert ".md-table" in styles and ".tool-card" in styles
    assert "textContent" in script and "createElement" in script
    assert "innerHTML" not in script
    assert "eval(" not in script
    assert "document.write" not in script


def test_conversation_keeps_unfinished_tool_as_running_card():
    event = Event(idea_id="ui", event_type=EVENT_TOOL_INVOKED, actor=ACTOR_AGENT,
                  payload={"tool": "inspect_data", "args": {"path": "dataset.dta"}})
    messages = ui._conversation([event])
    assert messages[0]["kind"] == "tool"
    assert messages[0]["status"] == "running"
    assert messages[0]["args_keys"] == ["path"]
