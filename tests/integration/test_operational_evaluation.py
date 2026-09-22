"""Operational evaluation derives metrics from a normal authoritative Turn trace."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from stata_research_agent.application.outcome_feedback import (
    OutcomeDisposition,
    OutcomeRating,
    RecordTurnOutcomeFeedbackCommand,
)
from stata_research_agent.application.outcome_feedback_service import (
    TurnOutcomeFeedbackService,
)
from stata_research_agent.domain.identifiers import CommandId, TurnId
from stata_research_agent.interfaces.evaluation_scenario import EvaluationScenarioLoader
from stata_research_agent.interfaces.trajectory_evaluation_adapter import (
    TurnLoopTrajectoryEvaluationAdapter,
)
from stata_research_agent.persistence.operational_evaluation_query import (
    SqliteOperationalEvaluationQuery,
)
from stata_research_agent.persistence.outcome_feedback_store import (
    SqliteTurnOutcomeFeedbackRepository,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator

ROOT = Path(__file__).parents[2]
SCENARIO = ROOT / "verification/evaluation-scenarios/turn-loop-stop-guard-success-v1.json"
PYTHON = ROOT / ".venv/Scripts/python.exe"


def _metrics(snapshot, layer: str):
    selected = next(item for item in snapshot.layers if item.layer == layer)
    return {item.metric_id: item for item in selected.metrics}


def test_real_turn_facts_produce_explainable_l1_l2_l3_metrics(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO)
    bundle = TurnLoopTrajectoryEvaluationAdapter(ROOT, PYTHON).execute(
        scenario,
        trial_id="operational-eval",
        trial_root=tmp_path / "bundle",
    )
    connection = sqlite3.connect(bundle.workspace_database)
    connection.row_factory = sqlite3.Row
    try:
        workspace = SqliteOperationalEvaluationQuery(connection).workspace()
    finally:
        connection.close()

    assert len(workspace.turns) == 1
    turn = workspace.turns[0]
    l1 = _metrics(turn, "L1")
    l2 = _metrics(turn, "L2")
    l3 = _metrics(turn, "L3")
    assert l1["l1.gateway.invocation_completion_rate"].value == 1.0
    assert l1["l1.tool.operation_without_admission_count"].value == 0.0
    assert l2["l2.loop.step_completion_rate"].value == 1.0
    assert l2["l2.loop.success_contract_violation_count"].value == 0.0
    assert l3["l3.product.stata_result_provenance_rate"].status == "not_applicable"
    assert l3["l3.product.replay_ready_stata_run_rate"].status == "not_applicable"


def test_workspace_rollup_preserves_denominators_and_not_applicable(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO)
    bundle = TurnLoopTrajectoryEvaluationAdapter(ROOT, PYTHON).execute(
        scenario,
        trial_id="operational-rollup",
        trial_root=tmp_path / "bundle",
    )
    connection = sqlite3.connect(bundle.workspace_database)
    connection.row_factory = sqlite3.Row
    try:
        workspace = SqliteOperationalEvaluationQuery(connection).workspace()
    finally:
        connection.close()

    l1 = _metrics(workspace, "L1")
    assert l1["l1.gateway.invocation_completion_rate"].numerator == 2.0
    assert l1["l1.gateway.invocation_completion_rate"].denominator == 2.0
    assert l1["l1.memory.source_link_rate"].status == "not_applicable"


def test_real_user_outcome_feedback_enters_operational_metrics_without_becoming_quality_truth(
    tmp_path: Path,
) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO)
    bundle = TurnLoopTrajectoryEvaluationAdapter(ROOT, PYTHON).execute(
        scenario,
        trial_id="operational-feedback",
        trial_root=tmp_path / "bundle",
    )
    connection = sqlite3.connect(bundle.workspace_database)
    connection.row_factory = sqlite3.Row
    try:
        turn_id = TurnId(
            str(connection.execute("SELECT turn_id FROM turns").fetchone()[0])
        )
        outcome = TurnOutcomeFeedbackService(
            SqliteTurnOutcomeFeedbackRepository(connection),
            UuidIdentityGenerator(),
        ).record(
            RecordTurnOutcomeFeedbackCommand(
                CommandId("cmd_record_real_outcome_feedback"),
                turn_id,
                OutcomeDisposition.NEEDS_REVISION,
                ratings=(OutcomeRating("research_fit", 3),),
                issue_codes=("explanation_too_shallow",),
                comment="Keep the results, but deepen the interpretation.",
            )
        )
        snapshot = SqliteOperationalEvaluationQuery(connection).turn(turn_id)
    finally:
        connection.close()

    assert outcome.disposition is OutcomeDisposition.NEEDS_REVISION
    l3 = _metrics(snapshot, "L3")
    acceptance = l3["l3.product.explicit_user_acceptance_rate"]
    assert acceptance.status == "observed"
    assert acceptance.numerator == 0.0
    assert acceptance.denominator == 1.0
    assert l3["l3.product.explicit_revision_request_count"].value == 1.0
    assert snapshot.configuration.main_skill_name is not None
    breakdown = next(
        item
        for item in snapshot.breakdowns
        if item.breakdown_id == "l3.product.user_outcome_history"
    )
    assert breakdown.items[0].key == "needs_revision"
