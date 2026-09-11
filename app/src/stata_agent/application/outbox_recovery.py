"""Framework-neutral operator recovery for failed memory outbox rows.

The durable outbox is an operational dispatch index.  This module deliberately
does not know about FastAPI, SQLite, queues, or providers.  It owns the policy
that decides whether one *observed* failed generation may be reconciled against
the canonical ledger or returned to the normal dispatcher path.

Transport adapters should construct :class:`OutboxRecoveryRequest`, call
:meth:`OutboxRecoveryService.list_failed` or
:meth:`OutboxRecoveryService.recover`, and serialize the explicit DTOs.  The
repository implementation remains responsible for an atomic state-version
compare-and-swap transition.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias, cast


MEMORY_EXTRACTION_KIND = "memory.extraction"
OUTBOX_FAILED = "failed"
OUTBOX_PENDING = "pending"
OUTBOX_COMPLETED = "completed"

TERMINAL_EXTRACTION_EVENTS = frozenset(
    {
        "memory.extraction.completed",
        "memory.extraction.noop",
        "memory.extraction.failed",
        "memory.extraction.denied",
    }
)

RecoveryOutcome: TypeAlias = Literal["reconciled", "redriven"]

# These are the only failure labels that may cross the application/transport
# boundary.  ``last_error`` itself is intentionally never part of a DTO.
_SAFE_ERROR_CODES = frozenset(
    {
        "delivery_failed",
        "invalid_outbox_metadata",
        "invalid_privacy_mode",
        "max_attempts_exceeded",
        "outbox_lease_lost",
        "provider_error",
        "provider_unavailable",
        "queue_closed",
        "queue_full",
        "terminal_event_missing",
        "unsupported_task_type",
        "worker_error",
    }
)


class OutboxRecoveryError(RuntimeError):
    """Base class for deterministic recovery policy failures."""

    code = "recovery_error"


class OutboxRecoveryValidationError(OutboxRecoveryError, ValueError):
    """The operator supplied an invalid request or scope."""

    code = "invalid_request"


class OutboxRecoveryNotFoundError(OutboxRecoveryError, LookupError):
    """The requested row is not visible in the resolved scope."""

    code = "not_found"


class OutboxRecoveryConflictError(OutboxRecoveryError):
    """The row cannot be safely recovered under the observed generation."""

    code = "conflict"


# Short aliases are convenient for transport adapters while the verbose names
# remain the stable public names used in error handling.
OutboxRecoveryValidation = OutboxRecoveryValidationError
OutboxRecoveryNotFound = OutboxRecoveryNotFoundError
OutboxRecoveryConflict = OutboxRecoveryConflictError


class OutboxRecoveryRepository(Protocol):
    """Minimal storage port required by :class:`OutboxRecoveryService`.

    The returned row may be any immutable or mutable record object with the
    fields read by the service.  A concrete SQLite adapter can therefore pass
    its existing ``OutboxRecord`` directly without an application dependency on
    SQLite.  Transition methods must perform their own atomic CAS and return
    the resulting row.
    """

    def list_outbox(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> Sequence[object]:
        """
        Return durable rows.  The service applies task/workspace filtering and
        stable ordering, so the adapter must not reinterpret the requested
        scope.
        """

        ...

    def get_outbox(self, *, idempotency_key: str) -> object | None:
        """Return one row by its idempotency key, or ``None`` if absent."""

        ...

    def reconcile_failed_outbox(
        self,
        idempotency_key: str,
        task_type: str,
        expected_state_version: int,
    ) -> object:
        """Atomically transition the exact failed generation to completed."""

        ...

    def redrive_failed_outbox(
        self,
        idempotency_key: str,
        task_type: str,
        expected_state_version: int,
    ) -> object:
        """Atomically transition the exact failed generation to pending."""

        ...


class TerminalEventLookup(Protocol):
    """Canonical-ledger lookup port for one extraction fingerprint.

    Implementations must scope their result to the supplied idea/workspace and
    return either ``None``/``False`` or a matching terminal event.  If an event
    object is returned, this module performs a second defensive check that its
    type is one of the four terminal extraction events.
    """

    def find_terminal_event(
        self,
        idea_id: str,
        workspace_id: str,
        fingerprint: str,
    ) -> object | None:
        ...


# A callable is a deliberately supported equivalent port for small composition
# roots and deterministic tests.
TerminalLookupCallable: TypeAlias = Callable[[str, str, str], object | None]


@dataclass(frozen=True, slots=True)
class FailedOutboxListRequest:
    """Validated scope and bound for a failed-row listing."""

    idea_id: str
    workspace_id: str
    limit: int = 100

    def __post_init__(self) -> None:
        _require_identity(self.idea_id, "idea_id")
        _require_identity(self.workspace_id, "workspace_id")
        _require_limit(self.limit)
        object.__setattr__(self, "idea_id", self.idea_id.strip())
        object.__setattr__(self, "workspace_id", self.workspace_id.strip())


@dataclass(frozen=True, slots=True)
class OutboxRecoveryRequest:
    """One explicit operator action against an observed outbox generation."""

    idea_id: str
    workspace_id: str
    idempotency_key: str
    expected_state_version: int
    acknowledge_at_least_once: bool

    def __post_init__(self) -> None:
        _require_identity(self.idea_id, "idea_id")
        _require_identity(self.workspace_id, "workspace_id")
        _require_identity(self.idempotency_key, "idempotency_key")
        if (
            isinstance(self.expected_state_version, bool)
            or not isinstance(self.expected_state_version, int)
            or self.expected_state_version < 0
        ):
            raise OutboxRecoveryValidationError("expected_state_version must be a non-negative integer")
        # ``True`` must be literal True.  In particular, 1, "true", and truthy
        # objects are not accepted as an acknowledgement of duplicate risk.
        if self.acknowledge_at_least_once is not True:
            raise OutboxRecoveryValidationError("acknowledge_at_least_once must be true")
        object.__setattr__(self, "idea_id", self.idea_id.strip())
        object.__setattr__(self, "workspace_id", self.workspace_id.strip())
        object.__setattr__(self, "idempotency_key", self.idempotency_key.strip())


@dataclass(frozen=True, slots=True)
class OutboxSafeItem:
    """Allow-listed, transport-safe projection of one outbox row.

    No payload, raw error, prompt, provider response, lease token, or secret is
    represented by this DTO.  ``status`` is intentionally omitted: failed-list
    items are always failed, while a recovery result carries its target state at
    the result envelope.
    """

    outbox_id: str
    idempotency_key: str
    fingerprint: str
    idea_id: str
    workspace_id: str
    attempt_count: int
    max_attempts: int
    created_at: int
    updated_at: int
    state_version: int
    error_code: str
    terminal_present: bool

    @property
    def attempt(self) -> int:
        """Compatibility alias for callers that use the shorter field name."""

        return self.attempt_count

    def to_dict(self) -> dict[str, object]:
        """Serialize only the explicit safe allow-list."""

        return {
            "outbox_id": self.outbox_id,
            "idempotency_key": self.idempotency_key,
            "fingerprint": self.fingerprint,
            "idea_id": self.idea_id,
            "workspace_id": self.workspace_id,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state_version": self.state_version,
            "error_code": self.error_code,
            "terminal_present": self.terminal_present,
        }

    as_dict = to_dict


@dataclass(frozen=True, slots=True)
class OutboxRecoveryResult:
    """Explicit result of one successful reconcile or redrive action."""

    outcome: RecoveryOutcome
    status: Literal["completed", "pending"]
    item: OutboxSafeItem

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "status": self.status,
            "item": self.item.to_dict(),
        }

    as_dict = to_dict


def _require_identity(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OutboxRecoveryValidationError(f"{name} must be a non-empty string")


def _require_limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise OutboxRecoveryValidationError("limit must be an integer between 1 and 100")


def _row_value(row: object, name: str) -> object:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _strict_int(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OutboxRecoveryConflictError(f"invalid outbox {name}")
    return value


def _strict_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutboxRecoveryConflictError(f"invalid outbox {name}")
    return value


def _stable_error_code(value: object) -> str:
    """Map exact known labels to safe values; collapse everything else."""

    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in _SAFE_ERROR_CODES:
            return candidate
    return "delivery_failed"


def _payload_mapping(row: object) -> Mapping[str, object]:
    payload = _row_value(row, "payload")
    if not isinstance(payload, Mapping):
        raise OutboxRecoveryConflictError("invalid outbox payload")
    return payload


def _validate_row(
    row: object,
    *,
    idea_id: str,
    workspace_id: str,
    require_failed: bool,
    enforce_scope: bool = True,
) -> tuple[Mapping[str, object], dict[str, object]]:
    """Validate row identity and return payload plus normalized safe fields."""

    outbox_id = _strict_string(_row_value(row, "outbox_id"), "outbox_id")
    key = _strict_string(_row_value(row, "idempotency_key"), "idempotency_key")
    task_type = _strict_string(_row_value(row, "task_type"), "task_type")
    status = _row_value(row, "status")
    if task_type != MEMORY_EXTRACTION_KIND:
        raise OutboxRecoveryConflictError("unsupported outbox task type")
    if require_failed and status != OUTBOX_FAILED:
        raise OutboxRecoveryConflictError("outbox row is not failed")

    payload = _payload_mapping(row)
    expected_metadata = {
        "idea_id": idea_id,
        "workspace_id": workspace_id,
        "fingerprint": key,
        "kind": MEMORY_EXTRACTION_KIND,
    }
    for name, expected in expected_metadata.items():
        actual = payload.get(name)
        if not isinstance(actual, str) or not actual:
            raise OutboxRecoveryConflictError("outbox payload identity mismatch")
        if name in {"fingerprint", "kind"} and actual != expected:
            raise OutboxRecoveryConflictError("outbox payload identity mismatch")
        if enforce_scope and name in {"idea_id", "workspace_id"} and actual != expected:
            raise OutboxRecoveryConflictError("outbox payload identity mismatch")

    normalized = {
        "outbox_id": outbox_id,
        "idempotency_key": key,
        "task_type": task_type,
        "status": status,
        "payload_idea_id": payload["idea_id"],
        "payload_workspace_id": payload["workspace_id"],
        "attempt_count": _strict_int(_row_value(row, "attempt_count"), "attempt_count"),
        "max_attempts": _strict_int(_row_value(row, "max_attempts"), "max_attempts", minimum=1),
        "created_at": _strict_int(_row_value(row, "created_at"), "created_at"),
        "updated_at": _strict_int(_row_value(row, "updated_at"), "updated_at"),
        "state_version": _strict_int(_row_value(row, "state_version"), "state_version"),
        "last_error": _row_value(row, "last_error"),
    }
    return payload, normalized


def _event_value(event: object, name: str) -> object:
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _matching_terminal_event(
    candidate: object,
    *,
    idea_id: str,
    workspace_id: str,
    fingerprint: str,
) -> bool:
    """Defensively recognize one event returned by a terminal lookup port."""

    if candidate is None or candidate is False:
        return False
    if candidate is True:
        # A boolean lookup is already scoped by its port contract.
        return True
    if isinstance(candidate, (list, tuple, set, frozenset)):
        return any(
            _matching_terminal_event(
                item,
                idea_id=idea_id,
                workspace_id=workspace_id,
                fingerprint=fingerprint,
            )
            for item in candidate
        )

    event_type = _event_value(candidate, "event_type")
    if event_type not in TERMINAL_EXTRACTION_EVENTS:
        return False
    event_idea = _event_value(candidate, "idea_id")
    if event_idea not in (None, "", idea_id):
        return False
    payload = _event_value(candidate, "payload")
    if not isinstance(payload, Mapping):
        payload = {}
    event_workspace = payload.get("workspace_id")
    if event_workspace not in (None, "", workspace_id):
        return False
    event_fingerprint = _event_value(candidate, "fingerprint") or payload.get("fingerprint")
    if event_fingerprint not in (None, "", fingerprint):
        return False
    return True


def _lookup_terminal(
    lookup: TerminalEventLookup | TerminalLookupCallable | object,
    *,
    idea_id: str,
    workspace_id: str,
    fingerprint: str,
) -> bool:
    if callable(lookup):
        result = lookup(idea_id, workspace_id, fingerprint)
    else:
        method = getattr(lookup, "find_terminal_event", None)
        if not callable(method):
            # These names keep composition lightweight while preserving one
            # semantic port; all are expected to be scoped terminal lookups.
            for name in (
                "lookup_terminal_event",
                "has_terminal_event",
                "has_terminal_extraction",
                "has_terminal",
            ):
                candidate = getattr(lookup, name, None)
                if callable(candidate):
                    method = candidate
                    break
        if not callable(method):
            raise TypeError("terminal lookup must be callable or expose a terminal lookup method")
        result = method(idea_id, workspace_id, fingerprint)
    return _matching_terminal_event(
        result,
        idea_id=idea_id,
        workspace_id=workspace_id,
        fingerprint=fingerprint,
    )


class OutboxRecoveryService:
    """Apply safe, single-item recovery policy over repository ports."""

    task_type = MEMORY_EXTRACTION_KIND

    def __init__(
        self,
        repository: OutboxRecoveryRepository,
        terminal_lookup: TerminalEventLookup | TerminalLookupCallable,
    ) -> None:
        if repository is None:
            raise TypeError("repository is required")
        if terminal_lookup is None:
            raise TypeError("terminal_lookup is required")
        self.repository = repository
        self.terminal_lookup = terminal_lookup

    def list_failed(
        self,
        idea_id: str,
        workspace_id: str,
        *,
        limit: int = 100,
    ) -> tuple[OutboxSafeItem, ...]:
        """List safe failed extraction rows in deterministic oldest-first order."""

        request = FailedOutboxListRequest(idea_id, workspace_id, limit)
        # Do not pass the caller's limit to the repository: filtering by the
        # resolved workspace/task happens here, so another workspace cannot
        # consume the bound before the desired rows are considered.
        rows = self.repository.list_outbox(status=OUTBOX_FAILED, limit=None)
        if rows is None:
            raise OutboxRecoveryError("outbox repository returned no row collection")
        candidates: list[tuple[object, Mapping[str, object], dict[str, object]]] = []
        for row in rows:
            if _row_value(row, "status") != OUTBOX_FAILED:
                continue
            if _row_value(row, "task_type") != MEMORY_EXTRACTION_KIND:
                continue
            payload, normalized = _validate_row(
                row,
                idea_id=request.idea_id,
                workspace_id=request.workspace_id,
                require_failed=True,
                enforce_scope=False,
            )
            if (
                normalized["payload_idea_id"] != request.idea_id
                or normalized["payload_workspace_id"] != request.workspace_id
            ):
                continue
            candidates.append((row, payload, normalized))

        candidates.sort(key=lambda item: (item[2]["created_at"], item[2]["outbox_id"]))
        result: list[OutboxSafeItem] = []
        for _row, _payload, normalized in candidates[: request.limit]:
            terminal_present = _lookup_terminal(
                self.terminal_lookup,
                idea_id=request.idea_id,
                workspace_id=request.workspace_id,
                fingerprint=str(normalized["idempotency_key"]),
            )
            result.append(self._safe_item(normalized, terminal_present=terminal_present))
        return tuple(result)

    # Descriptive aliases make the application service easy to discover from a
    # transport adapter without creating a second policy implementation.
    list_failed_items = list_failed

    def recover(self, request: OutboxRecoveryRequest) -> OutboxRecoveryResult:
        """Reconcile or redrive exactly one failed generation."""

        if not isinstance(request, OutboxRecoveryRequest):
            raise OutboxRecoveryValidationError("request must be an OutboxRecoveryRequest")
        row = self.repository.get_outbox(idempotency_key=request.idempotency_key)
        if row is None:
            raise OutboxRecoveryNotFoundError("outbox item not found")

        _payload, normalized = _validate_row(
            row,
            idea_id=request.idea_id,
            workspace_id=request.workspace_id,
            require_failed=True,
        )
        if normalized["idempotency_key"] != request.idempotency_key:
            raise OutboxRecoveryNotFoundError("outbox item not found")
        if normalized["state_version"] != request.expected_state_version:
            raise OutboxRecoveryConflictError("outbox generation is stale")

        terminal_present = _lookup_terminal(
            self.terminal_lookup,
            idea_id=request.idea_id,
            workspace_id=request.workspace_id,
            fingerprint=request.idempotency_key,
        )
        try:
            if terminal_present:
                transitioned = self.repository.reconcile_failed_outbox(
                    request.idempotency_key,
                    MEMORY_EXTRACTION_KIND,
                    request.expected_state_version,
                )
                _payload, after = _validate_row(
                    transitioned,
                    idea_id=request.idea_id,
                    workspace_id=request.workspace_id,
                    require_failed=False,
                )
                if after["status"] != OUTBOX_COMPLETED:
                    raise OutboxRecoveryConflictError("reconcile returned an invalid outbox state")
                return OutboxRecoveryResult(
                    outcome="reconciled",
                    status="completed",
                    item=self._safe_item(after, terminal_present=True),
                )

            transitioned = self.repository.redrive_failed_outbox(
                request.idempotency_key,
                MEMORY_EXTRACTION_KIND,
                request.expected_state_version,
            )
            _payload, after = _validate_row(
                transitioned,
                idea_id=request.idea_id,
                workspace_id=request.workspace_id,
                require_failed=False,
            )
            if after["status"] != OUTBOX_PENDING or after["attempt_count"] != 0:
                raise OutboxRecoveryConflictError("redrive returned an invalid outbox state")
            return OutboxRecoveryResult(
                outcome="redriven",
                status="pending",
                item=self._safe_item(after, terminal_present=False),
            )
        except OutboxRecoveryConflictError:
            raise
        except Exception as exc:
            # Storage's stable failed-recovery conflict is translated at the
            # application boundary.  All other errors intentionally propagate:
            # a terminal/repository outage must never become a false redrive.
            if (
                isinstance(exc, ValueError)
                and exc.__class__.__name__ == "OutboxRecoveryConflictError"
                and exc.__class__.__module__.endswith("storage.store")
            ):
                raise OutboxRecoveryConflictError("outbox generation is stale") from exc
            raise

    @staticmethod
    def _safe_item(normalized: Mapping[str, object], *, terminal_present: bool) -> OutboxSafeItem:
        return OutboxSafeItem(
            outbox_id=str(normalized["outbox_id"]),
            idempotency_key=str(normalized["idempotency_key"]),
            fingerprint=str(normalized["idempotency_key"]),
            idea_id=str(normalized["payload_idea_id"])
            if "payload_idea_id" in normalized
            else "",
            workspace_id=str(normalized["payload_workspace_id"])
            if "payload_workspace_id" in normalized
            else "",
            attempt_count=cast(int, normalized["attempt_count"]),
            max_attempts=cast(int, normalized["max_attempts"]),
            created_at=cast(int, normalized["created_at"]),
            updated_at=cast(int, normalized["updated_at"]),
            state_version=cast(int, normalized["state_version"]),
            error_code=_stable_error_code(normalized.get("last_error")),
            terminal_present=bool(terminal_present),
        )


__all__ = [
    "FailedOutboxListRequest",
    "MEMORY_EXTRACTION_KIND",
    "OutboxRecovery",
    "OutboxRecoveryConflict",
    "OutboxRecoveryConflictError",
    "OutboxRecoveryError",
    "OutboxRecoveryNotFound",
    "OutboxRecoveryNotFoundError",
    "OutboxRecoveryRepository",
    "OutboxRecoveryRequest",
    "OutboxRecoveryResult",
    "OutboxRecoveryService",
    "OutboxRecoveryValidation",
    "OutboxRecoveryValidationError",
    "OutboxSafeItem",
    "TerminalEventLookup",
    "TerminalLookupCallable",
    "TERMINAL_EXTRACTION_EVENTS",
]


# Public shorthand; unlike the error aliases this is intentionally named after
# the use case, which helps callers that prefer ``OutboxRecovery(...)``.
OutboxRecovery = OutboxRecoveryService
