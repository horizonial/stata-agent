"""Contract tests for durable memory-extraction dispatch."""

from __future__ import annotations

import time
import threading
import sqlite3
from dataclasses import replace

import pytest

from stata_agent.application import (
    FakeMemoryOutboxRepository,
    MemoryOutboxDispatcher,
    MemoryOutboxDeferred,
    MemoryOutboxIntent,
    MEMORY_EXTRACTION_KIND,
    SQLiteMemoryOutboxRepository,
    TaskSubmitResult,
    TaskSubmitStatus,
)
from stata_agent.application.local_task_queue import LocalTaskQueue
from stata_agent.application.outbox_retention import (
    OutboxRetentionPreviewRequest,
    OutboxRetentionPruneRequest,
    OutboxRetentionService,
)
from stata_agent.events.schema import (
    EVENT_MEMORY_EXTRACTION_COMPLETED,
    EVENT_MEMORY_EXTRACTION_REQUESTED,
    Event,
)
from stata_agent.memory.pipeline import MemoryExtractionRequest, MemorySource
from stata_agent.storage.sqlite_store import SQLiteStore


def _intent(*, fingerprint: str = "sha256:memory-1") -> MemoryOutboxIntent:
    request = MemoryExtractionRequest(
        idea_id="idea-1",
        workspace_id="workspace-1",
        from_seq=2,
        to_seq=4,
        sources=(MemorySource("seq:2", "user", "以后默认使用中文回答"),),
        fingerprint=fingerprint,
    )
    return MemoryOutboxIntent.from_request(
        request,
        privacy_mode="local_strict",
        provider_name="local",
    )


class _ManualQueue:
    def __init__(self, status: TaskSubmitStatus = TaskSubmitStatus.ACCEPTED):
        self.status = status
        self.callbacks = []

    def submit(self, key, callback):
        if self.status is TaskSubmitStatus.ACCEPTED:
            self.callbacks.append((key, callback))
        return TaskSubmitResult(self.status, key)

    def run_next(self):
        _key, callback = self.callbacks.pop(0)
        callback()

    def shutdown(self, *, wait=True, timeout=None):
        del wait, timeout


def _dispatcher(repository, queue, *, worker, terminal_checker, **kwargs):
    return MemoryOutboxDispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=terminal_checker,
        owner="test-worker",
        claim_limit=4,
        lease_seconds=kwargs.pop("lease_seconds", 5),
        **kwargs,
    )


def _memory_event(intent: MemoryOutboxIntent, event_type: str) -> Event:
    payload = intent.as_payload()
    payload["status"] = "requested" if event_type == EVENT_MEMORY_EXTRACTION_REQUESTED else "completed"
    return Event(
        idea_id=intent.idea_id,
        event_type=event_type,
        actor="orchestrator",
        source="orchestrator",
        payload=payload,
        fingerprint=intent.fingerprint,
    )


def test_intent_uses_fingerprint_as_queue_key_and_does_not_store_source_text():
    intent = _intent()

    assert intent.kind == MEMORY_EXTRACTION_KIND
    assert intent.key == intent.fingerprint
    assert intent.as_payload()["source_ids"] == ["seq:2"]
    assert "以后默认使用中文回答" not in str(intent.as_payload())


def test_ensure_is_idempotent_and_reconcile_delegates_to_store():
    repository = FakeMemoryOutboxRepository()
    repository.backfill_count = 3
    queue = LocalTaskQueue(max_pending=1)
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda _claim: None,
        terminal_checker=lambda _claim: False,
    )

    assert dispatcher.ensure_intent(_intent()) is True
    assert dispatcher.ensure_intent(_intent()) is False
    assert dispatcher.reconcile(idea_id="idea-1") == 3
    queue.shutdown(wait=True)


def test_terminal_event_is_required_before_ack():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1)
    finished = threading.Event()

    def worker(claim):
        repository.terminal.add(claim.key)
        finished.set()

    dispatcher = _dispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=lambda claim: claim.key in repository.terminal,
    )
    dispatcher.ensure_intent(_intent())

    report = dispatcher.pump()
    assert report.claimed == report.submitted == 1
    assert finished.wait(1)
    queue.shutdown(wait=True)
    assert repository.status[_intent().key] == "completed"
    assert repository.completions == [_intent().key]


