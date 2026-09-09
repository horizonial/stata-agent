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
    assert store.schema_version == 2
    assert [item["version"] for item in store.applied_migrations()] == [1, 2]
    columns = {
        row[1] for row in store.connection.execute("PRAGMA table_info(task_outbox)").fetchall()
    }
    assert {"outbox_id", "idempotency_key", "payload", "status", "lease_token"} <= columns
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
    assert store.schema_version == 2
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
    assert duplicate.status is OutboxEnqueueStatus.DUPLICATE
    assert duplicate.record.outbox_id == first.record.outbox_id
    assert store.outbox_stats()[OUTBOX_PENDING] == 1

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

    store = SQLiteStore(database, writer_id="finisher", takeover=True)
    completed = store.complete_outbox(claimed[0], now=100)
    assert completed.status == OUTBOX_COMPLETED
    assert completed.lease_token is None
    with pytest.raises(OutboxLeaseError):
        store.complete_outbox(claimed[0].outbox_id, claimed[0].lease_token, now=100)
    store.close()


def test_expired_lease_can_be_reclaimed_and_old_token_cannot_retry(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "expiry.sqlite3"), writer_id="writer")
    store.enqueue_outbox(_intent(max_attempts=3), now=100)
    first = store.claim_one("worker-a", lease_seconds=5, now=100)
    assert first is not None
    assert first.attempt_count == 1

    reclaimed = store.claim_one("worker-b", lease_seconds=5, now=106)
    assert reclaimed is not None
    assert reclaimed.outbox_id == first.outbox_id
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_token != first.lease_token
    with pytest.raises(OutboxLeaseError):
        store.retry_outbox(first, now=106)

    pending = store.retry_outbox(reclaimed, error="temporary", now=107)
    assert pending.status == OUTBOX_PENDING
    assert pending.available_at == 107
    final_claim = store.claim_one("worker-c", lease_seconds=5, now=107)
    assert final_claim is not None
    assert final_claim.attempt_count == 3
    failed = store.retry_outbox(final_claim, error="still failing", now=108)
    assert failed.status == OUTBOX_FAILED
    assert store.claim_one("worker-d", now=109) is None
    store.close()
