"""LedgerStore and the small durable task-outbox value objects."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Protocol

from ..domain.reducers import Projection
from ..events.schema import Event


class StoreError(Exception):
    """存储层错误基类。"""


class DuplicateFingerprint(StoreError):
    """同 (idea, event_type, fingerprint) 已存在。"""


class LeaseConflict(StoreError):
    """写者租约被他人持有且未过期。"""


class StaleWrite(StoreError):
    """revision/token 过期，旧进程提交被 fence 拒绝。"""


class AttachmentConflict(StoreError, ValueError):
    """附件幂等键、状态或 CAS 前置条件冲突。"""


AttachmentConflictError = AttachmentConflict


class AttachmentNotFound(StoreError, LookupError):
    """附件不存在，或不属于请求的 workspace。"""


class AttachmentStateError(StoreError, ValueError):
    """附件状态机或 metadata contract 无效。"""


ATTACHMENT_PENDING = "pending"
ATTACHMENT_READY = "ready"
ATTACHMENT_QUARANTINED = "quarantined"
ATTACHMENT_REJECTED = "rejected"
ATTACHMENT_FAILED = "failed"
ATTACHMENT_STATUSES = frozenset(
    {
        ATTACHMENT_PENDING,
        ATTACHMENT_READY,
        ATTACHMENT_QUARANTINED,
        ATTACHMENT_REJECTED,
        ATTACHMENT_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    """Small operational attachment index row.

    The record intentionally contains metadata and an opaque relative
    ``storage_key`` only.  It never carries binary bytes, extracted text, or
    an absolute path.
    """

    attachment_id: str
    workspace_id: str
    idea_id: str
    display_name: str
    declared_media_type: str
    detected_format: str
    source_role: str
    status: str
    byte_size: int
    sha256: str | None
    storage_key: str | None
    parser_version: int | None
    page_count: int
    chunk_count: int
    extracted_chars: int
    scanned_suspect: bool
    error_code: str | None
    created_at: int
    updated_at: int
    ready_at: int | None
    expires_at: int | None
    latest_event_seq: int | None
    state_version: int
    idempotency_key: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status == ATTACHMENT_READY

    def safe_manifest(self) -> dict[str, object]:
        """Return the bounded metadata allowed across an application boundary."""

        return {
            "attachment_id": self.attachment_id,
            "display_name": self.display_name,
            "detected_format": self.detected_format,
            "source_role": self.source_role,
            "status": self.status,
            "byte_size": self.byte_size,
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "extracted_chars": self.extracted_chars,
            "parser_version": self.parser_version,
            "error_code": self.error_code,
        }


class OutboxConflictError(StoreError, ValueError):
    """同幂等键对应不同任务定义。"""


OutboxConflict = OutboxConflictError


class OutboxRecoveryConflictError(OutboxConflictError):
    """Failed outbox recovery lost its exact state-generation precondition."""


OutboxRecoveryConflict = OutboxRecoveryConflictError


class OutboxRetentionConflictError(OutboxConflictError):
    """Retention delete lost its exact completed-row precondition."""


OutboxRetentionConflict = OutboxRetentionConflictError

OUTBOX_PENDING = "pending"
OUTBOX_PROCESSING = "processing"
OUTBOX_COMPLETED = "completed"
OUTBOX_FAILED = "failed"


class OutboxEnqueueStatus(str, Enum):
    ENQUEUED = "enqueued"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class OutboxIntent:
    """只描述可恢复任务的 metadata；不承载 transcript/provider response。"""

    idempotency_key: str
    task_type: str
    payload: Mapping[str, object] = field(default_factory=dict)
    available_at: int | None = None
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not str(self.idempotency_key or "").strip():
            raise ValueError("idempotency_key must not be blank")
        if not str(self.task_type or "").strip():
            raise ValueError("task_type must not be blank")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        if isinstance(self.max_attempts, bool) or int(self.max_attempts) <= 0:
            raise ValueError("max_attempts must be positive")
        object.__setattr__(self, "idempotency_key", str(self.idempotency_key).strip())
        object.__setattr__(self, "task_type", str(self.task_type).strip())
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "available_at", int(time.time()) if self.available_at is None else int(self.available_at))
        object.__setattr__(self, "max_attempts", int(self.max_attempts))


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: str
    idempotency_key: str
    task_type: str
    payload: dict[str, object]
    status: str
    attempt_count: int
    max_attempts: int
    available_at: int
    lease_owner: str | None
    lease_token: str | None
    lease_until: int | None
    last_error: str | None
    event_seq: int | None
    created_at: int
    updated_at: int
    completed_at: int | None
    state_version: int

    @property
    def id(self) -> str:
        return self.outbox_id

    @property
    def attempt(self) -> int:
        return self.attempt_count


@dataclass(frozen=True, slots=True, init=False)
class OutboxRetentionExpectation:
    """Exact immutable generation expected by a completed-row retention delete."""

    outbox_id: str
    idempotency_key: str
    task_type: str
    completed_at: int
    expected_state_version: int

    def __init__(
        self,
        outbox_id: str,
        idempotency_key: str,
        task_type: str,
        completed_at: int,
        expected_state_version: int | None = None,
        *,
        state_version: int | None = None,
    ) -> None:
        """Build an expectation, accepting ``state_version`` as a read-side alias."""

        if expected_state_version is None:
            selected_version = state_version
        elif state_version is not None and state_version != expected_state_version:
            raise ValueError("expected_state_version and state_version must match")
        else:
            selected_version = expected_state_version
        object.__setattr__(self, "outbox_id", outbox_id)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "task_type", task_type)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "expected_state_version", selected_version)
        self.__post_init__()

    def __post_init__(self) -> None:
        for name, value in (
            ("outbox_id", self.outbox_id),
            ("idempotency_key", self.idempotency_key),
            ("task_type", self.task_type),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be blank")
            object.__setattr__(self, name, value.strip())
        for name, numeric_value in (
            ("completed_at", self.completed_at),
            ("expected_state_version", self.expected_state_version),
        ):
            if (
                isinstance(numeric_value, bool)
                or not isinstance(numeric_value, int)
                or numeric_value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")

    @property
    def state_version(self) -> int:
        """Compatibility alias for the fenced generation value."""

        return self.expected_state_version


@dataclass(frozen=True, slots=True)
class OutboxEnqueueResult:
    status: OutboxEnqueueStatus
    record: OutboxRecord

    @property
    def accepted(self) -> bool:
        return self.status is OutboxEnqueueStatus.ENQUEUED

    @property
    def duplicate(self) -> bool:
        return self.status is OutboxEnqueueStatus.DUPLICATE

    @property
    def outbox_id(self) -> str:
        return self.record.outbox_id

    def __bool__(self) -> bool:
        return self.accepted


OutboxSubmitStatus = OutboxEnqueueStatus
OutboxSubmitResult = OutboxEnqueueResult
OutboxResult = OutboxEnqueueResult


@dataclass(frozen=True, slots=True)
class AppendOutboxResult:
    event_seq: int
    outbox: OutboxEnqueueResult

    def __iter__(self):
        yield self.event_seq
        yield self.outbox


class OutboxLeaseError(StaleWrite):
    """Outbox transition used an invalid or expired lease token."""


OutboxStaleLease = OutboxLeaseError


class LedgerStore(Protocol):
    """账本存储接口：单写者、append-only、投影可重建。"""

    def append(self, event: Event) -> int:
        ...

    def append_many(self, events: list[Event]) -> int:
        ...

    def scan(
        self,
        idea_id: str,
        *,
        after_seq: int = 0,
        branch: str | None = None,
        event_types: set[str] | None = None,
    ) -> Iterator[Event]:
        ...

    def project(self, idea_id: str) -> Projection:
        ...


class AttachmentStore(Protocol):
    """Narrow same-database primitives used by the framework-neutral intake.

    Implementations must couple each lifecycle row update and metadata-only
    ledger event in one SQLite write transaction.  The application service is
    deliberately unable to issue arbitrary SQL.
    """

    def begin_attachment(
        self,
        metadata: Mapping[str, object] | None = None,
        *,
        event: Event,
        now: int | None = None,
        **fields: object,
    ) -> AttachmentRecord:
        ...

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
        ...

    def get_attachment(self, attachment_id: str, *, workspace_id: str | None = None) -> AttachmentRecord | None:
        ...

    def get_attachment_by_hash(self, sha256: str, *, workspace_id: str) -> AttachmentRecord | None:
        ...

    def get_attachment_by_idempotency(self, idempotency_key: str, *, workspace_id: str) -> AttachmentRecord | None:
        ...

    def list_attachments(
        self,
        *,
        workspace_id: str,
        limit: int = 100,
        statuses: set[str] | None = None,
    ) -> list[AttachmentRecord]:
        ...

    def list_attachment_candidates(self, *, workspace_id: str, limit: int = 100) -> list[AttachmentRecord]:
        ...

    def attachment_usage(self, *, workspace_id: str) -> tuple[int, int]:
        ...


__all__ = [
    "AppendOutboxResult", "AttachmentConflict", "AttachmentConflictError", "AttachmentNotFound",
    "AttachmentRecord", "AttachmentStateError", "AttachmentStore", "ATTACHMENT_FAILED",
    "ATTACHMENT_PENDING", "ATTACHMENT_QUARANTINED", "ATTACHMENT_READY", "ATTACHMENT_REJECTED",
    "ATTACHMENT_STATUSES", "DuplicateFingerprint", "LeaseConflict", "LedgerStore",
    "OUTBOX_COMPLETED", "OUTBOX_FAILED", "OUTBOX_PENDING", "OUTBOX_PROCESSING",
    "OutboxConflict", "OutboxConflictError", "OutboxEnqueueResult", "OutboxEnqueueStatus",
    "OutboxIntent", "OutboxLeaseError", "OutboxRecord", "OutboxResult", "OutboxStaleLease",
    "OutboxRecoveryConflict", "OutboxRecoveryConflictError", "OutboxRetentionConflict",
    "OutboxRetentionConflictError", "OutboxRetentionExpectation",
    "OutboxSubmitResult", "OutboxSubmitStatus", "StaleWrite", "StoreError",
]
