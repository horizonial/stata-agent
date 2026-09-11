from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from stata_agent.events.schema import EVENT_IDEA, EVENT_SPEC_FREEZE, Event
from stata_agent.storage.migrations import MigrationRunner
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.storage.store import (
    OUTBOX_COMPLETED,
    OUTBOX_FAILED,
    OUTBOX_PENDING,
    OUTBOX_PROCESSING,
    OutboxConflictError,
    OutboxEnqueueStatus,
    OutboxIntent,
    OutboxLeaseError,
    OutboxRecoveryConflictError,
    OutboxRetentionConflictError,
    OutboxRetentionExpectation,
)


def _event(kind: str, *, idea_id: str = "idea-1", fingerprint: str | None = None) -> Event:
    return Event(
        idea_id=idea_id,
        event_type=kind,
        actor="agent",
        source="agent",
        payload={"kind": kind},
        fingerprint=fingerprint,
    )


def _intent(
    key: str = "memory:1",
    *,
    task_type: str = "memory.extract",
    max_attempts: int = 3,
    **payload: object,
) -> OutboxIntent:
    return OutboxIntent(
        idempotency_key=key,
        task_type=task_type,
        payload={"source_ids": ["event-1"], "from_seq": 1, "to_seq": 2, **payload},
        available_at=0,
        max_attempts=max_attempts,
    )


def test_new_store_uses_unified_schema_authority_and_creates_outbox(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "new.sqlite3"), writer_id="writer")
    assert store.schema_version == 4
    assert [item["version"] for item in store.applied_migrations()] == [1, 2, 3, 4]
    columns = {
        row[1] for row in store.connection.execute("PRAGMA table_info(task_outbox)").fetchall()
    }
    assert {"outbox_id", "idempotency_key", "payload", "status", "lease_token", "state_version"} <= columns
    store.close()


