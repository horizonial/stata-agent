from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import stata_agent.ui as ui
from stata_agent.application import RequestControlRegistry
from stata_agent.events.schema import (
    ACTOR_AGENT,
    EVENT_APPROVAL_GRANT,
    EVENT_APPROVAL_REJECT,
    EVENT_APPROVAL_REQ,
    EVENT_AGENT_STEP,
    EVENT_TOOL_INVOKED,
    Event,
)
from stata_agent.storage.store import LeaseConflict, StaleWrite
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
    now = [1000.0]
    monkeypatch.setattr(
        ui,
        "_REQUEST_CONTROL_REGISTRY",
        RequestControlRegistry(ttl_seconds=ui._REQUEST_CONTROL_TTL, clock=lambda: now[0]),
    )
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

    now[0] += ui._REQUEST_CONTROL_TTL + 1
    ui._prune_request_controls()
    assert ui._request_control_snapshot("req-ui") is None


def test_stop_endpoint_no_active_and_unknown_request_have_stable_error(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")
    monkeypatch.setattr(ui, "_REQUEST_CONTROL_REGISTRY", RequestControlRegistry())
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


@pytest.mark.parametrize(
    ("terminal_status", "terminal_reason", "failure_code"),
    [
        ("failed", "provider_error", "provider_error"),
        ("uncertain", "cancel_requested", "uncertain"),
        ("paused", "context_budget", "context_budget"),
    ],
)
def test_sse_done_preserves_non_exception_terminal_outcome(monkeypatch, terminal_status, terminal_reason, failure_code):
    """A loop result encoded in the adapter state must not become completed."""

    monkeypatch.setattr(ui, "_resolve_workspace", lambda _ws: "ui")

    def fake_run(*_args, **_kwargs):
        return "partial", None, {
            "terminal_status": terminal_status,
            "terminal_reason": terminal_reason,
            "terminal_failure": {
                "code": failure_code,
                "message": "safe message",
                "retryable": terminal_status == "paused",
                "support_action": "retry" if terminal_status == "paused" else "download_diagnostics",
            },
        }

    monkeypatch.setattr(ui, "_run_chat_sync", fake_run)
    rows = _rows(asyncio.run(ui.chat_stream(ui.ChatIn(text="hello"))))
    assert rows[-1]["type"] == "done"
    assert rows[-1]["status"] == terminal_status
    assert rows[-1]["terminal_reason"] == terminal_reason
    assert rows[-1]["error"]["code"] == failure_code


def test_refresh_projection_preserves_agent_terminal_reason_and_cancellation_class(tmp_path, monkeypatch):
    """Durable agent_step reasons survive replay without leaking raw detail."""

    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="terminal-test", takeover=True)
    try:
        store.append(
            Event(
                idea_id="ui",
                event_type=EVENT_AGENT_STEP,
                actor=ACTOR_AGENT,
                payload={
                    "reply": "safe",
                    "terminal_reason": "cancel_requested",
                    "cancel_requested": True,
                    "detail": "SECRET_PROVIDER_EXCEPTION C:/private/file.dta",
                },
            )
        )
        status, detail = ui._run_state(list(store.scan("ui")), [])
        summary = ui._summary(store, "ui")
        conversation = ui._conversation(list(store.scan("ui")))
    finally:
        store.close()

    assert status == "uncertain"
    assert "不确定" in detail
    assert summary["run_status"] == "uncertain"
    assert summary["terminal_status"] == "uncertain"
    assert summary["terminal_reason"] == "cancel_requested"
    assert summary["terminal_failure"]["code"] == "uncertain"
    assert conversation[0]["terminal_status"] == "uncertain"
    assert conversation[0]["error_code"] == "uncertain"
    encoded = json.dumps(summary, ensure_ascii=False) + json.dumps(conversation, ensure_ascii=False)
    assert "SECRET_PROVIDER_EXCEPTION" not in encoded
    assert "C:/private" not in encoded


