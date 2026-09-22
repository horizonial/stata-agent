"""Typed, non-authoritative operational diagnostics contracts."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


class DiagnosticFieldClass(StrEnum):
    SAFE_METADATA = "safe_metadata"
    LOCAL_IDENTIFIER = "local_identifier"
    PERSONAL_PATH = "personal_path"
    RESEARCH_CONTENT = "research_content"
    LICENSE_IDENTITY = "license_identity"
    SECRET = "secret"


@dataclass(frozen=True, slots=True)
class DiagnosticEventDefinition:
    event_name: str
    event_schema_version: str
    fields: Mapping[str, DiagnosticFieldClass]


@dataclass(frozen=True, slots=True)
class DiagnosticEventCandidate:
    event_name: str
    severity_number: int
    severity_text: str
    safe_code: str
    component: str
    process_role: str
    safe_attributes: Mapping[str, Any]
    trace_id: str | None = None
    span_id: str | None = None
    workspace_ref: str | None = None
    turn_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    duration_ms: float | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    diagnostic_event_id: str
    sequence: int
    diagnostic_schema_version: str
    event_schema_version: str
    timestamp_utc: str
    observed_timestamp_utc: str
    release_id: str
    build_id: str
    instance_id: str
    event_name: str
    severity_number: int
    severity_text: str
    safe_code: str
    component: str
    process_role: str
    safe_attributes: Mapping[str, Any]
    trace_id: str | None
    span_id: str | None
    workspace_ref: str | None
    turn_id: str | None
    operation_id: str | None
    attempt_id: str | None
    duration_ms: float | None

    def to_payload(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "diagnostic_event_id": self.diagnostic_event_id,
                "sequence": self.sequence,
                "diagnostic_schema_version": self.diagnostic_schema_version,
                "event_schema_version": self.event_schema_version,
                "timestamp_utc": self.timestamp_utc,
                "observed_timestamp_utc": self.observed_timestamp_utc,
                "release_id": self.release_id,
                "build_id": self.build_id,
                "instance_id": self.instance_id,
                "event_name": self.event_name,
                "severity_number": self.severity_number,
                "severity_text": self.severity_text,
                "safe_code": self.safe_code,
                "component": self.component,
                "process_role": self.process_role,
                "safe_attributes": dict(self.safe_attributes),
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "workspace_ref": self.workspace_ref,
                "turn_id": self.turn_id,
                "operation_id": self.operation_id,
                "attempt_id": self.attempt_id,
                "duration_ms": self.duration_ms,
            }.items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class DiagnosticHealth:
    last_successful_write_at: str | None
    last_failure_code: str | None
    degraded_since: str | None
    sink_generation: int


class DiagnosticSchemaRegistry:
    """Reject unknown events/fields and all unsafe field classifications."""

    _FORBIDDEN = {
        DiagnosticFieldClass.RESEARCH_CONTENT,
        DiagnosticFieldClass.LICENSE_IDENTITY,
        DiagnosticFieldClass.SECRET,
    }

    def __init__(self, definitions: tuple[DiagnosticEventDefinition, ...]) -> None:
        self._definitions = {item.event_name: item for item in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("Diagnostic event names must be unique")

    def validate(self, candidate: DiagnosticEventCandidate) -> DiagnosticEventDefinition:
        definition = self._definitions.get(candidate.event_name)
        if definition is None:
            raise ValueError("unregistered Diagnostic event")
        if set(candidate.safe_attributes) != set(definition.fields):
            raise ValueError("Diagnostic event fields do not match the registered schema")
        if any(value in self._FORBIDDEN for value in definition.fields.values()):
            raise ValueError("unsafe Diagnostic field classification cannot enter the sink")
        for key, value in candidate.safe_attributes.items():
            if definition.fields[key] is DiagnosticFieldClass.PERSONAL_PATH:
                if not isinstance(value, str) or not value.startswith(
                    ("WORKSPACE_ROOT/", "APP_CONTROL/", "APP_RUNTIME/")
                ):
                    raise ValueError("Diagnostic path must use a logical locator")
            self._validate_safe_value(value)
        if not candidate.safe_code or any(character.isspace() for character in candidate.safe_code):
            raise ValueError("Diagnostic safe_code must be a stable token")
        return definition

    @staticmethod
    def _validate_safe_value(value: Any) -> None:
        if value is None or isinstance(value, (bool, int, float)):
            return
        if isinstance(value, str):
            if "\n" in value or "\r" in value or len(value) > 512:
                raise ValueError("Diagnostic string is not bounded safe metadata")
            return
        if isinstance(value, list):
            for item in value:
                DiagnosticSchemaRegistry._validate_safe_value(item)
            return
        raise ValueError("Diagnostic field must use a registered scalar/list shape")


def default_diagnostic_registry() -> DiagnosticSchemaRegistry:
    safe = DiagnosticFieldClass.SAFE_METADATA
    local = DiagnosticFieldClass.LOCAL_IDENTIFIER
    return DiagnosticSchemaRegistry(
        (
            DiagnosticEventDefinition(
                "process.lifecycle",
                "1.0",
                {"state_code": safe, "generation": safe},
            ),
            DiagnosticEventDefinition(
                "operation.lifecycle",
                "1.0",
                {"state_code": safe, "operation_kind": safe},
            ),
            DiagnosticEventDefinition(
                "provider.transport",
                "1.0",
                {"phase_code": safe, "attempt_ordinal": safe},
            ),
            DiagnosticEventDefinition(
                "exception.safe",
                "1.0",
                {"exception_type": safe, "module": safe, "frame_names": safe},
            ),
            DiagnosticEventDefinition(
                "migration.lifecycle",
                "1.0",
                {"state_code": safe, "schema_version": safe},
            ),
            DiagnosticEventDefinition(
                "recovery.lifecycle",
                "1.0",
                {"classification": safe, "report_ref": local},
            ),
        )
    )


class SafeDiagnosticRing:
    def __init__(self, max_events: int = 200) -> None:
        if max_events < 1:
            raise ValueError("Diagnostic ring size must be positive")
        self._events: deque[DiagnosticEvent] = deque(maxlen=max_events)

    def append(self, event: DiagnosticEvent) -> None:
        self._events.append(event)

    def snapshot(self) -> tuple[DiagnosticEvent, ...]:
        return tuple(self._events)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class DiagnosticDebugMode:
    """In-memory density switch; it never changes the registered field boundary."""

    MAX_DURATION = timedelta(minutes=30)

    def __init__(self) -> None:
        self._expires_at: datetime | None = None

    def enable(self, *, now: datetime | None = None) -> datetime:
        observed = now or datetime.now(UTC)
        self._expires_at = observed + self.MAX_DURATION
        return self._expires_at

    def disable(self) -> None:
        self._expires_at = None

    def is_active(self, *, now: datetime | None = None) -> bool:
        if self._expires_at is None:
            return False
        observed = now or datetime.now(UTC)
        if observed >= self._expires_at:
            self._expires_at = None
            return False
        return True
