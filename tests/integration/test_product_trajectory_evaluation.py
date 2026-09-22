"""Product Evaluation reads Agent-loop truth from the Workspace authority."""

from __future__ import annotations

import json
from pathlib import Path

from stata_research_agent.application.product_evaluation import (
    ExperimentManifest,
    ObservationContractGrader,
    SystemUnderTestSnapshot,
    TaskOutcome,
    TrialGraderRegistry,
    TrialStatus,
)
from stata_research_agent.interfaces.evaluation_scenario import EvaluationScenarioLoader
from stata_research_agent.interfaces.product_evaluation_runner import (
    FilesystemEvaluationRunStore,
    ProductEvaluationTrialRunner,
)
from stata_research_agent.interfaces.trajectory_evaluation_adapter import (
    TurnLoopTrajectoryEvaluationAdapter,
    TurnLoopTrajectoryGrader,
)

ROOT = Path(__file__).parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
SCENARIO_PATH = (
    ROOT / "verification" / "evaluation-scenarios" / "turn-loop-stop-guard-success-v1.json"
)


def _manifest() -> ExperimentManifest:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    return ExperimentManifest.create(
        experiment_id="turn-loop-trajectory-eval",
        created_at="2026-09-21T00:00:00Z",
        harness_revision="product-eval/v1",
        environment_revision="deterministic-agent-loop/v1",
        stata_revision="supervised-fixture/v1",
        scenarios=(scenario,),
        system_under_test=SystemUnderTestSnapshot(
            "candidate",
            "deterministic",
            "two-step-model",
            "a" * 64,
            "system-prompt/v1",
            "b" * 64,
            "tool-catalog/v1",
            "rag-policy/v1",
            "memory-policy/v1",
            "context-policy/v1",
        ),
    )


def test_real_turn_driver_trajectory_is_graded_from_workspace_db(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest()
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry((ObservationContractGrader(), TurnLoopTrajectoryGrader())),
        (TurnLoopTrajectoryEvaluationAdapter(ROOT, PYTHON),),
    )

    result = runner.run_trial(manifest, scenario, 1)

    assert result.status is TrialStatus.COMPLETED
    assert result.task_outcome is TaskOutcome.PASS
    assert all(score.verdict.value == "pass" for score in result.scores)
    trial_root = experiment / "trials" / result.trial_id
    report = json.loads(
        (trial_root / "bundle" / "trajectory-report.json").read_text(encoding="utf-8")
    )
    assert report["outcome"] == {
        "executed_steps": 2,
        "status": "succeeded",
        "stop_guard_decision": "terminate",
        "tool_executions": 1,
    }
    assert report["goal_coverage"]["coverage_status"] == "satisfied"
    assert report["stop_guard"]["terminal_disposition"] == "succeed"


def test_turn_loop_case_is_reliable_across_three_isolated_trials(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest()
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry((ObservationContractGrader(), TurnLoopTrajectoryGrader())),
        (TurnLoopTrajectoryEvaluationAdapter(ROOT, PYTHON),),
    )

    trials, metrics = runner.run_case(manifest, scenario)

    assert len(trials) == 3
    assert metrics.completed_trials == 3
    assert metrics.infrastructure_errors == 0
    assert metrics.pass_at_1 == metrics.pass_at_k == metrics.pass_power_k == 1.0
