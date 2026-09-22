"""Ports for recoverable Project Memory background maintenance."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.memory_curator import (
    MemoryExtractionOutput,
    MemoryMaintenanceJob,
    MemoryMaintenanceOutcome,
    MemoryMaintenanceWindow,
)
from stata_research_agent.application.model_gateway import ProviderResponse
from stata_research_agent.domain.identifiers import (
    CommandId,
    MemoryMaintenanceJobId,
    MemoryProviderAttemptId,
    WorkspaceId,
)


class MemoryMaintenanceRepository(Protocol):
    def recover_interrupted(self) -> int: ...

    def discover_window(self) -> MemoryMaintenanceWindow | None: ...

    def enqueue(
        self,
        *,
        command_id: CommandId,
        job_id: MemoryMaintenanceJobId,
        window: MemoryMaintenanceWindow,
        curator_revision: str,
    ) -> MemoryMaintenanceJob: ...

    def prepare_attempt(
        self,
        *,
        command_id: CommandId,
        job_id: MemoryMaintenanceJobId,
        attempt_id: MemoryProviderAttemptId,
        provider_profile_id: str,
        credential_version_id: str,
        endpoint_origin: str,
        model_name: str,
        request_json: str,
    ) -> int: ...

    def mark_dispatch_started(
        self,
        *,
        command_id: CommandId,
        job_id: MemoryMaintenanceJobId,
        attempt_id: MemoryProviderAttemptId,
    ) -> None: ...

    def finalize(
        self,
        *,
        command_id: CommandId,
        job: MemoryMaintenanceJob,
        attempt_id: MemoryProviderAttemptId,
        response: ProviderResponse,
        extraction: MemoryExtractionOutput,
    ) -> MemoryMaintenanceOutcome: ...

    def fail(
        self,
        *,
        command_id: CommandId,
        job_id: MemoryMaintenanceJobId,
        attempt_id: MemoryProviderAttemptId,
        error_code: str,
        delivery_unknown: bool,
    ) -> None: ...

    def rebuild_summaries(self) -> None: ...


class WorkspaceMemoryMaintenanceRunner(Protocol):
    async def run_once(self, workspace_id: WorkspaceId) -> MemoryMaintenanceOutcome | None: ...