def test_missing_terminal_event_retries_and_preserves_pending():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1)
    finished = threading.Event()
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda _claim: finished.set(),
        terminal_checker=lambda _claim: False,
    )
    dispatcher.ensure_intent(_intent())

    assert dispatcher.pump().submitted == 1
    assert finished.wait(1)
    queue.shutdown(wait=True)
    assert repository.status[_intent().key] == "pending"
    assert repository.retries == [(_intent().key, "terminal_event_missing")]


def test_full_queue_releases_claim_back_to_pending():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1, shutdown_timeout=0.2)
    started = threading.Event()
    release = threading.Event()

    def blocking():
        started.set()
        release.wait(1)

    assert queue.submit("running", blocking).accepted
    assert started.wait(1)
    assert queue.submit("occupied", lambda: None).accepted
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda _claim: None,
        terminal_checker=lambda _claim: False,
    )
    dispatcher.ensure_intent(_intent())

    report = dispatcher.pump()
    assert report.claimed == 1
    assert report.submitted == 0
    assert report.retained_pending == 1
    assert repository.status[_intent().key] == "pending"
    assert repository.retries == []
    assert repository.releases == [(_intent().key, "queue_full")]

    release.set()
    queue.shutdown(wait=True)


def test_closed_queue_releases_claim_back_to_pending():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1)
    queue.shutdown(wait=True)
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda _claim: None,
        terminal_checker=lambda _claim: False,
    )
    dispatcher.ensure_intent(_intent())

    report = dispatcher.pump()
    assert report.claimed == 1
    assert report.submitted == 0
    assert report.retained_pending == 1
    assert repository.status[_intent().key] == "pending"
    assert repository.retries == []
    assert repository.releases == [(_intent().key, "queue_closed")]


def test_runtime_policy_deferral_releases_without_recording_execution_failure():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1)
    finished = threading.Event()

    def worker(_claim):
        finished.set()
        raise MemoryOutboxDeferred("provider_identity_mismatch")

    dispatcher = _dispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=lambda _claim: False,
    )
    dispatcher.ensure_intent(_intent())

    assert dispatcher.pump().submitted == 1
    assert finished.wait(1)
    queue.shutdown(wait=True)
    assert repository.status[_intent().key] == "pending"
    assert repository.retries == []
    assert repository.releases == [(_intent().key, "runtime_deferred")]


def test_claim_freezes_privacy_and_provider_identity_for_worker():
    repository = FakeMemoryOutboxRepository()
    queue = LocalTaskQueue(max_pending=1)
    seen = []
    finished = threading.Event()
    intent = MemoryOutboxIntent(
        idea_id="idea-1",
        workspace_id="workspace-1",
        fingerprint="sha256:memory-2",
        from_seq=1,
        to_seq=1,
        source_ids=("seq:1",),
        prompt_version="memory-v1",
        privacy_mode="mixed_sanitized",
        provider_name="deepseek",
    )

    def worker(claim):
        seen.append((claim.intent.privacy_mode, claim.intent.provider_name))
        repository.terminal.add(claim.key)
        finished.set()

    dispatcher = _dispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=lambda claim: claim.key in repository.terminal,
    )
    dispatcher.ensure_intent(intent)
    assert dispatcher.pump().submitted == 1
    assert finished.wait(1)
    queue.shutdown(wait=True)

    assert seen == [("mixed_sanitized", "deepseek")]
    assert repository.status[intent.key] == "completed"


def test_fake_repository_renew_is_owned_and_does_not_reclaim_before_expiry():
    clock = [100.0]
    repository = FakeMemoryOutboxRepository(clock=lambda: clock[0])
    intent = _intent(fingerprint="sha256:renew-fake")
    assert repository.ensure_outbox_intent(intent)
    claim = repository.claim(
        MEMORY_EXTRACTION_KIND,
        "worker-a",
        lease_seconds=5,
    )[0]

    assert repository.renew(
        MEMORY_EXTRACTION_KIND,
        claim.key,
        claim.lease_token,
        5,
    ) is True
    assert repository.renewals == [(claim.key, 5.0)]
    assert repository.claim(MEMORY_EXTRACTION_KIND, "worker-b", lease_seconds=5) == []

    clock[0] = 106.0
    assert repository.renew(
        MEMORY_EXTRACTION_KIND,
        claim.key,
        claim.lease_token,
        5,
    ) is False


