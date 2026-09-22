"""Application orchestration for Workspace data discovery and managed capture."""

from __future__ import annotations

from stata_research_agent.domain.artifact_data import (
    DataVersionKind,
    VerificationPurpose,
)
from stata_research_agent.domain.identifiers import CommandId

from .artifact_data import CaptureDataVersionCommand, VerifyArtifactCommand
from .artifact_service import ArtifactDataService
from .data_intake import (
    PreparedWorkspaceData,
    PrepareWorkspaceDataCommand,
    WorkspaceDataCandidate,
)
from .ports.data_intake import WorkspaceDataCatalog
from .ports.identity import IdentityGenerator


class WorkspaceDataIntakeService:
    def __init__(
        self,
        catalog: WorkspaceDataCatalog,
        artifacts: ArtifactDataService,
        identities: IdentityGenerator,
    ) -> None:
        self._catalog = catalog
        self._artifacts = artifacts
        self._identities = identities

    def discover(self) -> tuple[WorkspaceDataCandidate, ...]:
        return self._catalog.discover()

    def prepare_for_formal_run(self, command: PrepareWorkspaceDataCommand) -> PreparedWorkspaceData:
        source = self._catalog.resolve(command.relative_path)
        candidate = next(
            (
                item
                for item in self._catalog.discover()
                if item.relative_path == command.relative_path
            ),
            None,
        )
        if candidate is None:
            raise ValueError("Workspace Data candidate is no longer available")
        captured = self._artifacts.capture_data_version(
            CaptureDataVersionCommand(
                command.command_id,
                source,
                DataVersionKind.WORKING_CAPTURE,
                command.created_by_turn_id,
            )
        )
        verified = self._artifacts.verify_artifact(
            VerifyArtifactCommand(
                self._identities.new(CommandId),
                captured.artifact_id,
                VerificationPurpose.FORMAL_RUN_INPUT,
            )
        )
        return PreparedWorkspaceData(candidate, captured, verified)
