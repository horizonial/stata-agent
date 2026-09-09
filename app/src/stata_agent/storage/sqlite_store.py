"""SQLite 账本实现 + durable task outbox。"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from typing import Iterator, Optional

from ..domain.reducers import Projection, apply as _apply, fold as _fold
from ..events.append import assert_sane_event
from ..events.schema import Event
from ..events.upcast import upcast
from .migrations import MigrationRunner
from .store import (
    AppendOutboxResult,
    DuplicateFingerprint,
    LeaseConflict,
    OUTBOX_COMPLETED,
    OUTBOX_FAILED,
    OUTBOX_PENDING,
    OUTBOX_PROCESSING,
    OutboxConflictError,
    OutboxEnqueueResult,
    OutboxEnqueueStatus,
    OutboxIntent,
    OutboxLeaseError,
    OutboxRecord,
    StaleWrite,
)


class SQLiteStore:
    """单文件账本；单写者由 writer_lease + token 保证，append 每次核对。"""

    def __init__(self, path: str, *, writer_id: str = "w1", takeover: bool = False):
        self._path = path
        self._writer_id = writer_id
        self._token = uuid.uuid4().hex
        self._conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrations = MigrationRunner(self._conn)
        self._migrations.run()
        self._acquire_lease(takeover=takeover)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def schema_version(self) -> int:
        return self._migrations.current_version

    def migrate(self) -> int:
        return self._migrations.run()

    def applied_migrations(self) -> list[dict[str, object]]:
        return self._migrations.applied()

    # ---------------------------------------------------------------- lease
    def _acquire_lease(self, *, takeover: bool) -> None:
        now = int(time.time())
        if takeover:
            self._conn.execute(
                "INSERT INTO writer_lease(writer_id,token,revision,acquired_at,expires_at) "
                "VALUES('master',?,COALESCE((SELECT revision FROM writer_lease WHERE writer_id='master'),0),?,?) "
                "ON CONFLICT(writer_id) DO UPDATE SET token=excluded.token, acquired_at=excluded.acquired_at, "
                "expires_at=excluded.expires_at",
                (self._token, now, now + 3600),
            )
            self._conn.commit()
            return
        self._conn.execute(
            "INSERT INTO writer_lease(writer_id,token,revision,acquired_at,expires_at) "
            "VALUES('master',?,0,?,?) "
            "ON CONFLICT(writer_id) DO UPDATE SET token=excluded.token, acquired_at=excluded.acquired_at, "
            "expires_at=excluded.expires_at "
            "WHERE writer_lease.expires_at <= excluded.acquired_at",
            (self._token, now, now + 3600),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT token FROM writer_lease WHERE writer_id='master'").fetchone()
        if row["token"] != self._token:
            raise LeaseConflict("账本已被其他 writer 持有（单写者）")

    def _check_lease(self) -> None:
        row = self._conn.execute(
            "SELECT token, expires_at FROM writer_lease WHERE writer_id='master'"
        ).fetchone()
        now = int(time.time())
        if row is None or row["token"] != self._token:
            raise StaleWrite(f"writer={self._writer_id!r} 的租约已失效（fence）")
        if int(row["expires_at"]) <= now:
            raise StaleWrite(f"writer={self._writer_id!r} 的租约已过期（fence）")

    def _bump_revision(self) -> None:
        self._conn.execute("UPDATE writer_lease SET revision=revision+1 WHERE writer_id='master'")

    @property
    def revision(self) -> int:
        row = self._conn.execute("SELECT revision FROM writer_lease WHERE writer_id='master'").fetchone()
        return int(row["revision"]) if row else 0

    # ---------------------------------------------------------------- append
    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def append(
        self,
        event: Event,
        *,
        outbox: OutboxIntent | None = None,
        outbox_intent: OutboxIntent | None = None,
    ) -> int:
        if outbox is not None and outbox_intent is not None:
            raise ValueError("pass at most one outbox intent")
        with self._write_transaction():
            self._check_lease()
            projection = self.project(event.idea_id)
            self._validate_candidate(event, projection)
            seq = self._insert_event(event)
            intent = outbox if outbox is not None else outbox_intent
            if intent is not None:
                self._enqueue_outbox(intent, event_seq=seq)
            return seq

    def append_with_outbox(self, event: Event, intent: OutboxIntent) -> AppendOutboxResult:
        """Atomically append an event and ensure its durable intent."""

        with self._write_transaction():
            self._check_lease()
            projection = self.project(event.idea_id)
            self._validate_candidate(event, projection)
            seq = self._insert_event(event)
            result = self._enqueue_outbox(intent, event_seq=seq)
            return AppendOutboxResult(seq, result)

    append_event_with_outbox = append_with_outbox

    def append_many(self, events: list[Event]) -> int:
        """Atomically append a batch after folding every candidate event.

        Validation happens against an in-memory projection that is advanced for
        each event in the same batch.  Any invalid event therefore aborts the
        surrounding transaction and leaves no earlier batch item committed.
        """
        if not events:
            return 0
        with self._write_transaction():
            self._check_lease()
            projections: dict[str, Projection] = {}
            seen_fingerprints: set[tuple[str, str, str]] = set()
            last = 0
            for event in events:
                projection = projections.get(event.idea_id)
                if projection is None:
                    projection = self.project(event.idea_id)
                self._validate_candidate(
                    event,
                    projection,
                    seen_fingerprints=seen_fingerprints,
                )
                projections[event.idea_id] = _apply(projection, event)
                last = self._insert_event(event)
                if event.fingerprint:
                    seen_fingerprints.add((event.idea_id, event.event_type, event.fingerprint))
            return last

    def _validate_candidate(
        self,
        event: Event,
        projection: Projection,
        *,
        seen_fingerprints: set[tuple[str, str, str]] | None = None,
    ) -> None:
        """Validate one candidate before its INSERT executes."""
        assert_sane_event(event)
        if event.fingerprint:
            key = (event.idea_id, event.event_type, event.fingerprint)
            if (seen_fingerprints and key in seen_fingerprints) or self._exists_fingerprint(event):
                raise DuplicateFingerprint(
                    f"dup (idea={event.idea_id}, {event.event_type}, {event.fingerprint[:12]}…)"
                )
        # _apply raises IllegalEventSequence for invalid FSM/card/claim events.
        _apply(projection, event)

    def _insert_event(self, event: Event) -> int:
        """Insert one already validated event in the caller's transaction."""
        seq = self._next_seq()
        event_id = uuid.uuid4().hex
        prev = self._prev_event_id(event.idea_id, event.branch_id)
        now = int(time.time() * 1000)
        self._conn.execute(
            "INSERT INTO events(event_id,idea_id,seq,branch_id,prev_event_id,phase,event_type,"
            "schema_version,actor,source,correlation_id,causation_id,operation_id,attempt_id,"
            "fingerprint,confidence,side_effect_state,payload,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, event.idea_id, seq, event.branch_id, prev, event.phase, event.event_type,
                event.schema_version, event.actor, event.source, event.correlation_id, event.causation_id,
                event.operation_id, event.attempt_id, event.fingerprint, event.confidence,
                event.side_effect_state, json.dumps(event.payload, ensure_ascii=False), now,
            ),
        )
        self._set_last_seq(seq)
        self._bump_revision()
        return seq

    # --------------------------------------------------------------- outbox
    @staticmethod
    def _now(value: int | None) -> int:
        return int(time.time()) if value is None else int(value)

    @staticmethod
    def _safe_payload(payload: Mapping[str, object]) -> dict[str, object]:
        blocked = {
            "raw", "raw_text", "text", "content", "prompt", "response", "provider_response",
            "output", "message", "messages", "transcript", "conversation", "body", "input", "result",
        }

        def clean(value: object) -> object:
            if value is None or isinstance(value, (bool, int, float, str)):
                return value[:512] if isinstance(value, str) else value
            if isinstance(value, Mapping):
                return {
                    str(key): clean(child)
                    for key, child in value.items()
                    if not (
                        (key_name := str(key).strip().lower().replace("-", "_")) in blocked
                        or key_name.endswith(("_text", "_content", "_prompt", "_response", "_output"))
                    )
                }
            if isinstance(value, (list, tuple)):
                return [clean(child) for child in value]
            raise TypeError(f"unsupported outbox payload type: {type(value).__name__}")

        result = clean(payload)
        if not isinstance(result, dict):
            raise TypeError("payload must be a mapping")
        return result

    @classmethod
    def _payload_text(cls, payload: Mapping[str, object]) -> tuple[str, dict[str, object]]:
        safe = cls._safe_payload(payload)
        try:
            text = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("outbox payload must be finite JSON metadata") from exc
        if len(text.encode()) > 16_384:
            raise ValueError("outbox payload exceeds 16 KiB")
        return text, safe

    @staticmethod
    def _outbox(row: sqlite3.Row) -> OutboxRecord:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        return OutboxRecord(
            str(row["outbox_id"]), str(row["idempotency_key"]), str(row["task_type"]),
            payload if isinstance(payload, dict) else {}, str(row["status"]), int(row["attempt_count"]),
            int(row["max_attempts"]), int(row["available_at"]), row["lease_owner"], row["lease_token"],
            int(row["lease_until"]) if row["lease_until"] is not None else None, row["last_error"],
            int(row["event_seq"]) if row["event_seq"] is not None else None, int(row["created_at"]),
            int(row["updated_at"]), int(row["completed_at"]) if row["completed_at"] is not None else None,
        )

    def _enqueue_outbox(
        self,
        intent: OutboxIntent,
        *,
        event_seq: int | None = None,
        now: int | None = None,
    ) -> OutboxEnqueueResult:
        if not isinstance(intent, OutboxIntent):
            raise TypeError("intent must be an OutboxIntent")
        payload_text, safe = self._payload_text(intent.payload)
        selected_seq = None if event_seq is None else int(event_seq)
        if selected_seq is not None and selected_seq <= 0:
            raise ValueError("event_seq must be positive")
        row = self._conn.execute(
            "SELECT * FROM task_outbox WHERE idempotency_key=?", (intent.idempotency_key,)
        ).fetchone()
        timestamp = self._now(now)
        if row is not None:
            current = self._outbox(row)
            if current.task_type != intent.task_type or current.payload != safe:
                raise OutboxConflictError(f"outbox key conflict: {intent.idempotency_key!r}")
            if selected_seq is not None and current.event_seq not in (None, selected_seq):
                raise OutboxConflictError(f"outbox event conflict: {intent.idempotency_key!r}")
            if selected_seq is not None and current.event_seq is None:
                self._conn.execute(
                    "UPDATE task_outbox SET event_seq=?, updated_at=? WHERE outbox_id=?",
                    (selected_seq, timestamp, current.outbox_id),
                )
                row = self._conn.execute(
                    "SELECT * FROM task_outbox WHERE outbox_id=?", (current.outbox_id,)
                ).fetchone()
                current = self._outbox(row)
            return OutboxEnqueueResult(OutboxEnqueueStatus.DUPLICATE, current)
        outbox_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO task_outbox(outbox_id,idempotency_key,task_type,payload,status,attempt_count,max_attempts,"
            "available_at,lease_owner,lease_token,lease_until,last_error,event_seq,created_at,updated_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (outbox_id, intent.idempotency_key, intent.task_type, payload_text, OUTBOX_PENDING, 0,
             intent.max_attempts, int(intent.available_at or 0), None, None, None, None, selected_seq,
             timestamp, timestamp, None),
        )
        row = self._conn.execute("SELECT * FROM task_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        return OutboxEnqueueResult(OutboxEnqueueStatus.ENQUEUED, self._outbox(row))

    def enqueue_outbox(
        self,
        intent: OutboxIntent,
        *,
        event_seq: int | None = None,
        now: int | None = None,
    ) -> OutboxEnqueueResult:
        with self._write_transaction():
            self._check_lease()
            return self._enqueue_outbox(intent, event_seq=event_seq, now=now)

    ensure_outbox = enqueue_outbox

    def backfill_outbox(self, intents: Iterable[OutboxIntent], *, now: int | None = None) -> list[OutboxEnqueueResult]:
        with self._write_transaction():
            self._check_lease()
            return [self._enqueue_outbox(intent, now=now) for intent in intents]

    backfill = backfill_outbox

    def get_outbox(
        self,
        outbox_id: str | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> OutboxRecord | None:
        if (outbox_id is None) == (idempotency_key is None):
            raise ValueError("pass exactly one outbox id or idempotency key")
        column, value = ("outbox_id", outbox_id) if outbox_id is not None else ("idempotency_key", idempotency_key)
        row = self._conn.execute(f"SELECT * FROM task_outbox WHERE {column}=?", (value,)).fetchone()
        return self._outbox(row) if row is not None else None

    def list_outbox(self, *, status: str | None = None, limit: int | None = None) -> list[OutboxRecord]:
        sql = "SELECT * FROM task_outbox"
        args: list[object] = []
        if status is not None:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY created_at, outbox_id"
        if limit is not None:
            if int(limit) <= 0:
                raise ValueError("limit must be positive")
            sql += " LIMIT ?"
            args.append(int(limit))
        return [self._outbox(row) for row in self._conn.execute(sql, args).fetchall()]

    def outbox_stats(self) -> dict[str, int]:
        """Return stable durable counts for health/status surfaces."""

        stats = {OUTBOX_PENDING: 0, OUTBOX_PROCESSING: 0, OUTBOX_COMPLETED: 0, OUTBOX_FAILED: 0}
        for row in self._conn.execute("SELECT status,COUNT(*) FROM task_outbox GROUP BY status"):
            if row[0] in stats:
                stats[str(row[0])] = int(row[1])
        return stats

    def claim_outbox(
        self,
        worker_id: str = "worker",
        *,
        limit: int = 1,
        lease_seconds: int = 60,
        now: int | None = None,
    ) -> list[OutboxRecord]:
        owner, count, duration = str(worker_id).strip(), int(limit), int(lease_seconds)
        if not owner or count <= 0 or duration <= 0:
            raise ValueError("worker_id, limit and lease_seconds must be positive")
        timestamp = self._now(now)
        until = timestamp + duration
        claimed: list[OutboxRecord] = []
        with self._write_transaction():
            self._conn.execute(
                "UPDATE task_outbox SET status=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,"
                "last_error=COALESCE(last_error,?),updated_at=? WHERE attempt_count>=max_attempts AND "
                "(status=? OR (status=? AND lease_until IS NOT NULL AND lease_until<=?))",
                (OUTBOX_FAILED, "max_attempts_exceeded", timestamp, OUTBOX_PENDING, OUTBOX_PROCESSING, timestamp),
            )
            rows = self._conn.execute(
                "SELECT outbox_id FROM task_outbox WHERE available_at<=? AND attempt_count<max_attempts AND "
                "(status=? OR (status=? AND lease_until IS NOT NULL AND lease_until<=?)) "
                "ORDER BY created_at,outbox_id LIMIT ?",
                (timestamp, OUTBOX_PENDING, OUTBOX_PROCESSING, timestamp, count),
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                changed = self._conn.execute(
                    "UPDATE task_outbox SET status=?,lease_owner=?,lease_token=?,lease_until=?,"
                    "attempt_count=attempt_count+1,updated_at=? WHERE outbox_id=? AND available_at<=? "
                    "AND attempt_count<max_attempts AND (status=? OR (status=? AND lease_until<=?))",
                    (OUTBOX_PROCESSING, owner, token, until, timestamp, row["outbox_id"], timestamp,
                     OUTBOX_PENDING, OUTBOX_PROCESSING, timestamp),
                )
                if changed.rowcount:
                    claimed.append(self._outbox(self._conn.execute(
                        "SELECT * FROM task_outbox WHERE outbox_id=?", (row["outbox_id"],)
                    ).fetchone()))
        return claimed

    def claim_one(
        self,
        worker_id: str = "worker",
        *,
        lease_seconds: int = 60,
        now: int | None = None,
    ) -> OutboxRecord | None:
        rows = self.claim_outbox(worker_id, limit=1, lease_seconds=lease_seconds, now=now)
        return rows[0] if rows else None

    def _lease(self, item: str | OutboxRecord, token: str | None) -> tuple[str, str]:
        if isinstance(item, OutboxRecord):
            token = token or item.lease_token
            item = item.outbox_id
        if not item or not token:
            raise OutboxLeaseError("outbox id and lease token are required")
        return str(item), str(token)

    def _assert_lease(self, outbox_id: str, token: str, now: int) -> None:
        row = self._conn.execute(
            "SELECT status,lease_token,lease_until FROM task_outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        if row is None or row["status"] != OUTBOX_PROCESSING or row["lease_token"] != token or row["lease_until"] is None or int(row["lease_until"]) <= now:
            raise OutboxLeaseError(f"stale outbox lease: {outbox_id!r}")

    def complete_outbox(
        self,
        item: str | OutboxRecord,
        lease_token: str | None = None,
        *,
        now: int | None = None,
    ) -> OutboxRecord:
        outbox_id, token = self._lease(item, lease_token)
        timestamp = self._now(now)
        with self._write_transaction():
            self._assert_lease(outbox_id, token, timestamp)
            self._conn.execute(
                "UPDATE task_outbox SET status=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,"
                "completed_at=?,updated_at=? WHERE outbox_id=? AND lease_token=? AND lease_until>?",
                (OUTBOX_COMPLETED, timestamp, timestamp, outbox_id, token, timestamp),
            )
            return self._outbox(self._conn.execute(
                "SELECT * FROM task_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone())

    complete = complete_outbox

    def retry_outbox(
        self,
        item: str | OutboxRecord,
        lease_token: str | None = None,
        *,
        error: object | None = None,
        delay_seconds: int = 0,
        now: int | None = None,
    ) -> OutboxRecord:
        outbox_id, token = self._lease(item, lease_token)
        delay = int(delay_seconds)
        if delay < 0:
            raise ValueError("delay_seconds must be non-negative")
        timestamp = self._now(now)
        with self._write_transaction():
            self._assert_lease(outbox_id, token, timestamp)
            row = self._conn.execute(
                "SELECT attempt_count,max_attempts FROM task_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            status = OUTBOX_FAILED if int(row["attempt_count"]) >= int(row["max_attempts"]) else OUTBOX_PENDING
            message = None if error is None else str(error).strip()[:512] or None
            self._conn.execute(
                "UPDATE task_outbox SET status=?,available_at=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,"
                "last_error=?,updated_at=? WHERE outbox_id=? AND lease_token=? AND lease_until>?",
                (status, timestamp + delay, message, timestamp, outbox_id, token, timestamp),
            )
            return self._outbox(self._conn.execute(
                "SELECT * FROM task_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone())

    retry = retry_outbox

    # ---------------------------------------------------------------- query
    def scan(
        self,
        idea_id: str,
        *,
        after_seq: int = 0,
        branch: str | None = None,
        event_types: set[str] | None = None,
    ) -> Iterator[Event]:
        sql = "SELECT * FROM events WHERE idea_id=? AND seq>?"
        args: list = [idea_id, after_seq]
        if branch:
            sql += " AND branch_id=?"
            args.append(branch)
        if event_types:
            ph = ",".join("?" * len(event_types))
            sql += f" AND event_type IN ({ph})"
            args.extend(event_types)
        sql += " ORDER BY seq"
        cur = self._conn.execute(sql, args)
        for r in cur:
            yield self._row_to_event(r)

    def project(self, idea_id: str) -> Projection:
        return _fold((upcast(ev) for ev in self.scan(idea_id)), idea_id=idea_id)

    # ---------------------------------------------------------------- internal
    def _row_to_event(self, r: sqlite3.Row) -> Event:
        return Event(
            idea_id=r["idea_id"], event_type=r["event_type"], actor=r["actor"], source=r["source"],
            payload=json.loads(r["payload"]),
            event_id=r["event_id"], seq=r["seq"], branch_id=r["branch_id"], prev_event_id=r["prev_event_id"],
            phase=r["phase"], schema_version=r["schema_version"], correlation_id=r["correlation_id"],
            causation_id=r["causation_id"], operation_id=r["operation_id"], attempt_id=r["attempt_id"],
            fingerprint=r["fingerprint"], confidence=r["confidence"], side_effect_state=r["side_effect_state"],
            created_at=r["created_at"],
        )

    def _exists_fingerprint(self, ev: Event) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE idea_id=? AND event_type=? AND fingerprint=? LIMIT 1",
            (ev.idea_id, ev.event_type, ev.fingerprint),
        ).fetchone()
        return row is not None

    def _last_seq(self) -> int:
        row = self._conn.execute("SELECT MAX(seq) AS s FROM events").fetchone()
        return int(row["s"]) if row and row["s"] is not None else 0

    def _next_seq(self) -> int:
        return self._last_seq() + 1

    def _set_last_seq(self, seq: int) -> None:
        pass  # seq 由 MAX(events.seq) 推导，无需额外表

    def _prev_event_id(self, idea_id: str, branch: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT event_id FROM events WHERE idea_id=? AND branch_id=? ORDER BY seq DESC LIMIT 1",
            (idea_id, branch),
        ).fetchone()
        return row["event_id"] if row else None

    # ---------------------------------------------------------------- snapshot
    def save_snapshot(self, idea_id: str) -> int:
        """存当前投影摘要为快照（derived），返回 as_of_seq。恢复 = 快照 + 增量重放。"""
        asof = self._last_seq()
        summary = self.project(idea_id).summary()
        blob = json.dumps({"summary": summary, "seq": asof}, ensure_ascii=False).encode()
        now = int(time.time() * 1000)
        with self._conn:
            self._check_lease()
            self._conn.execute("DELETE FROM snapshots WHERE idea_id=? AND kind='full'", (idea_id,))
            self._conn.execute(
                "INSERT INTO snapshots(idea_id,as_of_seq,kind,blob,created_at) VALUES(?,?,'full',?,?)",
                (idea_id, asof, blob, now),
            )
        return asof

    def latest_snapshot(self, idea_id: str):
        """返回 (as_of_seq, summary) 或 None。"""
        row = self._conn.execute(
            "SELECT as_of_seq, blob FROM snapshots WHERE idea_id=? AND kind='full' "
            "ORDER BY as_of_seq DESC LIMIT 1",
            (idea_id,),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row["blob"])
        return int(row["as_of_seq"]), _to_tuples(data["summary"])

    def close(self) -> None:
        self._conn.close()


def _to_tuples(x):
    """json 会把 tuple 存成 list；读回时还原成 tuple。"""
    if isinstance(x, list):
        return tuple(_to_tuples(i) for i in x)
    if isinstance(x, tuple):
        return tuple(_to_tuples(i) for i in x)
    return x
