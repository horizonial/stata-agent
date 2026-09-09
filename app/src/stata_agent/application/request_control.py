"""Process-local request lifecycle controls.

The HTTP layer needs a small, thread-safe registry to address an in-flight
chat without making cancellation state part of the durable research ledger.
This module deliberately has no FastAPI dependency: transports can translate
the small set of lookup errors into their own response shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, RLock
from time import time
from typing import Any, Callable


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
"""Statuses after which a request can no longer be cancelled or changed."""


class RequestControlNotFound(LookupError):
    """Raised when a request is missing or belongs to another workspace."""


@dataclass
class _RequestControl:
    request_id: str
    workspace: str
    cancel_event: Any
    status: str
    cancel_requested: bool
    cancel_reason: str | None
    created_at: float
    finished_at: float | None = None


class RequestControlRegistry:
    """Thread-safe registry for short-lived, process-local request controls.

    The registry is intentionally ephemeral.  The event ledger remains the
    source of truth for research state; this class only records enough
    in-flight metadata for a transport to request cooperative cancellation and
    display stable terminal status briefly after a request completes.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time,
    ) -> None:
        if isinstance(ttl_seconds, bool) or ttl_seconds < 0:
            raise ValueError("ttl_seconds must be a non-negative number")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._controls: dict[str, _RequestControl] = {}
        self._lock = RLock()

    @property
    def ttl_seconds(self) -> float:
        """Configured retention for terminal snapshots."""

        return self._ttl_seconds

    def register(
        self,
        request_id: str,
        workspace: str,
        cancel_event: Any | None = None,
    ) -> dict[str, Any]:
        """Register or replace one running request and return its public view."""

        request_id = self._required_text(request_id, "request_id")
        workspace = self._required_text(workspace, "workspace")
        event = cancel_event if cancel_event is not None else Event()
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            self._controls[request_id] = _RequestControl(
                request_id=request_id,
                workspace=workspace,
                cancel_event=event,
                status="running",
                cancel_requested=False,
                cancel_reason=None,
                created_at=now,
            )
            return self._public_required(self._controls[request_id])

    def cancel(
        self,
        request_id: str,
        workspace: str,
        *,
        reason: str = "user",
    ) -> dict[str, Any]:
        """Request cooperative cancellation for a workspace-owned request.

        Repeated cancellation is idempotent: the first reason and state win,
        while a late request against a terminal row returns the stable
        terminal snapshot without touching its event.
        """

        with self._lock:
            control = self._lookup_locked(request_id, workspace)
            if control.status in TERMINAL_STATUSES:
                return self._public_required(control)
            if not control.cancel_requested:
                control.cancel_requested = True
                control.cancel_reason = str(reason or "user")
                control.status = "cancelling"
                self._set_event(control.cancel_event)
            return self._public_required(control)

    def disconnect(self, request_id: str) -> dict[str, Any] | None:
        """Treat a transport disconnect as a cancellation request.

        Missing requests and already-terminal rows are deliberately no-ops so
        response-generator cleanup can safely race normal completion.
        """

        with self._lock:
            control = self._controls.get(str(request_id))
            if control is None or control.status in TERMINAL_STATUSES:
                return self._public(control) if control is not None else None
            if not control.cancel_requested:
                control.cancel_requested = True
                control.cancel_reason = "disconnect"
                control.status = "cancelling"
                self._set_event(control.cancel_event)
            return self._public(control)

    def finish(self, request_id: str, status: str) -> dict[str, Any] | None:
        """Mark a request terminally, preserving the first terminal result."""

        status = str(status or "").strip().lower()
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"status must be one of {sorted(TERMINAL_STATUSES)}")
        with self._lock:
            control = self._controls.get(str(request_id))
            if control is None:
                return None
            if control.status in TERMINAL_STATUSES:
                return self._public(control)
            control.status = status
            control.finished_at = self._clock()
            return self._public(control)

    def snapshot(self, request_id: str) -> dict[str, Any] | None:
        """Return a copy suitable for transport serialization."""

        with self._lock:
            self._prune_locked(self._clock())
            control = self._controls.get(str(request_id))
            return self._public(control) if control is not None else None

    def latest_active(self, workspace: str) -> dict[str, Any] | None:
        """Return the newest non-terminal request for ``workspace``."""

        workspace = str(workspace)
        with self._lock:
            self._prune_locked(self._clock())
            active = [
                control
                for control in self._controls.values()
                if control.workspace == workspace and control.status not in TERMINAL_STATUSES
            ]
            control = max(active, key=lambda item: item.created_at, default=None)
            return self._public(control) if control is not None else None

    def prune(self, now: float | None = None) -> int:
        """Drop expired terminal rows and return the number removed."""

        timestamp = self._clock() if now is None else float(now)
        with self._lock:
            return self._prune_locked(timestamp)

    def _lookup_locked(self, request_id: str, workspace: str) -> _RequestControl:
        request_id = self._required_text(request_id, "request_id")
        workspace = self._required_text(workspace, "workspace")
        self._prune_locked(self._clock())
        control = self._controls.get(request_id)
        if control is None or control.workspace != workspace:
            raise RequestControlNotFound("request does not exist or belongs to another workspace")
        return control

    def _prune_locked(self, now: float) -> int:
        expired = [
            request_id
            for request_id, control in self._controls.items()
            if control.status in TERMINAL_STATUSES
            and now - float(
                control.finished_at if control.finished_at is not None else control.created_at
            ) > self._ttl_seconds
        ]
        for request_id in expired:
            self._controls.pop(request_id, None)
        return len(expired)

    @staticmethod
    def _required_text(value: str, name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{name} must not be empty")
        return text

    @staticmethod
    def _set_event(event: Any) -> None:
        setter = getattr(event, "set", None)
        if not callable(setter):
            raise TypeError("cancel_event must expose set()")
        setter()

    @staticmethod
    def _public(control: _RequestControl | None) -> dict[str, Any] | None:
        if control is None:
            return None
        return {
            "request_id": control.request_id,
            "workspace": control.workspace,
            "status": control.status,
            "cancel_requested": control.cancel_requested,
            "cancel_reason": control.cancel_reason,
            "created_at": control.created_at,
            "finished_at": control.finished_at,
        }

    @classmethod
    def _public_required(cls, control: _RequestControl) -> dict[str, Any]:
        public = cls._public(control)
        assert public is not None
        return public


__all__ = [
    "RequestControlNotFound",
    "RequestControlRegistry",
    "TERMINAL_STATUSES",
]