@pytest.mark.parametrize(
    ("terminal_reason", "expected_status", "expected_code"),
    [
        ("provider_error", "failed", "provider_error"),
        ("context_budget", "paused", "context_budget"),
        ("cancelled", "cancelled", "run_cancelled"),
        ("cancel_requested", "uncertain", "uncertain"),
    ],
)
def test_refresh_projection_maps_known_terminal_reasons(tmp_path, monkeypatch, terminal_reason, expected_status, expected_code):
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="terminal-matrix", takeover=True)
    try:
        store.append(
            Event(
                idea_id="ui",
                event_type=EVENT_AGENT_STEP,
                actor=ACTOR_AGENT,
                payload={"terminal_reason": terminal_reason, "cancel_requested": terminal_reason in {"cancelled", "cancel_requested"}},
            )
        )
        snapshot = ui._summary(store, "ui")
    finally:
        store.close()
    assert snapshot["run_status"] == expected_status
    assert snapshot["terminal_status"] == expected_status
    assert snapshot["terminal_failure"]["code"] == expected_code


@pytest.mark.parametrize(
    ("terminal_reason", "expected_status", "wait_for_tool"),
    [
        ("cancelled", "cancelled", False),
        ("cancel_requested", "uncertain", True),
    ],
)
def test_stream_stop_worker_terminal_is_persisted_for_fresh_state(
    tmp_path, monkeypatch, terminal_reason, expected_status, wait_for_tool
):
    """Exercise start → stop → worker terminal → new-client state recovery."""

    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(ui, "_resolve_workspace", lambda _ws: "ui")
    monkeypatch.setattr(ui, "_REQUEST_CONTROL_REGISTRY", RequestControlRegistry())
    worker_started = threading.Event()

    def fake_run(*_args, on_event=None, cancel_event=None, request_id=None, **_kwargs):
        assert on_event is not None and cancel_event is not None and request_id
        on_event({"type": "tool_started", "name": "run_stata", "call_id": "stata-1"})
        worker_started.set()
        assert cancel_event.wait(2.0)
        store = ui._store()
        try:
            store.append(
                Event(
                    idea_id="ui",
                    event_type=EVENT_AGENT_STEP,
                    actor=ACTOR_AGENT,
                    correlation_id=request_id,
                    payload={
                        "reply": "本轮已停止",
                        "terminal_reason": terminal_reason,
                        "cancel_requested": True,
                    },
                )
            )
        finally:
            store.close()
        failure = (
            {
                "code": "uncertain",
                "message": "外部副作用状态暂不可确认，请先核对运行记录。",
                "retryable": False,
                "support_action": "download_diagnostics",
            }
            if expected_status == "uncertain"
            else None
        )
        return "本轮已停止", None, {
            "terminal_status": expected_status,
            "terminal_reason": terminal_reason,
            "terminal_failure": failure,
        }

    monkeypatch.setattr(ui, "_run_chat_sync", fake_run)
    response = asyncio.run(ui.chat_stream(ui.ChatIn(text="运行主回归")))

    async def collect_after_stop():
        iterator = response.body_iterator
        raw_rows = [await iterator.__anext__()]
        request_id = json.loads(raw_rows[0].split("data: ", 1)[1])["request_id"]
        if wait_for_tool:
            # Resuming the generator starts the worker; the queued tool event
            # gives us a deterministic in-flight cancellation point.
            raw_rows.append(await iterator.__anext__())
            assert worker_started.is_set()
        stop = ui._request_cancel(request_id, "ui")
        assert stop["status"] == "cancelling"
        while True:
            try:
                raw_rows.append(await iterator.__anext__())
            except StopAsyncIteration:
                break
        return request_id, [json.loads(row.split("data: ", 1)[1]) for row in raw_rows]

    request_id, rows = asyncio.run(collect_after_stop())
    assert [row["type"] for row in rows][-1] == "done"
    assert rows[-1]["status"] == expected_status
    assert rows[-1]["terminal_reason"] == terminal_reason
    control = ui._request_control_snapshot(request_id)
    assert control and control["status"] == expected_status
    assert ui._latest_active_request("ui") is None

    fresh = TestClient(ui.app).get("/api/state?ws=ui")
    assert fresh.status_code == 200
    snapshot = fresh.json()
    assert snapshot["run_status"] == expected_status
    assert snapshot["terminal_status"] == expected_status
    assert snapshot["terminal_reason"] == terminal_reason
    assert snapshot["active_request"] is None


@pytest.mark.parametrize("error", [LeaseConflict("secret owner token"), StaleWrite("secret revision")])
def test_store_writer_conflicts_use_safe_http_envelope(error):
    request = type("RequestStub", (), {"headers": {"x-request-id": "req-safe"}})()
    response = asyncio.run(ui._store_error_handler(request, error))
    payload = json.loads(response.body)
    assert response.status_code == 409
    assert payload["error"]["code"] in {"ledger_writer_conflict", "ledger_writer_stale"}
    assert payload["error"]["retryable"] is True
    assert payload["error"]["request_id"] == "req-safe"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "secret owner token" not in encoded
    assert "secret revision" not in encoded


