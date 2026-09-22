from __future__ import annotations

from pathlib import Path

from stata_research_agent.application.artifact_service import ArtifactDataService
from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.data_intake import PrepareWorkspaceDataCommand
from stata_research_agent.application.data_intake_service import WorkspaceDataIntakeService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.artifact_data import (
    VerificationPurpose,
    VerificationVerdict,
)
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.filesystem_data_catalog import (
    FilesystemWorkspaceDataCatalog,
)
from stata_research_agent.persistence.artifact_data_store import (
    SqliteArtifactDataRepository,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def test_workspace_dta_becomes_verified_immutable_data_version(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_data_intake")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    source = database.root / "inputs" / "study.dta"
    source.parent.mkdir()
    source.write_bytes(b"fixture-dta-bytes")
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_data_workspace"), workspace_id)
        )
        turn = control.submit_message(
            SubmitMessageCommand(CommandId("cmd_data_turn"), "Use study.dta")
        )
        intake = WorkspaceDataIntakeService(
            FilesystemWorkspaceDataCatalog(database.root),
            ArtifactDataService(
                SqliteArtifactDataRepository(connection),
                FilesystemManagedArtifactStore(database.root),
                identities,
            ),
            identities,
        )

        prepared = intake.prepare_for_formal_run(
            PrepareWorkspaceDataCommand(
                CommandId("cmd_data_prepare"), turn.turn_id, "inputs/study.dta"
            )
        )

        assert prepared.candidate.relative_path == "inputs/study.dta"
        assert prepared.captured.size_bytes == len(b"fixture-dta-bytes")
        assert prepared.formal_input_verification.purpose is VerificationPurpose.FORMAL_RUN_INPUT
        assert prepared.formal_input_verification.verdict is VerificationVerdict.VERIFIED
        managed = database.root / prepared.captured.managed_handle
        assert managed.read_bytes() == b"fixture-dta-bytes"
        source.write_bytes(b"user keeps editing the working copy")
        assert managed.read_bytes() == b"fixture-dta-bytes"
    finally:
        connection.close()
