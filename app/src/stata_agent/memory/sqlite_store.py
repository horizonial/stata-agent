"""SQLite-backed compatibility facade for the project-memory V2 API.

``MemoryStore`` remains the public behavioural contract used by the runner,
tool context, extraction pipeline, and current UI.  This adapter intentionally
keeps that contract at the edge while delegating persistence to
``SQLiteMemoryRepository``.  It does not call ``MemoryStore.__init__``: doing
so would load a database path as JSON and acquire the legacy process-local
path lock.

The repository owns transactional writes and workspace constraints.  This
module owns the policy that is deliberately not a persistence concern:
secret/live-metric/evidence validation, deterministic token ranking, bounded
context formatting, and expiration/usage selection.
"""

from __future__ import annotations

import hashlib
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..rag.retriever import tokenize
from .memstore import (
    CONFIDENCE_EXPLICIT,
    CONFIDENCE_INFERRED,
    GLOBAL_WORKSPACE_ID,
    KIND_PREFERENCE,
    MemoryCandidateRejected,
    MemoryContextSelection,
    MemoryStore,
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    _format_memory_context,
    _lifecycle_issues,
    _memory_relevance,
    _rank_memory_entry,
)
from .sqlite_repository import SQLiteMemoryRepository


def _now() -> int:
    return int(time.time())


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _normalise_text(text: str) -> str:
    """Apply the non-policy normalization used by the JSON store."""

    if not isinstance(text, str):
        raise MemoryCandidateRejected("memory text must be a string")
    normalized = " ".join(unicodedata.normalize("NFC", text).split())
    if not normalized:
        raise MemoryCandidateRejected("memory text must not be blank")
    return normalized


def _normalise_workspace(value: Any, fallback: str) -> str:
    """Match MemoryStore/SQLiteMemoryRepository workspace identity rules."""

    if value is None:
        return fallback
    if isinstance(value, Path):
        canonical = str(value.expanduser().resolve(strict=False)).replace("\\", "/").lower()
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    normalized = str(value).strip()
    return normalized or fallback


def _normalise_scope(value: Any) -> str:
    return SCOPE_GLOBAL if str(value or SCOPE_PROJECT).strip().lower() in {"global", "user"} else SCOPE_PROJECT


def _is_expired(entry: Mapping[str, Any], now: int) -> bool:
    expiry = entry.get("expires_at")
    if expiry in (None, ""):
        return False
    if isinstance(expiry, str):
        try:
            expiry_value = float(expiry)
        except ValueError:
            try:
                expiry_value = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError):
                return False
    else:
        try:
            expiry_value = float(expiry)
        except (TypeError, ValueError, OverflowError):
            return False
    return expiry_value <= now


def _confidence_score(confidence: str) -> int:
    return {
        "explicit": 3,
        "verified": 2,
        "inferred": 1,
    }.get(confidence, 0)


