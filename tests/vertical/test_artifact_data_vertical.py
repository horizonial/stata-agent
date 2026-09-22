"""M1-01 Artifact/Data capture vertical and trust-boundary failures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stata_research_agent.application.artifact_data import (
    AdoptPathDataCommand,
    CaptureDataVersionCommand,
    VerifyArtifactCommand,
)
from stata_research_agent.application.artifact_service import (
    ArtifactDataService,
    CaptureCrashPoint,
)
from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.managed_store import (
    FilesystemManagedArtifactStore,
    InsufficientArtifactCapacityError,
    UnstableSourceError,
)
from stata_research_agent.domain.artifact_data import (
    ArtifactAvailability,
    DataVersionKind,
    VerificationPurpose,
    VerificationVerdict,
)
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.persistence.artifact_data_store import (
    SqliteArtifactDataRepository,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.sandbox_input_resolver import (
    SqliteSandboxArtifactPathResolver,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def initialized_service(tmp_path: Path, *, after_copy_hook=None):
    workspace_root = tmp_path / "workspace"
    workspace_id = WorkspaceId("ws_artifact_vertical")
    database = WorkspaceDatabase(workspace_root, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_artifact_initialize"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_artifact_turn"), "Capture the research data")
    )
    service = ArtifactDataService(
        SqliteArtifactDataRepository(connection),
        FilesystemManagedArtifactStore(workspace_root, after_copy_hook=after_copy_hook),
        identities,
    )
    return workspace_root, connection, service, initialized, turn


def capture_command(command_id: str, source: Path, turn, kind=DataVersionKind.EXTERNAL_IMPORT):
    return CaptureDataVersionCommand(
        command_id=CommandId(command_id),
        source_path=source,
        data_version_kind=kind,
        created_by_turn_id=turn.turn_id,
    )


@pytest.mark.parametrize(
    "kind",
    [
        DataVersionKind.EXTERNAL_IMPORT,
        DataVersionKind.WORKING_CAPTURE,
        DataVersionKind.INTERNAL_CHECKPOINT,
    ],
)
def test_capture_creates_immutable_payload_receipt_and_data_version_without_adoption(
    tmp_path: Path, kind: DataVersionKind
) -> None:
    workspace_root, connection, service, initialized, turn = initialized_service(tmp_path)
    source = workspace_root / f"auto-{kind.value}.dta"
    payload = b"Stata-like fixture\x00" + kind.value.encode("ascii")
    source.write_bytes(payload)
    try:
        captured = service.capture_data_version(
            capture_command(f"cmd_capture_{kind.value}", source, turn, kind)
        )
        managed = workspace_root / Path(captured.managed_handle)
        assert managed.read_bytes() == payload
        assert captured.size_bytes == len(payload)
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM data_versions").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT availability FROM artifact_states WHERE artifact_id = ?",
                (captured.artifact_id.value,),
            ).fetchone()[0]
            == "available"
        )
        receipt = connection.execute(
            """
            SELECT verification_purpose, verdict, artifact_identity_revision
            FROM artifact_verification_receipts WHERE verification_receipt_id = ?
            """,
            (captured.verification_receipt_id.value,),
        ).fetchone()
        assert tuple(receipt) == (
            "data_version_creation",
            "verified",
            captured.commit_revision.value,
        )
        assert connection.execute("SELECT count(*) FROM path_data_adoptions").fetchone()[0] == 0
        assert initialized.main_path_id.value
    finally:
        connection.close()


def test_capture_retry_returns_original_identity_without_second_payload(tmp_path: Path) -> None:
    workspace_root, connection, service, _, turn = initialized_service(tmp_path)
    source = workspace_root / "auto.dta"
    source.write_bytes(b"stable-data")
    command = capture_command("cmd_capture_retry", source, turn)
    try:
        first = service.capture_data_version(command)
        replay = service.capture_data_version(command)
        assert replay.replayed is True
        assert replay.artifact_id == first.artifact_id
        assert replay.data_version_id == first.data_version_id
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        managed_files = list((workspace_root / ".stata-agent" / "objects").rglob("*.payload"))
        assert len(managed_files) == 1
    finally:
        connection.close()


def test_sandbox_input_resolver_accepts_only_fresh_managed_artifact_identity(
    tmp_path: Path,
) -> None:
    workspace_root, connection, service, _, turn = initialized_service(tmp_path)
    source = workspace_root / "sandbox-input.dta"
    source.write_bytes(b"stable-sandbox-input")
    try:
        captured = service.capture_data_version(
            capture_command("cmd_capture_sandbox_input", source, turn)
        )
        resolver = SqliteSandboxArtifactPathResolver(connection, workspace_root)
        managed = resolver(captured.artifact_id.value)
        assert managed.read_bytes() == b"stable-sandbox-input"

        managed.chmod(0o666)
        managed.write_bytes(b"tampered-sandbox-input")
        with pytest.raises(ValueError, match="fresh identity verification"):
            resolver(captured.artifact_id.value)
    finally:
        connection.close()


def test_external_replacement_creates_new_identity_and_preserves_old_bytes(tmp_path: Path) -> None:
    workspace_root, connection, service, _, turn = initialized_service(tmp_path)
    source = workspace_root / "analysis.dta"
    source.write_bytes(b"version-one")
    try:
        first = service.capture_data_version(capture_command("cmd_capture_first", source, turn))
        first_payload = workspace_root / Path(first.managed_handle)
        source.write_bytes(b"version-two")
        second = service.capture_data_version(capture_command("cmd_capture_second", source, turn))
        assert first.artifact_id != second.artifact_id
        assert first.data_version_id != second.data_version_id
        assert first.content_sha256 != second.content_sha256
        assert first_payload.read_bytes() == b"version-one"
        assert (workspace_root / Path(second.managed_handle)).read_bytes() == b"version-two"
    finally:
        connection.close()


def test_verification_records_corrupt_then_missing_without_rewriting_history(
    tmp_path: Path,
) -> None:
    workspace_root, connection, service, _, turn = initialized_service(tmp_path)
    source = workspace_root / "analysis.dta"
    source.write_bytes(b"original")
    try:
        captured = service.capture_data_version(capture_command("cmd_capture_verify", source, turn))
        managed = workspace_root / Path(captured.managed_handle)
        managed.chmod(0o666)
        managed.write_bytes(b"tampered")
        corrupt = service.verify_artifact(
            VerifyArtifactCommand(
                CommandId("cmd_verify_corrupt"),
                captured.artifact_id,
                VerificationPurpose.FORMAL_RUN_INPUT,
            )
        )
        assert corrupt.verdict is VerificationVerdict.FAILED
        assert corrupt.availability is ArtifactAvailability.CORRUPT
        managed.unlink()
        missing = service.verify_artifact(
            VerifyArtifactCommand(
                CommandId("cmd_verify_missing"),
                captured.artifact_id,
                VerificationPurpose.RECOVERY,
            )
        )
        assert missing.availability is ArtifactAvailability.MISSING
        assert (
            connection.execute(
                "SELECT count(*) FROM artifact_state_history WHERE artifact_id = ?",
                (captured.artifact_id.value,),
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM data_versions WHERE data_version_id = ?",
                (captured.data_version_id.value,),
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_publish_interruption_leaves_no_authoritative_artifact(tmp_path: Path) -> None:
    workspace_root, connection, service, _, turn = initialized_service(tmp_path)
    source = workspace_root / "interrupted.dta"
    source.write_bytes(b"published but not finalized")

    def crash(point: CaptureCrashPoint) -> None:
        assert point is CaptureCrashPoint.AFTER_MANAGED_PUBLISH_BEFORE_FINALIZATION
        raise RuntimeError("simulated process loss")

    try:
        with pytest.raises(RuntimeError, match="simulated process loss"):
            service.capture_data_version(
                capture_command("cmd_capture_interrupted", source, turn),
                crash_injector=crash,
            )
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM data_versions").fetchone()[0] == 0
        assert len(list((workspace_root / ".stata-agent" / "objects").rglob("*.payload"))) == 1
    finally:
        connection.close()


@pytest.mark.parametrize("payload_mib", [100, 500, 1024])
def test_large_payload_capacity_policy_matches_verified_tq06_tiers(
    payload_mib: int,
) -> None:
    payload_bytes = payload_mib * 1024 * 1024
    assert FilesystemManagedArtifactStore.required_capacity_bytes(payload_bytes) == (
        3 * payload_bytes + 512 * 1024 * 1024
    )


def test_capacity_gate_rejects_before_staging_or_authoritative_commit(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_id = WorkspaceId("ws_artifact_capacity")
    database = WorkspaceDatabase(workspace_root, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_capacity_initialize"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_capacity_turn"), "Capture the research data")
    )
    source = workspace_root / "large.dta"
    source.write_bytes(b"capacity-fixture")
    required = FilesystemManagedArtifactStore.required_capacity_bytes(source.stat().st_size)
    store = FilesystemManagedArtifactStore(
        workspace_root,
        free_space_provider=lambda _path: required - 1,
    )
    service = ArtifactDataService(SqliteArtifactDataRepository(connection), store, identities)
    try:
        with pytest.raises(InsufficientArtifactCapacityError) as raised:
            service.capture_data_version(
                capture_command("cmd_capture_capacity_rejected", source, turn)
            )
        assert raised.value.required_bytes == required
        assert raised.value.available_bytes == required - 1
        assert not (workspace_root / ".stata-agent" / "staging").exists()
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM data_versions").fetchone()[0] == 0
    finally:
        connection.close()


def test_source_change_during_copy_fails_before_publish_and_finalization(tmp_path: Path) -> None:
    def modify_source(source: Path) -> None:
        source.write_bytes(b"changed-during-capture")

    workspace_root, connection, service, _, turn = initialized_service(
        tmp_path, after_copy_hook=modify_source
    )
    source = workspace_root / "unstable.dta"
    source.write_bytes(b"original")
    try:
        with pytest.raises(UnstableSourceError):
            service.capture_data_version(capture_command("cmd_capture_unstable", source, turn))
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert list((workspace_root / ".stata-agent" / "objects").rglob("*.payload")) == []
    finally:
        connection.close()


def test_data_checkpoint_requires_explicit_cas_adoption(tmp_path: Path) -> None:
    workspace_root, connection, service, initialized, turn = initialized_service(tmp_path)
    source = workspace_root / "checkpoint.dta"
    source.write_bytes(b"checkpoint-one")
    try:
        first = service.capture_data_version(
            capture_command(
                "cmd_checkpoint_one",
                source,
                turn,
                DataVersionKind.INTERNAL_CHECKPOINT,
            )
        )
        assert connection.execute("SELECT count(*) FROM path_data_adoptions").fetchone()[0] == 0
        adopted = service.adopt_path_data(
            AdoptPathDataCommand(
                CommandId("cmd_adopt_one"),
                initialized.main_path_id,
                "analysis.primary",
                first.data_version_id,
                expected_pointer_revision=0,
            )
        )
        assert adopted.pointer_revision == 1

        source.write_bytes(b"checkpoint-two")
        second = service.capture_data_version(
            capture_command(
                "cmd_checkpoint_two",
                source,
                turn,
                DataVersionKind.INTERNAL_CHECKPOINT,
            )
        )
        current = connection.execute(
            "SELECT target_data_version_id, pointer_revision FROM path_data_adoptions"
        ).fetchone()
        assert tuple(current) == (first.data_version_id.value, 1)
        with pytest.raises(ValueError, match="pointer revision mismatch"):
            service.adopt_path_data(
                AdoptPathDataCommand(
                    CommandId("cmd_adopt_stale"),
                    initialized.main_path_id,
                    "analysis.primary",
                    second.data_version_id,
                    expected_pointer_revision=0,
                )
            )
        moved = service.adopt_path_data(
            AdoptPathDataCommand(
                CommandId("cmd_adopt_two"),
                initialized.main_path_id,
                "analysis.primary",
                second.data_version_id,
                expected_pointer_revision=1,
            )
        )
        assert moved.pointer_revision == 2
        assert (
            connection.execute("SELECT count(*) FROM path_data_adoption_history").fetchone()[0] == 2
        )
    finally:
        connection.close()


def test_immutable_artifact_and_data_facts_reject_update(tmp_path: Path) -> None:
    workspace_root, connection, service, _, turn = initialized_service(tmp_path)
    source = workspace_root / "immutable.dta"
    source.write_bytes(b"immutable")
    try:
        captured = service.capture_data_version(
            capture_command("cmd_capture_immutable", source, turn)
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE artifacts SET size_bytes = size_bytes + 1 WHERE artifact_id = ?",
                (captured.artifact_id.value,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE data_versions SET schema_snapshot_json = '{\"x\":1}' "
                "WHERE data_version_id = ?",
                (captured.data_version_id.value,),
            )
    finally:
        connection.close()
