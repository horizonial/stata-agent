"""Thread-safe, cooperative cancellation primitives.

Cancellation in this package is deliberately a *request*, not an attempt to
kill a Python thread.  A caller sets :class:`CancellationToken`; code that
owns a safe boundary checks it and exits.  Code waiting on an external side
effect must close/abort that transport where possible and report the outcome
as uncertain when the side effect cannot be observed reliably.
"""

from __future__ import annotations

from threading import Event, RLock
from time import monotonic
from typing import Any


class CancellationRequested(RuntimeError):
    """Raised at a cooperative cancellation boundary."""

    cancelled = True
    cancel_requested = True

    def __init__(self, reason: str = "cancelled") -> None:
        self.reason = str(reason or "cancelled")
        super().__init__(self.reason)


class CancellationToken:
    """A one-shot, thread-safe cancellation token.

    ``cancel`` is idempotent: the first request wins and later calls only
    increment the diagnostic count.  ``state`` stays ``cancel_requested``
    until the owner acknowledges the request, which lets telemetry distinguish
    a request from an operation that actually reached its cancellation point.
    """

    ACTIVE = "active"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"

    def __init__(self) -> None:
        self._event = Event()
        self._lock = RLock()
        self._state = self.ACTIVE
        self._reason: str | None = None
        self._requested_at: float | None = None
        self._cancel_count = 0

    def cancel(self, reason: str = "cancelled") -> bool:
        """Request cancellation and return ``True`` only for the first call."""

        with self._lock:
            self._cancel_count += 1
            if self._event.is_set():
                return False
            self._reason = str(reason or "cancelled")
            self._requested_at = monotonic()
            self._state = self.CANCEL_REQUESTED
            self._event.set()
            return True

    def request_cancel(self, reason: str = "cancelled") -> bool:
        """Readable alias for integrations that call this a cancellation request."""

        return self.cancel(reason)

    def acknowledge(self) -> bool:
        """Mark a requested cancellation as observed by the owning operation."""

        with self._lock:
            if not self._event.is_set():
                return False
            self._state = self.CANCELLED
            return True

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    @property
    def cancel_count(self) -> int:
        with self._lock:
            return self._cancel_count

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested (Event-compatible meaning)."""

        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        """Short compatibility alias for integrations using a flag property."""

        return self._event.is_set()

    @property
    def is_cancel_requested(self) -> bool:
        return self._event.is_set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    @property
    def cancel_requested(self) -> bool:
        return self._event.is_set()

    def is_set(self) -> bool:
        """Compatibility with ``threading.Event`` used by older callers."""

        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def throw_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancellationRequested(self.reason or "cancelled")

    def throw_if_requested(self) -> None:
        self.throw_if_cancelled()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "reason": self._reason,
                "cancel_count": self._cancel_count,
                "requested": self._event.is_set(),
            }


def _flag(value: Any, name: str) -> bool:
    try:
        attr = getattr(value, name, None)
        return bool(attr() if callable(attr) else attr)
    except Exception:  # noqa: BLE001 - cancellation must fail safe
        return False


def is_cancel_requested(token: Any) -> bool:
    """Read a ``CancellationToken`` or legacy Event-like object safely."""

    if token is None:
        return False
    return (
        _flag(token, "is_cancel_requested")
        or _flag(token, "is_cancelled")
        or _flag(token, "is_set")
    )


def cancellation_reason(token: Any, default: str = "cancelled") -> str:
    if token is None:
        return default
    try:
        reason = getattr(token, "reason", None)
        if callable(reason):
            reason = reason()
        if reason:
            return str(reason)
    except Exception:  # noqa: BLE001
        pass
    return default


def raise_if_cancelled(token: Any) -> None:
    """Raise :class:`CancellationRequested` for any supported token shape."""

    if not is_cancel_requested(token):
        return
    reason = cancellation_reason(token)
    if isinstance(token, CancellationToken):
        token.throw_if_cancelled()
    raise CancellationRequested(reason)


__all__ = [
    "CancellationRequested",
    "CancellationToken",
    "cancellation_reason",
    "is_cancel_requested",
    "raise_if_cancelled",
]
