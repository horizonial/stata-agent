"""SQLite 账本实现 + durable task outbox。"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from typing import Iterator, Optional

from ..domain.reducers import Projection, apply as _apply, fold as _fold
from ..events.append import assert_sane_event
from ..events.schema import (
    EVENT_ARTIFACT,
    EVENT_ATTACHMENT_FAILED,
    EVENT_ATTACHMENT_INTAKE_REQUESTED,
    EVENT_ATTACHMENT_QUARANTINED,
    EVENT_ATTACHMENT_REJECTED,
    Event,
)
from ..events.upcast import upcast
from .migrations import MigrationRunner
from .store import (
    AppendOutboxResult,
    ATTACHMENT_FAILED,
    ATTACHMENT_PENDING,
    ATTACHMENT_QUARANTINED,
    ATTACHMENT_READY,
    ATTACHMENT_REJECTED,
    ATTACHMENT_STATUSES,
    AttachmentConflict,
    AttachmentRecord,
    AttachmentStateError,
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
    OutboxRecoveryConflictError,
    OutboxRecord,
    OutboxRetentionConflictError,
    OutboxRetentionExpectation,
    StaleWrite,
)


class SQLiteStore:
    """单文件账本；单写者由 writer_lease + token 保证，append 每次核对。"""

    def __init__(self, path: str, *, writer_id: str = "w1", takeover: bool = False):
        self._path = path
        self._writer_id = writer_id
        self._token = uuid.uuid4().hex
        # The framework-neutral attachment adapter may stream from an ASGI
        # worker thread while its transport-owned store is created on the
        # request thread.  Keep one ledger connection usable across those
        # threads; the transaction lock below serializes writes on this
        # process-local store instance.
        self._conn = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
        self._write_lock = threading.RLock()
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
        with self._write_lock:
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

    # ----------------------------------------------------------- attachments
    @staticmethod
    def _attachment_row(row: sqlite3.Row | None) -> AttachmentRecord | None:
        if row is None:
            return None
        return AttachmentRecord(
            attachment_id=str(row["attachment_id"]),
            workspace_id=str(row["workspace_id"]),
            idea_id=str(row["idea_id"]),
            display_name=str(row["display_name"]),
            declared_media_type=str(row["declared_media_type"] or ""),
            detected_format=str(row["detected_format"]),
            source_role=str(row["source_role"]),
            status=str(row["status"]),
            byte_size=int(row["byte_size"]),
            sha256=str(row["sha256"]) if row["sha256"] is not None else None,
            storage_key=str(row["storage_key"]) if row["storage_key"] is not None else None,
            parser_version=int(row["parser_version"]) if row["parser_version"] is not None else None,
            page_count=int(row["page_count"]),
            chunk_count=int(row["chunk_count"]),
            extracted_chars=int(row["extracted_chars"]),
            scanned_suspect=bool(row["scanned_suspect"]),
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            ready_at=int(row["ready_at"]) if row["ready_at"] is not None else None,
            expires_at=int(row["expires_at"]) if row["expires_at"] is not None else None,
            latest_event_seq=int(row["latest_event_seq"]) if row["latest_event_seq"] is not None else None,
            state_version=int(row["state_version"]),
            idempotency_key=str(row["idempotency_key"]) if row["idempotency_key"] is not None else None,
        )

    @staticmethod
    def _attachment_event_payload(payload: Mapping[str, object], *, expected_status: str) -> dict[str, object]:
        """Validate the deliberately tiny metadata-only attachment payload."""

        allowed = {
            "attachment_id", "workspace_id", "idea_id", "detected_format", "byte_size",
            "sha256", "source_role", "status", "parser_version", "page_count", "chunk_count",
            "extracted_chars", "scanned_suspect", "error_code", "storage_key",
        }
        forbidden = {
            "display_name", "filename", "path", "absolute_path", "content", "text", "body",
            "bytes", "mime_headers", "exception", "traceback", "parser_output",
        }
        if not isinstance(payload, Mapping):
            raise AttachmentStateError("attachment event payload must be a mapping")
        clean: dict[str, object] = {}
        for key, value in payload.items():
            name = str(key).strip()
            normalized = name.lower().replace("-", "_")
            if normalized in forbidden or normalized not in allowed:
                raise AttachmentStateError("attachment event contains disallowed metadata")
            if isinstance(value, (bytes, bytearray, memoryview, Mapping, list, tuple, set)):
                raise AttachmentStateError("attachment event values must be scalar metadata")
            if isinstance(value, str) and len(value) > 512:
                raise AttachmentStateError("attachment event metadata is too large")
            clean[name] = value
        if clean.get("status") != expected_status:
            raise AttachmentStateError("attachment event status mismatch")
        if "storage_key" in clean:
            storage_key = clean["storage_key"]
            if not isinstance(storage_key, str) or not storage_key or storage_key.startswith(("/", "\\")) or ".." in storage_key:
                raise AttachmentStateError("attachment storage key must be relative")
        return clean

    @staticmethod
    def _attachment_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
        required = {
            "attachment_id", "workspace_id", "idea_id", "display_name", "declared_media_type",
            "detected_format", "source_role", "status", "byte_size", "sha256", "storage_key",
            "parser_version", "page_count", "chunk_count", "extracted_chars", "scanned_suspect",
            "error_code", "created_at", "updated_at", "ready_at", "expires_at", "idempotency_key",
        }
        missing = required - set(metadata)
        if missing:
            raise AttachmentStateError("missing attachment metadata")
        selected = dict(metadata)
        if selected["status"] not in ATTACHMENT_STATUSES:
            raise AttachmentStateError("unknown attachment status")
        if selected["detected_format"] not in {"pdf", "unknown"}:
            raise AttachmentStateError("unknown detected format")
        if selected["source_role"] not in {"style_only", "citable_evidence"}:
            raise AttachmentStateError("unknown attachment source role")
        if not isinstance(selected["display_name"], str) or not 1 <= len(selected["display_name"]) <= 180:
            raise AttachmentStateError("display name is outside bounds")
        for key in ("attachment_id", "workspace_id", "idea_id"):
            identity = selected[key]
            if not isinstance(identity, str) or not identity.strip():
                raise AttachmentStateError(f"{key} must not be blank")
        if not isinstance(selected["declared_media_type"], str) or len(selected["declared_media_type"]) > 128:
            raise AttachmentStateError("declared media type is outside bounds")
        for key in ("byte_size", "page_count", "chunk_count", "extracted_chars", "created_at", "updated_at"):
            value = selected[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AttachmentStateError(f"{key} must be a non-negative integer")
        byte_size = selected["byte_size"]
        page_count = selected["page_count"]
        chunk_count = selected["chunk_count"]
        extracted_chars = selected["extracted_chars"]
        if (
            isinstance(byte_size, int) and byte_size > 25 * 1024 * 1024
            or isinstance(page_count, int) and page_count > 64
            or isinstance(chunk_count, int) and chunk_count > 10000
            or isinstance(extracted_chars, int) and extracted_chars > 2_000_000
        ):
            raise AttachmentStateError("attachment metadata exceeds bounds")
        if not isinstance(selected["scanned_suspect"], bool):
            raise AttachmentStateError("scanned_suspect must be boolean")
        if selected["sha256"] is not None and (
            not isinstance(selected["sha256"], str) or len(selected["sha256"]) != 64 or selected["sha256"] != selected["sha256"].lower()
            or any(ch not in "0123456789abcdef" for ch in selected["sha256"])
        ):
            raise AttachmentStateError("sha256 must be a lowercase digest")
        storage_key = selected["storage_key"]
        if storage_key is not None and (
            not isinstance(storage_key, str) or not storage_key or storage_key.startswith(("/", "\\")) or ".." in storage_key
        ):
            raise AttachmentStateError("storage key must be relative")
        for key in ("parser_version", "ready_at", "expires_at", "latest_event_seq"):
            value = selected.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise AttachmentStateError(f"{key} must be a non-negative integer or null")
        idem = selected["idempotency_key"]
        if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 256):
            raise AttachmentStateError("idempotency key is outside bounds")
        return selected

    def begin_attachment(
        self,
        metadata: Mapping[str, object] | None = None,
        *,
        event: Event,
        now: int | None = None,
        **fields: object,
    ) -> AttachmentRecord:
        """Insert a pending row and its requested event in one narrow txn."""

        selected = dict(metadata or {})
        selected.update(fields)
        selected["status"] = ATTACHMENT_PENDING
        selected = self._attachment_metadata(selected)
        if event.event_type != EVENT_ATTACHMENT_INTAKE_REQUESTED:
            raise AttachmentStateError("pending attachment requires intake requested event")
        if event.idea_id != selected["idea_id"]:
            raise AttachmentStateError("attachment event idea mismatch")
        self._attachment_event_payload(event.payload, expected_status=ATTACHMENT_PENDING)
        timestamp = self._now(now)
        with self._write_transaction():
            self._check_lease()
            existing = self._conn.execute(
                "SELECT * FROM attachments WHERE attachment_id=?", (selected["attachment_id"],)
            ).fetchone()
            if existing is not None:
                row = self._attachment_row(existing)
                if row is None:
                    raise AttachmentConflict("attachment id conflict")
                return row
            if selected["idempotency_key"] is not None:
                existing = self._conn.execute(
                    "SELECT * FROM attachments WHERE workspace_id=? AND idempotency_key=?",
                    (selected["workspace_id"], selected["idempotency_key"]),
                ).fetchone()
                if existing is not None:
                    row = self._attachment_row(existing)
                    if row is None:
                        raise AttachmentConflict("attachment idempotency conflict")
                    return row
            self._validate_candidate(event, self.project(event.idea_id))
            seq = self._insert_event(event)
            created_at = selected["created_at"]
            updated_at = selected["updated_at"]
            if not isinstance(created_at, int) or isinstance(created_at, bool):
                created_at = timestamp
            if not isinstance(updated_at, int) or isinstance(updated_at, bool):
                updated_at = timestamp
            selected["created_at"] = created_at or timestamp
            selected["updated_at"] = updated_at or timestamp
            selected["latest_event_seq"] = seq
            selected["state_version"] = 0
            self._conn.execute(
                "INSERT INTO attachments(attachment_id,workspace_id,idea_id,display_name,declared_media_type,"
                "detected_format,source_role,status,byte_size,sha256,storage_key,parser_version,page_count,"
                "chunk_count,extracted_chars,scanned_suspect,error_code,created_at,updated_at,ready_at,expires_at,"
                "latest_event_seq,state_version,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    selected["attachment_id"], selected["workspace_id"], selected["idea_id"], selected["display_name"],
                    selected["declared_media_type"], selected["detected_format"], selected["source_role"], selected["status"],
                    selected["byte_size"], selected["sha256"], selected["storage_key"], selected["parser_version"],
                    selected["page_count"], selected["chunk_count"], selected["extracted_chars"],
                    int(selected["scanned_suspect"]) if isinstance(selected["scanned_suspect"], bool) else 0,
                    selected["error_code"], selected["created_at"], selected["updated_at"], selected["ready_at"],
                    selected["expires_at"], selected["latest_event_seq"], selected["state_version"], selected["idempotency_key"],
                ),
            )
            return self._attachment_row(self._conn.execute(
                "SELECT * FROM attachments WHERE attachment_id=?", (selected["attachment_id"],)
            ).fetchone())  # type: ignore[return-value]

    def transition_attachment(
        self,
        attachment_id: str,
        *,
        workspace_id: str,
        expected_state_version: int,
        status: str,
        event: Event,
        updates: Mapping[str, object] | None = None,
        now: int | None = None,
    ) -> AttachmentRecord:
        """CAS one attachment lifecycle transition and append its event."""

        if status not in ATTACHMENT_STATUSES or status == ATTACHMENT_PENDING:
            raise AttachmentStateError("terminal attachment status required")
        if isinstance(expected_state_version, bool) or not isinstance(expected_state_version, int) or expected_state_version < 0:
            raise AttachmentConflict("invalid attachment state version")
        event_type = {
            ATTACHMENT_READY: EVENT_ARTIFACT,
            ATTACHMENT_QUARANTINED: EVENT_ATTACHMENT_QUARANTINED,
            ATTACHMENT_REJECTED: EVENT_ATTACHMENT_REJECTED,
            ATTACHMENT_FAILED: EVENT_ATTACHMENT_FAILED,
        }[status]
        if event.event_type != event_type:
            raise AttachmentStateError("attachment event type/status mismatch")
        self._attachment_event_payload(event.payload, expected_status=status)
        timestamp = self._now(now)
        values = dict(updates or {})
        allowed_updates = {
            "detected_format", "status", "byte_size", "sha256", "storage_key", "parser_version",
            "page_count", "chunk_count", "extracted_chars", "scanned_suspect", "error_code", "ready_at",
            "expires_at", "updated_at",
        }
        if set(values) - allowed_updates:
            raise AttachmentStateError("unsupported attachment transition metadata")
        values["status"] = status
        values["updated_at"] = timestamp
        with self._write_transaction():
            self._check_lease()
            row = self._conn.execute(
                "SELECT * FROM attachments WHERE attachment_id=? AND workspace_id=?", (attachment_id, workspace_id)
            ).fetchone()
            current = self._attachment_row(row)
            if current is None:
                raise AttachmentConflict("attachment not found in workspace")
            allowed_next = {
                ATTACHMENT_PENDING: {ATTACHMENT_READY, ATTACHMENT_QUARANTINED, ATTACHMENT_REJECTED, ATTACHMENT_FAILED},
                ATTACHMENT_QUARANTINED: {ATTACHMENT_REJECTED},
                # A ready row is immutable from the application's point of
                # view, but reconciliation must be able to fail closed when
                # its canonical object disappears or changes digest.
                ATTACHMENT_READY: {ATTACHMENT_FAILED},
                ATTACHMENT_REJECTED: set(), ATTACHMENT_FAILED: set(),
            }
            if current.state_version != expected_state_version or status not in allowed_next.get(current.status, set()):
                raise AttachmentConflict("stale or invalid attachment transition")
            if event.idea_id != current.idea_id:
                raise AttachmentStateError("attachment event idea mismatch")
            payload = dict(event.payload)
            if payload.get("attachment_id") != current.attachment_id or payload.get("workspace_id") != current.workspace_id:
                raise AttachmentStateError("attachment event identity mismatch")
            self._validate_candidate(event, self.project(event.idea_id))
            seq = self._insert_event(event)
            values["latest_event_seq"] = seq
            assignments = ",".join(f"{key}=?" for key in values)
            params = [
                int(value) if key == "scanned_suspect" and isinstance(value, bool) else value
                for key, value in values.items()
            ]
            params.extend([attachment_id, workspace_id, expected_state_version])
            try:
                changed = self._conn.execute(
                    f"UPDATE attachments SET {assignments}, state_version=state_version+1 "
                    "WHERE attachment_id=? AND workspace_id=? AND state_version=?",
                    (*params,),
                )
            except sqlite3.IntegrityError as exc:
                raise AttachmentConflict("attachment transition violates metadata constraints") from exc
            if changed.rowcount != 1:
                raise AttachmentConflict("stale attachment transition")
            return self._attachment_row(self._conn.execute(
                "SELECT * FROM attachments WHERE attachment_id=?", (attachment_id,)
            ).fetchone())  # type: ignore[return-value]

    def get_attachment(self, attachment_id: str, *, workspace_id: str | None = None) -> AttachmentRecord | None:
        if workspace_id is None:
            row = self._conn.execute("SELECT * FROM attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM attachments WHERE attachment_id=? AND workspace_id=?", (attachment_id, workspace_id)
            ).fetchone()
        return self._attachment_row(row)

    def get_attachment_by_hash(self, sha256: str, *, workspace_id: str) -> AttachmentRecord | None:
        if not isinstance(sha256, str) or len(sha256) != 64:
            return None
        row = self._conn.execute(
            "SELECT * FROM attachments WHERE workspace_id=? AND sha256=?", (workspace_id, sha256.lower())
        ).fetchone()
        return self._attachment_row(row)

    def get_attachment_by_idempotency(self, idempotency_key: str, *, workspace_id: str) -> AttachmentRecord | None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return None
        row = self._conn.execute(
            "SELECT * FROM attachments WHERE workspace_id=? AND idempotency_key=?",
            (workspace_id, idempotency_key.strip()),
        ).fetchone()
        return self._attachment_row(row)

    def list_attachments(
        self,
        *,
        workspace_id: str,
        limit: int = 100,
        statuses: set[str] | None = None,
    ) -> list[AttachmentRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        sql = "SELECT * FROM attachments WHERE workspace_id=?"
        args: list[object] = [workspace_id]
        if statuses:
            unknown = set(statuses) - set(ATTACHMENT_STATUSES)
            if unknown:
                raise ValueError("unknown attachment status")
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            args.extend(sorted(statuses))
        sql += " ORDER BY updated_at DESC, attachment_id DESC LIMIT ?"
        args.append(limit)
        records: list[AttachmentRecord] = []
        for row in self._conn.execute(sql, args).fetchall():
            record = self._attachment_row(row)
            if record is not None:
                records.append(record)
        return records

    def list_attachment_candidates(self, *, workspace_id: str, limit: int = 100) -> list[AttachmentRecord]:
        """Bounded maintenance page; it never scans or mutates a whole root."""

        return self.list_attachments(
            workspace_id=workspace_id,
            limit=limit,
            statuses={ATTACHMENT_PENDING, ATTACHMENT_QUARANTINED, ATTACHMENT_READY},
        )

    def attachment_usage(self, *, workspace_id: str) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(byte_size),0) AS bytes, "
            "SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) AS ready_count "
            "FROM attachments WHERE workspace_id=? AND status IN ('pending','quarantined','ready')",
            (workspace_id,),
        ).fetchone()
        return int(row["bytes"] or 0), int(row["ready_count"] or 0)

    def _validate_candidate(
        self,
        event: Event,
        projection: Projection,
        *,
        seen_fingerprints: set[tuple[str, str, str]] | None = None,
    ) -> None:
        """Validate one candidate before its INSERT executes."""
        attachment_event_types = {
            EVENT_ATTACHMENT_INTAKE_REQUESTED: ATTACHMENT_PENDING,
            EVENT_ARTIFACT: ATTACHMENT_READY,
            EVENT_ATTACHMENT_QUARANTINED: ATTACHMENT_QUARANTINED,
            EVENT_ATTACHMENT_REJECTED: ATTACHMENT_REJECTED,
            EVENT_ATTACHMENT_FAILED: ATTACHMENT_FAILED,
        }
        expected_attachment_status = attachment_event_types.get(event.event_type)
        if expected_attachment_status is not None and (
            event.event_type != EVENT_ARTIFACT or "attachment_id" in event.payload
        ):
            self._attachment_event_payload(event.payload, expected_status=expected_attachment_status)
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
            int(row["state_version"]),
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
                    "UPDATE task_outbox SET event_seq=?, updated_at=?,state_version=state_version+1 "
                    "WHERE outbox_id=?",
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
            "available_at,lease_owner,lease_token,lease_until,last_error,event_seq,created_at,updated_at,completed_at,"
            "state_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (outbox_id, intent.idempotency_key, intent.task_type, payload_text, OUTBOX_PENDING, 0,
             intent.max_attempts, int(intent.available_at or 0), None, None, None, None, selected_seq,
             timestamp, timestamp, None, 0),
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

    @staticmethod
    def _validate_retention_task_type(task_type: str) -> str:
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type must not be blank")
        return task_type.strip()

    @staticmethod
    def _validate_retention_integer(value: object, name: str, *, positive: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be a {'positive' if positive else 'non-negative'} integer")
        if (positive and value <= 0) or (not positive and value < 0):
            raise ValueError(f"{name} must be a {'positive' if positive else 'non-negative'} integer")
        return value

    def list_completed_outbox_before(
        self,
        task_type: str,
        completed_before: int,
        limit: int,
        after_completed_at: int | None = None,
        after_outbox_id: str | None = None,
    ) -> list[OutboxRecord]:
        """List completed rows using a stable, bounded keyset page.

        The cursor is an exclusive ``(completed_at, outbox_id)`` tuple.  The
        query intentionally remains a narrow storage primitive: callers choose
        the task type and cutoff, while status and non-null completion are
        fixed here so this method cannot become an arbitrary row scanner.
        """

        selected_type = self._validate_retention_task_type(task_type)
        cutoff = self._validate_retention_integer(completed_before, "completed_before")
        page_limit = self._validate_retention_integer(limit, "limit", positive=True)
        if (after_completed_at is None) != (after_outbox_id is None):
            raise ValueError("after_completed_at and after_outbox_id must be supplied together")
        if after_completed_at is not None:
            cursor_completed_at = self._validate_retention_integer(
                after_completed_at, "after_completed_at"
            )
            if not isinstance(after_outbox_id, str) or not after_outbox_id.strip():
                raise ValueError("after_outbox_id must not be blank")
            cursor_outbox_id = after_outbox_id.strip()
        else:
            cursor_completed_at = None
            cursor_outbox_id = None

        sql = (
            "SELECT * FROM task_outbox "
            "WHERE task_type=? AND status=? AND completed_at IS NOT NULL AND completed_at<=?"
        )
        args: list[object] = [selected_type, OUTBOX_COMPLETED, cutoff]
        if cursor_completed_at is not None and cursor_outbox_id is not None:
            sql += " AND (completed_at>? OR (completed_at=? AND outbox_id>?))"
            args.extend([cursor_completed_at, cursor_completed_at, cursor_outbox_id])
        sql += " ORDER BY completed_at ASC, outbox_id ASC LIMIT ?"
        args.append(page_limit)
        return [self._outbox(row) for row in self._conn.execute(sql, args).fetchall()]

    def delete_completed_outbox_batch(
        self,
        expectations: Iterable[OutboxRetentionExpectation],
        *,
        cutoff: int,
    ) -> int:
        """Delete one exact completed-row batch atomically.

        Every row is fenced by its immutable identity, completion timestamp,
        and state generation.  A single mismatch raises a stable retention
        conflict and rolls back all earlier deletes in this batch.
        """

        selected_cutoff = self._validate_retention_integer(cutoff, "cutoff")
        try:
            batch = tuple(expectations)
        except TypeError as exc:
            raise TypeError("expectations must be iterable") from exc
        if not batch:
            return 0

        seen_outbox_ids: set[str] = set()
        seen_keys: set[str] = set()
        for expectation in batch:
            if not isinstance(expectation, OutboxRetentionExpectation):
                raise TypeError("expectations must contain OutboxRetentionExpectation values")
            if expectation.outbox_id in seen_outbox_ids or expectation.idempotency_key in seen_keys:
                raise ValueError("duplicate retention expectation")
            seen_outbox_ids.add(expectation.outbox_id)
            seen_keys.add(expectation.idempotency_key)

        with self._write_transaction():
            self._check_lease()
            for expectation in batch:
                changed = self._conn.execute(
                    "DELETE FROM task_outbox WHERE outbox_id=? AND idempotency_key=? AND task_type=? "
                    "AND status=? AND completed_at IS NOT NULL AND completed_at=? AND completed_at<=? "
                    "AND state_version=?",
                    (
                        expectation.outbox_id,
                        expectation.idempotency_key,
                        expectation.task_type,
                        OUTBOX_COMPLETED,
                        expectation.completed_at,
                        selected_cutoff,
                        expectation.expected_state_version,
                    ),
                )
                if changed.rowcount != 1:
                    raise OutboxRetentionConflictError("completed outbox retention conflict")
        return len(batch)

    @staticmethod
    def _validate_failed_recovery_args(
        idempotency_key: str,
        task_type: str,
        expected_state_version: int,
    ) -> tuple[str, str, int]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise OutboxRecoveryConflictError("failed outbox recovery conflict")
        if not isinstance(task_type, str) or not task_type.strip():
            raise OutboxRecoveryConflictError("failed outbox recovery conflict")
        if (
            isinstance(expected_state_version, bool)
            or not isinstance(expected_state_version, int)
            or expected_state_version < 0
        ):
            raise OutboxRecoveryConflictError("failed outbox recovery conflict")
        return idempotency_key.strip(), task_type.strip(), expected_state_version

    def reconcile_failed_outbox(
        self,
        idempotency_key: str,
        task_type: str,
        expected_state_version: int,
        *,
        now: int | None = None,
    ) -> OutboxRecord:
        """Reconcile one exact failed generation to completed.

        The caller must establish terminal-event authority before invoking this
        storage primitive.  This method only performs the fenced row update;
        it never scans the ledger or dispatches provider work.
        """

        key, selected_type, version = self._validate_failed_recovery_args(
            idempotency_key, task_type, expected_state_version
        )
        timestamp = self._now(now)
        with self._write_transaction():
            self._check_lease()
            changed = self._conn.execute(
                "UPDATE task_outbox SET status=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,"
                "completed_at=?,updated_at=?,state_version=state_version+1 "
                "WHERE idempotency_key=? AND task_type=? AND status=? AND state_version=?",
                (OUTBOX_COMPLETED, timestamp, timestamp, key, selected_type, OUTBOX_FAILED, version),
            )
            if changed.rowcount != 1:
                raise OutboxRecoveryConflictError("failed outbox recovery conflict")
            row = self._conn.execute(
                "SELECT * FROM task_outbox WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None:
                raise OutboxRecoveryConflictError("failed outbox recovery conflict")
            return self._outbox(row)

    def redrive_failed_outbox(
        self,
        idempotency_key: str,
        task_type: str,
        expected_state_version: int,
        *,
        now: int | None = None,
    ) -> OutboxRecord:
        """Return one exact failed generation to normal pending dispatch.

        The transition does not call a dispatcher or provider.  It creates a
        fresh bounded attempt budget while retaining the original task
        identity, payload, event sequence, and failure evidence.
        """

        key, selected_type, version = self._validate_failed_recovery_args(
            idempotency_key, task_type, expected_state_version
        )
        timestamp = self._now(now)
        with self._write_transaction():
            self._check_lease()
            changed = self._conn.execute(
                "UPDATE task_outbox SET status=?,attempt_count=0,available_at=?,"
                "lease_owner=NULL,lease_token=NULL,lease_until=NULL,completed_at=NULL,"
                "updated_at=?,state_version=state_version+1 "
                "WHERE idempotency_key=? AND task_type=? AND status=? AND state_version=?",
                (OUTBOX_PENDING, timestamp, timestamp, key, selected_type, OUTBOX_FAILED, version),
            )
            if changed.rowcount != 1:
                raise OutboxRecoveryConflictError("failed outbox recovery conflict")
            row = self._conn.execute(
                "SELECT * FROM task_outbox WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None:
                raise OutboxRecoveryConflictError("failed outbox recovery conflict")
            return self._outbox(row)

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

    @staticmethod
    def _diagnostic_limit(limit: int, *, maximum: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise ValueError(f"limit must be an integer in 1..{maximum}")
        return limit

    def scan_recent_events(
        self,
        idea_id: str,
        *,
        limit: int = 200,
        correlation_id: str | None = None,
        before_seq: int | None = None,
    ) -> tuple[list[Event], bool]:
        """Return a bounded chronological event page for diagnostics.

        The query is deliberately separate from the general ``scan`` API so a
        support export cannot accidentally materialize an unbounded ledger.
        ``LIMIT limit + 1`` reports truncation without a count query.
        """

        page_size = self._diagnostic_limit(limit, maximum=500)
        if before_seq is not None and (
            isinstance(before_seq, bool) or not isinstance(before_seq, int) or before_seq < 0
        ):
            raise ValueError("before_seq must be a non-negative integer")
        sql = "SELECT * FROM events WHERE idea_id=?"
        args: list[object] = [idea_id]
        if correlation_id is not None:
            sql += " AND correlation_id=?"
            args.append(correlation_id)
        if before_seq is not None:
            sql += " AND seq<?"
            args.append(before_seq)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(page_size + 1)
        rows = self._conn.execute(sql, args).fetchall()
        truncated = len(rows) > page_size
        return [self._row_to_event(row) for row in reversed(rows[:page_size])], truncated

    def count_event_correlations(
        self,
        idea_id: str,
        *,
        event_types: set[str] | None = None,
    ) -> int:
        """Count request correlations without materialising event payloads.

        Activity pagination reads only a bounded event window.  This small
        aggregate keeps the unfiltered/category ``total_groups`` honest even
        when older pages are fetched, while the projector still owns all
        semantic status/search decisions.
        """

        sql = (
            "SELECT COUNT(DISTINCT correlation_id) FROM events "
            "WHERE idea_id=? AND correlation_id IS NOT NULL AND TRIM(correlation_id)<>''"
        )
        args: list[object] = [idea_id]
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            sql += f" AND event_type IN ({placeholders})"
            args.extend(sorted(event_types))
        row = self._conn.execute(sql, args).fetchone()
        return int(row[0] or 0) if row is not None else 0

    def scan_recent_outbox(
        self,
        *,
        task_type: str = "memory.extraction",
        limit: int = 100,
    ) -> tuple[list[OutboxRecord], bool]:
        """Return a bounded, deterministic outbox page for diagnostics."""

        page_size = self._diagnostic_limit(limit, maximum=200)
        rows = self._conn.execute(
            "SELECT * FROM task_outbox WHERE task_type=? "
            "ORDER BY updated_at DESC, outbox_id DESC LIMIT ?",
            (task_type, page_size + 1),
        ).fetchall()
        truncated = len(rows) > page_size
        return [self._outbox(row) for row in reversed(rows[:page_size])], truncated

    def outbox_stats(self) -> dict[str, int]:
        """Return stable durable counts for health/status surfaces."""

        stats = {OUTBOX_PENDING: 0, OUTBOX_PROCESSING: 0, OUTBOX_COMPLETED: 0, OUTBOX_FAILED: 0}
        for row in self._conn.execute("SELECT status,COUNT(*) FROM task_outbox GROUP BY status"):
            if row[0] in stats:
                stats[str(row[0])] = int(row[1])
        now = int(time.time())
        operational = self._conn.execute(
            "SELECT "
            "SUM(CASE WHEN status=? AND available_at<=? THEN 1 ELSE 0 END) AS ready,"
            "SUM(CASE WHEN status=? AND lease_until IS NOT NULL AND lease_until<=? THEN 1 ELSE 0 END) AS expired,"
            "MIN(CASE WHEN status=? THEN created_at END) AS oldest_pending "
            "FROM task_outbox",
            (OUTBOX_PENDING, now, OUTBOX_PROCESSING, now, OUTBOX_PENDING),
        ).fetchone()
        oldest = operational["oldest_pending"] if operational is not None else None
        stats["ready"] = int(operational["ready"] or 0) if operational is not None else 0
        stats["expired_leases"] = int(operational["expired"] or 0) if operational is not None else 0
        stats["oldest_pending_age_seconds"] = max(0, now - int(oldest)) if oldest is not None else 0
        return stats

    def claim_outbox(
        self,
        worker_id: str = "worker",
        *,
        task_type: str | None = None,
        limit: int = 1,
        lease_seconds: int = 60,
        now: int | None = None,
    ) -> list[OutboxRecord]:
        owner, count, duration = str(worker_id).strip(), int(limit), int(lease_seconds)
        if not owner or count <= 0 or duration <= 0:
            raise ValueError("worker_id, limit and lease_seconds must be positive")
        selected_type = str(task_type or "").strip() or None
        timestamp = self._now(now)
        until = timestamp + duration
        claimed: list[OutboxRecord] = []
        with self._write_transaction():
            self._conn.execute(
                "UPDATE task_outbox SET status=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,"
                "last_error=COALESCE(last_error,?),updated_at=?,state_version=state_version+1 "
                "WHERE attempt_count>=max_attempts AND "
                "(status=? OR (status=? AND lease_until IS NOT NULL AND lease_until<=?))",
                (OUTBOX_FAILED, "max_attempts_exceeded", timestamp, OUTBOX_PENDING, OUTBOX_PROCESSING, timestamp),
            )
            sql = (
                "SELECT outbox_id FROM task_outbox WHERE available_at<=? AND attempt_count<max_attempts AND "
                "(status=? OR (status=? AND lease_until IS NOT NULL AND lease_until<=?))"
            )
            args: list[object] = [timestamp, OUTBOX_PENDING, OUTBOX_PROCESSING, timestamp]
            if selected_type is not None:
                sql += " AND task_type=?"
                args.append(selected_type)
            sql += " ORDER BY created_at,outbox_id LIMIT ?"
            args.append(count)
            rows = self._conn.execute(sql, args).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                changed = self._conn.execute(
                    "UPDATE task_outbox SET status=?,lease_owner=?,lease_token=?,lease_until=?,"
                    "attempt_count=attempt_count+1,updated_at=?,state_version=state_version+1 "
                    "WHERE outbox_id=? AND available_at<=? "
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
        task_type: str | None = None,
        lease_seconds: int = 60,
        now: int | None = None,
    ) -> OutboxRecord | None:
        rows = self.claim_outbox(
            worker_id,
            task_type=task_type,
            limit=1,
            lease_seconds=lease_seconds,
            now=now,
        )
        return rows[0] if rows else None

    def renew_outbox_lease(
        self,
        item: str | OutboxRecord,
        lease_token: str | None = None,
        *,
        lease_seconds: int | float,
        now: int | None = None,
    ) -> OutboxRecord:
        """Extend one active outbox lease without changing delivery attempts.

        Renewal is deliberately fenced by the same token used for completion,
        retry, and release.  The stored lease is integer-second based, so a
        positive fractional duration is conservatively rounded up.
        """

        outbox_id, token = self._lease(item, lease_token)
        if isinstance(lease_seconds, bool):
            raise ValueError("lease_seconds must be positive and finite")
        try:
            requested = float(lease_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("lease_seconds must be positive and finite") from exc
        if not math.isfinite(requested) or requested <= 0:
            raise ValueError("lease_seconds must be positive and finite")
        duration = max(1, int(math.ceil(requested)))
        timestamp = self._now(now)
        with self._write_transaction():
            self._assert_lease(outbox_id, token, timestamp)
            row = self._conn.execute(
                "SELECT lease_until FROM task_outbox WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
            if row is None or row["lease_until"] is None:
                raise OutboxLeaseError(f"stale outbox lease: {outbox_id!r}")
            current_until = int(row["lease_until"])
            renewed_until = max(current_until, timestamp + duration)
            changed = self._conn.execute(
                "UPDATE task_outbox SET lease_until=?,updated_at=?,state_version=state_version+1 "
                "WHERE outbox_id=? AND status=? AND lease_token=? AND lease_until>?",
                (renewed_until, timestamp, outbox_id, OUTBOX_PROCESSING, token, timestamp),
            )
            if changed.rowcount != 1:
                raise OutboxLeaseError(f"stale outbox lease: {outbox_id!r}")
            return self._outbox(self._conn.execute(
                "SELECT * FROM task_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone())

    renew = renew_outbox_lease

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
                "completed_at=?,updated_at=?,state_version=state_version+1 "
                "WHERE outbox_id=? AND lease_token=? AND lease_until>?",
                (OUTBOX_COMPLETED, timestamp, timestamp, outbox_id, token, timestamp),
            )
            return self._outbox(self._conn.execute(
                "SELECT * FROM task_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone())

    complete = complete_outbox

    def release_outbox(
        self,
        item: str | OutboxRecord,
        lease_token: str | None = None,
        *,
        error: object | None = None,
        delay_seconds: int = 0,
        now: int | None = None,
    ) -> OutboxRecord:
        """Release an unstarted claim without consuming an execution attempt."""

        outbox_id, token = self._lease(item, lease_token)
        delay = int(delay_seconds)
        if delay < 0:
            raise ValueError("delay_seconds must be non-negative")
        timestamp = self._now(now)
        with self._write_transaction():
            self._assert_lease(outbox_id, token, timestamp)
            message = None if error is None else str(error).strip()[:512] or None
            self._conn.execute(
                "UPDATE task_outbox SET status=?,attempt_count=MAX(0,attempt_count-1),available_at=?,"
                "lease_owner=NULL,lease_token=NULL,lease_until=NULL,last_error=?,updated_at=?,"
                "state_version=state_version+1 "
                "WHERE outbox_id=? AND lease_token=? AND lease_until>?",
                (OUTBOX_PENDING, timestamp + delay, message, timestamp, outbox_id, token, timestamp),
            )
            return self._outbox(self._conn.execute(
                "SELECT * FROM task_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone())

    release = release_outbox

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
                "last_error=?,updated_at=?,state_version=state_version+1 "
                "WHERE outbox_id=? AND lease_token=? AND lease_until>?",
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