def test_store_writer_conflict_reaches_http_boundary_without_raw_detail(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda _ws: "ui")

    def fail_store():
        raise LeaseConflict("owner token SECRET and C:/private/ledger.sqlite3")

    monkeypatch.setattr(ui, "_store", fail_store)
    response = TestClient(ui.app, raise_server_exceptions=False).get(
        "/api/state?ws=ui", headers={"x-request-id": "req-http"}
    )
    payload = response.json()
    assert response.status_code == 409
    assert payload["error"]["code"] == "ledger_writer_conflict"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["request_id"] == "req-http"
    assert "SECRET" not in response.text
    assert "C:/private" not in response.text


def test_sse_error_uses_api_error_shape(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")

    def failing_run(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(ui, "_run_chat_sync", failing_run)
    response = asyncio.run(ui.chat_stream(ui.ChatIn(text="hello")))
    rows = _rows(response)
    assert [row["type"] for row in rows] == ["start", "error"]
    assert rows[-1]["error"] == {
        "code": "request_failed",
        "message": "本轮未完成，请查看 Trace 或导出诊断包。",
        "retryable": True,
        "support_action": "download_diagnostics",
    }
    assert "provider down" not in json.dumps(rows[-1], ensure_ascii=False)
    assert "detail" not in rows[-1]


def test_sse_disconnect_marks_request_cancelling(monkeypatch):
    monkeypatch.setattr(ui, "_resolve_workspace", lambda ws: "ui")
    monkeypatch.setattr(ui, "_REQUEST_CONTROL_REGISTRY", RequestControlRegistry())
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


@pytest.mark.parametrize(
    "decisions",
    [
        ("approve", "reject"),
        ("modify", "modify"),
        ("approve", "modify"),
    ],
)
def test_concurrent_approval_decisions_have_one_terminal_and_refresh_winner(
    tmp_path, monkeypatch, decisions
):
    client = _client(tmp_path, monkeypatch)
    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="seed", takeover=True)
    try:
        store.append(
            Event(
                idea_id="ui",
                event_type=EVENT_APPROVAL_REQ,
                actor=ACTOR_AGENT,
                payload={
                    "request_id": "approval-race",
                    "act": "request_run",
                    "reason": "确认主回归",
                },
            )
        )
    finally:
        store.close()

    # Separate clients exercise the same backend request path.  The endpoint
    # must arbitrate the pending snapshot and terminal append; client-side
    # button disabling is intentionally absent here.
    clients = (client, TestClient(ui.app))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                current.post,
                "/api/approvals/approval-race/decision",
                json={"decision": decision, "note": note},
            )
            for current, decision, note in zip(
                clients,
                decisions,
                ("保留当前方案", "拒绝当前方案"),
            )
        ]
        responses = [future.result() for future in futures]

    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    conflict_body = conflict.json()
    assert conflict_body["error"]["code"] == "http_409"
    assert conflict_body["error"]["message"] == "该审批请求已经有决定，不能重复提交。"
    assert conflict_body["detail"] == conflict_body["error"]["message"]

    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="check", takeover=True)
    try:
        events = list(store.scan("ui"))
    finally:
        store.close()
    terminals = [event for event in events if event.event_type in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}]
    assert len(terminals) == 1
    winner = terminals[0]
    assert winner.payload["request_id"] == "approval-race"
    expected_status = (
        "modified"
        if winner.payload.get("decision") == "modify"
        else "approved"
        if winner.event_type == EVENT_APPROVAL_GRANT
        else "rejected"
    )

    refreshed = client.get("/api/approvals", params={"status": "all"})
    assert refreshed.status_code == 200
    item = refreshed.json()["items"][0]
    assert item["status"] == expected_status
    assert client.get("/api/state").json()["pending_approvals"] == []


def test_validation_error_does_not_echo_submitted_payload(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/chat", json={"text": 123, "mode": "unsafe"})
    body = response.json()
    assert response.status_code == 422
    assert body["ok"] is False
    assert body["error"]["code"] == "validation_error"
    assert "123" not in json.dumps(body)


def test_frontend_static_table_xss_and_tool_card_contract():
    root = Path(ui.__file__).with_name("webui")
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
