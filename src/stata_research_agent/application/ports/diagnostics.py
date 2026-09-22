"""Ports for non-authoritative diagnostics and safe Workspace projections."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from stata_research_agent.application.diagnostics import (
    DiagnosticEvent,
    DiagnosticEventCandidate,
    DiagnosticEventDefinition,
    DiagnosticHealth,
)


class DiagnosticSink(Protocol):
    def append(
        self,
        candidate: DiagnosticEventCandidate,
        definition: DiagnosticEventDefinition,
        *,
        release_id: str,
        build_id: str,
        instance_id: str,
    ) -> DiagnosticEvent: ...

    def snapshot_end(self) -> int: ...

    def read_through(self, sequence: int) -> tuple[DiagnosticEvent, ...]: ...

    def health(self) -> DiagnosticHealth: ...

    def enforce_retention(self) -> Mapping[str, int]: ...

    def clear(self) -> Mapping[str, int]: ...

    def write_crash_capsule(self, *, capsule_id: str, payload: Mapping[str, Any]) -> Path: ...

    def read_crash_capsules(self) -> tuple[Mapping[str, Any], ...]: ...


class DiagnosticWorkspaceProjection(Protocol):
    def current_revision(self) -> int: ...

    def safe_projection(
        self,
        *,
        requested_revision: int,
        turn_id: str | None,
        operation_id: str | None,
    ) -> Mapping[str, Any]: ...
