"""Bounded Windows/Stata fixture integration for the M1 Artifact layer."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from stata_research_agent.application.artifact_data import CaptureDataVersionCommand
from stata_research_agent.application.artifact_service import ArtifactDataService
from stata_research_agent.application.control import CreateWorkspaceCommand, SubmitMessageCommand
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.artifact_data import DataVersionKind
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.persistence.artifact_data_store import SqliteArtifactDataRepository
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator

STATA_18_AUTO = Path(r"C:\Program Files\Stata18\auto.dta")


def test_real_stata18_auto_dta_is_captured_byte_for_byte(tmp_path: Path) -> None:
    if not STATA_18_AUTO.is_file():
        pytest.skip("certified local Stata 18 auto.dta fixture is unavailable")

    workspace_id = WorkspaceId("ws_real_auto_capture")
    root = tmp_path / "workspace"
    database = WorkspaceDatabase(root, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_real_init"), workspace_id))
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_real_turn"), "Capture Stata auto.dta")
    )
    service = ArtifactDataService(
        SqliteArtifactDataRepository(connection),
        FilesystemManagedArtifactStore(root),
        identities,
    )
    try:
        captured = service.capture_data_version(
            CaptureDataVersionCommand(
                CommandId("cmd_real_auto_capture"),
                STATA_18_AUTO,
                DataVersionKind.EXTERNAL_IMPORT,
                turn.turn_id,
            )
        )
        source_bytes = STATA_18_AUTO.read_bytes()
        managed_bytes = (root / Path(captured.managed_handle)).read_bytes()
        assert managed_bytes == source_bytes
        assert captured.size_bytes == len(source_bytes)
        assert captured.content_sha256 == hashlib.sha256(source_bytes).hexdigest()
        assert (
            connection.execute(
                """
            SELECT source_locator FROM file_observations fo
            JOIN artifact_file_observation_sources s USING(file_observation_id)
            WHERE s.artifact_id = ?
            """,
                (captured.artifact_id.value,),
            ).fetchone()[0]
            == "external-file://auto.dta"
        )
    finally:
        connection.close()