def _copy_record(record: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(record)
    if isinstance(copied.get("source_ids"), list):
        copied["source_ids"] = list(copied["source_ids"])
    # These columns belong to the repository's audit schema.  The JSON V2
    # projection omits unset optional fields, so hide their NULL values to
    # keep add()/search()/all() interchangeable for existing callers.
    for key in ("superseded_by", "decided_at", "decision_reason", "accepted_record_id"):
        if copied.get(key) is None:
            copied.pop(key, None)
    return copied


class SQLiteMemoryStore(MemoryStore):
    """MemoryStore-compatible facade backed exclusively by SQLite.

    The subclass relationship is intentional.  Existing integrations use an
    ``isinstance(memory, MemoryStore)`` guard, while this class overrides every
    storage-facing public method and never initializes the JSON implementation.
    Construction explicitly opens the repository (and therefore may create or
    migrate the requested SQLite database); importing this module has no such
    side effect.
    """

    def __init__(
        self,
        path: str | Path,
        workspace_id: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
        legacy_workspace_id: str | Path | None = None,
    ) -> None:
        configured_workspace = workspace_id if workspace_id is not None else workspace
        if configured_workspace is None:
            configured_workspace = legacy_workspace_id
        self._database_path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self._repository = SQLiteMemoryRepository(path, workspace_id=configured_workspace)
        self._closed = False

    @property
    def path(self) -> Path:
        """Return the configured database path, preserving the V1 property."""

        return self._database_path

    @property
    def workspace_id(self) -> str:
        return self._repository.workspace_id

    @staticmethod
    def validate_text(text: str) -> str:
        """Reuse the exact V2 safety guard before every SQLite write."""

        return MemoryStore.validate_text(text)

    def _selected_workspace(
        self,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> str:
        selected = workspace_id if workspace_id is not None else workspace
        return _normalise_workspace(selected, self.workspace_id)

    @staticmethod
    def _workspace_explicit(
        workspace_id: str | Path | None,
        workspace: str | Path | None,
    ) -> bool:
        return workspace_id is not None or workspace is not None

    def _record(self, entry_id: str) -> dict[str, Any] | None:
        """Resolve one record in the current workspace or user-global scope."""

        for record in self._repository.list_records(
            workspace_id=self.workspace_id,
            include_global=True,
            include_inactive=True,
        ):
            if str(record.get("id")) == str(entry_id):
                return record
        return None

    @staticmethod
    def _kind_set(kinds: Iterable[str] | str | None) -> set[str] | None:
        if isinstance(kinds, str):
            return {kinds}
        if kinds is None:
            return None
        return {str(kind) for kind in kinds}

    @staticmethod
    def _rank(
        entry: Mapping[str, Any],
        query_tokens: set[str],
        *,
        exact_workspace: bool,
    ) -> tuple[float, int, int, int, int, str]:
        return _rank_memory_entry(entry, query_tokens, exact_workspace=exact_workspace)

    # ---------------------------------------------------------------- write
    def add(
        self,
        text: str,
        kind: str = KIND_PREFERENCE,
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
        candidate: bool = False,
        accepted: bool = False,
        now: int | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        normalized = self.validate_text(text)
        replacement_id = supersedes or replace_id
        if candidate and not accepted:
            return self.add_candidate(
                normalized,
                kind=kind,
                scope=scope,
                workspace_id=workspace_id,
                workspace=workspace,
                confidence=confidence,
                source_ids=source_ids,
                provenance=provenance,
                supersedes=replacement_id,
                expires_at=expires_at,
                sensitive=sensitive,
                now=now,
            )
        record = self._repository.add(
            normalized,
            kind=kind,
            scope=scope,
            workspace_id=workspace_id,
            workspace=workspace,
            confidence=confidence,
            source_ids=source_ids,
            provenance=provenance,
            supersedes=replacement_id,
            expires_at=expires_at,
            sensitive=sensitive,
            candidate=candidate,
            accepted=accepted,
            now=now,
        )
        return _copy_record(record)

    def add_candidate(
        self,
        text: str,
        kind: str = KIND_PREFERENCE,
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
        now: int | None = None,
    ) -> dict[str, Any]:
        normalized = self.validate_text(text)
        record = self._repository.add_candidate(
            normalized,
            kind=kind,
            scope=scope,
            workspace_id=workspace_id,
            workspace=workspace,
            confidence=confidence,
            source_ids=source_ids,
            provenance=provenance,
            supersedes=supersedes,
            expires_at=expires_at,
            sensitive=sensitive,
            now=now,
        )
        return _copy_record(record)

    def accept_candidate(
        self,
        candidate_id: str,
        *,
        source_ids: Sequence[str] | str | None = None,
        provenance: Any = None,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        explicit_workspace = self._workspace_explicit(workspace_id, workspace)
        record = self._repository.accept_candidate(
            candidate_id,
            source_ids=source_ids,
            provenance=provenance,
            workspace_id=workspace_id,
            workspace=workspace,
            now=now,
        )
        # User-global candidates are intentionally visible without a project
        # override, while project candidates remain isolated to this facade's
        # default workspace.  An explicit workspace always wins.
        if record is None and not explicit_workspace:
            global_candidate = self._repository.get_candidate(
                candidate_id,
                workspace_id=GLOBAL_WORKSPACE_ID,
            )
            if global_candidate is not None and global_candidate.get("scope") == SCOPE_GLOBAL:
                record = self._repository.accept_candidate(
                    candidate_id,
                    source_ids=source_ids,
                    provenance=provenance,
                    workspace_id=GLOBAL_WORKSPACE_ID,
                    now=now,
                )
        return _copy_record(record) if record is not None else None

    def reject_candidate(
        self,
        candidate_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> bool:
        explicit_workspace = self._workspace_explicit(workspace_id, workspace)
        rejected = self._repository.reject_candidate(
            candidate_id,
            workspace_id=workspace_id,
            workspace=workspace,
        )
        if not rejected and not explicit_workspace:
            global_candidate = self._repository.get_candidate(
                candidate_id,
                workspace_id=GLOBAL_WORKSPACE_ID,
            )
            if global_candidate is not None and global_candidate.get("scope") == SCOPE_GLOBAL:
                rejected = self._repository.reject_candidate(
                    candidate_id,
                    workspace_id=GLOBAL_WORKSPACE_ID,
                )
        return rejected

    def retract(
        self,
        entry_id: str,
        *,
        source_ids: Sequence[str] | str | None = None,
        provenance: Any = None,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        target = self._record(entry_id)
        if target is None:
            return None
        now_value = _now() if now is None else _safe_int(now, _now())
        result = self._repository.retract_record(
            entry_id,
            workspace_id=str(target.get("workspace_id") or self.workspace_id),
            source_ids=source_ids,
            provenance=provenance,
            now=now_value,
        )
        if result is None:
            return None
        copied = _copy_record(result)
        # The V2 JSON projection exposes this audit timestamp.  The current
        # repository schema keeps the status/source provenance but has no
        # dedicated retracted_at column, so retain it on this mutation result.
        copied["retracted_at"] = now_value
        return copied

    def supersede(
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
    ) -> dict[str, Any]:
        target = self._record(entry_id)
        if target is None:
            raise KeyError(f"unknown memory to supersede: {entry_id}")
        normalized = self.validate_text(text)
        result = self._repository.add(
            normalized,
            kind=kind or str(target.get("kind", KIND_PREFERENCE)),
            scope=str(target.get("scope", SCOPE_PROJECT)),
            workspace_id=str(target.get("workspace_id") or self.workspace_id),
            confidence=confidence,
            source_ids=source_ids,
            provenance=provenance,
            supersedes=entry_id,
            expires_at=expires_at,
            sensitive=bool(target.get("sensitive", False)),
            now=now,
        )
        return _copy_record(result)

    def touch(self, entry_id: str) -> None:
        target = self._record(entry_id)
        if target is None:
            return
        self._repository.touch(
            entry_id,
            workspace_id=str(target.get("workspace_id") or self.workspace_id),
        )

    # ---------------------------------------------------------------- read
    def all(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        include_candidates: bool = False,
    ) -> list[dict[str, Any]]:
        records = self._repository.all(
            workspace_id=workspace_id,
            workspace=workspace,
            include_candidates=include_candidates,
            include_global=not self._workspace_explicit(workspace_id, workspace),
            include_inactive=True,
        )
        return [_copy_record(record) for record in records]

    def candidates(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        records = self._repository.list_candidates(
            workspace_id=workspace_id,
            workspace=workspace,
            include_global=not self._workspace_explicit(workspace_id, workspace),
            include_decided=False,
        )
        return [_copy_record(record) for record in records if record.get("status") == STATUS_CANDIDATE]

    def candidate(
        self,
        candidate_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> dict[str, Any] | None:
        explicit_workspace = self._workspace_explicit(workspace_id, workspace)
        record = self._repository.get_candidate(
            candidate_id,
            workspace_id=workspace_id,
            workspace=workspace,
        )
        if record is None and not explicit_workspace:
            global_candidate = self._repository.get_candidate(
                candidate_id,
                workspace_id=GLOBAL_WORKSPACE_ID,
            )
            if global_candidate is not None and global_candidate.get("scope") == SCOPE_GLOBAL:
                record = global_candidate
        return _copy_record(record) if record is not None else None

    def lifecycle_issues(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> tuple[str, ...]:
        """Return structural issues in explicit supersede links."""

        explicit_workspace = self._workspace_explicit(workspace_id, workspace)
        records = self._repository.all(
            workspace_id=workspace_id,
            workspace=workspace,
            include_candidates=False,
            include_global=not explicit_workspace,
            include_inactive=True,
        )
        return _lifecycle_issues(records)

    def validate_lifecycle(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> None:
        issues = self.lifecycle_issues(workspace_id=workspace_id, workspace=workspace)
        if issues:
            raise MemoryCandidateRejected("memory_lifecycle_invalid:" + ",".join(issues))

    def search(
        self,
        query: str | None,
        limit: int = 5,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        kinds: Iterable[str] | str | None = None,
        include_global: bool | None = None,
        include_inactive: bool = False,
        now: int | None = None,
        min_relevance: float = 0.0,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        current_workspace = self._selected_workspace(workspace_id, workspace)
        normalized_query = _normalise_text(query) if query and str(query).strip() else ""
        query_tokens = set(tokenize(normalized_query))
        allow_global = bool(include_global) if include_global is not None else False
        kind_set = self._kind_set(kinds)
        now_value = _now() if now is None else _safe_int(now, _now())
        relevance_floor = max(0.0, min(1.0, float(min_relevance)))
        records = self._repository.list_records(
            workspace_id=current_workspace,
            include_global=allow_global,
            include_inactive=include_inactive,
        )
        ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        for record in records:
            if kind_set and str(record.get("kind")) not in kind_set:
                continue
            if not include_inactive and record.get("status") != STATUS_ACTIVE:
                continue
            if not include_inactive and record.get("quarantined"):
                continue
            if not include_inactive and _is_expired(record, now_value):
                continue
            if str(record.get("scope", SCOPE_PROJECT)) == SCOPE_GLOBAL:
                visible = allow_global
            else:
                visible = str(record.get("workspace_id")) == current_workspace
            if not visible:
                continue
            if query_tokens:
                relevance = _memory_relevance(record, query_tokens)
                if relevance <= 0.0 or relevance < relevance_floor:
                    continue
            exact = record.get("scope") != SCOPE_GLOBAL and record.get("workspace_id") == current_workspace
            ranked.append((self._rank(record, query_tokens, exact_workspace=exact), record))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [_copy_record(record) for _, record in ranked[:limit]]

    def select_for_context(
        self,
        query: str | None = "",
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        kinds: Iterable[str] | str | None = None,
        limit: int = 6,
        token_cap: int = 1_500,
        char_cap: int | None = None,
        max_tokens: int | None = None,
        max_chars: int | None = None,
        max_items: int | None = None,
        chars_per_token: int = 4,
        include_global: bool | None = None,
        touch: bool = True,
        now: int | None = None,
        min_relevance: float = 0.0,
        whole_items: bool = False,
    ) -> MemoryContextSelection:
        if max_items is not None:
            limit = int(max_items)
        if max_tokens is not None:
            token_cap = int(max_tokens)
        if max_chars is not None:
            char_cap = int(max_chars)
        if limit <= 0 or token_cap <= 0:
            return MemoryContextSelection([], [], estimated_tokens=0)
        effective_chars_per_token = max(1, chars_per_token)
        max_chars_value = token_cap * effective_chars_per_token
        if char_cap is not None:
            max_chars_value = min(max_chars_value, max(0, int(char_cap)))
        if max_chars_value <= 0:
            return MemoryContextSelection([], [], estimated_tokens=0)

        # Whole-item mode may skip an over-cap high-ranked record. Fetch a
        # small bounded candidate window so a fitting lower-ranked memory can
        # still be selected without exposing an unbounded scan to callers.
        search_limit = max(limit, 32) if whole_items else max(limit, 1)
        records = self.search(
            query,
            limit=search_limit,
            workspace_id=workspace_id,
            workspace=workspace,
            kinds=kinds,
            include_global=include_global,
            now=now,
            min_relevance=min_relevance,
        )
        formatted: list[str] = []
        selected: list[dict[str, Any]] = []
        consumed_chars = 0
        used_records: list[dict[str, Any]] = []
        was_truncated = False
        for record in records:
            source_ids = record.get("source_ids") or []
            line = _format_memory_context(record)
            separator_chars = 1 if formatted else 0
            remaining = max_chars_value - consumed_chars - separator_chars
            if remaining <= 0:
                break
            if len(line) > remaining:
                if whole_items:
                    continue
                line = line[:remaining]
                was_truncated = True
                if line:
                    selected_record = _copy_record(record)
                    selected_record["context"] = line
                    selected_record["formatted"] = line
                    selected_record["provenance"] = list(source_ids)
                    selected.append(selected_record)
                    formatted.append(line)
                    used_records.append(record)
                    consumed_chars += separator_chars + len(line)
                break
            selected_record = _copy_record(record)
            selected_record["context"] = line
            selected_record["formatted"] = line
            selected_record["provenance"] = list(source_ids)
            selected.append(selected_record)
            formatted.append(line)
            used_records.append(record)
            consumed_chars += separator_chars + len(line)
            if len(selected) >= limit:
                break

        if touch:
            for record in used_records:
                self._repository.touch(
                    str(record.get("id")),
                    workspace_id=str(record.get("workspace_id") or self.workspace_id),
                    now=now,
                )
        estimated = (consumed_chars + effective_chars_per_token - 1) // effective_chars_per_token
        return MemoryContextSelection(
            selected,
            formatted,
            estimated_tokens=estimated,
            truncated=was_truncated,
        )

    def to_context(
        self,
        max_items: int = 6,
        *,
        query: str | None = "",
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        token_cap: int | None = None,
        char_cap: int | None = None,
        include_global: bool = False,
    ) -> list[str]:
        if max_items <= 0:
            return []
        if token_cap is None and char_cap is None:
            records = self.search(
                query,
                limit=max_items,
                workspace_id=workspace_id,
                workspace=workspace,
                include_global=include_global,
            )
            return [f"[记忆/{entry.get('kind', KIND_PREFERENCE)}] {entry.get('text', '')}" for entry in records]
        selection = self.select_for_context(
            query,
            workspace_id=workspace_id,
            workspace=workspace,
            limit=max_items,
            token_cap=token_cap if token_cap is not None else max(1, int(char_cap or 1) // 4),
            char_cap=char_cap,
            include_global=include_global,
            touch=False,
        )
        return list(selection.formatted)

    # ------------------------------------------------------------- maintenance
    def consolidate(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        now: int | None = None,
    ) -> int:
        """Return the duplicate count; SQLite prevents active duplicates.

        ``memory_records(workspace_id, scope, fingerprint)`` is a unique
        index.  Unlike a legacy JSON file, a correctly migrated SQLite store
        therefore cannot accumulate the duplicate rows that V2 consolidation
        repairs.  Keeping this method as an explicit no-op preserves the
        public maintenance API and makes the database invariant visible to
        callers without bypassing the repository with ad-hoc SQL writes.
        """

        del workspace_id, workspace, now
        return 0

    def prune(
        self,
        *,
        now: int | None = None,
        max_age_seconds: int | None = None,
        max_age: int | None = None,
        unused_after: int | None = None,
        max_unused_seconds: int | None = None,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        include_explicit: bool = False,
    ) -> int:
        now_value = _now() if now is None else _safe_int(now, _now())
        age = max_age_seconds if max_age_seconds is not None else max_age
        if age is None:
            age = unused_after if unused_after is not None else max_unused_seconds
        selected_workspace = self._selected_workspace(workspace_id, workspace)
        records = self._repository.list_records(
            workspace_id=selected_workspace,
            include_global=False,
            include_inactive=True,
        )
        removed = 0
        for record in records:
            if record.get("status") != STATUS_ACTIVE:
                continue
            expired = _is_expired(record, now_value)
            confidence = str(record.get("confidence", CONFIDENCE_INFERRED))
            updated = max(
                0,
                _safe_int(
                    record.get("last_used_at")
                    if record.get("last_used_at") is not None
                    else record.get("updated_at", record.get("updated", 0))
                ),
            )
            stale_low_confidence = (
                age is not None
                and age >= 0
                and updated + int(age) <= now_value
                and (confidence == CONFIDENCE_INFERRED or include_explicit)
            )
            if not (expired or stale_low_confidence):
                continue
            if self._repository.delete_record(
                str(record.get("id")),
                workspace_id=str(record.get("workspace_id") or selected_workspace),
            ):
                removed += 1
        return removed

    def prune_unused(self, min_used: int = 1) -> int:
        threshold = max(0, int(min_used))
        records = self._repository.list_records(
            workspace_id=self.workspace_id,
            include_global=False,
            include_inactive=True,
        )
        removed = 0
        for record in records:
            if record.get("status") != STATUS_ACTIVE:
                continue
            used = max(0, _safe_int(record.get("use_count", record.get("used", 0))))
            if used >= threshold:
                continue
            if self._repository.delete_record(
                str(record.get("id")),
                workspace_id=str(record.get("workspace_id") or self.workspace_id),
            ):
                removed += 1
        return removed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._repository.close()

    def __enter__(self) -> "SQLiteMemoryStore":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        self.close()


__all__ = ["SQLiteMemoryStore"]