def test_sqlite_repository_renew_maps_definite_lease_loss_and_closes_store(tmp_path):
    database = str(tmp_path / "adapter-renew.sqlite3")
    opened: list[SQLiteStore] = []

    def factory() -> SQLiteStore:
        store = SQLiteStore(database, writer_id=f"worker-{len(opened)}", takeover=True)
        opened.append(store)
        return store

    repository = SQLiteMemoryOutboxRepository(factory)
    intent = _intent(fingerprint="sha256:renew-sqlite")
    assert repository.ensure_outbox_intent(intent)
    claim = repository.claim(
        MEMORY_EXTRACTION_KIND,
        "worker-a",
        lease_seconds=30,
    )[0]

    assert repository.renew(
        MEMORY_EXTRACTION_KIND,
        claim.key,
        claim.lease_token,
        30,
    ) is True
    assert repository.renew(
        MEMORY_EXTRACTION_KIND,
        claim.key,
        "wrong-token",
        30,
    ) is False

    for store in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            store.connection.execute("SELECT 1")


def test_sqlite_repository_renew_preserves_unexpected_storage_errors_and_closes():
    closed = []

    class StubStore:
        def get_outbox(self, *, idempotency_key):
            del idempotency_key
            return type("Record", (), {"task_type": MEMORY_EXTRACTION_KIND})()

        def renew_outbox_lease(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("database unavailable")

        def close(self):
            closed.append(True)

    repository = SQLiteMemoryOutboxRepository(lambda: StubStore())
    with pytest.raises(RuntimeError, match="database unavailable"):
        repository.renew(MEMORY_EXTRACTION_KIND, "key", "token", 5)
    assert closed == [True]


def test_heartbeat_protects_queued_claim_past_original_lease_and_stops_after_completion():
    clock = [0.0]
    repository = FakeMemoryOutboxRepository(clock=lambda: clock[0])
    queue = _ManualQueue()
    finished = threading.Event()

    def worker(claim):
        repository.terminal.add(claim.key)
        finished.set()

    dispatcher = _dispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=lambda claim: claim.key in repository.terminal,
        lease_seconds=1,
        heartbeat_seconds=0.01,
    )
    dispatcher.ensure_intent(_intent(fingerprint="sha256:queued-heartbeat"))
    assert dispatcher.pump().submitted == 1
    assert repository.renew_event.wait(1)

    clock[0] = 0.8
    repository.renew_event.clear()
    assert repository.renew_event.wait(1)
    clock[0] = 1.2
    assert repository.claim(MEMORY_EXTRACTION_KIND, "worker-b", lease_seconds=1) == []

    queue.run_next()
    assert finished.is_set()
    assert repository.status["sha256:queued-heartbeat"] == "completed"
    count = len(repository.renewals)
    repository.renew_event.clear()
    time.sleep(0.03)
    assert len(repository.renewals) == count


def test_heartbeat_protects_long_worker_and_recovery_reclaims_after_it_stops():
    clock = [0.0]
    repository = FakeMemoryOutboxRepository(clock=lambda: clock[0])
    queue = _ManualQueue()
    started = threading.Event()
    release = threading.Event()

    def worker(claim):
        started.set()
        release.wait(1)
        repository.terminal.add(claim.key)

    dispatcher = _dispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=lambda claim: claim.key in repository.terminal,
        lease_seconds=1,
        heartbeat_seconds=0.01,
    )
    dispatcher.ensure_intent(_intent(fingerprint="sha256:running-heartbeat"))
    assert dispatcher.pump().submitted == 1

    callback_thread = threading.Thread(target=queue.run_next)
    callback_thread.start()
    assert started.wait(1)
    clock[0] = 0.8
    repository.renew_event.clear()
    assert repository.renew_event.wait(1)
    clock[0] = 1.2
    assert repository.claim(MEMORY_EXTRACTION_KIND, "worker-b", lease_seconds=1) == []

    release.set()
    callback_thread.join(1)
    assert not callback_thread.is_alive()
    assert repository.status["sha256:running-heartbeat"] == "completed"

    # Simulate a process that stopped heartbeats while a claim was active.
    repository.ensure_outbox_intent(_intent(fingerprint="sha256:crashed-heartbeat"))
    crashed = repository.claim(MEMORY_EXTRACTION_KIND, "worker-a", lease_seconds=1)[0]
    clock[0] = 2.3
    recovered = repository.claim(MEMORY_EXTRACTION_KIND, "worker-b", lease_seconds=1)
    assert recovered and recovered[0].key == crashed.key


