from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import stata_agent.ui as ui
from stata_agent.application import LocalTaskQueue, RequestControlRegistry
from stata_agent.application.memory_outbox import MEMORY_EXTRACTION_KIND
from stata_agent.events.schema import (
    ACTOR_USER,
    EVENT_MEMORY_EXTRACTION_NOOP,
    EVENT_MEMORY_EXTRACTION_REQUESTED,
    EVENT_USER,
    Event,
)
from stata_agent.memory.pipeline import MemoryExtractionPipeline
from stata_agent.memory.memstore import MemoryStore
from stata_agent.memory.sqlite_repository import SQLiteMemoryRepository
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


def test_ui_queue_does_not_retry_internal_type_error(monkeypatch):
    class BrokenQueue:
        def __init__(self):
            self.calls = 0

        def submit(self, key, callback):
            del key, callback
            self.calls += 1
            raise TypeError("queue implementation failed")

    queue = BrokenQueue()
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", queue)

    with pytest.raises(TypeError, match="queue implementation failed"):
        ui._submit_memory_extraction_task(key="one", callback=lambda: None)
    assert queue.calls == 1


def test_app_lifespan_starts_recovers_and_boundedly_stops_queue(monkeypatch):
    events = []

    class LifecycleQueue:
        def start(self):
            events.append("start")

        def shutdown(self, *, wait=True, timeout=None):
            events.append(("shutdown", wait, timeout))

    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", LifecycleQueue())
    monkeypatch.setattr(ui, "resume_memory_extractions", lambda: events.append("recover") or 0)

    with TestClient(ui.app):
        assert events == ["start", "recover"]

    assert events == ["start", "recover", ("shutdown", True, 1.0)]


def test_memory_outbox_pump_loop_keeps_retrying_until_cancelled():
    class Dispatcher:
        def __init__(self):
            self.calls = 0

        def pump(self):
            self.calls += 1

    dispatcher = Dispatcher()

    async def exercise() -> int:
        task = asyncio.create_task(
            ui._memory_outbox_pump_loop(dispatcher, interval_seconds=0.01)
        )
        try:
            for _ in range(20):
                if dispatcher.calls >= 2:
                    break
                await asyncio.sleep(0.01)
            return dispatcher.calls
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert asyncio.run(exercise()) >= 2


def test_ui_imports_legacy_memory_once_then_writes_only_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    workspace_id = ui._workspace_id("ui")
    source = tmp_path / "memory.json"
    legacy = MemoryStore(source, workspace_id=workspace_id)
    legacy.add("默认使用中文", workspace_id=workspace_id, source_ids=["seq:1"])
    original_source = source.read_bytes()

    first = ui._memory()
    assert first is not None
    try:
        assert [item["text"] for item in first.all(workspace_id=workspace_id)] == ["默认使用中文"]
        first.add("输出先给结论", workspace_id=workspace_id, source_ids=["seq:2"])
    finally:
        ui._close_memory(first)

    second = ui._memory()
    assert second is not None
    try:
        records = second.all(workspace_id=workspace_id)
    finally:
        ui._close_memory(second)

    assert {item["text"] for item in records} == {"默认使用中文", "输出先给结论"}
    assert len(records) == 2
    assert source.read_bytes() == original_source


def test_ui_imports_legacy_workspaces_then_stops_json_writes(tmp_path, monkeypatch):
    source = tmp_path / "workspaces.json"
    source.write_text(
        json.dumps([{"id": "legacy", "name": "旧研究", "created_at": 1234}], ensure_ascii=False),
        encoding="utf-8",
    )
    original_source = source.read_bytes()
    client = _client(tmp_path, monkeypatch)

    listed = client.get("/api/workspaces")
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()["items"]} == {"ui", "legacy"}

    created = client.post("/api/workspaces", json={"id": "sqlite-only", "name": "新研究"})
    assert created.status_code == 200
    assert source.read_bytes() == original_source

    with SQLiteMemoryRepository(ui.DEFAULT_DB) as repository:
        ids = {str(item["metadata"]["id"]) for item in repository.list_workspaces()}
    assert ids == {"ui", "legacy", "sqlite-only"}


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


def test_ui_post_turn_atomically_persists_requested_event_and_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("STATA_AGENT_MEMORY_EXTRACTION", "provider")
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    monkeypatch.setattr(ui, "_executor", lambda _store: None)
    monkeypatch.setattr(ui, "_rag", lambda: None)
    monkeypatch.setattr(ui, "_privacy_mode", lambda: "local_strict")

    class HeldScheduler:
        def __init__(self):
            self.callbacks = []

        def submit(self, key, callback):
            self.callbacks.append((key, callback))
            return True

    scheduler = HeldScheduler()
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", scheduler)

    class Provider:
        provider = "local"

        def chat(self, _messages, **kwargs):
            if kwargs.get("json_mode"):
                return {"content": json.dumps({"candidates": []})}
            return {"content": "ok", "tool_calls": None}

    monkeypatch.setattr(ui, "_provider", Provider)
    assert ui._run_chat_sync("ui", "以后默认使用中文回答", "interactive")[0] == "ok"

    ledger = SQLiteStore(str(ui.DEFAULT_DB), writer_id="audit", takeover=True)
    events = list(ledger.scan("ui"))
    requested = next(event for event in events if event.event_type == EVENT_MEMORY_EXTRACTION_REQUESTED)
    outbox = ledger.get_outbox(idempotency_key=str(requested.fingerprint))
    assert outbox is not None
    assert outbox.event_seq == requested.seq
    assert outbox.task_type == MEMORY_EXTRACTION_KIND
    assert "text" not in outbox.payload
    ledger.close()

    assert len(scheduler.callbacks) == 1
    scheduler.callbacks[0][1]()
    ledger = SQLiteStore(str(ui.DEFAULT_DB), writer_id="audit-2", takeover=True)
    try:
        terminal = [event for event in ledger.scan("ui") if event.event_type == EVENT_MEMORY_EXTRACTION_NOOP]
        assert len(terminal) == 1
        assert ledger.get_outbox(idempotency_key=str(requested.fingerprint)).status == "completed"
    finally:
        ledger.close()


def test_legacy_requested_backfill_freezes_missing_metadata_to_local_strict(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    ledger = SQLiteStore(str(database), writer_id="legacy")
    ledger.append(Event(
        idea_id="ui",
        event_type=EVENT_USER,
        actor=ACTOR_USER,
        source=ACTOR_USER,
        payload={"text": "以后默认使用中文回答"},
    ))
    request = MemoryExtractionPipeline().prepare(
        store=ledger,
        idea_id="ui",
        workspace_id="workspace-legacy",
    )
    assert request is not None
    ledger.close()

    from stata_agent.application.memory_outbox import SQLiteMemoryOutboxRepository

    adapter = SQLiteMemoryOutboxRepository(
        lambda: SQLiteStore(str(database), writer_id="backfill", takeover=True),
    )
    assert adapter.backfill_outbox_intents(
        kind=MEMORY_EXTRACTION_KIND,
        idea_id="ui",
    ) == 1

    audit = SQLiteStore(str(database), writer_id="audit", takeover=True)
    try:
        record = audit.get_outbox(idempotency_key=request.fingerprint)
        assert record is not None
        assert record.payload["privacy_mode"] == "local_strict"
        assert record.payload["provider_name"] == "local"
        assert "以后默认使用中文回答" not in str(record.payload)
    finally:
        audit.close()
