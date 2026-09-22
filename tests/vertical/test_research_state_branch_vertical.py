"""M3-01 vertical: Plan Revision, Path branching, and independent adoption."""

from __future__ import annotations

from pathlib import Path

import pytest

from stata_research_agent.application.artifact_data import (
    AdoptPathDataCommand,
    CaptureDataVersionCommand,
)
from stata_research_agent.application.artifact_service import ArtifactDataService
from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.research_state import (
    AdoptPlanRevisionCommand,
    AdoptResearchBundleCommand,
    CreatePlanRevisionCommand,
    CreateResearchPathBranchCommand,
    DataSlotAdoptionTarget,
    PlanDependencyCandidate,
    PlanNodeCandidate,
)
from stata_research_agent.application.research_state_service import ResearchStateService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.artifact_data import DataVersionKind
from stata_research_agent.domain.identifiers import CommandId, PlanNodeId, WorkspaceId
from stata_research_agent.persistence.artifact_data_store import (
    SqliteArtifactDataRepository,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.research_state_store import (
    SqliteResearchStateRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def test_branch_copies_current_adoptions_then_diverges_without_rewriting_parent(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_id = WorkspaceId("ws_research_branch")
    database = WorkspaceDatabase(workspace_root, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_branch_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_branch_turn"),
            "Build a baseline plan and preserve an alternative variable construction.",
        )
    )
    state = ResearchStateService(SqliteResearchStateRepository(connection), identities)
    artifacts = ArtifactDataService(
        SqliteArtifactDataRepository(connection),
        FilesystemManagedArtifactStore(workspace_root),
        identities,
    )
    source = workspace_root / "source.dta"
    source.write_bytes(b"version-one")
    try:
        data_v1 = artifacts.capture_data_version(
            CaptureDataVersionCommand(
                CommandId("cmd_branch_capture_v1"),
                source,
                DataVersionKind.EXTERNAL_IMPORT,
                turn.turn_id,
            )
        )
        data_adoption = artifacts.adopt_path_data(
            AdoptPathDataCommand(
                command_id=CommandId("cmd_branch_adopt_data_v1"),
                research_path_id=initialized.main_path_id,
                canonical_slot_key="analysis.primary",
                data_version_id=data_v1.data_version_id,
                expected_pointer_revision=0,
                display_name="Primary analysis data",
            )
        )
        plan_v1 = state.create_plan_revision(
            CreatePlanRevisionCommand(
                CommandId("cmd_branch_plan_v1"),
                turn.turn_id,
                "study.primary",
                "Baseline price model",
                {"outcome": "price", "covariates": ["mpg", "weight"]},
                (
                    PlanNodeCandidate(
                        "variables.construct",
                        "variable_construction",
                        {"variables": ["price", "mpg", "weight"]},
                    ),
                    PlanNodeCandidate(
                        "model.baseline",
                        "estimation",
                        {"command": "regress price mpg weight"},
                    ),
                ),
                (PlanDependencyCandidate("variables.construct", "model.baseline", "data"),),
            )
        )
        main_plan = state.adopt_plan_revision(
            AdoptPlanRevisionCommand(
                CommandId("cmd_branch_adopt_plan_v1"),
                initialized.main_path_id,
                plan_v1.plan_revision_id,
                turn.turn_id,
                0,
            )
        )
        branch_basis = connection.execute(
            "SELECT MAX(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        branch = state.create_path_branch(
            CreateResearchPathBranchCommand(
                CommandId("cmd_create_variable_branch"),
                initialized.main_path_id,
                turn.turn_id,
                "alternative-variable",
                "Alternative variable construction",
                "Explore a different construction from the confirmed variable node.",
                branch_basis,
                branch_basis,
            )
        )
        assert branch.copied_data_adoptions == 1
        assert branch.copied_plan_adoption is True
        assert (
            connection.execute(
                """
            SELECT target_data_version_id
            FROM path_data_adoptions AS adoption
            JOIN path_data_slots AS slot
              ON slot.path_data_slot_id = adoption.path_data_slot_id
            WHERE slot.research_path_id = ?
              AND slot.canonical_key = 'analysis.primary'
            """,
                (branch.research_path_id.value,),
            ).fetchone()[0]
            == data_v1.data_version_id.value
        )
        assert (
            connection.execute(
                """
            SELECT target_plan_revision_id FROM path_plan_adoptions
            WHERE research_path_id = ?
            """,
                (branch.research_path_id.value,),
            ).fetchone()[0]
            == plan_v1.plan_revision_id.value
        )

        node_rows = connection.execute(
            """
            SELECT node.plan_node_id, node.canonical_key
            FROM plan_nodes AS node WHERE node.plan_id = ?
            ORDER BY node.canonical_key
            """,
            (plan_v1.plan_id.value,),
        ).fetchall()
        node_ids = {str(row["canonical_key"]): str(row["plan_node_id"]) for row in node_rows}
        plan_v2 = state.create_plan_revision(
            CreatePlanRevisionCommand(
                CommandId("cmd_branch_plan_v2"),
                turn.turn_id,
                "study.primary",
                "Revised main-path price model",
                {"outcome": "price", "covariates": ["mpg", "weight", "foreign"]},
                (
                    PlanNodeCandidate(
                        "variables.construct",
                        "variable_construction",
                        {"variables": ["price", "mpg", "weight", "foreign"]},
                        retained_plan_node_id=PlanNodeId(node_ids["variables.construct"]),
                    ),
                    PlanNodeCandidate(
                        "model.baseline",
                        "estimation",
                        {"command": "regress price mpg weight foreign"},
                        retained_plan_node_id=PlanNodeId(node_ids["model.baseline"]),
                    ),
                ),
                (PlanDependencyCandidate("variables.construct", "model.baseline", "data"),),
                plan_id=plan_v1.plan_id,
                parent_revision_ids=(plan_v1.plan_revision_id,),
            )
        )
        source.write_bytes(b"version-two")
        data_v2 = artifacts.capture_data_version(
            CaptureDataVersionCommand(
                CommandId("cmd_branch_capture_v2"),
                source,
                DataVersionKind.WORKING_CAPTURE,
                turn.turn_id,
            )
        )
        bundle = state.adopt_research_bundle(
            AdoptResearchBundleCommand(
                CommandId("cmd_branch_bundle_main"),
                initialized.main_path_id,
                turn.turn_id,
                data_targets=(
                    DataSlotAdoptionTarget(
                        data_adoption.path_data_slot_id,
                        data_v2.data_version_id,
                        data_v2.verification_receipt_id,
                        data_adoption.pointer_revision,
                    ),
                ),
                plan_revision_id=plan_v2.plan_revision_id,
                expected_plan_pointer_revision=main_plan.pointer_revision,
            )
        )
        assert bundle.plan_pointer_revision == 2
        assert bundle.data_pointer_revisions[data_adoption.path_data_slot_id.value] == 2
        pointers = {
            str(row["research_path_id"]): str(row["target_plan_revision_id"])
            for row in connection.execute(
                "SELECT research_path_id, target_plan_revision_id FROM path_plan_adoptions"
            ).fetchall()
        }
        assert pointers[initialized.main_path_id.value] == plan_v2.plan_revision_id.value
        assert pointers[branch.research_path_id.value] == plan_v1.plan_revision_id.value
        data_pointers = {
            str(row["research_path_id"]): str(row["target_data_version_id"])
            for row in connection.execute(
                """
                SELECT slot.research_path_id, adoption.target_data_version_id
                FROM path_data_adoptions AS adoption
                JOIN path_data_slots AS slot
                  ON slot.path_data_slot_id = adoption.path_data_slot_id
                WHERE slot.canonical_key = 'analysis.primary'
                """
            ).fetchall()
        }
        assert data_pointers[initialized.main_path_id.value] == data_v2.data_version_id.value
        assert data_pointers[branch.research_path_id.value] == data_v1.data_version_id.value
        assert data_adoption.pointer_revision == 1
        assert node_ids["variables.construct"]

        branch_turn = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_branch_bound_turn"),
                "Continue the alternative construction.",
                research_path_id=branch.research_path_id,
            )
        )
        assert branch_turn.research_path_id == branch.research_path_id
        assert connection.execute("SELECT count(*) FROM research_path_parents").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM path_branch_manifests").fetchone()[0] == 1
    finally:
        connection.close()


def test_stale_branch_revision_rolls_back_without_creating_a_path(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_id = WorkspaceId("ws_stale_branch")
    database = WorkspaceDatabase(workspace_root, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_stale_branch_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_stale_branch_turn"), "Create a branch")
    )
    state = ResearchStateService(SqliteResearchStateRepository(connection), identities)
    before_revision = connection.execute(
        "SELECT MAX(workspace_revision) FROM workspace_commits"
    ).fetchone()[0]
    try:
        stale_basis = before_revision - 1
        with pytest.raises(ValueError, match="stale Workspace revision"):
            state.create_path_branch(
                CreateResearchPathBranchCommand(
                    CommandId("cmd_stale_branch"),
                    initialized.main_path_id,
                    turn.turn_id,
                    "stale",
                    "Stale branch",
                    "Prove stale branch CAS.",
                    stale_basis,
                    stale_basis,
                )
            )
        assert (
            connection.execute("SELECT MAX(workspace_revision) FROM workspace_commits").fetchone()[
                0
            ]
            == before_revision
        )
        assert connection.execute("SELECT count(*) FROM research_paths").fetchone()[0] == 1
    finally:
        connection.close()
