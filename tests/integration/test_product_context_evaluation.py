"""Product Evaluation exercises the real deterministic Context Compiler."""

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
from stata_research_agent.interfaces.context_evaluation_adapter import (
    ContextCompilerEvaluationAdapter,
    ContextCompilerReportGrader,
)
from stata_research_agent.interfaces.evaluation_scenario import EvaluationScenarioLoader
from stata_research_agent.interfaces.product_evaluation_runner import (
    FilesystemEvaluationRunStore,
    ProductEvaluationTrialRunner,
)

ROOT = Path(__file__).parents[2]
SCENARIO_PATH = (
    ROOT / "verification" / "evaluation-scenarios" / "context-compiler-v1.json"
)


def _manifest() -> ExperimentManifest:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    return ExperimentManifest.create(
        experiment_id="context-eval",
        created_at="2026-09-21T00:00:00Z",
        harness_revision="product-eval/v1",
        environment_revision="windows11-stata18/v1",
        stata_revision="stata18-mp",
        scenarios=(scenario,),
        system_under_test=SystemUnderTestSnapshot(
            "candidate",
            "offline",
            "no-model",
            "a" * 64,
            "system-prompt/v1",
            "b" * 64,
            "tool-catalog/v1",
            "rag-policy/v1",
            "memory-policy/v1",
            "context-policy/v1",
        ),
    )


def test_context_compiler_case_preserves_boundaries_across_trials(
    tmp_path: Path,
) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest()
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry(
            (ObservationContractGrader(), ContextCompilerReportGrader())
        ),
        (ContextCompilerEvaluationAdapter(ROOT),),
    )

    trials, metrics = runner.run_case(manifest, scenario)

    assert len(trials) == 3
    assert all(trial.status is TrialStatus.COMPLETED for trial in trials)
    assert all(trial.task_outcome is TaskOutcome.PASS for trial in trials)
    assert metrics.pass_at_1 == metrics.pass_at_k == metrics.pass_power_k == 1.0
    report = json.loads(
        (
            experiment
            / "trials"
            / trials[0].trial_id
            / "bundle"
            / "context-report.json"
        ).read_text(encoding="utf-8")
    )
    assert all(report["checks"].values())
    assert any(
        decision["reason_code"] == "provider_policy"
        for decision in report["build_decisions"]
    )
