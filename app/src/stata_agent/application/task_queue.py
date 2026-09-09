"""Small replaceable port for bounded background work.

The application layer only needs to admit an idempotent callback and observe
its lifecycle.  This protocol deliberately says nothing about threads,
``queue.Queue``, providers, ledgers, or memory stores.  A durable outbox,
RQ/Redis adapter, or another process scheduler can implement the same port
later without leaking its transport into business code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


TaskCallback = Callable[[], object]


class TaskSubmitStatus(str, Enum):
    """Admission result for one idempotency key."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    FULL = "full"
    CLOSED = "closed"


@dataclass(frozen=True, eq=False)
class TaskSubmitResult:
    """Stable admission result, including the normalized key for logging."""

    status: TaskSubmitStatus
    idempotency_key: str

    @property
    def accepted(self) -> bool:
        return self.status is TaskSubmitStatus.ACCEPTED

    @property
    def is_accepted(self) -> bool:
        """Readable compatibility alias for callers that prefer a predicate."""

        return self.accepted

    def __bool__(self) -> bool:
        return self.accepted

    def __str__(self) -> str:
        return self.status.value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TaskSubmitResult):
            return (self.status, self.idempotency_key) == (other.status, other.idempotency_key)
        if isinstance(other, (TaskSubmitStatus, str)):
            return self.status == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.status, self.idempotency_key))


@dataclass(frozen=True)
class QueueStats:
    """Immutable, fixed-shape queue snapshot.

    ``pending`` excludes the currently running callback.  Counters are
    process-local diagnostics; they are not a durability or delivery claim.
    """

    accepting: bool
    pending: int
    running: int
    accepted: int
    completed: int
    failed: int
    duplicates: int
    full: int
    closed: int

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "accepting": self.accepting,
            "pending": self.pending,
            "running": self.running,
            "accepted": self.accepted,
            "completed": self.completed,
            "failed": self.failed,
            "duplicates": self.duplicates,
            "full": self.full,
            "closed": self.closed,
        }

    to_dict = as_dict

    def __getitem__(self, name: str) -> int | bool:
        return self.as_dict()[name]

    def __call__(self) -> "QueueStats":
        """Allow ``queue.stats()`` while keeping the property-style API."""

        return self


class TaskQueue(Protocol):
    """Transport-neutral port for short-lived background callbacks."""

    def submit(self, idempotency_key: str, callback: TaskCallback) -> TaskSubmitResult:
        """Attempt admission and distinguish accepted/duplicate/full/closed."""

    def start(self) -> None:
        """Start or re-open processing."""

    def resume(self) -> None:
        """Re-open processing after a bounded shutdown."""

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Stop admission and wait no longer than the implementation bound."""

    @property
    def stats(self) -> QueueStats:
        """Return a fixed-shape, point-in-time snapshot."""


# Concise aliases make the port easy to discover without multiplying concepts.
SubmitStatus = TaskSubmitStatus
SubmitResult = TaskSubmitResult


__all__ = [
    "QueueStats",
    "SubmitResult",
    "SubmitStatus",
    "TaskCallback",
    "TaskQueue",
    "TaskSubmitResult",
    "TaskSubmitStatus",
]
