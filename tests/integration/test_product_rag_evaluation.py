"""Product Evaluation runs the real canonical RAG implementation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stata_research_agent.application.product_evaluation import (
    ExperimentManifest,
    ObservationContractGrader,
    ProductEvaluationError,
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
from stata_research_agent.interfaces.memory_evaluation_adapter import (
    MemoryCorrectionEvaluationAdapter,
)
from stata_research_agent.interfaces.product_evaluation_runner import (
    FilesystemEvaluationRunStore,
    ProductEvaluationTrialRunner,
)
from stata_research_agent.interfaces.rag_evaluation_adapter import (
    RagIntrinsicEvaluationAdapter,
    RagRetrievalReportGrader,
)

ROOT = Path(__file__).parents[2]
SCENARIO_PATH = (
    ROOT / "verification" / "evaluation-scenarios" / "rag-corpus-isolation-v1.json"
)
MEMORY_SCENARIO_PATH = (
    ROOT / "verification" / "evaluation-scenarios" / "memory-correction-v1.json"
)
CONTEXT_SCENARIO_PATH = (
    ROOT / "verification" / "evaluation-scenarios" / "context-compiler-v1.json"
)


def _manifest() -> ExperimentManifest:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    return ExperimentManifest.create(
        experiment_id="rag-eval",
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


def test_real_canonical_rag_trial_is_scored_and_published(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest()
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry(
            (ObservationContractGrader(), RagRetrievalReportGrader())
        ),
        (RagIntrinsicEvaluationAdapter(ROOT),),
    )

    result = runner.run_trial(manifest, scenario, 1)

    assert result.status is TrialStatus.COMPLETED
    assert result.task_outcome is TaskOutcome.PASS
    assert len(result.scores) == 2
    trial_root = experiment / "trials" / result.trial_id
    report = json.loads((trial_root / "bundle" / "rag-report.json").read_text())
    assert report["macro_recall_at_k"] == 1.0
    assert report["total_role_leaks"] == 0
    integrity = json.loads((trial_root / "artifact-integrity.json").read_text())
    assert any(
        item["role"] == "evaluation_payload:rag-report"
        for item in integrity["artifacts"]
    )


def test_rag_grader_rejects_payload_changed_after_execution(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    bundle = RagIntrinsicEvaluationAdapter(ROOT).execute(
        scenario,
        trial_id="rag-direct-001",
        trial_root=tmp_path / "bundle",
    )
    bundle.evaluation_payloads[0].path.write_text("{}", encoding="utf-8")

    with pytest.raises(ProductEvaluationError, match="digest mismatch"):
        RagRetrievalReportGrader().grade(scenario, scenario.graders[1], bundle)


def test_rag_case_runs_repeated_trials_and_persists_reliability(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest()
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry(
            (ObservationContractGrader(), RagRetrievalReportGrader())
        ),
        (RagIntrinsicEvaluationAdapter(ROOT),),
    )

    trials, metrics = runner.run_case(manifest, scenario)

    assert len(trials) == 3
    assert metrics.pass_at_1 == 1.0
    assert metrics.pass_at_k == 1.0
    assert metrics.pass_power_k == 1.0
    persisted = json.loads(
        (experiment / "case-metrics" / f"{scenario.scenario_id}.json").read_text()
    )
    assert persisted["completed_trials"] == 3
    assert persisted["pass_power_k"] == 1.0


def test_experiment_runs_memory_and_rag_without_collapsing_case_metrics(
    tmp_path: Path,
) -> None:
    rag = EvaluationScenarioLoader().load(SCENARIO_PATH)
    memory = EvaluationScenarioLoader().load(MEMORY_SCENARIO_PATH)
    context = EvaluationScenarioLoader().load(CONTEXT_SCENARIO_PATH)
    base = _manifest()
    manifest = ExperimentManifest.create(
        experiment_id="agent-subsystems-eval",
        created_at=base.created_at,
        harness_revision=base.harness_revision,
        environment_revision=base.environment_revision,
        stata_revision=base.stata_revision,
        scenarios=(memory, rag, context),
        system_under_test=base.system_under_test,
    )
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry(
            (
                ObservationContractGrader(),
                RagRetrievalReportGrader(),
                ContextCompilerReportGrader(),
            )
        ),
        (
            MemoryCorrectionEvaluationAdapter(),
            RagIntrinsicEvaluationAdapter(ROOT),
            ContextCompilerEvaluationAdapter(ROOT),
        ),
    )

    metrics = runner.run_experiment(manifest, (memory, rag, context))

    assert metrics.requested_trials == metrics.completed_trials == 9
    assert metrics.infrastructure_errors == 0
    assert {case.scenario_id for case in metrics.cases} == {
        memory.scenario_id,
        rag.scenario_id,
        context.scenario_id,
    }
    persisted = json.loads((experiment / "experiment-result.json").read_text())
    assert len(persisted["cases"]) == 3
    assert "overall_score" not in persisted
