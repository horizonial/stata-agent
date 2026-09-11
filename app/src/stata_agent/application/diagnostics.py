"""Bounded, privacy-safe operational diagnostic bundle.

The diagnostic bundle is a disposable read model.  It deliberately starts
from an empty allow-list projection instead of reusing the content-bearing
Trace serializers or dumping event/outbox payloads.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .. import __version__


BUNDLE_SCHEMA = "diagnostic.bundle.v1"
MAX_EVENT_LIMIT = 500
MAX_OUTBOX_LIMIT = 200
MAX_REQUEST_ID_LENGTH = 128


class DiagnosticValidationError(ValueError):
    """Caller supplied an invalid diagnostic scope, filter, or bound."""


class DiagnosticReadError(RuntimeError):
    """A diagnostic source could not be read safely."""


class DiagnosticStore(Protocol):
    def scan_recent_events(
        self,
        idea_id: str,
        *,
        limit: int,
        correlation_id: str | None = None,
    ) -> tuple[list[Any], bool]:
        ...

    def scan_recent_outbox(
        self,
        *,
        task_type: str,
        limit: int,
    ) -> tuple[list[Any], bool]:
        ...

    def outbox_stats(self) -> Mapping[str, object]:
        ...


@dataclass(frozen=True, slots=True)
class DiagnosticBundleRequest:
    """Validated inputs for one bounded diagnostic snapshot."""

    idea_id: str
    workspace_id: str | None = None
    event_limit: int = 200
    outbox_limit: int = 100
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.idea_id, str) or not self.idea_id.strip():
            raise DiagnosticValidationError("idea_id must not be blank")
        object.__setattr__(self, "idea_id", self.idea_id.strip())
        if self.workspace_id is not None:
            object.__setattr__(self, "workspace_id", _validate_opaque_id(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "event_limit", _bounded_int(self.event_limit, 1, MAX_EVENT_LIMIT, "event_limit"))
        object.__setattr__(
            self,
            "outbox_limit",
            _bounded_int(self.outbox_limit, 1, MAX_OUTBOX_LIMIT, "outbox_limit"),
        )
        if self.request_id is not None:
            object.__setattr__(self, "request_id", _validate_opaque_id(self.request_id, "request_id"))


def _bounded_int(value: object, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DiagnosticValidationError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _validate_opaque_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_REQUEST_ID_LENGTH or value != value.strip():
        raise DiagnosticValidationError(f"{name} must be a 1..{MAX_REQUEST_ID_LENGTH} character opaque string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise DiagnosticValidationError(f"{name} must not contain control characters")
    return value


def _safe_id(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_REQUEST_ID_LENGTH:
        return None
    if value != value.strip() or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    return value


def _safe_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _safe_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


_KNOWN_EVENT_TYPES = frozenset(
    {
        "idea.declared",
        "user.message",
        "agent_step",
        "tool.invoked",
        "tool.done",
        "tool.call",
        "tool.result",
        "run.requested",
        "run.succeeded",
        "run.failed",
        "run.uncertain",
        "budget.limit",
        "context.assembled",
        "compaction.boundary",
        "health.probe",
        "approval.requested",
        "approval.granted",
        "approval.rejected",
        "phase.transition",
        "system.restored",
        "branch.created",
        "memory.extraction.requested",
        "memory.extraction.completed",
        "memory.extraction.failed",
        "memory.review.requested",
        "memory.review.completed",
        "memory.review.failed",
    }
)
_SAFE_TERMINAL_REASONS = frozenset(
    {
        "model_stop",
        "cancelled",
        "cancel_requested",
        "provider_error",
        "provider_unsupported",
        "context_error",
        "context_budget",
        "budget_invalid",
        "tool_calls",
        "goal_max_steps",
        "privacy_denied",
        "tool_error",
        "tool_timeout",
        "tool_loop",
        "script_error",
    }
)
_SAFE_PHASES = frozenset(
    {"IDEA", "LITERATURE", "DESIGN", "DATA", "ESTIMATION", "ROBUSTNESS", "WRITING", "VALIDATION", "DONE"}
)
_SAFE_OUTBOX_STAT_KEYS = frozenset(
    {"pending", "processing", "completed", "failed", "ready", "expired_leases", "oldest_pending_age_seconds"}
)
_SAFE_QUEUE_KEYS = frozenset(
    {"accepting", "pending", "running", "accepted", "completed", "failed", "duplicates", "full", "closed"}
)
_SAFE_PRIVACY_MODES = frozenset({"local_strict", "approved_remote", "mixed_sanitized"})


def _safe_event_type(value: object) -> str:
    return value if isinstance(value, str) and value in _KNOWN_EVENT_TYPES else "unknown"


def _safe_phase(value: object) -> str | None:
    if value is None:
        return None
    candidate = getattr(value, "value", value)
    return candidate if isinstance(candidate, str) and candidate in _SAFE_PHASES else "unknown"


def _safe_terminal_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value in _SAFE_TERMINAL_REASONS else "other"


def _safe_payload(event_type: str, payload: Mapping[str, object]) -> dict[str, object]:
    """Copy only event-specific operational scalars from an untrusted payload."""

    out: dict[str, object] = {}

    def copy_id(key: str) -> None:
        value = _safe_id(payload.get(key))
        if value is not None:
            out[key] = value

    def copy_bool(key: str) -> None:
        value = _safe_bool(payload.get(key))
        if value is not None:
            out[key] = value

    def copy_int(key: str) -> None:
        value = _safe_nonnegative_int(payload.get(key))
        if value is not None:
            out[key] = value

    if event_type in {"tool.invoked", "tool.done"}:
        copy_id("tool")
        copy_bool("ok")
        copy_bool("is_error")
        if event_type == "tool.done":
            result = payload.get("result")
            if isinstance(result, Mapping):
                data = result.get("data")
                if isinstance(data, Mapping):
                    run_id = _safe_id(data.get("run_id"))
                    if run_id is not None:
                        out["run_id"] = run_id
                    reused = _safe_bool(data.get("reused"))
                    if reused is not None:
                        out["reused"] = reused
    elif event_type in {"tool.call", "tool.result"}:
        for key in ("run_id", "call_id", "spec_id"):
            copy_id(key)
        copy_int("rc")
        copy_bool("is_error")
        copy_bool("test_only")
    elif event_type in {"run.requested", "run.succeeded", "run.failed", "run.uncertain"}:
        for key in ("run_id", "call_id", "spec_id"):
            copy_id(key)
        for key in ("test_only", "reused", "cancel_requested"):
            copy_bool(key)
        reason = _safe_terminal_reason(payload.get("terminal_reason"))
        if reason is not None:
            out["terminal_reason"] = reason
    elif event_type == "agent_step":
        reason = _safe_terminal_reason(payload.get("terminal_reason"))
        if reason is not None:
            out["terminal_reason"] = reason
        copy_bool("cancel_requested")
    elif event_type == "budget.limit":
        kind = payload.get("kind")
        if isinstance(kind, str) and kind in {"tool_calls", "steps", "context", "input_tokens", "output_tokens"}:
            out["kind"] = kind
        copy_int("limit")
        copy_int("tool_calls")
    elif event_type in {
        "memory.extraction.requested",
        "memory.extraction.completed",
        "memory.extraction.failed",
        "memory.review.requested",
        "memory.review.completed",
        "memory.review.failed",
    }:
        status = payload.get("status")
        if isinstance(status, str) and status in {"requested", "processing", "completed", "failed", "rejected"}:
            out["status"] = status

    return out


def _project_event(event: Any) -> dict[str, object]:
    event_type = _safe_event_type(getattr(event, "event_type", None))
    result: dict[str, object] = {
        "seq": _safe_nonnegative_int(getattr(event, "seq", None)),
        "event_type": event_type,
        "created_at": _safe_nonnegative_int(getattr(event, "created_at", None)),
        "actor": _safe_id(getattr(event, "actor", None)) or "unknown",
        "source": _safe_id(getattr(event, "source", None)) or "unknown",
        "phase": _safe_phase(getattr(event, "phase", None)),
        "correlation_id": _safe_id(getattr(event, "correlation_id", None)),
        "operation_id": _safe_id(getattr(event, "operation_id", None)),
        "attempt_id": _safe_nonnegative_int(getattr(event, "attempt_id", None)),
        "side_effect_state": _safe_id(getattr(event, "side_effect_state", None)),
    }
    payload = getattr(event, "payload", {})
    if isinstance(payload, Mapping):
        result["facts"] = _safe_payload(event_type, payload)
    else:
        result["facts"] = {}
    return result


def _project_outbox(row: Any, *, now: int) -> dict[str, object]:
    lease_until = _safe_nonnegative_int(getattr(row, "lease_until", None))
    raw_status = getattr(row, "status", None)
    status = raw_status if isinstance(raw_status, str) and raw_status in {
        "pending", "processing", "completed", "failed"
    } else "unknown"
    return {
        "outbox_id": _safe_id(getattr(row, "outbox_id", None)) or "unknown",
        "task_type": "memory.extraction" if getattr(row, "task_type", None) == "memory.extraction" else "unknown",
        "status": status,
        "attempt_count": _safe_nonnegative_int(getattr(row, "attempt_count", None)),
        "max_attempts": _safe_nonnegative_int(getattr(row, "max_attempts", None)),
        "lease_present": bool(
            getattr(row, "lease_owner", None) is not None
            or getattr(row, "lease_token", None) is not None
            or lease_until is not None
        ),
        "lease_expired": bool(lease_until is not None and lease_until <= now),
        "event_seq": _safe_nonnegative_int(getattr(row, "event_seq", None)),
        "created_at": _safe_nonnegative_int(getattr(row, "created_at", None)),
        "updated_at": _safe_nonnegative_int(getattr(row, "updated_at", None)),
        "completed_at": _safe_nonnegative_int(getattr(row, "completed_at", None)),
        "state_version": _safe_nonnegative_int(getattr(row, "state_version", None)),
        "has_error": bool(getattr(row, "last_error", None)),
    }


def _safe_stats(source: Mapping[str, object]) -> dict[str, int]:
    return {
        key: _safe_nonnegative_int(source.get(key)) or 0
        for key in sorted(_SAFE_OUTBOX_STAT_KEYS)
    }


def _safe_queue(source: object) -> dict[str, object]:
    if hasattr(source, "as_dict") and callable(source.as_dict):
        raw = source.as_dict()
    elif isinstance(source, Mapping):
        raw = source
    else:
        raw = {}
    result: dict[str, object] = {"durability": "process_local"}
    for key in sorted(_SAFE_QUEUE_KEYS):
        value = raw.get(key)
        if key == "accepting":
            result[key] = bool(value) if isinstance(value, bool) else False
        else:
            result[key] = _safe_nonnegative_int(value) or 0
    return result


def _safe_health(source: object) -> dict[str, object]:
    raw = source if isinstance(source, Mapping) else {}
    privacy = raw.get("privacy_mode")
    safe_privacy = privacy if isinstance(privacy, str) and privacy in _SAFE_PRIVACY_MODES else "unknown"
    return {
        "ok": bool(raw.get("ok")) if isinstance(raw.get("ok"), bool) else None,
        "privacy_mode": safe_privacy,
        "local_strict": bool(raw.get("local_strict")) if isinstance(raw.get("local_strict"), bool) else None,
        "network_available": (
            bool(raw.get("network_available")) if isinstance(raw.get("network_available"), bool) else None
        ),
        "skill_error_count": _safe_nonnegative_int(raw.get("skill_error_count")) or 0,
    }


@dataclass
class _CorrelationAccumulator:
    event_seqs: set[int]
    operation_ids: set[str]
    run_ids: set[str]
    outbox_ids: set[str]


def _correlation_groups(events: list[dict[str, object]], outbox: list[dict[str, object]]) -> list[dict[str, object]]:
    by_request: dict[str, _CorrelationAccumulator] = {}
    outbox_by_seq = {
        row["event_seq"]: row["outbox_id"]
        for row in outbox
        if isinstance(row.get("event_seq"), int) and isinstance(row.get("outbox_id"), str)
    }
    for event in events:
        request_id = event.get("correlation_id")
        if not isinstance(request_id, str):
            continue
        group = by_request.setdefault(request_id, _CorrelationAccumulator(set(), set(), set(), set()))
        seq = event.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            group.event_seqs.add(seq)
        operation_id = event.get("operation_id")
        if isinstance(operation_id, str):
            group.operation_ids.add(operation_id)
        facts = event.get("facts")
        if isinstance(facts, Mapping) and isinstance(facts.get("run_id"), str):
            group.run_ids.add(facts["run_id"])
        event_seq = seq
        if isinstance(event_seq, int):
            outbox_id = outbox_by_seq.get(event_seq)
            if isinstance(outbox_id, str):
                group.outbox_ids.add(outbox_id)
    return [
        {
            "request_id": request_id,
            "event_seqs": sorted(values.event_seqs),
            "operation_ids": sorted(values.operation_ids),
            "run_ids": sorted(values.run_ids),
            "outbox_ids": sorted(values.outbox_ids),
        }
        for request_id, values in sorted(by_request.items())
    ]


class DiagnosticBundleService:
    """Build one bounded diagnostic snapshot from existing read boundaries."""

    def __init__(
        self,
        store: DiagnosticStore,
        *,
        queue_snapshot: object = None,
        health_snapshot: object = None,
        clock: Callable[[], float] | None = None,
        product_version: str = __version__,
    ) -> None:
        self._store = store
        self._queue_snapshot = queue_snapshot
        self._health_snapshot = health_snapshot
        self._clock = clock or time.time
        self._product_version = str(product_version)

    def build(self, request: DiagnosticBundleRequest) -> dict[str, object]:
        if not isinstance(request, DiagnosticBundleRequest):
            raise TypeError("request must be DiagnosticBundleRequest")
        generated_at = int(self._clock())
        try:
            events, events_truncated = self._store.scan_recent_events(
                request.idea_id,
                limit=request.event_limit,
                correlation_id=request.request_id,
            )
            outbox_rows, outbox_truncated = self._store.scan_recent_outbox(
                task_type="memory.extraction",
                limit=request.outbox_limit,
            )
            stats = self._store.outbox_stats()
        except (DiagnosticValidationError, DiagnosticReadError):
            raise
        except Exception as error:  # noqa: BLE001 - never expose source exception text
            raise DiagnosticReadError("diagnostic sources unavailable") from error

        safe_events = [_project_event(event) for event in events]
        safe_outbox = [_project_outbox(row, now=generated_at) for row in outbox_rows]
        groups = _correlation_groups(safe_events, safe_outbox)
        correlated = sum(1 for event in safe_events if event.get("correlation_id") is not None)
        linked = 0
        for group in groups:
            outbox_ids = group.get("outbox_ids")
            if isinstance(outbox_ids, list):
                linked += len(outbox_ids)
        return {
            "manifest": {
                "schema": BUNDLE_SCHEMA,
                "schema_version": 1,
                "product_version": self._product_version,
                "generated_at": generated_at,
                "workspace_id": _safe_id(request.workspace_id),
                "request_filter_applied": request.request_id is not None,
                "limits": {"event_limit": request.event_limit, "outbox_limit": request.outbox_limit},
                "truncated": {"events": events_truncated, "outbox": outbox_truncated},
            },
            "health": _safe_health(self._health_snapshot),
            "queue": _safe_queue(self._queue_snapshot),
            "outbox": {
                "stats": _safe_stats(stats if isinstance(stats, Mapping) else {}),
                "items": safe_outbox,
                "truncated": outbox_truncated,
            },
            "events": safe_events,
            "correlations": groups,
            "coverage": {
                "events_included": len(safe_events),
                "correlated_events": correlated,
                "uncorrelated_events": len(safe_events) - correlated,
                "correlation_groups": len(groups),
                "linked_outbox_rows": linked,
            },
        }


__all__ = [
    "BUNDLE_SCHEMA",
    "DiagnosticBundleRequest",
    "DiagnosticBundleService",
    "DiagnosticReadError",
    "DiagnosticValidationError",
    "MAX_EVENT_LIMIT",
    "MAX_OUTBOX_LIMIT",
]
