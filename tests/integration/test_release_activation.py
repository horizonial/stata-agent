"""D-230 persisted activation, irreversible guard, and rollback proof."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.release_activation import (
    ActivationState,
    IrreversibleCapability,
    ReleaseActivationError,
    RollbackEligibility,
    VerifiedRelease,
)
from stata_research_agent.application.release_activation_service import (
    ReleaseActivationService,
    ReleaseIrreversibilityGuard,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.release_control_store import (
    SqliteReleaseControlStore,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def release(root: Path, release_id: str, digest_char: str) -> VerifiedRelease:
    directory = root / release_id
    directory.mkdir(parents=True)
    return VerifiedRelease(
        release_id,
        "0.1.0",
        f"build-{release_id}",
        "publisher-key-v1",
        digest_char * 64,
        str(directory.resolve()),
        "agent.exe",
        1,
        1,
        1,
        26,
        26,
        26,
        "1.0.0",
    )


class KnownReleaseVerifier:
    def __init__(self, releases: tuple[VerifiedRelease, ...]) -> None:
        self._by_directory = {item.version_directory: item for item in releases}

    def verify(self, version_directory: str) -> VerifiedRelease:
        try:
            return self._by_directory[version_directory]
        except KeyError as error:
            raise ReleaseActivationError("release is no longer verifiable") from error


class PassingProbe:
    def run(self, release: VerifiedRelease) -> tuple[bool, tuple[str, ...]]:
        del release
        return True, ("bundle_ok", "imports_ok", "loopback_ok", "control_read_only_ok")


def activate_first(service: ReleaseActivationService, item: VerifiedRelease) -> None:
    service.stage(item.version_directory)
    started = service.start(item.release_id)
    assert started.attempt.state == ActivationState.STARTING_SAFE
    service.runtime_ready(started.attempt.activation_attempt_id)
    service.complete(started.attempt.activation_attempt_id)


def test_verified_reversible_failure_rolls_back_to_exact_previous_release(
    tmp_path: Path,
) -> None:
    one = release(tmp_path / "versions", "release-1", "1")
    two = release(tmp_path / "versions", "release-2", "2")
    verifier = KnownReleaseVerifier((one, two))
    store = SqliteReleaseControlStore(tmp_path / "control" / "release.sqlite3")
    service = ReleaseActivationService(store, verifier, PassingProbe())
    activate_first(service, one)

    service.stage(two.version_directory)
    started = service.start(two.release_id)
    failed = service.fail(started.attempt.activation_attempt_id, "candidate_crashed")
    assert failed.state == ActivationState.FAILED
    assert failed.rollback_eligibility == RollbackEligibility.VERIFIED_REVERSIBLE
    report = service.rollback(failed.activation_attempt_id)
    assert report.outcome == "ROLLED_BACK"
    assert report.active_release_id == one.release_id
    assert store.active_release_id() == one.release_id


def test_any_guarded_side_effect_persists_irreversibility_and_forbids_rollback(
    tmp_path: Path,
) -> None:
    one = release(tmp_path / "versions", "release-1", "1")
    two = release(tmp_path / "versions", "release-2", "2")
    verifier = KnownReleaseVerifier((one, two))
    path = tmp_path / "control" / "release.sqlite3"
    store = SqliteReleaseControlStore(path)
    service = ReleaseActivationService(store, verifier, PassingProbe())
    activate_first(service, one)
    service.stage(two.version_directory)
    started = service.start(two.release_id)

    ReleaseIrreversibilityGuard(store).before(
        IrreversibleCapability.PROVIDER_DISPATCH,
        reference="provider_attempt_1",
    )
    observed = SqliteReleaseControlStore(path).load_attempt(started.attempt.activation_attempt_id)
    assert observed.state == ActivationState.IRREVERSIBLE
    assert observed.rollback_eligibility == RollbackEligibility.FORBIDDEN_IRREVERSIBLE
    assert observed.irreversible_reasons == ("provider_dispatch:provider_attempt_1",)

    failed = service.fail(observed.activation_attempt_id, "candidate_crashed")
    with pytest.raises(ReleaseActivationError, match="not provably reversible"):
        service.rollback(failed.activation_attempt_id)
    assert store.active_release_id() == two.release_id


def test_previous_release_identity_change_makes_rollback_fail_closed(tmp_path: Path) -> None:
    one = release(tmp_path / "versions", "release-1", "1")
    two = release(tmp_path / "versions", "release-2", "2")
    verifier = KnownReleaseVerifier((one, two))
    store = SqliteReleaseControlStore(tmp_path / "control" / "release.sqlite3")
    service = ReleaseActivationService(store, verifier, PassingProbe())
    activate_first(service, one)
    service.stage(two.version_directory)
    started = service.start(two.release_id)
    failed = service.fail(started.attempt.activation_attempt_id, "candidate_crashed")

    verifier._by_directory[one.version_directory] = replace(one, manifest_sha256="f" * 64)
    with pytest.raises(ReleaseActivationError, match="not provably reversible"):
        service.rollback(failed.activation_attempt_id)


def test_workspace_authoritative_write_uses_the_same_activation_guard(tmp_path: Path) -> None:
    one = release(tmp_path / "versions", "release-1", "1")
    two = release(tmp_path / "versions", "release-2", "2")
    verifier = KnownReleaseVerifier((one, two))
    store = SqliteReleaseControlStore(tmp_path / "control" / "release.sqlite3")
    service = ReleaseActivationService(store, verifier, PassingProbe())
    activate_first(service, one)
    service.stage(two.version_directory)
    started = service.start(two.release_id)

    workspace_id = WorkspaceId("ws_activation_guard")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    try:
        workspace = WorkspaceControlService(
            SqliteControlStore(connection),
            UuidIdentityGenerator(),
            ReleaseIrreversibilityGuard(store),
        )
        workspace.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_activation_guard"), workspace_id)
        )
    finally:
        connection.close()

    observed = store.load_attempt(started.attempt.activation_attempt_id)
    assert observed.state == ActivationState.IRREVERSIBLE
    assert observed.irreversible_reasons == ("authoritative_mutation:cmd_activation_guard",)


def test_read_only_activation_finishes_before_normal_workspace_recovery_writes(
    tmp_path: Path,
) -> None:
    one = release(tmp_path / "versions", "release-1", "1")
    two = release(tmp_path / "versions", "release-2", "2")
    verifier = KnownReleaseVerifier((one, two))
    store = SqliteReleaseControlStore(tmp_path / "control" / "release.sqlite3")
    service = ReleaseActivationService(store, verifier, PassingProbe())
    activate_first(service, one)

    workspace_id = WorkspaceId("ws_activation_order")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    workspace = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    workspace.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_activation_order_init"), workspace_id)
    )
    revision_before = int(
        connection.execute("SELECT MAX(workspace_revision) FROM workspace_commits").fetchone()[0]
    )

    service.stage(two.version_directory)
    started = service.start(two.release_id)
    assert (
        int(
            connection.execute("SELECT MAX(workspace_revision) FROM workspace_commits").fetchone()[
                0
            ]
        )
        == revision_before
    )
    service.runtime_ready(started.attempt.activation_attempt_id)
    completed = service.complete(started.attempt.activation_attempt_id)

    guarded_workspace = WorkspaceControlService(
        SqliteControlStore(connection),
        UuidIdentityGenerator(),
        ReleaseIrreversibilityGuard(store),
    )
    guarded_workspace.submit_message(
        SubmitMessageCommand(CommandId("cmd_post_activation_recovery"), "continue recovery")
    )
    connection.close()
    assert store.load_attempt(completed.activation_attempt_id).state == ActivationState.SUCCEEDED
