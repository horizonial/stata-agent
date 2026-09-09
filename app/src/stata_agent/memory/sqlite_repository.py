"""SQLite-backed persistence for the project-memory V2 records.

This module is deliberately a persistence adapter, not a second memory
ranking engine.  It stores the same V2-shaped mappings used by
``MemoryStore`` and leaves context selection, safety policy, and ranking to
the existing service layer.  The adapter is useful during the JSON-to-SQLite
migration because it can be introduced and tested without changing callers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..storage.migrations import MigrationRunner

GLOBAL_WORKSPACE_ID = "global"
SCOPE_PROJECT = "project"
SCOPE_GLOBAL = "global"

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_RETRACTED = "retracted"
STATUS_CANDIDATE = "candidate"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"

CONFIDENCE_EXPLICIT = "explicit"
CONFIDENCE_VERIFIED = "verified"
CONFIDENCE_INFERRED = "inferred"

RECORD_SCHEMA_VERSION = 2
_UNSET = object()

Record = dict[str, Any]


class DuplicateMemoryFingerprint(ValueError):
    """Raised when an update would create a second record fingerprint."""


def _now() -> int:
    return int(time.time())


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _normalise_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("memory text must be a string")
    normalized = " ".join(unicodedata.normalize("NFC", text).split())
    if not normalized:
        raise ValueError("memory text must not be blank")
    return normalized


def _import_text(text: Any) -> tuple[str, bool]:
    """Normalize imported text while retaining blank legacy rows as quarantine."""

    value = text if isinstance(text, str) else str(text or "")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    return normalized, not bool(normalized)


def _fingerprint(text: str) -> str:
    canonical = " ".join(unicodedata.normalize("NFKC", text).split()).casefold()
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _workspace_from_path(path: Path) -> str:
    canonical = str(path.expanduser().resolve(strict=False).parent).replace("\\", "/").lower()
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalise_workspace(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, Path):
        canonical = str(value.expanduser().resolve(strict=False)).replace("\\", "/").lower()
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    normalized = str(value).strip()
    return normalized or fallback


def _normalise_scope(value: Any) -> str:
    return SCOPE_GLOBAL if str(value or SCOPE_PROJECT).strip().lower() in {"global", "user"} else SCOPE_PROJECT


def _normalise_kind(value: Any) -> str:
    normalized = str(value or "preference").strip().lower()
    return normalized or "preference"


def _normalise_confidence(value: Any) -> str:
    normalized = str(value or CONFIDENCE_EXPLICIT).strip().lower()
    return normalized if normalized in {
        CONFIDENCE_EXPLICIT,
        CONFIDENCE_VERIFIED,
        CONFIDENCE_INFERRED,
    } else CONFIDENCE_INFERRED


def _source_ids(*values: Any) -> list[str]:
    collected: list[Any] = []

    def extend(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            collected.append(value)
            return
        if isinstance(value, Mapping):
            for key in ("id", "source_id", "event_id", "seq"):
                if key in value:
                    collected.append(value[key])
            for key in ("ids", "source_ids", "event_ids"):
                if key in value:
                    extend(value[key])
            return
        if isinstance(value, Iterable):
            collected.extend(value)
            return
        collected.append(value)

    for value in values:
        extend(value)
    result: list[str] = []
    seen: set[str] = set()
    for value in collected:
        if value is None:
            continue
        item = str(value).strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _json_text(value: Any, default: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(default, ensure_ascii=False, separators=(",", ":"))


def _decode_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _copy_record(record: Mapping[str, Any]) -> Record:
    copied = dict(record)
    if isinstance(copied.get("source_ids"), list):
        copied["source_ids"] = list(copied["source_ids"])
    return copied


class SQLiteMemoryRepository:
    """Transactional SQLite repository for memory records and candidates.

    A repository has a default workspace used by calls that omit
    ``workspace_id``.  Callers migrating multiple projects should pass the
    workspace explicitly for every operation.  Construction configures a
    connection and runs the explicit schema runner; importing this module or
    reading a JSON file never changes disk state.
    """

    def __init__(
        self,
        path: str | Path,
        workspace_id: str | Path | None = None,
        *,
        timeout: float = 5.0,
        busy_timeout_ms: int | None = None,
    ) -> None:
        database = str(path)
        self._path = Path(path) if database != ":memory:" else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        configured_busy_timeout = (
            max(0, int(timeout * 1000)) if busy_timeout_ms is None else int(busy_timeout_ms)
        )
        if configured_busy_timeout < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self._conn = sqlite3.connect(
            database,
            timeout=max(0.0, float(timeout)),
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={configured_busy_timeout}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._default_workspace_id = _normalise_workspace(
            workspace_id,
            _workspace_from_path(self._path) if self._path is not None else "default",
        )
        self._migrations = MigrationRunner(self._conn)
        self._migrations.run()

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the configured connection for diagnostics and migrations."""

        return self._conn

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def workspace_id(self) -> str:
        return self._default_workspace_id

    @property
    def schema_version(self) -> int:
        return self._migrations.current_version

    def migrate(self) -> int:
        """Explicitly re-run pending migrations; safe and idempotent."""

        return self._migrations.run()

    def applied_migrations(self) -> list[dict[str, object]]:
        return self._migrations.applied()

    def _workspace(self, workspace_id: str | Path | None, workspace: str | Path | None = None) -> str:
        selected = workspace_id if workspace_id is not None else workspace
        return _normalise_workspace(selected, self._default_workspace_id)

    @staticmethod
    def _record_workspace(scope: str, workspace_id: str) -> str:
        return GLOBAL_WORKSPACE_ID if scope == SCOPE_GLOBAL else workspace_id

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @staticmethod
    def _row_mapping(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    @classmethod
    def _row_to_record(cls, row: sqlite3.Row, *, candidate: bool = False) -> Record:
        data = cls._row_mapping(row)
        data["source_ids"] = _decode_json(data.get("source_ids"), [])
        if not isinstance(data["source_ids"], list):
            data["source_ids"] = _source_ids(data["source_ids"])
        for key in ("sensitive", "quarantined"):
            data[key] = bool(data.get(key, 0))
        data["use_count"] = max(0, _safe_int(data.get("use_count")))
        data["used"] = data["use_count"]
        # V1 callers used ``updated`` for usage recency, while V2 separates
        # content recency (``updated_at``) from ``last_used_at``.
        data["updated"] = data.get("last_used_at") or data.get("updated_at")
        if candidate:
            data["status"] = str(data.get("status", STATUS_CANDIDATE))
        return data

    @staticmethod
    def _new_entry(
        text: str,
        *,
        entry_id: str,
        workspace_id: str,
        scope: str,
        kind: str,
        status: str,
        confidence: str,
        source_ids: list[str],
        supersedes: str | None,
        expires_at: Any,
        sensitive: bool,
        quarantined: bool,
        now: int,
    ) -> Record:
        return {
            "id": entry_id,
            "workspace_id": workspace_id,
            "scope": scope,
            "kind": kind,
            "text": text,
            "status": status,
            "confidence": confidence,
            "source_ids": list(source_ids),
            "fingerprint": _fingerprint(text),
            "created_at": now,
            "updated_at": now,
            "last_used_at": None,
            "use_count": 0,
            "supersedes": supersedes,
            "expires_at": expires_at,
            "sensitive": bool(sensitive),
            "quarantined": bool(quarantined),
            "schema_version": RECORD_SCHEMA_VERSION,
            "used": 0,
            "updated": now,
        }

    @staticmethod
    def _insert_record(connection: sqlite3.Connection, entry: Mapping[str, Any]) -> None:
        connection.execute(
            "INSERT INTO memory_records ("
            "id, workspace_id, scope, kind, text, status, confidence, source_ids, fingerprint, "
            "created_at, updated_at, last_used_at, use_count, supersedes, superseded_by, expires_at, "
            "sensitive, quarantined, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["id"],
                entry["workspace_id"],
                entry["scope"],
                entry["kind"],
                entry["text"],
                entry["status"],
                entry["confidence"],
                _json_text(entry.get("source_ids", []), []),
                entry["fingerprint"],
                entry["created_at"],
                entry["updated_at"],
                entry.get("last_used_at"),
                entry.get("use_count", 0),
                entry.get("supersedes"),
                entry.get("superseded_by"),
                entry.get("expires_at"),
                int(bool(entry.get("sensitive", False))),
                int(bool(entry.get("quarantined", False))),
                entry.get("schema_version", RECORD_SCHEMA_VERSION),
            ),
        )

    @staticmethod
    def _insert_candidate(connection: sqlite3.Connection, entry: Mapping[str, Any]) -> None:
        connection.execute(
            "INSERT INTO memory_candidates ("
            "id, workspace_id, scope, kind, text, status, confidence, source_ids, fingerprint, "
            "created_at, updated_at, last_used_at, use_count, supersedes, expires_at, sensitive, "
            "quarantined, schema_version, decided_at, decision_reason, accepted_record_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["id"],
                entry["workspace_id"],
                entry["scope"],
                entry["kind"],
                entry["text"],
                entry["status"],
                entry["confidence"],
                _json_text(entry.get("source_ids", []), []),
                entry["fingerprint"],
                entry["created_at"],
                entry["updated_at"],
                entry.get("last_used_at"),
                entry.get("use_count", 0),
                entry.get("supersedes"),
                entry.get("expires_at"),
                int(bool(entry.get("sensitive", False))),
                int(bool(entry.get("quarantined", False))),
                entry.get("schema_version", RECORD_SCHEMA_VERSION),
                entry.get("decided_at"),
                entry.get("decision_reason"),
                entry.get("accepted_record_id"),
            ),
        )

    @staticmethod
    def _find_record_row(
        connection: sqlite3.Connection,
        *,
        entry_id: str,
        workspace_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM memory_records WHERE id=? AND workspace_id=?",
            (entry_id, workspace_id),
        ).fetchone()

    @staticmethod
    def _find_candidate_row(
        connection: sqlite3.Connection,
        *,
        candidate_id: str,
        workspace_id: str,
        pending_only: bool = False,
    ) -> sqlite3.Row | None:
        sql = "SELECT * FROM memory_candidates WHERE id=? AND workspace_id=?"
        if pending_only:
            sql += " AND status='candidate'"
        return connection.execute(sql, (candidate_id, workspace_id)).fetchone()

    @staticmethod
    def _find_fingerprint_row(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        scope: str,
        fingerprint: str,
        include_candidates: bool,
    ) -> tuple[sqlite3.Row | None, bool]:
        row = connection.execute(
            "SELECT * FROM memory_records WHERE workspace_id=? AND scope=? AND fingerprint=? "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (workspace_id, scope, fingerprint),
        ).fetchone()
        if row is not None:
            return row, False
        if not include_candidates:
            return None, False
        row = connection.execute(
            "SELECT * FROM memory_candidates WHERE workspace_id=? AND scope=? AND fingerprint=? "
            "AND status='candidate' ORDER BY updated_at DESC, id DESC LIMIT 1",
            (workspace_id, scope, fingerprint),
        ).fetchone()
        return row, row is not None

    def add(
        self,
        text: str,
        kind: str = "preference",
        *,
        scope: str = SCOPE_PROJECT,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        confidence: str = CONFIDENCE_EXPLICIT,
        source_ids: Sequence[str] | str | None = None,
        provenance: Any = None,
        supersedes: str | None = None,
        replace_id: str | None = None,
        expires_at: Any = None,
        sensitive: bool = False,
        quarantined: bool = False,
        candidate: bool = False,
        accepted: bool = False,
        now: int | None = None,
    ) -> Record:
        """Create an active record, or delegate to candidate intake."""

        if candidate and not accepted:
            return self.add_candidate(
                text,
                kind=kind,
                scope=scope,
                workspace_id=workspace_id,
                workspace=workspace,
                confidence=confidence,
                source_ids=source_ids,
                provenance=provenance,
                supersedes=supersedes or replace_id,
                expires_at=expires_at,
                sensitive=sensitive,
                quarantined=quarantined,
                now=now,
            )
        normalized_text = _normalise_text(text)
        normalized_scope = _normalise_scope(scope)
        selected_workspace = self._workspace(workspace_id, workspace)
        record_workspace = self._record_workspace(normalized_scope, selected_workspace)
        fingerprint = _fingerprint(normalized_text)
        now_value = _now() if now is None else _safe_int(now, _now())
        replacement_id = supersedes or replace_id
        with self._transaction() as connection:
            existing, is_candidate = self._find_fingerprint_row(
                connection,
                workspace_id=record_workspace,
                scope=normalized_scope,
                fingerprint=fingerprint,
                include_candidates=False,
            )
            if existing is not None and not is_candidate:
                return self._row_to_record(existing)
            target: sqlite3.Row | None = None
            if replacement_id:
                target = self._find_record_row(
                    connection,
                    entry_id=str(replacement_id),
                    workspace_id=record_workspace,
                )
                if target is None or str(target["scope"]) != normalized_scope:
                    raise KeyError(f"unknown memory to supersede: {replacement_id}")
            entry = self._new_entry(
                normalized_text,
                entry_id=f"mem-{uuid.uuid4().hex[:12]}",
                workspace_id=record_workspace,
                scope=normalized_scope,
                kind=_normalise_kind(kind),
                status=STATUS_ACTIVE,
                confidence=_normalise_confidence(confidence),
                source_ids=_source_ids(source_ids, provenance),
                supersedes=str(replacement_id) if replacement_id else None,
                expires_at=expires_at,
                sensitive=sensitive,
                quarantined=quarantined,
                now=now_value,
            )
            if target is not None:
                connection.execute(
                    "UPDATE memory_records SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                    (entry["id"], now_value, target["id"]),
                )
            self._insert_record(connection, entry)
            return _copy_record(entry)

    create_record = add

    def add_candidate(
        self,
        text: str,
        kind: str = "preference",
        *,
        scope: str = SCOPE_PROJECT,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        confidence: str = CONFIDENCE_INFERRED,
        source_ids: Sequence[str] | str | None = None,
        provenance: Any = None,
        supersedes: str | None = None,
        expires_at: Any = None,
        sensitive: bool = False,
        quarantined: bool = False,
        now: int | None = None,
    ) -> Record:
        """Persist a pending candidate with fingerprint idempotence."""

        normalized_text = _normalise_text(text)
        normalized_scope = _normalise_scope(scope)
        selected_workspace = self._workspace(workspace_id, workspace)
        record_workspace = self._record_workspace(normalized_scope, selected_workspace)
        fingerprint = _fingerprint(normalized_text)
        now_value = _now() if now is None else _safe_int(now, _now())
        with self._transaction() as connection:
            existing, is_candidate = self._find_fingerprint_row(
                connection,
                workspace_id=record_workspace,
                scope=normalized_scope,
                fingerprint=fingerprint,
                include_candidates=True,
            )
            if existing is not None:
                return self._row_to_record(existing, candidate=is_candidate)
            entry = self._new_entry(
                normalized_text,
                entry_id=f"cand-{uuid.uuid4().hex[:12]}",
                workspace_id=record_workspace,
                scope=normalized_scope,
                kind=_normalise_kind(kind),
                status=STATUS_CANDIDATE,
                confidence=_normalise_confidence(confidence),
                source_ids=_source_ids(source_ids, provenance),
                supersedes=str(supersedes) if supersedes else None,
                expires_at=expires_at,
                sensitive=sensitive,
                quarantined=quarantined,
                now=now_value,
            )
            self._insert_candidate(connection, entry)
            return _copy_record(entry)

    def get_record(
        self,
        entry_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> Record | None:
        row = self._find_record_row(
            self._conn,
            entry_id=str(entry_id),
            workspace_id=self._workspace(workspace_id, workspace),
        )
        return self._row_to_record(row) if row is not None else None

    read_record = get_record

    def list_records(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        include_global: bool = False,
        include_inactive: bool = True,
        status: str | Iterable[str] | None = None,
    ) -> list[Record]:
        selected_workspace = self._workspace(workspace_id, workspace)
        clauses = ["(workspace_id=? OR (?=1 AND scope='global' AND workspace_id=?))"]
        args: list[Any] = [selected_workspace, int(include_global), GLOBAL_WORKSPACE_ID]
        if not include_inactive:
            clauses.append("status='active'")
        if status is not None:
            statuses = [status] if isinstance(status, str) else [str(item) for item in status]
            if not statuses:
                return []
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
        rows = self._conn.execute(
            "SELECT * FROM memory_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC, id DESC",
            args,
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def all(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        include_candidates: bool = False,
        include_global: bool = False,
        include_inactive: bool = True,
    ) -> list[Record]:
        records = self.list_records(
            workspace_id=workspace_id,
            workspace=workspace,
            include_global=include_global,
            include_inactive=include_inactive,
        )
        if include_candidates:
            records.extend(
                self.list_candidates(
                    workspace_id=workspace_id,
                    workspace=workspace,
                    include_global=include_global,
                )
            )
        return records

    def find_by_fingerprint(
        self,
        fingerprint: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        scope: str = SCOPE_PROJECT,
        include_candidates: bool = False,
    ) -> Record | None:
        normalized_scope = _normalise_scope(scope)
        selected_workspace = self._record_workspace(
            normalized_scope,
            self._workspace(workspace_id, workspace),
        )
        row, is_candidate = self._find_fingerprint_row(
            self._conn,
            workspace_id=selected_workspace,
            scope=normalized_scope,
            fingerprint=str(fingerprint),
            include_candidates=include_candidates,
        )
        return self._row_to_record(row, candidate=is_candidate) if row is not None else None

    def update_record(
        self,
        entry_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        text: str | None = None,
        kind: str | None = None,
        confidence: str | None = None,
        source_ids: Sequence[str] | str | None = None,
        provenance: Any = None,
        expires_at: Any = _UNSET,
        sensitive: bool | None = None,
        quarantined: bool | None = None,
        now: int | None = None,
    ) -> Record | None:
        """Update mutable record fields in one transaction."""

        selected_workspace = self._workspace(workspace_id, workspace)
        now_value = _now() if now is None else _safe_int(now, _now())
        with self._transaction() as connection:
            row = self._find_record_row(connection, entry_id=str(entry_id), workspace_id=selected_workspace)
            if row is None:
                return None
            changes: dict[str, Any] = {"updated_at": now_value}
            if text is not None:
                normalized_text = _normalise_text(text)
                fingerprint = _fingerprint(normalized_text)
                existing, _ = self._find_fingerprint_row(
                    connection,
                    workspace_id=str(row["workspace_id"]),
                    scope=str(row["scope"]),
                    fingerprint=fingerprint,
                    include_candidates=False,
                )
                if existing is not None and str(existing["id"]) != str(entry_id):
                    raise DuplicateMemoryFingerprint(f"memory fingerprint already exists: {fingerprint[:20]}")
                changes.update({"text": normalized_text, "fingerprint": fingerprint})
            if kind is not None:
                changes["kind"] = _normalise_kind(kind)
            if confidence is not None:
                changes["confidence"] = _normalise_confidence(confidence)
            if source_ids is not None or provenance is not None:
                changes["source_ids"] = _json_text(_source_ids(source_ids, provenance), [])
            if expires_at is not _UNSET:
                changes["expires_at"] = expires_at
            if sensitive is not None:
                changes["sensitive"] = int(bool(sensitive))
            if quarantined is not None:
                changes["quarantined"] = int(bool(quarantined))
            assignments = ", ".join(f"{key}=?" for key in changes)
            connection.execute(
                f"UPDATE memory_records SET {assignments} WHERE id=? AND workspace_id=?",
                [*changes.values(), str(entry_id), selected_workspace],
            )
            updated = self._find_record_row(connection, entry_id=str(entry_id), workspace_id=selected_workspace)
            return self._row_to_record(updated) if updated is not None else None

    def delete_record(
        self,
        entry_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_records WHERE id=? AND workspace_id=?",
                (str(entry_id), self._workspace(workspace_id, workspace)),
            )
            return cursor.rowcount > 0

    def retract_record(
        self,
        entry_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        source_ids: Sequence[str] | str | None = None,
        provenance: Any = None,
        now: int | None = None,
    ) -> Record | None:
        selected_workspace = self._workspace(workspace_id, workspace)
        now_value = _now() if now is None else _safe_int(now, _now())
        with self._transaction() as connection:
            row = self._find_record_row(connection, entry_id=str(entry_id), workspace_id=selected_workspace)
            if row is None:
                return None
            merged = _source_ids(_decode_json(row["source_ids"], []), source_ids, provenance)
            connection.execute(
                "UPDATE memory_records SET status='retracted', source_ids=?, updated_at=? WHERE id=? AND workspace_id=?",
                (_json_text(merged, []), now_value, str(entry_id), selected_workspace),
            )
            updated = self._find_record_row(connection, entry_id=str(entry_id), workspace_id=selected_workspace)
            return self._row_to_record(updated) if updated is not None else None

    def supersede_record(
        self,
        entry_id: str,
        text: str,
        *,
        kind: str | None = None,
        confidence: str = CONFIDENCE_EXPLICIT,
        source_ids: Sequence[str] | str | None = None,
        provenance: Any = None,
        expires_at: Any = None,
        now: int | None = None,
    ) -> Record:
        target = self.get_record(entry_id)
        if target is None:
            raise KeyError(f"unknown memory to supersede: {entry_id}")
        return self.add(
            text,
            kind=kind or str(target["kind"]),
            scope=str(target["scope"]),
            workspace_id=str(target["workspace_id"]),
            confidence=confidence,
            source_ids=source_ids,
            provenance=provenance,
            supersedes=entry_id,
            expires_at=expires_at,
            now=now,
        )

    def touch(
        self,
        entry_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        now: int | None = None,
    ) -> bool:
        selected_workspace = self._workspace(workspace_id, workspace)
        now_value = _now() if now is None else _safe_int(now, _now())
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE memory_records SET use_count=use_count+1, last_used_at=? "
                "WHERE id=? AND workspace_id=? AND status='active'",
                (now_value, str(entry_id), selected_workspace),
            )
            return cursor.rowcount > 0

    def list_candidates(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        include_global: bool = False,
        include_decided: bool = False,
    ) -> list[Record]:
        selected_workspace = self._workspace(workspace_id, workspace)
        clauses = ["(workspace_id=? OR (?=1 AND scope='global' AND workspace_id=?))"]
        args: list[Any] = [selected_workspace, int(include_global), GLOBAL_WORKSPACE_ID]
        if not include_decided:
            clauses.append("status='candidate'")
        rows = self._conn.execute(
            "SELECT * FROM memory_candidates WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC, id DESC",
            args,
        ).fetchall()
        return [self._row_to_record(row, candidate=True) for row in rows]

    candidates = list_candidates

    def get_candidate(
        self,
        candidate_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        include_decided: bool = False,
    ) -> Record | None:
        row = self._find_candidate_row(
            self._conn,
            candidate_id=str(candidate_id),
            workspace_id=self._workspace(workspace_id, workspace),
            pending_only=not include_decided,
        )
        return self._row_to_record(row, candidate=True) if row is not None else None

    candidate = get_candidate

    def accept_candidate(
        self,
        candidate_id: str,
        *,
        source_ids: Sequence[str] | str | None = None,
        provenance: Any = None,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        now: int | None = None,
    ) -> Record | None:
        """Promote a candidate and its decision marker in one transaction."""

        selected_workspace = self._workspace(workspace_id, workspace)
        now_value = _now() if now is None else _safe_int(now, _now())
        with self._transaction() as connection:
            row = self._find_candidate_row(
                connection,
                candidate_id=str(candidate_id),
                workspace_id=selected_workspace,
                pending_only=True,
            )
            if row is None:
                return None
            candidate = self._row_to_record(row, candidate=True)
            merged_sources = _source_ids(candidate.get("source_ids"), source_ids, provenance)
            record_workspace = str(candidate["workspace_id"])
            scope = str(candidate["scope"])
            existing, _ = self._find_fingerprint_row(
                connection,
                workspace_id=record_workspace,
                scope=scope,
                fingerprint=str(candidate["fingerprint"]),
                include_candidates=False,
            )
            accepted_record: Record
            if existing is not None:
                accepted_record = self._row_to_record(existing)
                connection.execute(
                    "UPDATE memory_candidates SET status='accepted', source_ids=?, updated_at=?, decided_at=?, "
                    "accepted_record_id=? WHERE id=? AND workspace_id=?",
                    (
                        _json_text(merged_sources, []),
                        now_value,
                        now_value,
                        accepted_record["id"],
                        str(candidate_id),
                        selected_workspace,
                    ),
                )
                return accepted_record
            replacement_id = candidate.get("supersedes")
            target: sqlite3.Row | None = None
            if replacement_id:
                target = self._find_record_row(
                    connection,
                    entry_id=str(replacement_id),
                    workspace_id=record_workspace,
                )
                if target is None or str(target["scope"]) != scope:
                    raise KeyError(f"unknown memory to supersede: {replacement_id}")
            accepted_record = self._new_entry(
                str(candidate["text"]),
                entry_id=f"mem-{uuid.uuid4().hex[:12]}",
                workspace_id=record_workspace,
                scope=scope,
                kind=str(candidate["kind"]),
                status=STATUS_ACTIVE,
                confidence=str(candidate["confidence"]),
                source_ids=merged_sources,
                supersedes=str(replacement_id) if replacement_id else None,
                expires_at=candidate.get("expires_at"),
                sensitive=bool(candidate.get("sensitive", False)),
                quarantined=bool(candidate.get("quarantined", False)),
                now=now_value,
            )
            if target is not None:
                connection.execute(
                    "UPDATE memory_records SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                    (accepted_record["id"], now_value, target["id"]),
                )
            self._insert_record(connection, accepted_record)
            connection.execute(
                "UPDATE memory_candidates SET status='accepted', source_ids=?, updated_at=?, decided_at=?, "
                "accepted_record_id=? WHERE id=? AND workspace_id=?",
                (
                    _json_text(merged_sources, []),
                    now_value,
                    now_value,
                    accepted_record["id"],
                    str(candidate_id),
                    selected_workspace,
                ),
            )
            return _copy_record(accepted_record)

    def reject_candidate(
        self,
        candidate_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        reason: str | None = None,
        now: int | None = None,
    ) -> bool:
        selected_workspace = self._workspace(workspace_id, workspace)
        now_value = _now() if now is None else _safe_int(now, _now())
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE memory_candidates SET status='rejected', decision_reason=?, updated_at=?, decided_at=? "
                "WHERE id=? AND workspace_id=? AND status='candidate'",
                (reason, now_value, now_value, str(candidate_id), selected_workspace),
            )
            return cursor.rowcount > 0

    def register_workspace(
        self,
        workspace_id: str | Path,
        *,
        root: str | Path | None = None,
        name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        now: int | None = None,
    ) -> Record:
        selected_workspace = _normalise_workspace(workspace_id, self._default_workspace_id)
        now_value = _now() if now is None else _safe_int(now, _now())
        metadata_text = _json_text(dict(metadata or {}), {})
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_registry WHERE workspace_id=?",
                (selected_workspace,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO workspace_registry(workspace_id, root, name, metadata, created_at, updated_at, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (selected_workspace, str(root) if root is not None else None, name, metadata_text, now_value, now_value),
                )
            else:
                connection.execute(
                    "UPDATE workspace_registry SET root=?, name=?, metadata=?, updated_at=? WHERE workspace_id=?",
                    (str(root) if root is not None else row["root"], name if name is not None else row["name"], metadata_text, now_value, selected_workspace),
                )
            result = connection.execute(
                "SELECT * FROM workspace_registry WHERE workspace_id=?",
                (selected_workspace,),
            ).fetchone()
            return self._row_mapping(result) if result is not None else {}

    def get_workspace(self, workspace_id: str | Path) -> Record | None:
        selected_workspace = _normalise_workspace(workspace_id, self._default_workspace_id)
        row = self._conn.execute(
            "SELECT * FROM workspace_registry WHERE workspace_id=?",
            (selected_workspace,),
        ).fetchone()
        if row is None:
            return None
        result = self._row_mapping(row)
        result["metadata"] = _decode_json(result.get("metadata"), {})
        return result

    def list_workspaces(self) -> list[Record]:
        rows = self._conn.execute("SELECT * FROM workspace_registry ORDER BY updated_at DESC, workspace_id").fetchall()
        result: list[Record] = []
        for row in rows:
            item = self._row_mapping(row)
            item["metadata"] = _decode_json(item.get("metadata"), {})
            result.append(item)
        return result

    def import_json(
        self,
        json_path: str | Path,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> dict[str, Any]:
        """Explicitly import a legacy/current ``memory.json`` once.

        The source is read only.  Project records retain an embedded
        ``workspace_id`` unless a target workspace is explicitly provided;
        global records always remain global.  Fingerprints make retries
        idempotent, and the complete import is one transaction.
        """

        source = Path(json_path)
        raw = json.loads(source.read_text(encoding="utf-8"))
        records: Any = raw
        candidates: Any = []
        if isinstance(raw, Mapping):
            records = raw.get("records", raw.get("entries", raw.get("memories", [])))
            candidates = raw.get("candidates", [])
        if not isinstance(records, list):
            records = []
        if not isinstance(candidates, list):
            candidates = []
        selected_workspace = self._workspace(workspace_id, workspace)
        override_workspace = workspace_id is not None or workspace is not None
        report: dict[str, Any] = {
            "path": str(source),
            "workspace_id": selected_workspace,
            "records_seen": 0,
            "candidates_seen": 0,
            "records_imported": 0,
            "candidates_imported": 0,
            "duplicates_skipped": 0,
            "invalid_skipped": 0,
        }

        def prepare(raw_entry: Any, *, candidate: bool) -> Record | None:
            if not isinstance(raw_entry, Mapping):
                report["invalid_skipped"] += 1
                return None
            text, blank = _import_text(raw_entry.get("text", ""))
            scope = _normalise_scope(raw_entry.get("scope", SCOPE_PROJECT))
            embedded_workspace = _normalise_workspace(
                raw_entry.get("workspace_id"),
                selected_workspace,
            )
            entry_workspace = (
                GLOBAL_WORKSPACE_ID
                if scope == SCOPE_GLOBAL
                else selected_workspace
                if override_workspace
                else embedded_workspace
            )
            old_updated = _safe_int(raw_entry.get("updated_at", raw_entry.get("updated", 0)))
            created_at = _safe_int(raw_entry.get("created_at", old_updated))
            status = STATUS_CANDIDATE if candidate else str(raw_entry.get("status", STATUS_ACTIVE)).lower()
            if status == STATUS_CANDIDATE:
                candidate = True
            if candidate:
                status = STATUS_CANDIDATE
            elif status not in {STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_RETRACTED}:
                status = STATUS_ACTIVE
            source_ids = _source_ids(
                raw_entry.get("source_ids"),
                raw_entry.get("provenance"),
                raw_entry.get("source_id"),
            )
            return self._new_entry(
                text,
                entry_id=str(raw_entry.get("id") or ("cand-" if candidate else "mem-") + uuid.uuid4().hex[:12]),
                workspace_id=entry_workspace,
                scope=scope,
                kind=_normalise_kind(raw_entry.get("kind", "preference")),
                status=status,
                confidence=_normalise_confidence(raw_entry.get("confidence", CONFIDENCE_EXPLICIT)),
                source_ids=source_ids,
                supersedes=(str(raw_entry["supersedes"]) if raw_entry.get("supersedes") else None),
                expires_at=raw_entry.get("expires_at"),
                sensitive=bool(raw_entry.get("sensitive", False)),
                quarantined=bool(raw_entry.get("quarantined", False)) or blank,
                now=created_at,
            ) | {
                "updated_at": old_updated,
                "updated": old_updated,
                "last_used_at": raw_entry.get("last_used_at"),
                "use_count": max(0, _safe_int(raw_entry.get("use_count", raw_entry.get("used", 0)))),
                "superseded_by": raw_entry.get("superseded_by"),
            }

        with self._transaction() as connection:
            for raw_entry in records:
                report["records_seen"] += 1
                entry = prepare(raw_entry, candidate=False)
                if entry is None:
                    continue
                if entry["status"] == STATUS_CANDIDATE:
                    report["candidates_seen"] += 1
                    entry["id"] = str(entry["id"]).replace("mem-", "cand-", 1)
                    candidate_entry = entry
                    existing, is_candidate = self._find_fingerprint_row(
                        connection,
                        workspace_id=str(entry["workspace_id"]),
                        scope=str(entry["scope"]),
                        fingerprint=str(entry["fingerprint"]),
                        include_candidates=True,
                    )
                    if existing is not None:
                        report["duplicates_skipped"] += 1
                        continue
                    if connection.execute(
                        "SELECT 1 FROM memory_records WHERE id=? UNION ALL SELECT 1 FROM memory_candidates WHERE id=? LIMIT 1",
                        (candidate_entry["id"], candidate_entry["id"]),
                    ).fetchone() is not None:
                        raise ValueError(f"import id collision: {candidate_entry['id']}")
                    self._insert_candidate(connection, candidate_entry)
                    report["candidates_imported"] += 1
                    continue
                existing, _ = self._find_fingerprint_row(
                    connection,
                    workspace_id=str(entry["workspace_id"]),
                    scope=str(entry["scope"]),
                    fingerprint=str(entry["fingerprint"]),
                    include_candidates=True,
                )
                if existing is not None:
                    report["duplicates_skipped"] += 1
                    continue
                if connection.execute(
                    "SELECT 1 FROM memory_records WHERE id=? UNION ALL SELECT 1 FROM memory_candidates WHERE id=? LIMIT 1",
                    (entry["id"], entry["id"]),
                ).fetchone() is not None:
                    raise ValueError(f"import id collision: {entry['id']}")
                self._insert_record(connection, entry)
                report["records_imported"] += 1

            for raw_entry in candidates:
                report["candidates_seen"] += 1
                entry = prepare(raw_entry, candidate=True)
                if entry is None:
                    continue
                existing, _ = self._find_fingerprint_row(
                    connection,
                    workspace_id=str(entry["workspace_id"]),
                    scope=str(entry["scope"]),
                    fingerprint=str(entry["fingerprint"]),
                    include_candidates=True,
                )
                if existing is not None:
                    report["duplicates_skipped"] += 1
                    continue
                if connection.execute(
                    "SELECT 1 FROM memory_records WHERE id=? UNION ALL SELECT 1 FROM memory_candidates WHERE id=? LIMIT 1",
                    (entry["id"], entry["id"]),
                ).fetchone() is not None:
                    raise ValueError(f"import id collision: {entry['id']}")
                self._insert_candidate(connection, entry)
                report["candidates_imported"] += 1
        return report

    import_memory_json = import_json

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteMemoryRepository":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        self.close()


__all__ = [
    "CONFIDENCE_EXPLICIT",
    "CONFIDENCE_INFERRED",
    "CONFIDENCE_VERIFIED",
    "DuplicateMemoryFingerprint",
    "GLOBAL_WORKSPACE_ID",
    "RECORD_SCHEMA_VERSION",
    "SCOPE_GLOBAL",
    "SCOPE_PROJECT",
    "SQLiteMemoryRepository",
    "STATUS_ACCEPTED",
    "STATUS_ACTIVE",
    "STATUS_CANDIDATE",
    "STATUS_REJECTED",
    "STATUS_RETRACTED",
    "STATUS_SUPERSEDED",
]
