"""Adaptive semantic Plan behavior without a research-method whitelist."""

from __future__ import annotations

from pathlib import Path

from stata_research_agent.application.control import CreateWorkspaceCommand, SubmitMessageCommand
from stata_research_agent.application.research_state_service import ResearchStateService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, ResearchPathId, WorkspaceId
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.research_state_store import SqliteResearchStateRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.ipc_contract import PlanProposal, ToolProposal
from stata_research_agent.runtime.plan_coordinator import ResearchPlanCoordinator
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def test_semantic_plan_is_deduplicated_and_binds_arbitrary_stata_code(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_adaptive_plan")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_adaptive_plan_workspace"), workspace_id)
        )
        turn = control.submit_message(
            SubmitMessageCommand(CommandId("cmd_adaptive_plan_turn"), "Explore a new estimator")
        )
        path_id = ResearchPathId(
            str(
                connection.execute(
                    "SELECT research_path_id FROM turns WHERE turn_id = ?",
                    (turn.turn_id.value,),
                ).fetchone()[0]
            )
        )
        coordinator = ResearchPlanCoordinator(
            ResearchStateService(SqliteResearchStateRepository(connection), identities), identities
        )
        proposal = PlanProposal(
            protocol_version="2",
            message_type="plan_proposal",
            message_id="message_plan",
            turn_id=turn.turn_id.value,
            context_revision=1,
            summary="Estimate the proposed design and inspect its identifying behavior",
            structured_plan={
                "change_kind": "initial_design",
                "trigger_references": ["user_idea", "current_data_profile"],
                "nodes": [
                    {
                        "canonical_key": "estimate.experimental",
                        "node_kind": "new_literature_estimator",
                        "specification": {
                            "objective": "Try the estimator described in the supplied paper"
                        },
                    }
                ],
                "dependencies": [],
            },
        )

        first = coordinator.commit_proposal(turn.turn_id, path_id, proposal)
        replay = coordinator.commit_proposal(turn.turn_id, path_id, proposal)
        assert replay == first
        assert connection.execute("SELECT count(*) FROM plan_revisions").fetchone()[0] == 1
        stored = connection.execute(
            "SELECT node_kind, specification_json FROM plan_revision_nodes"
        ).fetchone()
        assert stored["node_kind"] == "research_design"
        assert "new_literature_estimator" in str(stored["specification_json"])

        tool = ToolProposal(
            protocol_version="2",
            message_type="tool_proposal",
            message_id="message_tool",
            turn_id=turn.turn_id.value,
            context_revision=1,
            call_ordinal=1,
            tool_name="stata.execute",
            arguments={
                "dataset_relative_path": "auto.dta",
                "code": "arbitrary_new_estimator price weight",
                "execution_role": "formal_result_candidate",
                "plan_node_key": "estimate.experimental",
            },
        )
        bound = coordinator.bind_formal_tools(path_id, (tool,))[0]

        assert bound.arguments["code"] == "arbitrary_new_estimator price weight"
        assert bound.arguments["plan_node_key"] == "estimate.experimental"
        assert bound.arguments["plan_revision_id"] == first.plan_revision_id
        assert str(bound.arguments["plan_node_id"]).startswith("plannode_")
    finally:
        connection.close()


def test_runtime_fallback_never_turns_exact_code_into_plan_intent(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_minimal_plan")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        initialized = control.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_minimal_plan_workspace"), workspace_id)
        )
        turn = control.submit_message(
            SubmitMessageCommand(CommandId("cmd_minimal_plan_turn"), "Run a formal candidate")
        )
        coordinator = ResearchPlanCoordinator(
            ResearchStateService(SqliteResearchStateRepository(connection), identities), identities
        )
        tool = ToolProposal(
            protocol_version="2",
            message_type="tool_proposal",
            message_id="message_minimal_tool",
            turn_id=turn.turn_id.value,
            context_revision=1,
            call_ordinal=1,
            tool_name="stata.execute",
            arguments={
                "dataset_relative_path": "auto.dta",
                "code": "brand_new_method outcome treatment",
                "execution_role": "formal_result_candidate",
            },
        )

        coordinator.ensure_minimal_formal_plan(
            turn.turn_id, initialized.main_path_id, (tool,)
        )
        bound = coordinator.bind_formal_tools(initialized.main_path_id, (tool,))[0]
        stored = str(
            connection.execute("SELECT specification_json FROM plan_revision_nodes").fetchone()[0]
        )

        assert "brand_new_method" not in stored
        assert bound.arguments["plan_node_key"] == "formal_result.candidate.1"
        assert connection.execute("SELECT count(*) FROM plan_revisions").fetchone()[0] == 1
    finally:
        connection.close()
