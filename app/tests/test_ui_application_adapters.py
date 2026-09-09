from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import stata_agent.ui as ui
from stata_agent.application import LocalTaskQueue, RequestControlRegistry
from stata_agent.events.schema import (
    EVENT_MEMORY_EXTRACTION_NOOP,
    EVENT_MEMORY_EXTRACTION_REQUESTED,
)
from stata_agent.storage.sqlite_store import SQLiteStore


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    monkeypatch.setattr(ui, "_REQUEST_CONTROL_REGISTRY", RequestControlRegistry())
    return TestClient(ui.app)


def test_stop_http_is_concurrent_idempotent_and_workspace_scoped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    cancel_event = threading.Event()
    ui._register_request_control("request-ui", "ui", cancel_event)

    def stop_request():
        return client.post(
            "/api/control/stop",
            params={"ws": "ui"},
            json={"request_id": "request-ui"},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _index: stop_request(), range(8)))

    assert all(response.status_code == 200 for response in responses)
    assert {response.json()["status"] for response in responses} == {"cancelling"}
    assert cancel_event.is_set()
    assert client.post(
        "/api/control/stop",
        params={"ws": "other"},
        json={"request_id": "request-ui"},
    ).status_code == 404

    ui._finish_request_control("request-ui", status="cancelled")
    terminal = client.post(
        "/api/control/stop",
        params={"ws": "ui"},
        json={"request_id": "request-ui"},
    )
    assert terminal.status_code == 200
    assert terminal.json()["status"] == "cancelled"
    assert terminal.json()["cancel_requested"] is True


def test_ui_queue_adapter_preserves_typed_admission_and_lifecycle(monkeypatch):
    queue = LocalTaskQueue(max_pending=1, shutdown_timeout=0.2)
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", queue)
    started = threading.Event()
    release = threading.Event()
    pending_done = threading.Event()

    def blocking_task() -> None:
        started.set()
        release.wait(2)

    assert ui._submit_memory_extraction_task(key="running", callback=blocking_task)
    assert started.wait(1)
    assert not ui._submit_memory_extraction_task(key="running", callback=lambda: None)
    assert ui._submit_memory_extraction_task(key="pending", callback=pending_done.set)
    assert not ui._submit_memory_extraction_task(key="full", callback=lambda: None)

    release.set()
    ui.shutdown_memory_extractions(wait=True)
    assert pending_done.is_set()
    stats = queue.stats
    assert stats.accepted == 2
    assert stats.completed == 2
    assert stats.duplicates == 1
    assert stats.full == 1
    assert not stats.accepting

    queue.resume()
    resumed = threading.Event()
    assert ui._submit_memory_extraction_task(key="resumed", callback=resumed.set)
    ui.shutdown_memory_extractions(wait=True)
    assert resumed.is_set()


def test_durable_memory_request_survives_queue_full_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("STATA_AGENT_MEMORY_EXTRACTION", "provider")
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    monkeypatch.setattr(ui, "_executor", lambda _store: None)
    monkeypatch.setattr(ui, "_rag", lambda: None)
    monkeypatch.setattr(ui, "_privacy_mode", lambda: "local_strict")

    saturated_queue = LocalTaskQueue(max_pending=1, shutdown_timeout=0.2)
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", saturated_queue)
    started = threading.Event()
    release = threading.Event()
    def blocking_task() -> None:
        started.set()
        release.wait(2)

    assert saturated_queue.submit("running", blocking_task).accepted
    assert started.wait(1)
    assert saturated_queue.submit("occupied", lambda: None).accepted

    extraction_called = threading.Event()

    class Provider:
        provider = "local"

        def chat(self, _messages, *, json_mode=False, tools=None):
            del tools
            if json_mode:
                extraction_called.set()
                return {"content": json.dumps({"candidates": []})}
            return {"content": "ok", "tool_calls": None}

    provider = Provider()
    monkeypatch.setattr(ui, "_provider", lambda: provider)
    assert ui._run_chat_sync("ui", "以后默认使用中文回答", "interactive")[0] == "ok"

    ledger = SQLiteStore(str(ui.DEFAULT_DB), writer_id="verify", takeover=True)
    try:
        events = list(ledger.scan("ui"))
    finally:
        ledger.close()
    assert [event.event_type for event in events].count(EVENT_MEMORY_EXTRACTION_REQUESTED) == 1

    # The old worker is full, but the durable request remains recoverable.
    release.set()
    saturated_queue.shutdown(wait=True)
    resumed_queue = LocalTaskQueue(max_pending=1, shutdown_timeout=0.2)
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", resumed_queue)
    assert ui.resume_memory_extractions("ui") == 1
    assert extraction_called.wait(2)
    resumed_queue.shutdown(wait=True)

    ledger = SQLiteStore(str(ui.DEFAULT_DB), writer_id="verify-resume", takeover=True)
    try:
        events = list(ledger.scan("ui"))
    finally:
        ledger.close()
    assert [event.event_type for event in events].count(EVENT_MEMORY_EXTRACTION_REQUESTED) == 1
    assert [event.event_type for event in events].count(EVENT_MEMORY_EXTRACTION_NOOP) == 1