def test_lease_loss_before_worker_start_skips_worker_and_stale_callback_transition():
    clock = [0.0]
    repository = FakeMemoryOutboxRepository(clock=lambda: clock[0])
    queue = _ManualQueue()
    worker_calls = []
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda claim: worker_calls.append(claim.key),
        terminal_checker=lambda _claim: False,
        lease_seconds=1,
        heartbeat_seconds=0.01,
    )
    dispatcher.ensure_intent(_intent(fingerprint="sha256:lost-before-start"))
    assert dispatcher.pump().submitted == 1
    clock[0] = 2.0
    assert repository.renew_lost_event.wait(1)

    queue.run_next()
    assert worker_calls == []
    assert repository.retries == []
    assert repository.releases == []
    assert repository.claim(MEMORY_EXTRACTION_KIND, "worker-b", lease_seconds=1)


def test_lease_loss_during_worker_does_not_ack_or_retry_with_stale_token():
    clock = [0.0]
    repository = FakeMemoryOutboxRepository(clock=lambda: clock[0])
    queue = _ManualQueue()
    started = threading.Event()
    release = threading.Event()

    def worker(_claim):
        started.set()
        release.wait(1)

    dispatcher = _dispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=lambda _claim: False,
        lease_seconds=1,
        heartbeat_seconds=0.01,
    )
    dispatcher.ensure_intent(_intent(fingerprint="sha256:lost-during-work"))
    assert dispatcher.pump().submitted == 1
    callback_thread = threading.Thread(target=queue.run_next)
    callback_thread.start()
    assert started.wait(1)
    clock[0] = 2.0
    assert repository.renew_lost_event.wait(1)
    release.set()
    callback_thread.join(1)
    assert not callback_thread.is_alive()
    assert repository.completions == []
    assert repository.retries == []
    assert repository.releases == []
    assert repository.claim(MEMORY_EXTRACTION_KIND, "worker-b", lease_seconds=1)


def test_terminal_precheck_completes_recovery_claim_without_worker_call():
    repository = FakeMemoryOutboxRepository()
    queue = _ManualQueue()
    worker_calls = []
    intent = _intent(fingerprint="sha256:terminal-precheck")
    repository.ensure_outbox_intent(intent)
    repository.terminal.add(intent.key)
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda _claim: worker_calls.append(True),
        terminal_checker=lambda claim: claim.key in repository.terminal,
        heartbeat_seconds=0.01,
    )

    assert dispatcher.pump().submitted == 1
    queue.run_next()
    assert worker_calls == []
    assert repository.status[intent.key] == "completed"


def test_heartbeat_retries_transient_renewal_failure_and_validates_interval():
    repository = FakeMemoryOutboxRepository()
    queue = _ManualQueue()
    repository.renew_exception = RuntimeError("temporary storage failure")
    dispatcher = _dispatcher(
        repository,
        queue,
        worker=lambda claim: repository.terminal.add(claim.key),
        terminal_checker=lambda claim: claim.key in repository.terminal,
        lease_seconds=1,
        heartbeat_seconds=0.01,
    )
    dispatcher.ensure_intent(_intent(fingerprint="sha256:transient-renew"))
    assert dispatcher.pump().submitted == 1
    assert repository.renew_event.wait(1)
    queue.run_next()
    assert repository.status["sha256:transient-renew"] == "completed"

    with pytest.raises(ValueError, match="shorter than"):
        _dispatcher(
            FakeMemoryOutboxRepository(),
            _ManualQueue(),
            worker=lambda _claim: None,
            terminal_checker=lambda _claim: False,
            lease_seconds=1,
            heartbeat_seconds=1,
        )


