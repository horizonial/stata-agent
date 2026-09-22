"""M2-05 vertical tests for evaluator authority and deterministic Stop Guard."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
    SubmitMessageResult,
)
from stata_research_agent.application.evaluation import (
    ContractObligationCandidate,
    DecideNaturalStopCommand,
    EvaluationDimension,
    EvaluationFindingCandidate,
    EvaluationVerdict,
    NormalizeCompletionContractCommand,
    ObligationProvenance,
    RecordEvaluationCommand,
    RecordObligationStateCommand,
    RequirementLevel,
)
from stata_research_agent.application.evaluation_service import RuntimeEvaluationService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.status import (
    ContinueDirective,
    StopGuardDecision,
    TerminalDisposition,
    WaitReason,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.evaluation_store import SqliteEvaluationRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def initialized_turn(
    tmp_path: Path, workspace_name: str
) -> tuple[sqlite3.Connection, RuntimeEvaluationService, SubmitMessageResult]:
    workspace_id = WorkspaceId(f"ws_{workspace_name}")
    database = WorkspaceDatabase(tmp_path / workspace_name, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    control.create_workspace(
        CreateWorkspaceCommand(CommandId(f"cmd_init_{workspace_name}"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId(f"cmd_turn_{workspace_name}"),
            "Use the available data to prepare a traceable result.",
        )
    )
    return (
        connection,
        RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities),
        turn,
    )


def required_output() -> ContractObligationCandidate:
    return ContractObligationCandidate(
        "deliver.traceable.output",
        "Deliver a traceable research output",
        ObligationProvenance.USER_EXPLICIT,
        "message",
        "user-request",
        RequirementLevel.REQUIRED,
        "A source-linked output exists and is available to the user.",
    )


def normalize(service: RuntimeEvaluationService, turn: SubmitMessageResult, command_suffix: str):
    return service.normalize_contract(
        NormalizeCompletionContractCommand(
            CommandId(f"cmd_normalize_{command_suffix}"),
            turn.turn_id,
            1,
            "Prepare one source-linked research output.",
            (required_output(),),
        )
    )


def evaluate(
    service: RuntimeEvaluationService,
    turn: SubmitMessageResult,
    *,
    command_suffix: str,
    turn_revision: int,
    verdict: EvaluationVerdict = EvaluationVerdict.PASS,
    findings: tuple[EvaluationFindingCandidate, ...] = (),
    unknowns: tuple[str, ...] = (),
    required_evidence: tuple[str, ...] = (),
):
    return service.record_evaluation(
        RecordEvaluationCommand(
            CommandId(f"cmd_evaluate_{command_suffix}"),
            turn.turn_id,
            turn_revision,
            None,
            "natural_stop_readiness",
            EvaluationDimension.COMPLETION,
            "model_natural_stop",
            "turn",
            turn.turn_id.value,
            str(turn_revision),
            (),
            ("goal_coverage", "current_runtime_state"),
            ("unadopted_paths", "hidden_candidates"),
            "rule",
            "completion-rule-v1",
            verdict,
            findings,
            unknowns,
            required_evidence,
            (),
        )
    )


def test_intake_contract_cannot_be_declared_successful(tmp_path: Path) -> None:
    connection, service, turn = initialized_turn(tmp_path, "intake_stop")
    try:
        report = evaluate(service, turn, command_suffix="intake", turn_revision=1)
        outcome = service.decide_natural_stop(
            DecideNaturalStopCommand(
                CommandId("cmd_stop_intake"), turn.turn_id, 1, report.evaluation_report_id
            )
        )
        assert outcome.decision is StopGuardDecision.CONTINUE
        assert outcome.directive is ContinueDirective.REPLAN
        assert outcome.reason_code == "normalize_completion_contract"
        assert "contract_not_execution_ready" in outcome.blockers
        assert outcome.turn_status == "running"
        assert (
            connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
            ).fetchone()[0]
            == "running"
        )
    finally:
        connection.close()


def test_required_obligation_blocks_then_satisfied_contract_succeeds(
    tmp_path: Path,
) -> None:
    connection, service, turn = initialized_turn(tmp_path, "coverage")
    try:
        normalized = normalize(service, turn, "coverage")
        first_report = evaluate(service, turn, command_suffix="incomplete", turn_revision=2)
        blocked = service.decide_natural_stop(
            DecideNaturalStopCommand(
                CommandId("cmd_stop_incomplete"),
                turn.turn_id,
                2,
                first_report.evaluation_report_id,
            )
        )
        assert blocked.decision is StopGuardDecision.CONTINUE
        assert blocked.directive is ContinueDirective.REPLAN
        assert blocked.reason_code == "required_obligations_incomplete"

        service.record_obligation_state(
            RecordObligationStateCommand(
                CommandId("cmd_satisfy_output"),
                turn.turn_id,
                normalized.obligation_ids[0],
                "satisfied",
                ("document:docrev_test", "evidence:evidence_test"),
                "system_fact",
                "Delivery and evidence gates passed.",
            )
        )
        final_report = evaluate(service, turn, command_suffix="complete", turn_revision=2)
        completed = service.decide_natural_stop(
            DecideNaturalStopCommand(
                CommandId("cmd_stop_complete"),
                turn.turn_id,
                2,
                final_report.evaluation_report_id,
            )
        )
        assert completed.decision is StopGuardDecision.TERMINATE
        assert completed.terminal_disposition is TerminalDisposition.SUCCEED
        assert completed.turn_status == "succeeded"
        assert (
            connection.execute(
                "SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1"
            ).fetchone()[0]
            is None
        )
    finally:
        connection.close()


def test_quality_warning_is_recorded_but_cannot_invent_a_hard_block(
    tmp_path: Path,
) -> None:
    connection, service, turn = initialized_turn(tmp_path, "quality")
    try:
        normalized = normalize(service, turn, "quality")
        service.record_obligation_state(
            RecordObligationStateCommand(
                CommandId("cmd_quality_satisfied"),
                turn.turn_id,
                normalized.obligation_ids[0],
                "satisfied",
            )
        )
        report = evaluate(
            service,
            turn,
            command_suffix="quality",
            turn_revision=2,
            verdict=EvaluationVerdict.WARN,
            findings=(
                EvaluationFindingCandidate(
                    "quality_concern",
                    "warn",
                    "The prose could be clearer, but no required obligation is missing.",
                    "medium",
                ),
            ),
        )
        outcome = service.decide_natural_stop(
            DecideNaturalStopCommand(
                CommandId("cmd_stop_quality"), turn.turn_id, 2, report.evaluation_report_id
            )
        )
        assert outcome.decision is StopGuardDecision.TERMINATE
        finding = connection.execute("SELECT control_effect FROM evaluation_findings").fetchone()
        assert finding[0] == "none"
    finally:
        connection.close()


def test_semantic_ambiguity_waits_for_user_and_keeps_write_lane(tmp_path: Path) -> None:
    connection, service, turn = initialized_turn(tmp_path, "ambiguity")
    try:
        normalized = normalize(service, turn, "ambiguity")
        service.record_obligation_state(
            RecordObligationStateCommand(
                CommandId("cmd_ambiguity_satisfied"),
                turn.turn_id,
                normalized.obligation_ids[0],
                "satisfied",
            )
        )
        report = evaluate(
            service,
            turn,
            command_suffix="ambiguity",
            turn_revision=2,
            verdict=EvaluationVerdict.UNKNOWN,
            findings=(
                EvaluationFindingCandidate(
                    "research_semantic_ambiguity",
                    "warn",
                    "Two defensible variable meanings remain and the user must choose.",
                ),
            ),
            unknowns=("preferred variable meaning",),
        )
        outcome = service.decide_natural_stop(
            DecideNaturalStopCommand(
                CommandId("cmd_stop_ambiguity"),
                turn.turn_id,
                2,
                report.evaluation_report_id,
            )
        )
        assert outcome.decision is StopGuardDecision.WAIT
        assert outcome.wait_reason is WaitReason.USER_CONFIRMATION
        assert outcome.waiting_request_id is not None
        assert outcome.turn_status == "waiting"
        assert (
            connection.execute(
                "SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1"
            ).fetchone()[0]
            == turn.turn_id.value
        )
    finally:
        connection.close()


def test_registered_plan_deviation_blocks_termination_but_not_execution(
    tmp_path: Path,
) -> None:
    connection, service, turn = initialized_turn(tmp_path, "plan_deviation")
    try:
        normalized = normalize(service, turn, "plan_deviation")
        service.record_obligation_state(
            RecordObligationStateCommand(
                CommandId("cmd_plan_output_satisfied"),
                turn.turn_id,
                normalized.obligation_ids[0],
                "satisfied",
            )
        )
        report = evaluate(
            service,
            turn,
            command_suffix="plan_deviation",
            turn_revision=2,
            verdict=EvaluationVerdict.FAIL,
            findings=(
                EvaluationFindingCandidate(
                    "plan_deviation",
                    "error",
                    "The formal result does not follow the adopted design.",
                ),
            ),
        )
        outcome = service.decide_natural_stop(
            DecideNaturalStopCommand(
                CommandId("cmd_stop_plan_deviation"),
                turn.turn_id,
                2,
                report.evaluation_report_id,
            )
        )
        assert outcome.decision is StopGuardDecision.CONTINUE
        assert outcome.directive is ContinueDirective.REVISE
        assert outcome.reason_code == "evaluation_blocks_termination"
        assert "finding:plan_deviation" in outcome.blockers
    finally:
        connection.close()


def test_stop_guard_rejects_report_after_any_intervening_authoritative_change(
    tmp_path: Path,
) -> None:
    connection, service, turn = initialized_turn(tmp_path, "stale_evaluation")
    try:
        normalized = normalize(service, turn, "stale_evaluation")
        report = evaluate(service, turn, command_suffix="stale", turn_revision=2)
        service.record_obligation_state(
            RecordObligationStateCommand(
                CommandId("cmd_state_after_evaluation"),
                turn.turn_id,
                normalized.obligation_ids[0],
                "satisfied",
            )
        )
        before = connection.execute(
            "SELECT MAX(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        with pytest.raises(ValueError, match="not fresh"):
            service.decide_natural_stop(
                DecideNaturalStopCommand(
                    CommandId("cmd_stop_stale_evaluation"),
                    turn.turn_id,
                    2,
                    report.evaluation_report_id,
                )
            )
        after = connection.execute(
            "SELECT MAX(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        assert after == before
    finally:
        connection.close()


def test_agent_normalization_cannot_create_required_obligation() -> None:
    with pytest.raises(ValueError, match="cannot create a required obligation"):
        ContractObligationCandidate(
            "agent.idea",
            "An evaluator invented requirement",
            ObligationProvenance.AGENT_NORMALIZATION,
            "evaluation_report",
            "eval_fake",
            RequirementLevel.REQUIRED,
            "Do something the user never asked for.",
        )
