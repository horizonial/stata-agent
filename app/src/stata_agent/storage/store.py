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


class OutboxConflictError(StoreError, ValueError):
    """同幂等键对应不同任务定义。"""


OutboxConflict = OutboxConflictError

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

    @property
    def id(self) -> str:
        return self.outbox_id

    @property
    def attempt(self) -> int:
        return self.attempt_count


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


__all__ = [
    "AppendOutboxResult", "DuplicateFingerprint", "LeaseConflict", "LedgerStore",
    "OUTBOX_COMPLETED", "OUTBOX_FAILED", "OUTBOX_PENDING", "OUTBOX_PROCESSING",
    "OutboxConflict", "OutboxConflictError", "OutboxEnqueueResult", "OutboxEnqueueStatus",
    "OutboxIntent", "OutboxLeaseError", "OutboxRecord", "OutboxResult", "OutboxStaleLease",
    "OutboxSubmitResult", "OutboxSubmitStatus", "StaleWrite", "StoreError",
]
