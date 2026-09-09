"""Durable project memory.

Memory is deliberately kept separate from the research ledger and evidence
store.  A record in this module is a constraint or a piece of working
knowledge which may be shown to a model; it is never a source of evidence.

The first version of :class:`MemoryStore` stored a plain JSON list with only
``id``, ``text``, ``kind``, ``used`` and ``updated``.  V2 reads that format
without touching the file and upgrades it in memory.  The next mutating
operation atomically writes records containing the V2 fields.  Keeping a list
as the normal on-disk representation is intentional: small legacy tools can
continue to inspect the file while the optional candidate queue uses a
versioned envelope only when it is needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..rag.retriever import tokenize

# Public kind constants.  ``rule`` and ``plan`` remain accepted because both
# are used by the V1 toolkit and runner APIs.
KIND_PREFERENCE = "preference"
KIND_CONSTRAINT = "constraint"
KIND_DECISION = "decision"
KIND_REJECTION = "rejection"
KIND_PROCEDURE = "procedure"
KIND_RULE = "rule"
KIND_PLAN = "plan"

VALID_KINDS = frozenset(
    {
        KIND_PREFERENCE,
        KIND_CONSTRAINT,
        KIND_DECISION,
        KIND_REJECTION,
        KIND_PROCEDURE,
        KIND_RULE,
        KIND_PLAN,
    }
)

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_RETRACTED = "retracted"
STATUS_CANDIDATE = "candidate"

CONFIDENCE_EXPLICIT = "explicit"
CONFIDENCE_VERIFIED = "verified"
CONFIDENCE_INFERRED = "inferred"
VALID_CONFIDENCE = frozenset(
    {CONFIDENCE_EXPLICIT, CONFIDENCE_VERIFIED, CONFIDENCE_INFERRED}
)

SCOPE_PROJECT = "project"
SCOPE_GLOBAL = "global"
GLOBAL_WORKSPACE_ID = "global"
SCHEMA_VERSION = 2

_SECRET_PATTERNS = (
    re.compile(
        r"(?:api[_ -]?key|secret(?:[_ -]?key)?|password|passwd|passphrase|"
        r"access[_ -]?token|token|auth(?:orization)?|bearer|private[_ -]?key)"
        r"\s*[:=：＝]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
)

# A memory can say that a user prefers p-values in a table.  It must not
# contain a result such as ``p=0.03`` or a live metric such as ``最新样本量
# 1200``.  The numeric suffix is important to avoid rejecting normal prose.
_LIVE_METRIC_PATTERNS = (
    re.compile(
        r"(?:\bp\b\s*[-_ ]?(?:value|值)?|p值|样本量|sample\s*(?:size|n)|\bn\b|"
        r"r(?:2|²)|系数|coefficient|coef|beta|estimate|std\.?\s*err(?:or)?|"
        r"\bse\b|\bt\b|\bz\b|显著性|统计量|置信区间|confidence\s+interval)"
        r"\s*(?:is|为|=|:|：|＝)?\s*[-+]?\d+(?:\.\d+)?%?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:当前|目前|最新|实时|today|now|live)\S{0,24}"
        r"(?:[-+]?\d+(?:\.\d+)?|20\d{2}[-/]\d{1,2})",
        re.IGNORECASE,
    ),
)

_EVIDENCE_PATTERNS = (
    re.compile(
        r"(?:claim|evidence|evidence[-_ ]?card|run[-_ ]?id|证据|证据卡|"
        r"回归结果|结果表|数据显示|研究发现|数据表)"
        r"[^。.!?；;]{0,100}(?:=|是|为|显示|证明|支持|显著|significant|effect)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:result|results|finding|findings)\b"
        r"[^.!?；;]{0,100}\b(?:show|shows|suggest|suggests|prove|proves|significant)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:claim|evidence|card|run)[-_ ]?[A-Za-z0-9]+\b", re.IGNORECASE),
    re.compile(r"(?:https?|ftp)://\S+", re.IGNORECASE),
)


class MemoryCandidateRejected(ValueError):
    """Raised when text is unsafe or unsuitable for durable memory."""


class MemoryContextSelection(list[dict[str, Any]]):
    """List-like result returned by :meth:`MemoryStore.select_for_context`.

    The list behaviour keeps the API easy to use for callers that only need
    records.  ``records``, ``formatted``, ``text`` and ``estimated_tokens``
    expose the context projection without forcing callers to recompute
    provenance.
    """

    def __init__(
        self,
        records: Sequence[dict[str, Any]],
        formatted: Sequence[str],
        *,
        estimated_tokens: int,
        truncated: bool = False,
    ) -> None:
        super().__init__(records)
        self.formatted = list(formatted)
        self.text = "\n".join(self.formatted)
        self.estimated_tokens = estimated_tokens
        self.truncated = truncated

    @property
    def records(self) -> list[dict[str, Any]]:
        return list(self)


    def as_dict(self) -> dict[str, Any]:
        """Return the projection in an explicit mapping shape for adapters."""

        return {
            "records": self.records,
            "formatted": list(self.formatted),
            "text": self.text,
            "estimated_tokens": self.estimated_tokens,
            "truncated": self.truncated,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def __getitem__(self, index: Any) -> Any:  # type: ignore[override]
        if isinstance(index, str):
            value = self.as_dict()
            if index not in value:
                raise KeyError(index)
            return value[index]
        return super().__getitem__(index)


def _now() -> int:
    return int(time.time())


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _normalise_text(text: str) -> str:
    if not isinstance(text, str):
        raise MemoryCandidateRejected("memory text must be a string")
    # Preserve user-visible punctuation (in particular Chinese full-width
    # punctuation used by the V1 UI), while collapsing accidental whitespace.
    # Fingerprints apply NFKC separately so visually equivalent forms still
    # deduplicate.
    normalized = " ".join(unicodedata.normalize("NFC", text).split())
    if not normalized:
        raise MemoryCandidateRejected("memory text must not be blank")
    return normalized


def _fingerprint(text: str) -> str:
    canonical = " ".join(unicodedata.normalize("NFKC", text).split()).casefold()
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _workspace_from_path(path: Path) -> str:
    """Return a stable non-secret identity for a default/legacy workspace."""

    canonical = str(path.expanduser().resolve().parent).replace("\\", "/").lower()
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalise_workspace(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, Path):
        canonical = str(value.expanduser().resolve()).replace("\\", "/").lower()
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    value = str(value).strip()
    return value or fallback


def _normalise_scope(scope: Any) -> str:
    return SCOPE_GLOBAL if str(scope or SCOPE_PROJECT).lower() in {"global", "user"} else SCOPE_PROJECT


def _normalise_kind(kind: Any) -> str:
    value = str(kind or KIND_PREFERENCE).strip().lower()
    # Unknown legacy kinds remain callable and visible; the known aliases are
    # retained for backwards compatibility with the old toolkit.
    return value or KIND_PREFERENCE


def _normalise_confidence(value: Any) -> str:
    value = str(value or CONFIDENCE_EXPLICIT).strip().lower()
    return value if value in VALID_CONFIDENCE else CONFIDENCE_INFERRED


def _source_ids(source_ids: Any = None, provenance: Any = None) -> list[str]:
    values: list[Any] = []
    if source_ids is not None:
        if isinstance(source_ids, str):
            values.append(source_ids)
        elif isinstance(source_ids, Iterable):
            values.extend(source_ids)
    if provenance is not None:
        if isinstance(provenance, str):
            values.append(provenance)
        elif isinstance(provenance, Mapping):
            for key in ("id", "source_id", "event_id", "seq"):
                if key in provenance:
                    values.append(provenance[key])
            for key in ("ids", "source_ids", "event_ids"):
                value = provenance.get(key)
                if isinstance(value, str):
                    values.append(value)
                elif isinstance(value, Iterable):
                    values.extend(value)
        elif isinstance(provenance, Iterable):
            values.extend(provenance)

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _is_expired(entry: Mapping[str, Any], now: int) -> bool:
    expiry = entry.get("expires_at")
    if expiry in (None, ""):
        return False
    if isinstance(expiry, str):
        try:
            # Numeric strings are common in hand-authored legacy files.
            expiry_value = float(expiry)
        except ValueError:
            try:
                from datetime import datetime

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
    return {CONFIDENCE_EXPLICIT: 3, CONFIDENCE_VERIFIED: 2, CONFIDENCE_INFERRED: 1}.get(confidence, 0)


def _copy_record(record: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(record)
    for key in ("source_ids",):
        value = copied.get(key)
        if isinstance(value, list):
            copied[key] = list(value)
    return copied


class MemoryStore:
    """JSON-backed, project-scoped memory with a V1-compatible API."""

    def __init__(
        self,
        path: str | Path,
        workspace_id: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
        legacy_workspace_id: str | Path | None = None,
    ) -> None:
        self._path = Path(path)
        configured_workspace = workspace_id if workspace_id is not None else workspace
        default_legacy = _workspace_from_path(self._path)
        self._legacy_workspace_id = _normalise_workspace(legacy_workspace_id, default_legacy)
        self._workspace_id = _normalise_workspace(configured_workspace, self._legacy_workspace_id)
        self._entries: list[dict[str, Any]] = []
        self._candidates: list[dict[str, Any]] = []
        self._needs_migration_save = False
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    # ---------------------------------------------------------------- load
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError):
            return

        records: Any = raw
        candidates: Any = []
        if isinstance(raw, Mapping):
            records = raw.get("records", raw.get("entries", raw.get("memories", [])))
            candidates = raw.get("candidates", [])
        if not isinstance(records, list):
            records = []
        if not isinstance(candidates, list):
            candidates = []

        for raw_entry in records:
            if not isinstance(raw_entry, Mapping):
                continue
            entry, migrated = self._upgrade_record(raw_entry, candidate=False)
            self._entries.append(entry)
            self._needs_migration_save = self._needs_migration_save or migrated
        for raw_entry in candidates:
            if not isinstance(raw_entry, Mapping):
                continue
            entry, _ = self._upgrade_record(raw_entry, candidate=True)
            entry["status"] = STATUS_CANDIDATE
            self._candidates.append(entry)

    def _upgrade_record(self, raw_entry: Mapping[str, Any], *, candidate: bool) -> tuple[dict[str, Any], bool]:
        """Upgrade one V1/V2 object in memory without writing the file."""

        original = dict(raw_entry)
        text = original.get("text", "")
        try:
            normalized_text = _normalise_text(str(text))
        except MemoryCandidateRejected:
            # Do not discard legacy data merely because V2's write guard would
            # reject it.  It stays readable but is never selected for context.
            normalized_text = str(text).strip()
            quarantined = True
        else:
            quarantined = False
        old_used = max(0, _safe_int(original.get("use_count", original.get("used", 0))))
        old_updated = _safe_int(original.get("updated_at", original.get("updated", 0)))
        created = _safe_int(original.get("created_at", old_updated))
        status = str(original.get("status", STATUS_CANDIDATE if candidate else STATUS_ACTIVE)).lower()
        if candidate:
            status = STATUS_CANDIDATE
        elif status not in {STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_RETRACTED}:
            status = STATUS_ACTIVE
        scope = _normalise_scope(original.get("scope", SCOPE_PROJECT))
        record_workspace = original.get("workspace_id")
        if scope == SCOPE_GLOBAL:
            record_workspace = _normalise_workspace(record_workspace, GLOBAL_WORKSPACE_ID)
        else:
            record_workspace = _normalise_workspace(record_workspace, self._legacy_workspace_id)
        source_ids = _source_ids(original.get("source_ids"), original.get("provenance"))
        if not source_ids and isinstance(original.get("source_id"), str):
            source_ids = _source_ids(original.get("source_id"))
        computed_fingerprint = _fingerprint(normalized_text)
        fingerprint = str(original.get("fingerprint") or computed_fingerprint)
        if fingerprint != computed_fingerprint:
            fingerprint = computed_fingerprint

        result = dict(original)
        result.update(
            {
                "id": str(original.get("id") or f"mem-{uuid.uuid4().hex[:8]}"),
                "workspace_id": record_workspace,
                "scope": scope,
                "kind": _normalise_kind(original.get("kind", KIND_PREFERENCE)),
                "text": normalized_text,
                "status": status,
                "confidence": _normalise_confidence(original.get("confidence", CONFIDENCE_EXPLICIT)),
                "source_ids": source_ids,
                "fingerprint": fingerprint,
                "created_at": created,
                "updated_at": old_updated,
                "last_used_at": original.get("last_used_at"),
                "use_count": old_used,
                "supersedes": original.get("supersedes"),
                "expires_at": original.get("expires_at"),
                "sensitive": bool(original.get("sensitive", False)),
                "quarantined": bool(original.get("quarantined", quarantined)),
                "schema_version": SCHEMA_VERSION,
                # V1 aliases are kept so old callers can continue to inspect
                # and use the dictionaries returned by all()/search().
                "used": old_used,
                "updated": old_updated,
            }
        )
        migrated = (
            original.get("schema_version") != SCHEMA_VERSION
            or "workspace_id" not in original
            or "fingerprint" not in original
            or "status" not in original
        )
        return result, migrated

    # -------------------------------------------------------------- validation
    @staticmethod
    def _validate_text(text: str) -> str:
        normalized = _normalise_text(text)
        for pattern in _SECRET_PATTERNS:
            if pattern.search(normalized):
                raise MemoryCandidateRejected("memory text looks like a secret")
        for pattern in _LIVE_METRIC_PATTERNS:
            if pattern.search(normalized):
                raise MemoryCandidateRejected("live metrics/results do not belong in memory")
        for pattern in _EVIDENCE_PATTERNS:
            if pattern.search(normalized):
                raise MemoryCandidateRejected("evidence assertions belong in the ledger, not memory")
        return normalized

    @staticmethod
    def validate_text(text: str) -> str:
        """Validate and normalize text before a caller sends it to a provider.

        The write path has always used :meth:`_validate_text`; this small
        public facade lets bounded intake pipelines apply the exact same
        secret/live-metric/evidence guard before any model I/O.  It is an
        alias rather than a second validator so the two paths cannot drift.
        """

        return MemoryStore._validate_text(text)

    def _make_record(
        self,
        text: str,
        *,
        kind: str,
        scope: str,
        workspace_id: str,
        confidence: str,
        source_ids: list[str],
        status: str,
        supersedes: str | None,
        expires_at: Any,
        sensitive: bool,
        now: int,
        candidate: bool = False,
    ) -> dict[str, Any]:
        normalized = self._validate_text(text)
        used = 0
        return {
            "id": f"mem-{uuid.uuid4().hex[:8]}",
            "workspace_id": workspace_id,
            "scope": scope,
            "kind": _normalise_kind(kind),
            "text": normalized,
            "status": STATUS_CANDIDATE if candidate else status,
            "confidence": _normalise_confidence(confidence),
            "source_ids": source_ids,
            "fingerprint": _fingerprint(normalized),
            "created_at": now,
            "updated_at": now,
            "last_used_at": None,
            "use_count": used,
            "supersedes": supersedes,
            "expires_at": expires_at,
            "sensitive": bool(sensitive),
            "quarantined": False,
            "schema_version": SCHEMA_VERSION,
            "used": used,
            "updated": now,
        }

    def _existing_by_fingerprint(
        self,
        fingerprint: str,
        *,
        workspace_id: str,
        scope: str,
        include_candidates: bool = False,
    ) -> dict[str, Any] | None:
        pool: Iterable[dict[str, Any]] = self._entries
        if include_candidates:
            pool = (*self._entries, *self._candidates)
        for entry in pool:
            if (
                entry.get("fingerprint") == fingerprint
                and entry.get("workspace_id") == workspace_id
                and entry.get("scope") == scope
            ):
                return entry
        return None

    def _find(self, entry_id: str, *, include_candidates: bool = False) -> dict[str, Any] | None:
        for entry in self._entries:
            if entry.get("id") == entry_id:
                return entry
        if include_candidates:
            for entry in self._candidates:
                if entry.get("id") == entry_id:
                    return entry
        return None

    def _mark_superseded(self, target: dict[str, Any], replacement_id: str, now: int) -> None:
        if target.get("status") == STATUS_ACTIVE:
            target["status"] = STATUS_SUPERSEDED
            target["superseded_by"] = replacement_id
            target["updated_at"] = now
            target["updated"] = now

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
        """Add a memory, preserving ``add(text, kind)`` from V1.

        ``candidate=True`` places assistant-originated suggestions in the
        candidate queue.  They become readable only after
        :meth:`accept_candidate`.  Duplicate content is a successful no-op
        and returns the existing record.
        """

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
                now=now,
            )

        normalized = self._validate_text(text)
        now_value = _now() if now is None else _safe_int(now, _now())
        selected_workspace = workspace_id if workspace_id is not None else workspace
        normalized_scope = _normalise_scope(scope)
        record_workspace = (
            _normalise_workspace(selected_workspace, GLOBAL_WORKSPACE_ID)
            if normalized_scope == SCOPE_GLOBAL
            else _normalise_workspace(selected_workspace, self._workspace_id)
        )
        fingerprint = _fingerprint(normalized)
        existing = self._existing_by_fingerprint(
            fingerprint,
            workspace_id=record_workspace,
            scope=normalized_scope,
            include_candidates=False,
        )
        if existing is not None:
            # A duplicate is intentionally a no-op: do not bump usage or
            # updated_at merely because a caller retried an accepted write.
            if self._needs_migration_save:
                self._save()
            return _copy_record(existing)

        source_list = _source_ids(source_ids, provenance)
        replacement_id = supersedes or replace_id
        if replacement_id:
            target = self._find(replacement_id)
            if target is None:
                raise KeyError(f"unknown memory to supersede: {replacement_id}")
            if (
                target.get("workspace_id") != record_workspace
                or target.get("scope") != normalized_scope
            ):
                raise ValueError("a replacement must stay within the same memory workspace and scope")

        entry = self._make_record(
            normalized,
            kind=kind,
            scope=normalized_scope,
            workspace_id=record_workspace,
            confidence=confidence,
            source_ids=source_list,
            status=STATUS_ACTIVE,
            supersedes=replacement_id,
            expires_at=expires_at,
            sensitive=sensitive,
            now=now_value,
        )
        if replacement_id:
            target = self._find(replacement_id)
            if target is not None:
                self._mark_superseded(target, entry["id"], now_value)
        self._entries.append(entry)
        self._save()
        return _copy_record(entry)

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
        """Store an assistant suggestion outside the active memory set."""

        normalized = self._validate_text(text)
        now_value = _now() if now is None else _safe_int(now, _now())
        normalized_scope = _normalise_scope(scope)
        selected_workspace = workspace_id if workspace_id is not None else workspace
        record_workspace = (
            _normalise_workspace(selected_workspace, GLOBAL_WORKSPACE_ID)
            if normalized_scope == SCOPE_GLOBAL
            else _normalise_workspace(selected_workspace, self._workspace_id)
        )
        existing = self._existing_by_fingerprint(
            _fingerprint(normalized),
            workspace_id=record_workspace,
            scope=normalized_scope,
            include_candidates=True,
        )
        if existing is not None:
            return _copy_record(existing)
        entry = self._make_record(
            normalized,
            kind=kind,
            scope=normalized_scope,
            workspace_id=record_workspace,
            confidence=confidence,
            source_ids=_source_ids(source_ids, provenance),
            status=STATUS_CANDIDATE,
            supersedes=supersedes,
            expires_at=expires_at,
            sensitive=sensitive,
            now=now_value,
            candidate=True,
        )
        self._candidates.append(entry)
        self._save()
        return _copy_record(entry)

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
        candidate = self._find(candidate_id, include_candidates=True)
        if candidate is None or candidate.get("status") != STATUS_CANDIDATE:
            return None
        selected_workspace = workspace_id if workspace_id is not None else workspace
        if selected_workspace is not None:
            normalized_workspace = _normalise_workspace(selected_workspace, self._workspace_id)
            if candidate.get("workspace_id") != normalized_workspace:
                return None
        merged_sources = _source_ids(candidate.get("source_ids"), provenance)
        merged_sources = _source_ids(merged_sources, source_ids)
        self._candidates = [entry for entry in self._candidates if entry.get("id") != candidate_id]
        result = self.add(
            str(candidate.get("text", "")),
            kind=str(candidate.get("kind", KIND_PREFERENCE)),
            scope=str(candidate.get("scope", SCOPE_PROJECT)),
            workspace_id=str(candidate.get("workspace_id", self._workspace_id)),
            confidence=str(candidate.get("confidence", CONFIDENCE_INFERRED)),
            source_ids=merged_sources,
            supersedes=candidate.get("supersedes"),
            expires_at=candidate.get("expires_at"),
            sensitive=bool(candidate.get("sensitive", False)),
            now=now,
        )
        return result

    def reject_candidate(
        self,
        candidate_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> bool:
        candidate = self._find(candidate_id, include_candidates=True)
        if candidate is None or candidate.get("status") != STATUS_CANDIDATE:
            return False
        selected_workspace = workspace_id if workspace_id is not None else workspace
        if selected_workspace is not None:
            normalized_workspace = _normalise_workspace(selected_workspace, self._workspace_id)
            if candidate.get("workspace_id") != normalized_workspace:
                return False
        before = len(self._candidates)
        self._candidates = [entry for entry in self._candidates if entry.get("id") != candidate_id]
        if len(self._candidates) == before:
            return False
        self._save()
        return True

    def retract(
        self,
        entry_id: str,
        *,
        source_ids: Sequence[str] | str | None = None,
        provenance: Any = None,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        """Retract a record in place, preserving it as an auditable tombstone."""

        entry = self._find(entry_id)
        if entry is None:
            return None
        now_value = _now() if now is None else _safe_int(now, _now())
        merged = _source_ids(entry.get("source_ids"), source_ids)
        merged = _source_ids(merged, provenance)
        entry["source_ids"] = merged
        entry["status"] = STATUS_RETRACTED
        entry["retracted_at"] = now_value
        entry["updated_at"] = now_value
        entry["updated"] = now_value
        self._save()
        return _copy_record(entry)

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
        target = self._find(entry_id)
        if target is None:
            raise KeyError(f"unknown memory to supersede: {entry_id}")
        return self.add(
            text,
            kind=kind or str(target.get("kind", KIND_PREFERENCE)),
            scope=str(target.get("scope", SCOPE_PROJECT)),
            workspace_id=str(target.get("workspace_id", self._workspace_id)),
            confidence=confidence,
            source_ids=source_ids,
            provenance=provenance,
            supersedes=entry_id,
            expires_at=expires_at,
            now=now,
        )

    def touch(self, entry_id: str) -> None:
        """Record usage while keeping the legacy ``used`` counter in sync."""

        self._touch_ids([entry_id])

    def _touch_ids(self, entry_ids: Iterable[str], *, now: int | None = None) -> None:
        ids = set(entry_ids)
        if not ids:
            return
        now_value = _now() if now is None else _safe_int(now, _now())
        changed = False
        for entry in self._entries:
            if entry.get("id") not in ids or entry.get("status") != STATUS_ACTIVE:
                continue
            count = max(0, _safe_int(entry.get("use_count", entry.get("used", 0)))) + 1
            entry["use_count"] = count
            entry["used"] = count
            entry["last_used_at"] = now_value
            # ``updated`` was a usage timestamp in V1; keep that alias for
            # callers while V2's actual content recency remains updated_at.
            entry["updated"] = now_value
            changed = True
        if changed:
            self._save()

    # ---------------------------------------------------------------- persist
    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        try:
            payload: Any
            if self._candidates:
                payload = {
                    "schema_version": SCHEMA_VERSION,
                    "records": self._entries,
                    "candidates": self._candidates,
                }
            else:
                # A list keeps old V1 readers usable.  Each item itself is a
                # V2 record, so no top-level envelope is needed.
                payload = self._entries
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=1, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            self._needs_migration_save = False
        finally:
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    # ---------------------------------------------------------------- read
    def all(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
        include_candidates: bool = False,
    ) -> list[dict[str, Any]]:
        """Return records without mutating usage or triggering migration save."""

        selected_workspace = workspace_id if workspace_id is not None else workspace
        if selected_workspace is None:
            records = self._entries
        else:
            normalized = _normalise_workspace(selected_workspace, self._workspace_id)
            records = [entry for entry in self._entries if entry.get("workspace_id") == normalized]
        output = [_copy_record(entry) for entry in records]
        if include_candidates:
            candidates = self._candidates
            if selected_workspace is not None:
                candidates = [entry for entry in candidates if entry.get("workspace_id") == normalized]
            output.extend(_copy_record(entry) for entry in candidates)
        return output

    def candidates(
        self,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """Return pending candidates, isolated to the requested workspace.

        Candidate records are deliberately not returned by ``search`` or
        ``select_for_context``.  This helper gives review surfaces a small,
        read-only projection without making ``all(include_candidates=True)``
        accidentally cross a project boundary.
        """

        selected_workspace = workspace_id if workspace_id is not None else workspace
        if selected_workspace is None:
            records = self._candidates
        else:
            normalized = _normalise_workspace(selected_workspace, self._workspace_id)
            records = [entry for entry in self._candidates if entry.get("workspace_id") == normalized]
        return [_copy_record(entry) for entry in records if entry.get("status") == STATUS_CANDIDATE]

    def candidate(
        self,
        candidate_id: str,
        *,
        workspace_id: str | Path | None = None,
        workspace: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Read one pending candidate, returning ``None`` across workspaces."""

        record = self._find(candidate_id, include_candidates=True)
        if record is None or record.get("status") != STATUS_CANDIDATE:
            return None
        selected_workspace = workspace_id if workspace_id is not None else workspace
        if selected_workspace is not None:
            normalized = _normalise_workspace(selected_workspace, self._workspace_id)
            if record.get("workspace_id") != normalized:
                return None
        return _copy_record(record)

    def _visible(
        self,
        entry: Mapping[str, Any],
        *,
        now: int,
        workspace_id: str,
        include_global: bool,
        kinds: set[str] | None,
        include_inactive: bool,
    ) -> bool:
        if kinds and str(entry.get("kind")) not in kinds:
            return False
        if not include_inactive and entry.get("status") != STATUS_ACTIVE:
            return False
        if not include_inactive and entry.get("quarantined"):
            return False
        if not include_inactive and _is_expired(entry, now):
            return False
        entry_scope = str(entry.get("scope", SCOPE_PROJECT))
        if entry_scope == SCOPE_GLOBAL:
            return include_global
        return str(entry.get("workspace_id")) == workspace_id

    @staticmethod
    def _rank(
        entry: Mapping[str, Any],
        query_tokens: set[str],
        *,
        exact_workspace: bool,
    ) -> tuple[float, int, int, int, int, str]:
        entry_tokens = tokenize(str(entry.get("text", "")))
        token_set = set(entry_tokens)
        overlap = len(query_tokens & token_set)
        # A phrase/substring match outranks a coincidental token overlap, but
        # lexical relevance remains the first ranking dimension.
        query_text = " ".join(sorted(query_tokens))
        text_lower = str(entry.get("text", "")).lower()
        phrase_bonus = 0.25 if query_text and query_text in text_lower else 0.0
        lexical = overlap / max(len(query_tokens), 1) + phrase_bonus
        confidence = _confidence_score(str(entry.get("confidence", CONFIDENCE_INFERRED)))
        used = max(0, _safe_int(entry.get("use_count", entry.get("used", 0))))
        recency = max(0, _safe_int(entry.get("updated_at", entry.get("updated", 0))))
        return (lexical, 1 if exact_workspace else 0, confidence, used, recency, str(entry.get("id", "")))

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
    ) -> list[dict[str, Any]]:
        """Search active records with deterministic project-aware ranking."""

        if limit <= 0:
            return []
        selected_workspace = workspace_id if workspace_id is not None else workspace
        current_workspace = _normalise_workspace(selected_workspace, self._workspace_id)
        normalized_query = _normalise_text(query) if query and str(query).strip() else ""
        query_tokens = set(tokenize(normalized_query))
        # Global memory is opt-in for every query.  In particular, a project
        # query must not accidentally turn a user-global note into a project
        # fact; callers with an explicit global policy can pass True.
        allow_global = bool(include_global) if include_global is not None else False
        if isinstance(kinds, str):
            kind_set = {kinds}
        elif kinds is None:
            kind_set = None
        else:
            kind_set = {str(kind) for kind in kinds}
        now_value = _now() if now is None else _safe_int(now, _now())

        candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        for entry in self._entries:
            if not self._visible(
                entry,
                now=now_value,
                workspace_id=current_workspace,
                include_global=allow_global,
                kinds=kind_set,
                include_inactive=include_inactive,
            ):
                continue
            if query_tokens:
                entry_tokens = set(tokenize(str(entry.get("text", ""))))
                if not query_tokens & entry_tokens:
                    continue
            exact = entry.get("scope") != SCOPE_GLOBAL and entry.get("workspace_id") == current_workspace
            rank = self._rank(entry, query_tokens, exact_workspace=exact)
            candidates.append((rank, entry))

        # Python's sort is stable; id is included in the key so the result is
        # deterministic even when timestamps and scores are equal.
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return [_copy_record(entry) for _, entry in candidates[:limit]]

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
    ) -> MemoryContextSelection:
        """Select a bounded context projection and touch only selected rows."""

        if max_items is not None:
            limit = int(max_items)
        if max_tokens is not None:
            token_cap = int(max_tokens)
        if max_chars is not None:
            char_cap = int(max_chars)
        if limit <= 0 or token_cap <= 0:
            return MemoryContextSelection([], [], estimated_tokens=0)
        effective_chars_per_token = max(1, chars_per_token)
        max_chars = token_cap * effective_chars_per_token
        if char_cap is not None:
            max_chars = min(max_chars, max(0, int(char_cap)))
        if max_chars <= 0:
            return MemoryContextSelection([], [], estimated_tokens=0)

        # Request all potential records up to the item limit.  Search already
        # filters by project and performs relevance ordering.
        records = self.search(
            query,
            limit=max(limit, 1),
            workspace_id=workspace_id,
            workspace=workspace,
            kinds=kinds,
            include_global=include_global,
            now=now,
        )
        formatted: list[str] = []
        selected: list[dict[str, Any]] = []
        used_ids: list[str] = []
        consumed_chars = 0
        was_truncated = False
        for record in records:
            source_ids = record.get("source_ids") or []
            source_text = ", ".join(str(source) for source in source_ids) if source_ids else "unspecified"
            line = (
                f"[memory/{record.get('kind', KIND_PREFERENCE)}; working knowledge, not evidence] "
                f"{record.get('text', '')} (provenance: {source_text})"
            )
            separator_chars = 1 if formatted else 0
            remaining = max_chars - consumed_chars - separator_chars
            if remaining <= 0:
                break
            if len(line) > remaining:
                line = line[:remaining]
                was_truncated = True
                if line:
                    selected_record = _copy_record(record)
                    selected_record["context"] = line
                    selected_record["formatted"] = line
                    selected_record["provenance"] = list(source_ids)
                    selected.append(selected_record)
                    formatted.append(line)
                    used_ids.append(str(record.get("id")))
                    consumed_chars += separator_chars + len(line)
                break
            selected_record = _copy_record(record)
            selected_record["context"] = line
            selected_record["formatted"] = line
            selected_record["provenance"] = list(source_ids)
            selected.append(selected_record)
            formatted.append(line)
            used_ids.append(str(record.get("id")))
            consumed_chars += separator_chars + len(line)
            if len(selected) >= limit:
                break
        if touch and used_ids:
            self._touch_ids(used_ids, now=now)
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
        """Legacy context formatter; unlike V2 selection, it does not touch."""

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
            # Preserve the V1 line format used by the existing UI/runner.
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
        """Mark duplicate active fingerprints superseded; never erase rows."""

        selected_workspace = workspace_id if workspace_id is not None else workspace
        filter_workspace = None if selected_workspace is None else _normalise_workspace(selected_workspace, self._workspace_id)
        now_value = _now() if now is None else _safe_int(now, _now())
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for entry in self._entries:
            if entry.get("status") != STATUS_ACTIVE:
                continue
            if filter_workspace is not None and entry.get("workspace_id") != filter_workspace:
                continue
            key = (
                str(entry.get("workspace_id")),
                str(entry.get("scope", SCOPE_PROJECT)),
                str(entry.get("fingerprint")),
            )
            groups.setdefault(key, []).append(entry)

        changed = 0
        for entries in groups.values():
            if len(entries) < 2:
                continue
            winner = max(
                entries,
                key=lambda entry: (
                    _confidence_score(str(entry.get("confidence", CONFIDENCE_INFERRED))),
                    max(0, _safe_int(entry.get("use_count", entry.get("used", 0)))),
                    max(0, _safe_int(entry.get("updated_at", entry.get("updated", 0)))),
                    str(entry.get("id", "")),
                ),
            )
            merged_sources = _source_ids(winner.get("source_ids"))
            for entry in entries:
                if entry is winner:
                    continue
                merged_sources = _source_ids(merged_sources, entry.get("source_ids"))
                self._mark_superseded(entry, str(winner.get("id")), now_value)
                changed += 1
            winner["source_ids"] = merged_sources
        if changed:
            self._save()
        return changed

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
        """Safely prune expiry and stale low-confidence active records.

        Superseded/retracted records are tombstones and are retained.  By
        default explicit/verified preferences are retained even when old;
        only inferred memories are eligible for age-based removal.
        """

        now_value = _now() if now is None else _safe_int(now, _now())
        age = max_age_seconds if max_age_seconds is not None else max_age
        if age is None:
            age = unused_after if unused_after is not None else max_unused_seconds
        filter_workspace = workspace_id if workspace_id is not None else workspace
        if filter_workspace is not None:
            filter_workspace = _normalise_workspace(filter_workspace, self._workspace_id)
        kept: list[dict[str, Any]] = []
        removed = 0
        for entry in self._entries:
            if filter_workspace is not None and entry.get("workspace_id") != filter_workspace:
                kept.append(entry)
                continue
            if entry.get("status") != STATUS_ACTIVE:
                kept.append(entry)
                continue
            expired = _is_expired(entry, now_value)
            confidence = str(entry.get("confidence", CONFIDENCE_INFERRED))
            updated = max(
                0,
                _safe_int(
                    entry.get("last_used_at")
                    if entry.get("last_used_at") is not None
                    else entry.get("updated_at", entry.get("updated", 0))
                ),
            )
            stale_low_confidence = (
                age is not None
                and age >= 0
                and updated + int(age) <= now_value
                and (confidence == CONFIDENCE_INFERRED or include_explicit)
            )
            if expired or stale_low_confidence:
                removed += 1
                continue
            kept.append(entry)
        if removed:
            self._entries = kept
            self._save()
        return removed

    def prune_unused(self, min_used: int = 1) -> int:
        """V1 compatibility helper with its original usage-count semantics."""

        threshold = max(0, int(min_used))
        before = len(self._entries)
        self._entries = [
            entry
            for entry in self._entries
            if entry.get("status") != STATUS_ACTIVE
            or max(0, _safe_int(entry.get("use_count", entry.get("used", 0)))) >= threshold
        ]
        removed = before - len(self._entries)
        if removed:
            self._save()
        return removed


def remember_decision(
    memory: MemoryStore,
    note: str,
    *,
    workspace_id: str | Path | None = None,
    workspace: str | Path | None = None,
    source_ids: Sequence[str] | str | None = None,
    provenance: Any = None,
) -> dict[str, Any] | None:
    """Persist an approved decision as explicit project working knowledge."""

    note = (note or "").strip()
    if not note:
        return None
    try:
        return memory.add(
            f"研究决定：{note}",
            kind=KIND_DECISION,
            workspace_id=workspace_id,
            workspace=workspace,
            confidence=CONFIDENCE_EXPLICIT,
            source_ids=source_ids,
            provenance=provenance,
        )
    except MemoryCandidateRejected:
        # Approval notes are user-facing and should not crash an approval
        # transaction if a pasted secret/result is detected.
        return None
