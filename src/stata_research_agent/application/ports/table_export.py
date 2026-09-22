"""Persistence port for the registered esttab export profile."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.stata_operation import StataOperationOutcome
from stata_research_agent.application.table_export import (
    CompletedTableArtifact,
    EsttabTableOutcome,
    ExportEsttabTableCommand,
    PreparedTableExport,
    RegisterEsttabProfileCommand,
    TableVerificationIdentity,
)
from stata_research_agent.domain.identifiers import CommandId, TableExportInputManifestId
from stata_research_agent.domain.table_export import VerifiedEsttabTable


class TableExportRepository(Protocol):
    def register_profile(self, command: RegisterEsttabProfileCommand) -> None: ...

    def find_reusable_export(
        self, command: ExportEsttabTableCommand
    ) -> EsttabTableOutcome | None: ...

    def prepare_export(
        self,
        command: ExportEsttabTableCommand,
        input_manifest_id: TableExportInputManifestId,
        stored_estimate_alias: str,
        output_relative_path: str,
    ) -> PreparedTableExport: ...

    def load_completed_artifact(
        self,
        prepared: PreparedTableExport,
        operation: StataOperationOutcome,
    ) -> CompletedTableArtifact: ...

    def commit_verification(
        self,
        command: ExportEsttabTableCommand,
        prepared: PreparedTableExport,
        operation: StataOperationOutcome,
        completed: CompletedTableArtifact,
        verified: VerifiedEsttabTable,
        identities: TableVerificationIdentity,
        finalization_command_id: CommandId,
    ) -> EsttabTableOutcome: ...