def test_pre_migration_ledger_database_is_upgraded_without_losing_events(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    MigrationRunner(connection).run(target_version=1)
    connection.execute(
        """
        CREATE TABLE events (
          event_id TEXT PRIMARY KEY,
          idea_id TEXT NOT NULL,
          seq INTEGER NOT NULL,
          event_type TEXT NOT NULL,
          actor TEXT NOT NULL,
          source TEXT NOT NULL,
          payload TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute(
        "INSERT INTO events(event_id, idea_id, seq, event_type, actor, source, payload) "
        "VALUES ('legacy-event', 'idea-1', 1, ?, 'agent', 'agent', '{}')",
        (EVENT_IDEA,),
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(str(database), writer_id="writer")
    assert store.schema_version == 4
    assert store.connection.execute("SELECT event_id FROM events").fetchone()[0] == "legacy-event"
    assert store.connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0] == 0
    store.close()


def test_event_and_intent_are_one_transaction_and_conflict_rolls_back_event(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "atomic.sqlite3"), writer_id="writer")
    first = store.append_with_outbox(_event(EVENT_IDEA), _intent())
    assert first.event_seq == 1
    assert first.outbox.record.event_seq == 1
    assert [event.seq for event in store.scan("idea-1")] == [1]

    with pytest.raises(OutboxConflictError):
        store.append_with_outbox(
            _event(EVENT_SPEC_FREEZE),
            _intent(task_type="different.task"),
        )
    assert [event.event_type for event in store.scan("idea-1")] == [EVENT_IDEA]
    assert len(store.list_outbox()) == 1
    store.close()


def test_outbox_enqueue_is_idempotent_and_backfill_is_atomic(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "idempotent.sqlite3"), writer_id="writer")
    intent = _intent()
    first = store.enqueue_outbox(intent, now=10)
    duplicate = store.ensure_outbox(intent, now=11)
    assert first.status is OutboxEnqueueStatus.ENQUEUED
    assert first.record.state_version == 0
    assert duplicate.status is OutboxEnqueueStatus.DUPLICATE
    assert duplicate.record.outbox_id == first.record.outbox_id
    assert duplicate.record.state_version == first.record.state_version
    assert store.outbox_stats()[OUTBOX_PENDING] == 1
    stats = store.outbox_stats()
    assert stats["ready"] == 1
    assert stats["expired_leases"] == 0
    assert stats["oldest_pending_age_seconds"] >= 0
    assert store.list_outbox()[0].state_version == first.record.state_version

    with pytest.raises(OutboxConflictError):
        store.enqueue_outbox(_intent(task_type="other.task"), now=12)
    assert len(store.list_outbox()) == 1

    results = store.backfill(
        [_intent("memory:2"), _intent("memory:3", task_type="review")],
        now=13,
    )
    assert [result.status for result in results] == [
        OutboxEnqueueStatus.ENQUEUED,
        OutboxEnqueueStatus.ENQUEUED,
    ]
    store.close()


def test_duplicate_event_seq_backfill_is_the_only_mutating_duplicate_enqueue(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "duplicate-event-seq.sqlite3"), writer_id="writer")
    intent = _intent()
    first = store.enqueue_outbox(intent, now=10)
    filled = store.ensure_outbox(intent, event_seq=7, now=11)
    repeated = store.ensure_outbox(intent, event_seq=7, now=12)

    assert first.record.event_seq is None
    assert filled.status is OutboxEnqueueStatus.DUPLICATE
    assert filled.record.event_seq == 7
    assert filled.record.state_version == first.record.state_version + 1
    assert repeated.record.event_seq == 7
    assert repeated.record.state_version == filled.record.state_version
    store.close()


def test_outbox_drops_raw_text_and_provider_response_from_payload(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "privacy.sqlite3"), writer_id="writer")
    result = store.enqueue_outbox(
        _intent(
            raw_text="do not persist",
            provider_response="secret provider output",
            nested={"content": "also secret", "digest": "sha256:abc"},
        ),
        now=10,
    )
    assert result.record.payload == {
        "from_seq": 1,
        "nested": {"digest": "sha256:abc"},
        "source_ids": ["event-1"],
        "to_seq": 2,
    }
    raw = store.connection.execute(
        "SELECT payload FROM task_outbox WHERE outbox_id=?", (result.outbox_id,)
    ).fetchone()[0]
    assert "do not persist" not in raw
    assert "secret provider output" not in raw
    assert json.loads(raw) == result.record.payload
    store.close()


def test_claim_is_atomic_and_leases_fence_completion(tmp_path) -> None:
    database = str(tmp_path / "claim.sqlite3")
    seed = SQLiteStore(database, writer_id="seed")
    seed.backfill([_intent("memory:1"), _intent("memory:2")], now=100)
    seed.close()

    def claim(worker_id: str):
        store = SQLiteStore(database, writer_id=worker_id, takeover=True)
        try:
            return store.claim_outbox(worker_id, limit=1, lease_seconds=10, now=100)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(claim, ("worker-a", "worker-b"))
    claimed = first + second
    assert len(claimed) == 2
    assert {record.status for record in claimed} == {OUTBOX_PROCESSING}
    assert len({record.outbox_id for record in claimed}) == 2
    assert len({record.lease_token for record in claimed}) == 2
    assert {record.state_version for record in claimed} == {1}

    store = SQLiteStore(database, writer_id="finisher", takeover=True)
    completed = store.complete_outbox(claimed[0], now=100)
    assert completed.status == OUTBOX_COMPLETED
    assert completed.lease_token is None
    assert completed.state_version == claimed[0].state_version + 1
    with pytest.raises(OutboxLeaseError):
        store.complete_outbox(claimed[0].outbox_id, claimed[0].lease_token, now=100)
    assert store.get_outbox(outbox_id=claimed[0].outbox_id).state_version == completed.state_version
    store.close()


def test_expired_lease_can_be_reclaimed_and_old_token_cannot_retry(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "expiry.sqlite3"), writer_id="writer")
    store.enqueue_outbox(_intent(max_attempts=3), now=100)
    first = store.claim_one("worker-a", lease_seconds=5, now=100)
    assert first is not None
    assert first.attempt_count == 1
    assert first.state_version == 1

    reclaimed = store.claim_one("worker-b", lease_seconds=5, now=106)
    assert reclaimed is not None
    assert reclaimed.outbox_id == first.outbox_id
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_token != first.lease_token
    assert reclaimed.state_version == first.state_version + 1
    with pytest.raises(OutboxLeaseError):
        store.retry_outbox(first, now=106)
    assert store.get_outbox(outbox_id=first.outbox_id).state_version == reclaimed.state_version

    pending = store.retry_outbox(reclaimed, error="temporary", now=107)
    assert pending.status == OUTBOX_PENDING
    assert pending.available_at == 107
    assert pending.state_version == reclaimed.state_version + 1
    final_claim = store.claim_one("worker-c", lease_seconds=5, now=107)
    assert final_claim is not None
    assert final_claim.attempt_count == 3
    assert final_claim.state_version == pending.state_version + 1
    failed = store.retry_outbox(final_claim, error="still failing", now=108)
    assert failed.status == OUTBOX_FAILED
    assert failed.state_version == final_claim.state_version + 1
    assert store.claim_one("worker-d", now=109) is None
    assert store.get_outbox(outbox_id=failed.outbox_id).state_version == failed.state_version
    store.close()


def test_claim_auto_dead_letters_each_exhausted_row_once_and_fences_noop(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "auto-dead-letter.sqlite3"), writer_id="writer")
    store.backfill(
        [_intent("memory:1", max_attempts=1), _intent("memory:2", max_attempts=1)],
        now=100,
    )
    claimed = store.claim_outbox("worker-a", limit=2, lease_seconds=5, now=100)
    assert len(claimed) == 2
    assert {item.state_version for item in claimed} == {1}

    assert store.claim_outbox("worker-b", limit=2, now=106) == []
    failed = store.list_outbox(status=OUTBOX_FAILED)
    assert len(failed) == 2
    assert {item.state_version for item in failed} == {2}
    assert {item.last_error for item in failed} == {"max_attempts_exceeded"}

    assert store.claim_outbox("worker-c", limit=2, now=107) == []
    assert {item.state_version for item in store.list_outbox(status=OUTBOX_FAILED)} == {2}
    store.close()


def test_renew_outbox_lease_is_fenced_monotonic_and_does_not_consume_attempt(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "renew.sqlite3"), writer_id="writer")
    store.enqueue_outbox(_intent(), now=100)
    claimed = store.claim_one("worker-a", lease_seconds=5, now=100)
    assert claimed is not None

    renewed_early = store.renew_outbox_lease(claimed, lease_seconds=2, now=101)
    assert renewed_early.lease_until == claimed.lease_until == 105
    assert renewed_early.attempt_count == claimed.attempt_count == 1
    assert renewed_early.status == OUTBOX_PROCESSING
    assert renewed_early.lease_owner == claimed.lease_owner == "worker-a"
    assert renewed_early.lease_token == claimed.lease_token
    assert renewed_early.payload == claimed.payload
    assert renewed_early.event_seq == claimed.event_seq
    assert renewed_early.state_version == claimed.state_version + 1

    renewed = store.renew_outbox_lease(renewed_early, lease_seconds=5, now=103)
    assert renewed.lease_until == 108
    assert renewed.attempt_count == 1
    assert renewed.updated_at == 103
    assert renewed.state_version == renewed_early.state_version + 1
    store.close()


@pytest.mark.parametrize("duration", [0, -1, False, float("inf"), float("nan"), "invalid"])
def test_renew_outbox_lease_rejects_invalid_duration(tmp_path, duration) -> None:
    store = SQLiteStore(str(tmp_path / "renew-invalid.sqlite3"), writer_id="writer")
    store.enqueue_outbox(_intent(), now=100)
    claimed = store.claim_one("worker-a", lease_seconds=5, now=100)
    assert claimed is not None
    initial_version = claimed.state_version

    with pytest.raises(ValueError, match="positive and finite"):
        store.renew_outbox_lease(claimed, lease_seconds=duration, now=101)
    assert store.get_outbox(outbox_id=claimed.outbox_id).state_version == initial_version
    store.close()


def test_renew_outbox_lease_rejects_expired_replaced_completed_and_missing_claims(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "renew-fence.sqlite3"), writer_id="writer")
    store.enqueue_outbox(_intent(), now=100)
    first = store.claim_one("worker-a", lease_seconds=5, now=100)
    assert first is not None

    with pytest.raises(OutboxLeaseError):
        store.renew_outbox_lease(first, lease_seconds=5, now=105)
    assert store.get_outbox(outbox_id=first.outbox_id).state_version == first.state_version

    second = store.claim_one("worker-b", lease_seconds=5, now=106)
    assert second is not None
    assert second.state_version == first.state_version + 1
    with pytest.raises(OutboxLeaseError):
        store.renew_outbox_lease(first, lease_seconds=5, now=106)
    assert store.get_outbox(outbox_id=second.outbox_id).state_version == second.state_version

    store.complete_outbox(second, now=106)
    with pytest.raises(OutboxLeaseError):
        store.renew_outbox_lease(second, lease_seconds=5, now=106)
    assert store.get_outbox(outbox_id=second.outbox_id).state_version == second.state_version + 1

    with pytest.raises(OutboxLeaseError):
        store.renew_outbox_lease("missing", "token", lease_seconds=5, now=106)
    store.close()


def test_release_preserves_attempt_budget_and_claim_filters_task_type(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "release.sqlite3"), writer_id="writer")
    store.enqueue_outbox(_intent("memory:1", task_type="memory.extraction"), now=100)
    store.enqueue_outbox(_intent("review:1", task_type="memory.review"), now=100)

    claimed = store.claim_one(
        "memory-worker",
        task_type="memory.extraction",
        lease_seconds=5,
        now=100,
    )
    assert claimed is not None
    assert claimed.task_type == "memory.extraction"
    assert claimed.attempt_count == 1
    assert claimed.state_version == 1

    released = store.release_outbox(claimed, error="queue_full", now=100)
    assert released.status == OUTBOX_PENDING
    assert released.attempt_count == 0
    assert released.last_error == "queue_full"
    assert released.state_version == claimed.state_version + 1

    claimed_again = store.claim_one(
        "memory-worker",
        task_type="memory.extraction",
        lease_seconds=5,
        now=100,
    )
    assert claimed_again is not None
    assert claimed_again.outbox_id == claimed.outbox_id
    assert claimed_again.attempt_count == 1
    assert claimed_again.state_version == released.state_version + 1
    assert store.get_outbox(idempotency_key="review:1").status == OUTBOX_PENDING
    store.close()


def _failed_outbox(store: SQLiteStore, key: str = "memory:failed", *, max_attempts: int = 2):
    store.enqueue_outbox(_intent(key, max_attempts=max_attempts), event_seq=17, now=100)
    now = 100
    while True:
        claimed = store.claim_one("worker", lease_seconds=5, now=now)
        assert claimed is not None
        failed = store.retry_outbox(claimed, error="delivery failed", now=now + 1)
        if failed.status == OUTBOX_FAILED:
            return failed
        now += 2


def test_reconcile_failed_outbox_is_exact_fenced_transition(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "reconcile-failed.sqlite3"), writer_id="writer")
    failed = _failed_outbox(store)
    # A failed row normally has no lease/completion marker.  Keep this
    # intentionally inconsistent legacy shape to prove reconcile clears both.
    store.connection.execute(
        "UPDATE task_outbox SET lease_owner='stale-worker',lease_token='stale-token',"
        "lease_until=999,completed_at=77 WHERE outbox_id=?",
        (failed.outbox_id,),
    )
    before = store.get_outbox(outbox_id=failed.outbox_id)
    assert before is not None
    event_count = store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    row_count = store.connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0]

    completed = store.reconcile_failed_outbox(
        before.idempotency_key,
        before.task_type,
        before.state_version,
        now=200,
    )

    assert completed.status == OUTBOX_COMPLETED
    assert completed.outbox_id == before.outbox_id
    assert completed.idempotency_key == before.idempotency_key
    assert completed.task_type == before.task_type
    assert completed.payload == before.payload
    assert completed.attempt_count == before.attempt_count
    assert completed.max_attempts == before.max_attempts
    assert completed.available_at == before.available_at
    assert completed.event_seq == before.event_seq
    assert completed.created_at == before.created_at
    assert completed.last_error == before.last_error
    assert completed.lease_owner is None
    assert completed.lease_token is None
    assert completed.lease_until is None
    assert completed.completed_at == 200
    assert completed.updated_at == 200
    assert completed.state_version == before.state_version + 1
    assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count
    assert store.connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0] == row_count
    store.close()


def test_redrive_failed_outbox_resets_only_attempt_and_reenters_claim_path(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "redrive-failed.sqlite3"), writer_id="writer")
    failed = _failed_outbox(store, max_attempts=1)
    store.connection.execute(
        "UPDATE task_outbox SET lease_owner='stale-worker',lease_token='stale-token',"
        "lease_until=999,completed_at=77 WHERE outbox_id=?",
        (failed.outbox_id,),
    )
    before = store.get_outbox(outbox_id=failed.outbox_id)
    assert before is not None
    event_count = store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    row_count = store.connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0]

    pending = store.redrive_failed_outbox(
        before.idempotency_key,
        before.task_type,
        before.state_version,
        now=200,
    )

    assert pending.status == OUTBOX_PENDING
    assert pending.outbox_id == before.outbox_id
    assert pending.idempotency_key == before.idempotency_key
    assert pending.task_type == before.task_type
    assert pending.payload == before.payload
    assert pending.attempt_count == 0
    assert pending.max_attempts == before.max_attempts
    assert pending.available_at == 200
    assert pending.event_seq == before.event_seq
    assert pending.created_at == before.created_at
    assert pending.last_error == before.last_error
    assert pending.lease_owner is None
    assert pending.lease_token is None
    assert pending.lease_until is None
    assert pending.completed_at is None
    assert pending.updated_at == 200
    assert pending.state_version == before.state_version + 1
    assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count
    assert store.connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0] == row_count

    claimed = store.claim_one("worker-again", lease_seconds=5, now=200)
    assert claimed is not None
    assert claimed.outbox_id == pending.outbox_id
    assert claimed.status == OUTBOX_PROCESSING
    assert claimed.attempt_count == 1
    assert claimed.max_attempts == pending.max_attempts
    assert claimed.state_version == pending.state_version + 1
    failed_again = store.retry_outbox(claimed, error="failed again", now=201)
    assert failed_again.status == OUTBOX_FAILED
    assert failed_again.attempt_count == failed_again.max_attempts == 1
    assert store.claim_one("worker-third", now=202) is None
    store.close()


@pytest.mark.parametrize("operation_name", ["reconcile_failed_outbox", "redrive_failed_outbox"])
def test_failed_recovery_rejects_invalid_identity_generation_and_status_without_mutation(
    tmp_path, operation_name
) -> None:
    store = SQLiteStore(str(tmp_path / f"recovery-conflicts-{operation_name}.sqlite3"), writer_id="writer")
    failed = _failed_outbox(store, max_attempts=2)
    operation = getattr(store, operation_name)
    before = store.get_outbox(outbox_id=failed.outbox_id)
    assert before is not None

    invalid_calls = [
        ("", before.task_type, before.state_version),
        (before.idempotency_key, "", before.state_version),
        (before.idempotency_key, before.task_type, -1),
        (before.idempotency_key, before.task_type, True),
        (before.idempotency_key, before.task_type, "{}"),
        ("missing", before.task_type, before.state_version),
        (before.idempotency_key, "other.task", before.state_version),
        (before.idempotency_key, before.task_type, before.state_version - 1),
    ]
    for args in invalid_calls:
        with pytest.raises(OutboxRecoveryConflictError):
            operation(*args, now=200)
        current = store.get_outbox(outbox_id=before.outbox_id)
        assert current == before

    # The first successful call consumes the observed generation; a second
    # request with that same generation must fail regardless of target path.
    operation(before.idempotency_key, before.task_type, before.state_version, now=200)
    with pytest.raises(OutboxRecoveryConflictError):
        operation(before.idempotency_key, before.task_type, before.state_version, now=201)
    store.close()


def test_failed_recovery_rejects_pending_processing_and_completed_rows(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "recovery-statuses.sqlite3"), writer_id="writer")
    pending_result = store.enqueue_outbox(_intent("memory:pending"), now=100)
    pending = pending_result.record
    with pytest.raises(OutboxRecoveryConflictError):
        store.redrive_failed_outbox(pending.idempotency_key, pending.task_type, pending.state_version, now=200)
    assert store.get_outbox(outbox_id=pending.outbox_id) == pending

    processing = store.claim_one("worker", lease_seconds=5, now=100)
    assert processing is not None
    with pytest.raises(OutboxRecoveryConflictError):
        store.reconcile_failed_outbox(
            processing.idempotency_key, processing.task_type, processing.state_version, now=200
        )
    assert store.get_outbox(outbox_id=processing.outbox_id) == processing
    completed = store.complete_outbox(processing, now=100)
    with pytest.raises(OutboxRecoveryConflictError):
        store.reconcile_failed_outbox(
            completed.idempotency_key, completed.task_type, completed.state_version, now=200
        )
    assert store.get_outbox(outbox_id=completed.outbox_id) == completed
    store.close()


def test_failed_recovery_old_generation_cannot_replay_after_aba_cycle(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "recovery-aba.sqlite3"), writer_id="writer")
    failed = _failed_outbox(store, max_attempts=1)
    old_version = failed.state_version
    pending = store.redrive_failed_outbox(failed.idempotency_key, failed.task_type, old_version, now=200)
    claimed = store.claim_one("worker", lease_seconds=5, now=200)
    assert claimed is not None
    failed_again = store.retry_outbox(claimed, error="failed again", now=201)
    assert failed_again.status == OUTBOX_FAILED
    assert failed_again.state_version > old_version
    assert failed_again.attempt_count == 1
    with pytest.raises(OutboxRecoveryConflictError):
        store.redrive_failed_outbox(failed_again.idempotency_key, failed_again.task_type, old_version, now=202)
    assert store.get_outbox(outbox_id=failed_again.outbox_id) == failed_again
    assert pending.state_version == old_version + 1
    store.close()


def test_failed_recovery_handles_malformed_payload_without_rewriting_raw_storage(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "recovery-malformed.sqlite3"), writer_id="writer")
    failed = _failed_outbox(store)
    raw_payload = "not-json-legacy-payload"
    store.connection.execute(
        "UPDATE task_outbox SET payload=? WHERE outbox_id=?", (raw_payload, failed.outbox_id)
    )
    before = store.get_outbox(outbox_id=failed.outbox_id)
    assert before is not None
    assert before.payload == {}
    recovered = store.redrive_failed_outbox(
        before.idempotency_key, before.task_type, before.state_version, now=200
    )
    assert recovered.status == OUTBOX_PENDING
    assert store.connection.execute(
        "SELECT payload FROM task_outbox WHERE outbox_id=?", (failed.outbox_id,)
    ).fetchone()[0] == raw_payload
    store.close()


def _completed_outbox(
    store: SQLiteStore,
    key: str,
    *,
    task_type: str = "memory.extraction",
    completed_at: int = 100,
):
    store.enqueue_outbox(_intent(key, task_type=task_type), now=10)
    claimed = store.claim_one(f"worker-{key}", lease_seconds=1_000, now=10)
    assert claimed is not None
    return store.complete_outbox(claimed, now=completed_at)


def test_completed_outbox_keyset_query_is_ordered_bounded_and_cutoff_inclusive(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "retention-query.sqlite3"), writer_id="writer")
    old = _completed_outbox(store, "memory:old", completed_at=90)
    first_same = _completed_outbox(store, "memory:first-same", completed_at=100)
    second_same = _completed_outbox(store, "memory:second-same", completed_at=100)
    _completed_outbox(store, "memory:new", completed_at=101)
    _completed_outbox(store, "review:old", task_type="memory.review", completed_at=80)
    pending = store.enqueue_outbox(_intent("memory:pending"), now=10).record

    rows = store.list_completed_outbox_before("memory.extraction", 100, 2)
    assert len(rows) == 2
    assert [(row.completed_at, row.outbox_id) for row in rows] == sorted(
        (row.completed_at, row.outbox_id) for row in rows
    )
    assert {row.idempotency_key for row in rows} <= {
        old.idempotency_key,
        first_same.idempotency_key,
        second_same.idempotency_key,
    }
    assert pending.idempotency_key not in {row.idempotency_key for row in rows}
    assert all(row.status == OUTBOX_COMPLETED and row.completed_at is not None for row in rows)

    all_rows = store.list_completed_outbox_before("memory.extraction", 100, 10)
    assert [row.idempotency_key for row in all_rows] == [
        row.idempotency_key for row in sorted(all_rows, key=lambda item: (item.completed_at, item.outbox_id))
    ]
    assert {row.idempotency_key for row in all_rows} == {
        old.idempotency_key,
        first_same.idempotency_key,
        second_same.idempotency_key,
    }
    store.close()


def test_completed_outbox_keyset_cursor_is_exclusive_without_same_second_duplicates(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "retention-cursor.sqlite3"), writer_id="writer")
    for key in ("memory:a", "memory:b", "memory:c", "memory:d"):
        _completed_outbox(store, key, completed_at=100)

    first_page = store.list_completed_outbox_before("memory.extraction", 100, 2)
    second_page = store.list_completed_outbox_before(
        "memory.extraction",
        100,
        2,
        after_completed_at=first_page[-1].completed_at,
        after_outbox_id=first_page[-1].outbox_id,
    )
    assert len(first_page) == len(second_page) == 2
    assert {row.outbox_id for row in first_page}.isdisjoint(row.outbox_id for row in second_page)
    assert [row.outbox_id for row in first_page + second_page] == [
        row.outbox_id
        for row in sorted(first_page + second_page, key=lambda item: (item.completed_at, item.outbox_id))
    ]
    assert store.list_completed_outbox_before(
        "memory.extraction",
        100,
        10,
        after_completed_at=second_page[-1].completed_at,
        after_outbox_id=second_page[-1].outbox_id,
    ) == []
    store.close()


@pytest.mark.parametrize(
    ("task_type", "completed_before", "limit", "after_completed_at", "after_outbox_id"),
    [
        ("", 100, 1, None, None),
        ("memory.extraction", -1, 1, None, None),
        ("memory.extraction", 100, 0, None, None),
        ("memory.extraction", True, 1, None, None),
        ("memory.extraction", 100, True, None, None),
        ("memory.extraction", 100, 1, 100, None),
        ("memory.extraction", 100, 1, None, "outbox-id"),
        ("memory.extraction", 100, 1, -1, "outbox-id"),
        ("memory.extraction", 100, 1, 100, ""),
    ],
)
def test_completed_outbox_keyset_query_rejects_invalid_arguments(
    tmp_path,
    task_type,
    completed_before,
    limit,
    after_completed_at,
    after_outbox_id,
) -> None:
    store = SQLiteStore(str(tmp_path / "retention-query-invalid.sqlite3"), writer_id="writer")
    with pytest.raises(ValueError):
        store.list_completed_outbox_before(
            task_type,
            completed_before,
            limit,
            after_completed_at=after_completed_at,
            after_outbox_id=after_outbox_id,
        )
    store.close()


def test_completed_outbox_batch_delete_is_exact_and_leaves_unselected_rows_and_ledger_untouched(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "retention-delete.sqlite3"), writer_id="writer")
    first = _completed_outbox(store, "memory:first", completed_at=100)
    second = _completed_outbox(store, "memory:second", completed_at=100)
    pending = store.enqueue_outbox(_intent("memory:pending"), now=10).record
    event_count = store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    schema_version = store.schema_version

    deleted = store.delete_completed_outbox_batch(
        [
            OutboxRetentionExpectation(
                first.outbox_id,
                first.idempotency_key,
                first.task_type,
                first.completed_at,
                first.state_version,
            )
        ],
        cutoff=100,
    )

    assert deleted == 1
    assert store.get_outbox(outbox_id=first.outbox_id) is None
    assert store.get_outbox(outbox_id=second.outbox_id) == second
    assert store.get_outbox(outbox_id=pending.outbox_id) == pending
    assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count
    assert store.schema_version == schema_version == 4
    store.close()


def test_completed_outbox_batch_delete_is_atomic_when_a_later_expectation_is_stale(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "retention-delete-atomic.sqlite3"), writer_id="writer")
    first = _completed_outbox(store, "memory:first", completed_at=100)
    second = _completed_outbox(store, "memory:second", completed_at=100)
    before_count = len(store.list_outbox())

    with pytest.raises(OutboxRetentionConflictError):
        store.delete_completed_outbox_batch(
            [
                OutboxRetentionExpectation(
                    first.outbox_id,
                    first.idempotency_key,
                    first.task_type,
                    first.completed_at,
                    first.state_version,
                ),
                OutboxRetentionExpectation(
                    second.outbox_id,
                    second.idempotency_key,
                    second.task_type,
                    second.completed_at,
                    second.state_version + 1,
                ),
            ],
            cutoff=100,
        )
    assert len(store.list_outbox()) == before_count
    assert store.get_outbox(outbox_id=first.outbox_id) == first
    assert store.get_outbox(outbox_id=second.outbox_id) == second
    store.close()


@pytest.mark.parametrize("mutation", ["status", "task_type", "completed_at", "state_version", "delete"])
def test_completed_outbox_batch_delete_conflicts_on_any_exact_row_mismatch_without_mutation(
    tmp_path, mutation
) -> None:
    store = SQLiteStore(str(tmp_path / f"retention-delete-{mutation}.sqlite3"), writer_id="writer")
    record = _completed_outbox(store, "memory:target", completed_at=100)
    if mutation == "status":
        store.connection.execute(
            "UPDATE task_outbox SET status=? WHERE outbox_id=?", (OUTBOX_FAILED, record.outbox_id)
        )
    elif mutation == "task_type":
        store.connection.execute(
            "UPDATE task_outbox SET task_type=? WHERE outbox_id=?", ("memory.other", record.outbox_id)
        )
    elif mutation == "completed_at":
        store.connection.execute(
            "UPDATE task_outbox SET completed_at=? WHERE outbox_id=?", (101, record.outbox_id)
        )
    elif mutation == "state_version":
        store.connection.execute(
            "UPDATE task_outbox SET state_version=state_version+1 WHERE outbox_id=?", (record.outbox_id,)
        )
    else:
        store.connection.execute("DELETE FROM task_outbox WHERE outbox_id=?", (record.outbox_id,))

    with pytest.raises(OutboxRetentionConflictError):
        store.delete_completed_outbox_batch(
            [
                OutboxRetentionExpectation(
                    record.outbox_id,
                    record.idempotency_key,
                    record.task_type,
                    record.completed_at,
                    record.state_version,
                )
            ],
            cutoff=100,
        )
    if mutation != "delete":
        assert store.get_outbox(outbox_id=record.outbox_id) is not None
    store.close()


def test_completed_outbox_batch_delete_rejects_duplicate_or_invalid_expectations_before_writing(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "retention-delete-invalid.sqlite3"), writer_id="writer")
    record = _completed_outbox(store, "memory:target", completed_at=100)
    expectation = OutboxRetentionExpectation(
        record.outbox_id,
        record.idempotency_key,
        record.task_type,
        record.completed_at,
        record.state_version,
    )

    with pytest.raises(ValueError, match="duplicate"):
        store.delete_completed_outbox_batch([expectation, expectation], cutoff=100)
    with pytest.raises(TypeError):
        store.delete_completed_outbox_batch([object()], cutoff=100)
    with pytest.raises(ValueError):
        store.delete_completed_outbox_batch([], cutoff=-1)
    assert store.get_outbox(outbox_id=record.outbox_id) == record
    assert store.delete_completed_outbox_batch([], cutoff=100) == 0
    assert store.get_outbox(outbox_id=record.outbox_id) == record
    store.close()
