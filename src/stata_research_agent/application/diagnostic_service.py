"""Fail-safe orchestration for typed, non-authoritative diagnostics."""

from __future__ import annotations

import secrets
import traceback
from dataclasses import dataclass
from typing import Any

from .diagnostics import (
    DiagnosticEvent,
    DiagnosticEventCandidate,
    DiagnosticSchemaRegistry,
    SafeDiagnosticRing,
    utc_now,
)
from .ports.diagnostics import DiagnosticSink
from .sensitive_output import SensitiveOutputGate, SensitiveOutputGateUnavailable


@dataclass(frozen=True, slots=True)
class CrashCapsuleOutcome:
    capsule_id: str
    path_name: str
    event_count: int


class DiagnosticService:
    def __init__(
        self,
        sink: DiagnosticSink,
        registry: DiagnosticSchemaRegistry,
        gate: SensitiveOutputGate,
        *,
        release_id: str,
        build_id: str,
        instance_id: str,
        ring_size: int = 200,
    ) -> None:
        self._sink = sink
        self._registry = registry
        self._gate = gate
        self._release_id = release_id
        self._build_id = build_id
        self._instance_id = instance_id
        self._ring = SafeDiagnosticRing(ring_size)
        self._degraded = False

    @property
    def degraded(self) -> bool:
        return self._degraded

    def record(
        self,
        candidate: DiagnosticEventCandidate,
        *,
        protected_values: tuple[str, ...] = (),
    ) -> DiagnosticEvent | None:
        definition = self._registry.validate(candidate)
        try:
            inspected = self._gate.inspect_json(
                "diagnostic.event",
                {
                    "safe_code": candidate.safe_code,
                    "component": candidate.component,
                    "process_role": candidate.process_role,
                    "safe_attributes": dict(candidate.safe_attributes),
                },
                protected_values=protected_values,
            )
        except SensitiveOutputGateUnavailable:
            self._degraded = True
            return None
        if inspected.verdict != "safe":
            return None
        try:
            event = self._sink.append(
                candidate,
                definition,
                release_id=self._release_id,
                build_id=self._build_id,
                instance_id=self._instance_id,
            )
        except RuntimeError:
            self._degraded = True
            return None
        self._ring.append(event)
        self._degraded = False
        return event

    def record_exception(
        self,
        error: BaseException,
        *,
        component: str,
        process_role: str,
        safe_code: str,
    ) -> DiagnosticEvent | None:
        frames = traceback.extract_tb(error.__traceback__)
        safe_frames = [frame.name for frame in frames[-8:]]
        return self.record(
            DiagnosticEventCandidate(
                "exception.safe",
                17,
                "ERROR",
                safe_code,
                component,
                process_role,
                {
                    "exception_type": type(error).__name__,
                    "module": type(error).__module__,
                    "frame_names": safe_frames,
                },
            )
        )

    def write_crash_capsule(
        self,
        *,
        process_role: str,
        exit_classification: str,
        last_state_codes: tuple[str, ...],
    ) -> CrashCapsuleOutcome | None:
        events = self._ring.snapshot()
        payload: dict[str, Any] = {
            "schema_version": "safe-crash-capsule/1.0",
            "redaction_policy_version": "sensitive-output-v1",
            "created_at": utc_now(),
            "release_id": self._release_id,
            "build_id": self._build_id,
            "instance_id": self._instance_id,
            "process_role": process_role,
            "exit_classification": exit_classification,
            "last_state_codes": list(last_state_codes),
            "events": [event.to_payload() for event in events],
        }
        try:
            inspected = self._gate.inspect_json("diagnostic.crash_capsule", payload)
        except SensitiveOutputGateUnavailable:
            self._degraded = True
            return None
        if inspected.verdict != "safe":
            return None
        capsule_id = secrets.token_hex(16)
        try:
            path = self._sink.write_crash_capsule(
                capsule_id=capsule_id,
                payload=inspected.safe_value,
            )
        except (OSError, RuntimeError, TypeError):
            self._degraded = True
            return None
        return CrashCapsuleOutcome(capsule_id, path.name, len(events))

    def clear(self) -> dict[str, int]:
        return dict(self._sink.clear())
