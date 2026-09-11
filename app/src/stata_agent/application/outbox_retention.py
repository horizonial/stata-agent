"""Framework-neutral retention policy for completed memory outbox rows.

The event ledger remains the source of truth.  This module only coordinates a
bounded, preview-then-prune operation over the durable dispatch index; it never
invokes a provider, queue, dispatcher, or ledger writer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..storage.store import (
    OutboxRetentionConflictError as StorageOutboxRetentionConflictError,
    OutboxRetentionExpectation,
)
from .outbox_recovery import _lookup_terminal

MEMORY_EXTRACTION_KIND = "memory.extraction"
OUTBOX_COMPLETED = "completed"

DEFAULT_RETENTION_DAYS = 90
MIN_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 3650
DEFAULT_LIMIT = 100
MIN_LIMIT = 1
MAX_LIMIT = 100
MAX_SCAN_ROWS = 1000
_CURSOR_VERSION = 1
_TOKEN_VERSION = 1
_MAX_CURSOR_BYTES = 512
_MAX_TOKEN_BYTES = 256
_CURSOR_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class OutboxRetentionError(RuntimeError):
    """Base class for retention policy failures."""

    code = "retention_error"


class OutboxRetentionValidationError(OutboxRetentionError, ValueError):
    """The operator supplied invalid retention input."""

    code = "invalid_request"


class OutboxRetentionConflictError(OutboxRetentionError):
    """The preview selection is stale or a row changed during pruning."""

    code = "conflict"


class OutboxRetentionRepository(Protocol):
    """Narrow storage port used by :class:`OutboxRetentionService`."""

    def list_completed_outbox_before(
        self,
        *,
        task_type: str,
        completed_before: int,
        limit: int,
        after_completed_at: int | None = None,
        after_outbox_id: str | None = None,
    ) -> Sequence[object]:
        ...

    def delete_completed_outbox_batch(
        self,
        expectations: Sequence[OutboxRetentionExpectation],
        *,
        cutoff: int,
    ) -> int:
        ...


class RetentionTerminalLookup(Protocol):
    """Lookup port for the canonical terminal extraction event."""

    def find_terminal_event(
        self,
        idea_id: str,
        workspace_id: str,
        fingerprint: str,
    ) -> object | None:
        ...


TerminalLookup = Callable[[str, str, str], object | None]


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutboxRetentionValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _strict_int(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OutboxRetentionValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _bounded_limit(value: object) -> int:
    result = _strict_int(value, "limit", minimum=MIN_LIMIT)
    if result > MAX_LIMIT:
        raise OutboxRetentionValidationError("limit must be between 1 and 100")
    return result


def _bounded_days(value: object) -> int:
    result = _strict_int(value, "retention_days", minimum=MIN_RETENTION_DAYS)
    if result > MAX_RETENTION_DAYS:
        raise OutboxRetentionValidationError("retention_days must be between 7 and 3650")
    return result


def _encode_cursor(
    *, idea_id: str, workspace_id: str, cutoff: int, completed_at: int, outbox_id: str
) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "idea_id": idea_id,
        "workspace_id": workspace_id,
        "cutoff": cutoff,
        "completed_at": completed_at,
        "outbox_id": outbox_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(
    value: object, *, idea_id: str, workspace_id: str
) -> tuple[int, int, str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value.encode()) > _MAX_CURSOR_BYTES:
        raise OutboxRetentionValidationError("cursor is invalid")
    try:
        encoded = value.encode("ascii")
        if not encoded or any(chr(byte) not in _CURSOR_ALPHABET for byte in encoded):
            raise ValueError("cursor contains non-canonical characters")
        raw = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
            raise ValueError("cursor is not canonical base64url")
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise OutboxRetentionValidationError("cursor is invalid") from exc
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"v", "idea_id", "workspace_id", "cutoff", "completed_at", "outbox_id"}
        or type(payload.get("v")) is not int
        or payload.get("v") != _CURSOR_VERSION
    ):
        raise OutboxRetentionValidationError("cursor is invalid")
    if payload.get("idea_id") != idea_id or payload.get("workspace_id") != workspace_id:
        raise OutboxRetentionValidationError("cursor scope mismatch")
    cutoff = _strict_int(payload.get("cutoff"), "cursor.cutoff")
    completed_at = _strict_int(payload.get("completed_at"), "cursor.completed_at")
    outbox_id = _identity(payload.get("outbox_id"), "cursor.outbox_id")
    return cutoff, completed_at, outbox_id


def _row_value(row: object, name: str) -> object:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _payload(row: object) -> Mapping[str, object]:
    payload = _row_value(row, "payload")
    if not isinstance(payload, Mapping):
        raise ValueError("invalid outbox payload")
    return payload


def _has_terminal(
    lookup: RetentionTerminalLookup | TerminalLookup | object,
    *,
    idea_id: str,
    workspace_id: str,
    fingerprint: str,
) -> bool:
    return _lookup_terminal(
        lookup,
        idea_id=idea_id,
        workspace_id=workspace_id,
        fingerprint=fingerprint,
    )


@dataclass(frozen=True, slots=True)
class OutboxRetentionPreviewRequest:
    idea_id: str
    workspace_id: str
    retention_days: int = DEFAULT_RETENTION_DAYS
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "idea_id", _identity(self.idea_id, "idea_id"))
        object.__setattr__(self, "workspace_id", _identity(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "retention_days", _bounded_days(self.retention_days))
        object.__setattr__(self, "limit", _bounded_limit(self.limit))
        if self.cursor is not None and (not isinstance(self.cursor, str) or not self.cursor.strip()):
            raise OutboxRetentionValidationError("cursor is invalid")


@dataclass(frozen=True, slots=True)
class OutboxRetentionPruneRequest:
    idea_id: str
    workspace_id: str
    cutoff: int
    limit: int
    selection_token: str
    acknowledge_irreversible_delete: bool
    cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "idea_id", _identity(self.idea_id, "idea_id"))
        object.__setattr__(self, "workspace_id", _identity(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "cutoff", _strict_int(self.cutoff, "cutoff"))
        object.__setattr__(self, "limit", _bounded_limit(self.limit))
        if not isinstance(self.selection_token, str) or not self.selection_token or len(self.selection_token.encode()) > _MAX_TOKEN_BYTES:
            raise OutboxRetentionValidationError("selection_token is invalid")
        if self.acknowledge_irreversible_delete is not True:
            raise OutboxRetentionValidationError("acknowledge_irreversible_delete must be true")
        if self.cursor is not None and (not isinstance(self.cursor, str) or not self.cursor.strip()):
            raise OutboxRetentionValidationError("cursor is invalid")


@dataclass(frozen=True, slots=True)
class OutboxRetentionItem:
    outbox_id: str
    idempotency_key: str
    fingerprint: str
    idea_id: str
    workspace_id: str
    completed_at: int
    state_version: int

    def to_dict(self) -> dict[str, object]:
        return {
            "outbox_id": self.outbox_id,
            "idempotency_key": self.idempotency_key,
            "fingerprint": self.fingerprint,
            "idea_id": self.idea_id,
            "workspace_id": self.workspace_id,
            "completed_at": self.completed_at,
            "state_version": self.state_version,
        }


@dataclass(frozen=True, slots=True)
class OutboxRetentionPreview:
    cutoff: int
    retention_days: int
    limit: int
    eligible_count: int
    blocked_count: int
    scanned_count: int
    scan_truncated: bool
    next_cursor: str | None
    selection_token: str
    items: tuple[OutboxRetentionItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "cutoff": self.cutoff,
            "retention_days": self.retention_days,
            "limit": self.limit,
            "eligible_count": self.eligible_count,
            "blocked_count": self.blocked_count,
            "scanned_count": self.scanned_count,
            "scan_truncated": self.scan_truncated,
            "next_cursor": self.next_cursor,
            "selection_token": self.selection_token,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class OutboxRetentionPruneResult:
    outcome: str
    deleted_count: int
    cutoff: int
    selection_token: str

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "deleted_count": self.deleted_count,
            "cutoff": self.cutoff,
            "selection_token": self.selection_token,
        }


def _canonical_token(
    *,
    idea_id: str,
    workspace_id: str,
    cutoff: int,
    limit: int,
    cursor: str | None,
    expectations: Sequence[OutboxRetentionExpectation],
) -> str:
    rows = [
        {
            "outbox_id": expectation.outbox_id,
            "idempotency_key": expectation.idempotency_key,
            "completed_at": expectation.completed_at,
            "state_version": expectation.expected_state_version,
        }
        for expectation in expectations
    ]
    body = {
        "v": _TOKEN_VERSION,
        "idea_id": idea_id,
        "workspace_id": workspace_id,
        "task_type": MEMORY_EXTRACTION_KIND,
        "cutoff": cutoff,
        "limit": limit,
        "cursor": cursor,
        "rows": rows,
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"v{_TOKEN_VERSION}:" + hashlib.sha256(encoded).hexdigest()


class OutboxRetentionService:
    """Preview and prune completed memory extraction dispatch rows."""

    task_type = MEMORY_EXTRACTION_KIND

    def __init__(
        self,
        repository: OutboxRetentionRepository,
        terminal_lookup: RetentionTerminalLookup | TerminalLookup,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if repository is None or terminal_lookup is None:
            raise TypeError("repository and terminal_lookup are required")
        self.repository = repository
        self.terminal_lookup = terminal_lookup
        self.clock = clock or time.time

    def preview(self, request: OutboxRetentionPreviewRequest) -> OutboxRetentionPreview:
        if not isinstance(request, OutboxRetentionPreviewRequest):
            raise OutboxRetentionValidationError("request must be an OutboxRetentionPreviewRequest")
        cutoff, after_completed_at, after_outbox_id = self._page_state(request)
        rows = self.repository.list_completed_outbox_before(
            task_type=self.task_type,
            completed_before=cutoff,
            limit=MAX_SCAN_ROWS,
            after_completed_at=after_completed_at,
            after_outbox_id=after_outbox_id,
        )
        if rows is None:
            raise OutboxRetentionError("retention repository returned no row collection")
        if len(rows) > MAX_SCAN_ROWS:
            raise OutboxRetentionError("retention repository exceeded scan bound")
        items: list[OutboxRetentionItem] = []
        expectations: list[OutboxRetentionExpectation] = []
        blocked_count = 0
        last_key: tuple[int, str] | None = None
        scanned_count = 0
        scan_truncated = False
        for index, row in enumerate(rows):
            scanned_at, outbox_id = self._scan_row(row)
            last_key = (scanned_at, outbox_id)
            scanned_count += 1
            try:
                item, expectation = self._eligible(row, request, cutoff)
            except ValueError:
                blocked_count += 1
                continue
            if not _has_terminal(
                self.terminal_lookup,
                idea_id=request.idea_id,
                workspace_id=request.workspace_id,
                fingerprint=item.fingerprint,
            ):
                blocked_count += 1
                continue
            if len(items) < request.limit:
                items.append(item)
                expectations.append(expectation)
            if len(items) >= request.limit:
                # The output limit is also a page boundary.  Keep the cursor
                # immediately after the last selected row so a later request
                # can continue without skipping eligible rows that followed it.
                scan_truncated = index < len(rows) - 1 or len(rows) == MAX_SCAN_ROWS
                break
        if len(rows) == MAX_SCAN_ROWS:
            scan_truncated = True
        next_cursor = None
        if scan_truncated and last_key is not None:
            next_cursor = _encode_cursor(
                idea_id=request.idea_id,
                workspace_id=request.workspace_id,
                cutoff=cutoff,
                completed_at=last_key[0],
                outbox_id=last_key[1],
            )
        token = _canonical_token(
            idea_id=request.idea_id,
            workspace_id=request.workspace_id,
            cutoff=cutoff,
            limit=request.limit,
            cursor=request.cursor,
            expectations=expectations,
        )
        return OutboxRetentionPreview(
            cutoff=cutoff,
            retention_days=request.retention_days,
            limit=request.limit,
            eligible_count=len(items),
            blocked_count=blocked_count,
            scanned_count=scanned_count,
            scan_truncated=scan_truncated,
            next_cursor=next_cursor,
            selection_token=token,
            items=tuple(items),
        )

    preview_page = preview

    def prune(self, request: OutboxRetentionPruneRequest) -> OutboxRetentionPruneResult:
        if not isinstance(request, OutboxRetentionPruneRequest):
            raise OutboxRetentionValidationError("request must be an OutboxRetentionPruneRequest")
        cursor_state = _decode_cursor(request.cursor, idea_id=request.idea_id, workspace_id=request.workspace_id)
        if cursor_state is not None and cursor_state[0] != request.cutoff:
            raise OutboxRetentionValidationError("cursor cutoff mismatch")
        # Rebuild the exact page with an absolute cutoff.  The request object is
        # intentionally not reused as a preview request because the cutoff must
        # never be recomputed from a new wall clock value.
        page = self._preview_at_cutoff(
            idea_id=request.idea_id,
            workspace_id=request.workspace_id,
            cutoff=request.cutoff,
            limit=request.limit,
            cursor=request.cursor,
        )
        if not hmac.compare_digest(page.selection_token, request.selection_token):
            raise OutboxRetentionConflictError("retention selection is stale")
        if not page.items:
            return OutboxRetentionPruneResult("noop", 0, request.cutoff, page.selection_token)
        expectations = [
            OutboxRetentionExpectation(
                outbox_id=item.outbox_id,
                idempotency_key=item.idempotency_key,
                task_type=self.task_type,
                completed_at=item.completed_at,
                expected_state_version=item.state_version,
            )
            for item in page.items
        ]
        try:
            deleted = self.repository.delete_completed_outbox_batch(expectations, cutoff=request.cutoff)
        except (StorageOutboxRetentionConflictError, OutboxRetentionConflictError) as exc:
            raise OutboxRetentionConflictError("retention selection is stale") from exc
        except Exception:
            raise
        if deleted != len(expectations):
            raise OutboxRetentionConflictError("retention delete count mismatch")
        return OutboxRetentionPruneResult("pruned", deleted, request.cutoff, page.selection_token)

    prune_page = prune

    def _page_state(self, request: OutboxRetentionPreviewRequest) -> tuple[int, int | None, str | None]:
        cursor = _decode_cursor(request.cursor, idea_id=request.idea_id, workspace_id=request.workspace_id)
        if cursor is not None:
            # The cursor carries the original absolute cutoff.  Recomputing it
            # from wall-clock time would make a valid next page stale merely
            # because the operator clicked it a second later.
            return cursor
        now_value = self.clock()
        if isinstance(now_value, bool) or not isinstance(now_value, (int, float)):
            raise OutboxRetentionError("retention clock returned an invalid value")
        cutoff = int(now_value) - request.retention_days * 24 * 60 * 60
        return cutoff, None, None

    def _preview_at_cutoff(
        self,
        *,
        idea_id: str,
        workspace_id: str,
        cutoff: int,
        limit: int,
        cursor: str | None,
    ) -> OutboxRetentionPreview:
        cursor_state = _decode_cursor(cursor, idea_id=idea_id, workspace_id=workspace_id)
        after_completed_at = cursor_state[1] if cursor_state else None
        after_outbox_id = cursor_state[2] if cursor_state else None
        rows = self.repository.list_completed_outbox_before(
            task_type=self.task_type,
            completed_before=cutoff,
            limit=MAX_SCAN_ROWS,
            after_completed_at=after_completed_at,
            after_outbox_id=after_outbox_id,
        )
        if rows is None or len(rows) > MAX_SCAN_ROWS:
            raise OutboxRetentionError("retention repository returned an invalid bounded page")
        items: list[OutboxRetentionItem] = []
        expectations: list[OutboxRetentionExpectation] = []
        blocked_count = 0
        last_key: tuple[int, str] | None = None
        scanned_count = 0
        scan_truncated = False
        for index, row in enumerate(rows):
            scanned_at, outbox_id = self._scan_row(row)
            last_key = (scanned_at, outbox_id)
            scanned_count += 1
            try:
                item, expectation = self._eligible(
                    row,
                    OutboxRetentionPreviewRequest(idea_id, workspace_id, DEFAULT_RETENTION_DAYS, limit, cursor),
                    cutoff,
                )
            except ValueError:
                blocked_count += 1
                continue
            if not _has_terminal(
                self.terminal_lookup,
                idea_id=idea_id,
                workspace_id=workspace_id,
                fingerprint=item.fingerprint,
            ):
                blocked_count += 1
                continue
            items.append(item)
            expectations.append(expectation)
            if len(items) >= limit:
                scan_truncated = index < len(rows) - 1 or len(rows) == MAX_SCAN_ROWS
                break
        if len(rows) == MAX_SCAN_ROWS:
            scan_truncated = True
        next_cursor = None
        if scan_truncated and last_key:
            next_cursor = _encode_cursor(
                idea_id=idea_id,
                workspace_id=workspace_id,
                cutoff=cutoff,
                completed_at=last_key[0],
                outbox_id=last_key[1],
            )
        token = _canonical_token(
            idea_id=idea_id,
            workspace_id=workspace_id,
            cutoff=cutoff,
            limit=limit,
            cursor=cursor,
            expectations=expectations[:limit],
        )
        return OutboxRetentionPreview(
            cutoff=cutoff,
            retention_days=DEFAULT_RETENTION_DAYS,
            limit=limit,
            eligible_count=min(len(expectations), limit),
            blocked_count=blocked_count,
            scanned_count=scanned_count,
            scan_truncated=scan_truncated,
            next_cursor=next_cursor,
            selection_token=token,
            items=tuple(items[:limit]),
        )

    @staticmethod
    def _scan_row(row: object) -> tuple[int, str]:
        completed_at = _row_value(row, "completed_at")
        outbox_id = _row_value(row, "outbox_id")
        if isinstance(completed_at, bool) or not isinstance(completed_at, int):
            raise OutboxRetentionConflictError("invalid completed row ordering")
        return completed_at, _identity(outbox_id, "outbox_id")

    def _eligible(
        self, row: object, request: OutboxRetentionPreviewRequest, cutoff: int
    ) -> tuple[OutboxRetentionItem, OutboxRetentionExpectation]:
        if _row_value(row, "task_type") != self.task_type or _row_value(row, "status") != OUTBOX_COMPLETED:
            raise ValueError("row is not completed memory extraction")
        completed_at = _row_value(row, "completed_at")
        if isinstance(completed_at, bool) or not isinstance(completed_at, int) or completed_at > cutoff:
            raise ValueError("row is not older than cutoff")
        outbox_id = _identity(_row_value(row, "outbox_id"), "outbox_id")
        key = _identity(_row_value(row, "idempotency_key"), "idempotency_key")
        version = _strict_int(_row_value(row, "state_version"), "state_version")
        payload = _payload(row)
        for name, expected in (
            ("idea_id", request.idea_id),
            ("workspace_id", request.workspace_id),
            ("fingerprint", key),
            ("kind", self.task_type),
        ):
            if payload.get(name) != expected:
                raise ValueError("outbox payload identity mismatch")
        item = OutboxRetentionItem(
            outbox_id=outbox_id,
            idempotency_key=key,
            fingerprint=key,
            idea_id=request.idea_id,
            workspace_id=request.workspace_id,
            completed_at=completed_at,
            state_version=version,
        )
        return item, OutboxRetentionExpectation(
            outbox_id=outbox_id,
            idempotency_key=key,
            task_type=self.task_type,
            completed_at=completed_at,
            expected_state_version=version,
        )


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_RETENTION_DAYS",
    "MAX_RETENTION_DAYS",
    "MAX_SCAN_ROWS",
    "MIN_RETENTION_DAYS",
    "OutboxRetentionConflictError",
    "OutboxRetentionError",
    "OutboxRetentionItem",
    "OutboxRetentionPreview",
    "OutboxRetentionPreviewRequest",
    "OutboxRetentionPruneRequest",
    "OutboxRetentionPruneResult",
    "OutboxRetentionRepository",
    "OutboxRetentionService",
    "OutboxRetentionValidationError",
    "RetentionTerminalLookup",
]
