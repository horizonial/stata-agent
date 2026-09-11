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
    ACTOR_ORCH,
    ACTOR_USER,
    EVENT_IDEA,
    EVENT_AGENT_STEP,
    EVENT_MEMORY_EXTRACTION_COMPLETED,
    EVENT_MEMORY_EXTRACTION_DENIED,
    EVENT_MEMORY_EXTRACTION_FAILED,
    EVENT_MEMORY_EXTRACTION_NOOP,
    EVENT_MEMORY_EXTRACTION_REQUESTED,
    EVENT_PHASE,
    EVENT_USER,
    Event,
)
from stata_agent.memory.pipeline import MemoryExtractionPipeline
from stata_agent.memory.memstore import MemoryStore
from stata_agent.memory.sqlite_repository import SQLiteMemoryRepository
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.storage.store import OutboxIntent


def test_chat_bootstrap_activates_the_explicit_demo_flow(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(str(tmp_path / "demo.sqlite3"), writer_id="demo-test")
    monkeypatch.setenv("STATA_AGENT_DEMO", "1")
    monkeypatch.setattr(ui, "_touch_workspace_name", lambda idea, text: None)
    try:
        ui._chat_bootstrap(store, "demo", "compare specifications")
        assert [event.event_type for event in store.scan("demo")] == [EVENT_IDEA, EVENT_PHASE]
        assert store.project("demo").phase == "ESTIMATION"
    finally:
        store.close()


def test_chat_turn_reuses_ledger_for_workspace_rag_without_fencing_writer(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "chat.sqlite3"
    monkeypatch.setattr(ui, "DEFAULT_DB", database)
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    monkeypatch.setenv("STATA_AGENT_DEMO", "1")
    monkeypatch.setenv("STATA_AGENT_EXECUTOR", "fake")
    monkeypatch.setattr(ui, "_skills", lambda: {})
    monkeypatch.setattr(
        ui,
        "_provider",
        lambda **_kwargs: ui.MockReplayProvider(
            [ui.ActionProposal(decision_summary="先定主 spec")]
        ),
    )

    reply, ask, state = ui._run_chat_sync("ui", "hello", "interactive")

    assert reply == "先定主 spec"
    assert ask is None
    assert state["messages"][-1]["role"] == "assistant"
    store = SQLiteStore(str(database), writer_id="assert", takeover=True)
    try:
        event_types = [event.event_type for event in store.scan("ui")]
        assert EVENT_USER in event_types
        assert event_types[-1] == EVENT_AGENT_STEP
    finally:
        store.close()


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(ui, "DEFAULT_DB", tmp_path / "ledger.sqlite3")
    monkeypatch.setenv("STATA_AGENT_WORKSPACES", str(tmp_path / "workspaces.json"))
    monkeypatch.setattr(ui, "_REQUEST_CONTROL_REGISTRY", RequestControlRegistry())
    return TestClient(ui.app)


def _seed_failed_outbox(
    tmp_path,
    *,
    idea: str = "ui",
    workspace_id: str | None = None,
    key: str = "sha256:operator-failed",
    task_type: str = MEMORY_EXTRACTION_KIND,
    error: str = "provider_error",
    payload_updates: dict[str, object] | None = None,
):
    """Create one durable failed row without invoking the scheduler/provider."""

    workspace_id = workspace_id or ui._workspace_id(idea)
    payload = {
        "idea_id": idea,
        "workspace_id": workspace_id,
        "fingerprint": key,
        "kind": task_type,
        "from_seq": 0,
        "to_seq": 0,
        "source_ids": [],
        "prompt_version": "memory-extraction-v1",
        "privacy_mode": "local_strict",
        "provider_name": "local",
        "raw_text": "PRIVATE_SOURCE_SHOULD_NOT_ESCAPE",
        "prompt": "SECRET_PROMPT_SHOULD_NOT_ESCAPE",
        "provider_response": "SECRET_PROVIDER_RESPONSE_SHOULD_NOT_ESCAPE",
    }
    if payload_updates:
        payload.update(payload_updates)
    event = Event(
        idea_id=idea,
        event_type=EVENT_MEMORY_EXTRACTION_REQUESTED,
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        payload={
            "workspace_id": workspace_id,
            "fingerprint": key,
            "from_seq": 0,
            "to_seq": 0,
            "source_ids": [],
        },
        fingerprint=key,
    )
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"), writer_id="seed", takeover=True)
    try:
        store.append_with_outbox(
            event,
            OutboxIntent(
                idempotency_key=key,
                task_type=task_type,
                payload=payload,
                available_at=0,
                max_attempts=1,
            ),
        )
        claim = store.claim_one(
            "seed-worker",
            task_type=task_type,
            lease_seconds=60,
            now=100,
        )
        assert claim is not None
        failed = store.retry_outbox(claim, claim.lease_token, error=error, now=101)
        return failed
    finally:
        store.close()


def _append_terminal(tmp_path, *, idea: str, workspace_id: str, key: str, event_type: str) -> None:
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"), writer_id="terminal", takeover=True)
    try:
        store.append(Event(
            idea_id=idea,
            event_type=event_type,
            actor=ACTOR_ORCH,
            source=ACTOR_ORCH,
            payload={"workspace_id": workspace_id, "fingerprint": key},
            fingerprint=key,
        ))
    finally:
        store.close()


def _seed_completed_outbox(
    tmp_path,
    *,
    idea: str = "ui",
    key: str = "sha256:retention-completed",
    completed_at: int = 100,
    terminal: bool = True,
):
    workspace_id = ui._workspace_id(idea)
    payload = {
        "idea_id": idea,
        "workspace_id": workspace_id,
        "fingerprint": key,
        "kind": MEMORY_EXTRACTION_KIND,
        "from_seq": 0,
        "to_seq": 0,
        "source_ids": [],
        "prompt_version": "memory-extraction-v1",
        "privacy_mode": "local_strict",
        "provider_name": "local",
    }
    event = Event(
        idea_id=idea,
        event_type=EVENT_MEMORY_EXTRACTION_REQUESTED,
        actor=ACTOR_ORCH,
        source=ACTOR_ORCH,
        payload={
            "workspace_id": workspace_id,
            "fingerprint": key,
            "from_seq": 0,
            "to_seq": 0,
            "source_ids": [],
        },
        fingerprint=key,
    )
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"), writer_id="seed", takeover=True)
    try:
        store.append_with_outbox(
            event,
            OutboxIntent(
                idempotency_key=key,
                task_type=MEMORY_EXTRACTION_KIND,
                payload=payload,
                available_at=0,
                max_attempts=3,
            ),
        )
        claim = store.claim_one("seed-worker", task_type=MEMORY_EXTRACTION_KIND, lease_seconds=1_000, now=10)
        assert claim is not None
        completed = store.complete_outbox(claim, now=completed_at)
    finally:
        store.close()
    if terminal:
        _append_terminal(
            tmp_path,
            idea=idea,
            workspace_id=workspace_id,
            key=key,
            event_type=EVENT_MEMORY_EXTRACTION_COMPLETED,
        )
    return completed


def test_memory_outbox_operator_lists_safe_failed_items_and_redrives_without_io(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    failed = _seed_failed_outbox(tmp_path, error="API_KEY=do-not-return")
    provider_calls = []

    class Provider:
        def chat(self, *args, **kwargs):
            provider_calls.append((args, kwargs))
            return {"content": "unexpected"}

    class Scheduler:
        def __init__(self):
            self.calls = []

        def submit(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return True

    scheduler = Scheduler()
    monkeypatch.setattr(ui, "_provider", lambda: Provider())
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", scheduler)

    listed = client.get(
        "/api/operations/memory-outbox/failed",
        params={"ws": "ui", "limit": "1"},
    )
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["workspace"] == "ui"
    assert payload["workspace_id"] == ui._workspace_id("ui")
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["idempotency_key"] == failed.idempotency_key
    assert item["state_version"] == failed.state_version
    assert item["error_code"] == "delivery_failed"
    assert item["terminal_present"] is False
    assert set(item) == {
        "outbox_id",
        "idempotency_key",
        "fingerprint",
        "idea_id",
        "workspace_id",
        "attempt_count",
        "max_attempts",
        "created_at",
        "updated_at",
        "state_version",
        "error_code",
        "terminal_present",
    }
    assert "PRIVATE_SOURCE_SHOULD_NOT_ESCAPE" not in listed.text
    assert "SECRET_PROMPT_SHOULD_NOT_ESCAPE" not in listed.text
    assert "API_KEY=do-not-return" not in listed.text

    recovered = client.post(
        "/api/operations/memory-outbox/recover",
        params={"ws": "ui"},
        json={
            "idempotency_key": failed.idempotency_key,
            "expected_state_version": failed.state_version,
            "acknowledge_at_least_once": True,
        },
    )
    assert recovered.status_code == 200
    assert recovered.json()["outcome"] == "redriven"
    assert recovered.json()["status"] == "pending"
    assert recovered.json()["item"]["attempt_count"] == 0
    assert recovered.json()["item"]["state_version"] == failed.state_version + 1
    assert scheduler.calls == []
    assert provider_calls == []

    audit = SQLiteStore(str(ui.DEFAULT_DB), writer_id="audit", takeover=True)
    try:
        current = audit.get_outbox(idempotency_key=failed.idempotency_key)
        assert current is not None
        assert current.status == "pending"
        assert current.attempt_count == 0
        assert current.max_attempts == failed.max_attempts
    finally:
        audit.close()


@pytest.mark.parametrize(
    "event_type",
    [
        EVENT_MEMORY_EXTRACTION_COMPLETED,
        EVENT_MEMORY_EXTRACTION_NOOP,
        EVENT_MEMORY_EXTRACTION_FAILED,
        EVENT_MEMORY_EXTRACTION_DENIED,
    ],
)
def test_memory_outbox_operator_reconciles_matching_terminal_without_provider(
    tmp_path,
    monkeypatch,
    event_type,
):
    client = _client(tmp_path, monkeypatch)
    key = f"sha256:terminal-{event_type.rsplit('.', 1)[-1]}"
    failed = _seed_failed_outbox(tmp_path, key=key, error="provider_error")
    _append_terminal(
        tmp_path,
        idea="ui",
        workspace_id=ui._workspace_id("ui"),
        key=key,
        event_type=event_type,
    )
    provider_calls = []
    scheduler_calls = []
    monkeypatch.setattr(ui, "_provider", lambda: provider_calls.append(True))
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", scheduler_calls)

    listed = client.get("/api/operations/memory-outbox/failed", params={"ws": "ui"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["terminal_present"] is True

    recovered = client.post(
        "/api/operations/memory-outbox/recover",
        params={"ws": "ui"},
        json={
            "idempotency_key": key,
            "expected_state_version": failed.state_version,
            "acknowledge_at_least_once": True,
        },
    )
    assert recovered.status_code == 200
    assert recovered.json()["outcome"] == "reconciled"
    assert recovered.json()["status"] == "completed"
    assert recovered.json()["item"]["terminal_present"] is True
    assert provider_calls == []
    assert scheduler_calls == []

    audit = SQLiteStore(str(ui.DEFAULT_DB), writer_id="audit", takeover=True)
    try:
        current = audit.get_outbox(idempotency_key=key)
        assert current is not None
        assert current.status == "completed"
        assert current.attempt_count == failed.attempt_count
    finally:
        audit.close()


def test_memory_outbox_operator_isolates_workspaces_and_hides_cross_scope_recovery(
    tmp_path,
    monkeypatch,
):
    client = _client(tmp_path, monkeypatch)
    created = client.post("/api/workspaces", json={"id": "other", "name": "Other"})
    assert created.status_code == 200
    other_key = "sha256:other-workspace"
    other = _seed_failed_outbox(
        tmp_path,
        idea="other",
        workspace_id=ui._workspace_id("other"),
        key=other_key,
    )

    own = client.get("/api/operations/memory-outbox/failed", params={"ws": "ui"})
    assert own.status_code == 200
    assert other_key not in {item["idempotency_key"] for item in own.json()["items"]}

    selected = client.get("/api/operations/memory-outbox/failed", params={"ws": "other"})
    assert selected.status_code == 200
    assert [item["idempotency_key"] for item in selected.json()["items"]] == [other_key]

    hidden = client.post(
        "/api/operations/memory-outbox/recover",
        params={"ws": "ui"},
        json={
            "idempotency_key": other_key,
            "expected_state_version": other.state_version,
            "acknowledge_at_least_once": True,
        },
    )
    assert hidden.status_code == 404
    assert other_key not in hidden.text


@pytest.mark.parametrize("limit", ["0", "101", "1.0", "true", "abc"])
def test_memory_outbox_operator_rejects_invalid_limits(tmp_path, monkeypatch, limit):
    client = _client(tmp_path, monkeypatch)
    response = client.get(
        "/api/operations/memory-outbox/failed",
        params={"ws": "ui", "limit": limit},
    )
    assert response.status_code == 422
    assert "limit" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"idempotency_key": "sha256:ack", "expected_state_version": 0},
        {
            "idempotency_key": "sha256:ack",
            "expected_state_version": 0,
            "acknowledge_at_least_once": False,
        },
        {
            "idempotency_key": "sha256:ack",
            "expected_state_version": 0,
            "acknowledge_at_least_once": 1,
        },
        {
            "idempotency_key": "sha256:ack",
            "expected_state_version": 0,
            "acknowledge_at_least_once": True,
            "force": True,
        },
    ],
)
def test_memory_outbox_operator_requires_strict_ack_and_exact_body(tmp_path, monkeypatch, body):
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/api/operations/memory-outbox/recover",
        params={"ws": "ui"},
        json=body,
    )
    assert response.status_code == 422
    assert "PRIVATE_SOURCE_SHOULD_NOT_ESCAPE" not in response.text


def test_memory_outbox_operator_maps_missing_stale_and_payload_conflicts_safely(
    tmp_path,
    monkeypatch,
):
    client = _client(tmp_path, monkeypatch)
    failed = _seed_failed_outbox(tmp_path, error="SECRET_PROVIDER_RESPONSE")
    request = {
        "idempotency_key": failed.idempotency_key,
        "expected_state_version": failed.state_version,
        "acknowledge_at_least_once": True,
    }

    missing = client.post(
        "/api/operations/memory-outbox/recover",
        params={"ws": "ui"},
        json={**request, "idempotency_key": "sha256:missing"},
    )
    assert missing.status_code == 404
    assert "SECRET_PROVIDER_RESPONSE" not in missing.text

    stale = client.post(
        "/api/operations/memory-outbox/recover",
        params={"ws": "ui"},
        json={**request, "expected_state_version": failed.state_version + 1},
    )
    assert stale.status_code == 409
    assert "SECRET_PROVIDER_RESPONSE" not in stale.text

    malformed = _seed_failed_outbox(
        tmp_path,
        key="sha256:malformed-api",
        payload_updates={"fingerprint": "sha256:not-the-key"},
    )
    bad = client.post(
        "/api/operations/memory-outbox/recover",
        params={"ws": "ui"},
        json={
            "idempotency_key": malformed.idempotency_key,
            "expected_state_version": malformed.state_version,
            "acknowledge_at_least_once": True,
        },
    )
    assert bad.status_code == 409
    assert "SECRET_PROVIDER_RESPONSE" not in bad.text


def test_memory_outbox_operator_double_submit_has_one_success(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    failed = _seed_failed_outbox(tmp_path)
    body = {
        "idempotency_key": failed.idempotency_key,
        "expected_state_version": failed.state_version,
        "acknowledge_at_least_once": True,
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _index: client.post(
                    "/api/operations/memory-outbox/recover",
                    params={"ws": "ui"},
                    json=body,
                ),
                range(2),
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_memory_outbox_retention_preview_and_prune_are_terminal_scoped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    first = _seed_completed_outbox(tmp_path, key="sha256:retention-first", completed_at=100)
    second = _seed_completed_outbox(tmp_path, key="sha256:retention-second", completed_at=101)
    blocked = _seed_completed_outbox(
        tmp_path,
        key="sha256:retention-blocked",
        completed_at=102,
        terminal=False,
    )
    failed = _seed_failed_outbox(tmp_path, key="sha256:retention-failed")
    scheduler_calls: list[object] = []
    monkeypatch.setattr(ui, "_MEMORY_EXTRACTION_SCHEDULER", scheduler_calls)

    preview = client.get(
        "/api/operations/memory-outbox/retention/preview",
        params={"ws": "ui", "retention_days": "7", "limit": "100"},
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["workspace"] == "ui"
    assert body["eligible_count"] == 2
    assert body["blocked_count"] == 1
    assert [item["idempotency_key"] for item in body["items"]] == [
        first.idempotency_key,
        second.idempotency_key,
    ]
    assert set(body["items"][0]) == {
        "outbox_id",
        "idempotency_key",
        "fingerprint",
        "idea_id",
        "workspace_id",
        "completed_at",
        "state_version",
    }

    pruned = client.post(
        "/api/operations/memory-outbox/retention/prune",
        params={"ws": "ui"},
        json={
            "cutoff": body["cutoff"],
            "limit": body["limit"],
            "selection_token": body["selection_token"],
            "acknowledge_irreversible_delete": True,
        },
    )
    assert pruned.status_code == 200
    assert pruned.json()["outcome"] == "pruned"
    assert pruned.json()["deleted_count"] == 2
    assert scheduler_calls == []

    audit = SQLiteStore(str(ui.DEFAULT_DB), writer_id="audit", takeover=True)
    try:
        assert audit.get_outbox(idempotency_key=first.idempotency_key) is None
        assert audit.get_outbox(idempotency_key=second.idempotency_key) is None
        assert audit.get_outbox(idempotency_key=blocked.idempotency_key) is not None
        assert audit.get_outbox(idempotency_key=failed.idempotency_key) is not None
        terminal_keys = {
            event.fingerprint
            for event in audit.scan("ui")
            if event.event_type == EVENT_MEMORY_EXTRACTION_COMPLETED
        }
        assert first.idempotency_key in terminal_keys
        assert second.idempotency_key in terminal_keys
    finally:
        audit.close()


@pytest.mark.parametrize(
    "params",
    [
        {"retention_days": "6"},
        {"retention_days": "3651"},
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "true"},
    ],
)
def test_memory_outbox_retention_preview_rejects_invalid_bounds(tmp_path, monkeypatch, params):
    client = _client(tmp_path, monkeypatch)
    response = client.get(
        "/api/operations/memory-outbox/retention/preview",
        params={"ws": "ui", **params},
    )
    assert response.status_code == 422
    assert "PRIVATE_SOURCE_SHOULD_NOT_ESCAPE" not in response.text


def test_memory_outbox_retention_prune_requires_ack_and_fresh_selection(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _seed_completed_outbox(tmp_path, key="sha256:retention-ack", completed_at=100)
    preview = client.get(
        "/api/operations/memory-outbox/retention/preview",
        params={"ws": "ui", "retention_days": "7"},
    ).json()
    invalid_ack = client.post(
        "/api/operations/memory-outbox/retention/prune",
        params={"ws": "ui"},
        json={
            "cutoff": preview["cutoff"],
            "limit": preview["limit"],
            "selection_token": preview["selection_token"],
            "acknowledge_irreversible_delete": False,
        },
    )
    assert invalid_ack.status_code == 422
    stale = client.post(
        "/api/operations/memory-outbox/retention/prune",
        params={"ws": "ui"},
        json={
            "cutoff": preview["cutoff"],
            "limit": preview["limit"],
            "selection_token": "v1:stale",
            "acknowledge_irreversible_delete": True,
        },
    )
    assert stale.status_code == 409


def test_memory_outbox_retention_double_submit_has_one_success(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _seed_completed_outbox(tmp_path, key="sha256:retention-double", completed_at=100)
    preview = client.get(
        "/api/operations/memory-outbox/retention/preview",
        params={"ws": "ui", "retention_days": "7"},
    ).json()
    body = {
        "cutoff": preview["cutoff"],
        "limit": preview["limit"],
        "selection_token": preview["selection_token"],
        "acknowledge_irreversible_delete": True,
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _index: client.post(
                    "/api/operations/memory-outbox/retention/prune",
                    params={"ws": "ui"},
                    json=body,
                ),
                range(2),
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_diagnostic_bundle_endpoint_is_bounded_metadata_only_and_read_only(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    secret = "DIAGNOSTIC_ENDPOINT_SECRET"
    store = SQLiteStore(str(ui.DEFAULT_DB), writer_id="seed", takeover=True)
    try:
        store.append(Event(
            idea_id="ui",
            event_type=EVENT_USER,
            actor=ACTOR_USER,
            source=ACTOR_USER,
            correlation_id="req-api-1",
            payload={"text": secret, "prompt": secret},
        ))
    finally:
        store.close()

    provider_calls: list[object] = []
    monkeypatch.setattr(ui, "_provider", lambda: provider_calls.append(True))
    response = client.get(
        "/api/operations/diagnostics/bundle",
        params={"ws": "ui", "event_limit": "1", "outbox_limit": "1", "request_id": "req-api-1"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    assert body["manifest"]["schema"] == "diagnostic.bundle.v1"
    assert body["manifest"]["limits"] == {"event_limit": 1, "outbox_limit": 1}
    assert body["manifest"]["request_filter_applied"] is True
    assert secret not in response.text
    assert provider_calls == []

    for key, value in (("event_limit", "0"), ("event_limit", "501"), ("outbox_limit", "201"), ("event_limit", "true")):
        invalid = client.get("/api/operations/diagnostics/bundle", params={"ws": "ui", key: value})
        assert invalid.status_code == 422
        assert secret not in invalid.text

    assert client.get("/api/operations/diagnostics/bundle", params={"ws": "missing"}).status_code == 404


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
    user_event = next(event for event in events if event.event_type == EVENT_USER)
    assert user_event.correlation_id == user_event.payload["request_id"]
    assert requested.correlation_id == user_event.correlation_id
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