def test_sqlite_dispatcher_heartbeat_terminal_recovery_and_expired_reclaim(tmp_path, monkeypatch):
    """Exercise the lease protocol through SQLiteStore and the real local queue."""

    database = str(tmp_path / "dispatcher-recovery.sqlite3")
    first = _intent(fingerprint="sha256:sqlite-heartbeat")
    recovered_intent = _intent(fingerprint="sha256:sqlite-crash")
    terminal_before_dispatch = _intent(fingerprint="sha256:sqlite-terminal")

    # Keep all adapter calls on a deterministic clock.  Explicit ``now`` values
    # below model lease expiry without sleeping for a real second.
    monkeypatch.setattr(
        SQLiteStore,
        "_now",
        staticmethod(lambda value: 100 if value is None else int(value)),
    )
    seed = SQLiteStore(database, writer_id="seed", takeover=True)
    try:
        seed.append_with_outbox(
            _memory_event(first, EVENT_MEMORY_EXTRACTION_REQUESTED),
            replace(SQLiteMemoryOutboxRepository.to_storage_intent(first), available_at=100),
        )
        seed.append_with_outbox(
            _memory_event(terminal_before_dispatch, EVENT_MEMORY_EXTRACTION_REQUESTED),
            replace(
                SQLiteMemoryOutboxRepository.to_storage_intent(terminal_before_dispatch),
                available_at=100,
            ),
        )
        seed.append(
            _memory_event(terminal_before_dispatch, EVENT_MEMORY_EXTRACTION_COMPLETED),
        )
    finally:
        seed.close()

    renewed = threading.Event()
    renewed_keys: set[str] = set()
    renewed_lock = threading.Lock()
    original_renew = SQLiteStore.renew_outbox_lease

    def instrumented_renew(store, item, *args, **kwargs):
        result = original_renew(store, item, *args, **kwargs)
        key = getattr(item, "idempotency_key", None)
        if key is not None:
            with renewed_lock:
                renewed_keys.add(str(key))
            renewed.set()
        return result

    monkeypatch.setattr(SQLiteStore, "renew_outbox_lease", instrumented_renew)
    factory_calls = [0]

    def factory() -> SQLiteStore:
        writer_id = f"adapter-{factory_calls[0]}"
        factory_calls[0] += 1
        return SQLiteStore(database, writer_id=writer_id, takeover=True)

    repository = SQLiteMemoryOutboxRepository(factory)
    queue = LocalTaskQueue(max_pending=1, shutdown_timeout=1.0)
    first_started = threading.Event()
    release_first = threading.Event()
    worker_calls: list[str] = []

    def worker(claim):
        worker_calls.append(claim.key)
        if claim.key == first.key:
            first_started.set()
            assert release_first.wait(1)
            store = SQLiteStore(database, writer_id="worker", takeover=True)
            try:
                store.append(_memory_event(first, EVENT_MEMORY_EXTRACTION_COMPLETED))
            finally:
                store.close()

    def terminal_checker(claim):
        store = SQLiteStore(database, writer_id="checker", takeover=True)
        try:
            return any(
                event.event_type == EVENT_MEMORY_EXTRACTION_COMPLETED
                and event.fingerprint == claim.key
                for event in store.scan(claim.intent.idea_id)
            )
        finally:
            store.close()

    dispatcher = _dispatcher(
        repository,
        queue,
        worker=worker,
        terminal_checker=terminal_checker,
        lease_seconds=1,
        heartbeat_seconds=0.01,
    )
    report = dispatcher.pump(limit=2)
    assert (report.claimed, report.submitted) == (2, 2)
    assert first_started.wait(1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with renewed_lock:
            if first.key in renewed_keys:
                break
        renewed.wait(0.02)
        renewed.clear()
    with renewed_lock:
        assert first.key in renewed_keys

    # The queued second callback is still protected by its heartbeat; an
    # independent owner cannot reclaim it while the queue is draining.
    contender = SQLiteStore(database, writer_id="contender", takeover=True)
    try:
        assert contender.claim_one(
            "worker-b",
            task_type=MEMORY_EXTRACTION_KIND,
            lease_seconds=1,
            now=100,
        ) is None
    finally:
        contender.close()

    release_first.set()
    queue.shutdown(wait=True, timeout=1.0)
    assert worker_calls == [first.key]
    verify = SQLiteStore(database, writer_id="verify", takeover=True)
    try:
        assert verify.get_outbox(idempotency_key=first.key).status == "completed"
        assert verify.get_outbox(idempotency_key=terminal_before_dispatch.key).status == "completed"
    finally:
        verify.close()

    # A process that dies after claiming leaves an expired row reclaimable;
    # the old token is fenced by the adapter after the new owner takes over.
    seed = SQLiteStore(database, writer_id="seed-recovery", takeover=True)
    try:
        seed.enqueue_outbox(
            replace(SQLiteMemoryOutboxRepository.to_storage_intent(recovered_intent), available_at=100),
            now=100,
        )
    finally:
        seed.close()
    crashed_store = SQLiteStore(database, writer_id="crashed", takeover=True)
    try:
        crashed = crashed_store.claim_one(
            "worker-a",
            task_type=MEMORY_EXTRACTION_KIND,
            lease_seconds=1,
            now=100,
        )
    finally:
        crashed_store.close()
    assert crashed is not None and crashed.idempotency_key == recovered_intent.key

    recovered_store = SQLiteStore(database, writer_id="recovered", takeover=True)
    try:
        recovered = recovered_store.claim_one(
            "worker-b",
            task_type=MEMORY_EXTRACTION_KIND,
            lease_seconds=1,
            now=102,
        )
    finally:
        recovered_store.close()
    assert recovered is not None and recovered.lease_token != crashed.lease_token
    assert repository.renew(
        MEMORY_EXTRACTION_KIND,
        recovered_intent.key,
        crashed.lease_token,
        1,
    ) is False
    assert repository.complete(
        MEMORY_EXTRACTION_KIND,
        recovered_intent.key,
        crashed.lease_token,
    ) is False


def test_pruned_completed_intent_is_not_recreated_by_startup_backfill(tmp_path):
    database = str(tmp_path / "retention-backfill.sqlite3")
    intent = _intent(fingerprint="sha256:retention-backfill")
    seed = SQLiteStore(database, writer_id="seed", takeover=True)
    try:
        seed.append_with_outbox(
            _memory_event(intent, EVENT_MEMORY_EXTRACTION_REQUESTED),
            replace(SQLiteMemoryOutboxRepository.to_storage_intent(intent), available_at=10),
        )
        claim = seed.claim_one(
            "seed-worker",
            task_type=MEMORY_EXTRACTION_KIND,
            lease_seconds=1_000,
            now=10,
        )
        assert claim is not None
        seed.complete_outbox(claim, now=100)
        seed.append(_memory_event(intent, EVENT_MEMORY_EXTRACTION_COMPLETED))

        retention = OutboxRetentionService(
            seed,
            lambda idea, workspace, fingerprint: next(
                (
                    event
                    for event in seed.scan(idea)
                    if event.event_type == EVENT_MEMORY_EXTRACTION_COMPLETED
                    and (event.payload or {}).get("workspace_id") == workspace
                    and event.fingerprint == fingerprint
                ),
                None,
            ),
            clock=lambda: 10 * 86400,
        )
        preview = retention.preview(
            OutboxRetentionPreviewRequest(
                intent.idea_id,
                intent.workspace_id,
                retention_days=7,
                limit=100,
            )
        )
        assert [item.idempotency_key for item in preview.items] == [intent.key]
        result = retention.prune(
            OutboxRetentionPruneRequest(
                intent.idea_id,
                intent.workspace_id,
                preview.cutoff,
                preview.limit,
                preview.selection_token,
                True,
            )
        )
        assert result.outcome == "pruned"
        assert result.deleted_count == 1
        assert seed.get_outbox(idempotency_key=intent.key) is None
        assert any(
            event.event_type == EVENT_MEMORY_EXTRACTION_COMPLETED and event.fingerprint == intent.fingerprint
            for event in seed.scan(intent.idea_id)
        )
    finally:
        seed.close()

    def factory() -> SQLiteStore:
        return SQLiteStore(database, writer_id="backfill", takeover=True)

    backfill = SQLiteMemoryOutboxRepository(factory, idea_ids_factory=lambda: [intent.idea_id])
    assert backfill.backfill_outbox_intents(kind=MEMORY_EXTRACTION_KIND, idea_id=intent.idea_id) == 0
    verify = SQLiteStore(database, writer_id="verify", takeover=True)
    try:
        assert verify.get_outbox(idempotency_key=intent.key) is None
        assert any(
            event.event_type == EVENT_MEMORY_EXTRACTION_COMPLETED and event.fingerprint == intent.fingerprint
            for event in verify.scan(intent.idea_id)
        )
    finally:
        verify.close()

