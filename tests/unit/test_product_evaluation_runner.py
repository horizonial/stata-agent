"""The Product Evaluation runner isolates and persists real Trial outcomes."""

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
from stata_research_agent.interfaces.evaluation_scenario import EvaluationScenarioLoader
from stata_research_agent.interfaces.memory_evaluation_adapter import (
    MemoryCorrectionEvaluationAdapter,
)
from stata_research_agent.interfaces.product_evaluation_runner import (
    FilesystemEvaluationRunStore,
    ProductEvaluationTrialRunner,
)

ROOT = Path(__file__).parents[2]
SCENARIO_PATH = (
    ROOT / "verification" / "evaluation-scenarios" / "memory-correction-v1.json"
)


def _manifest(experiment_id: str) -> ExperimentManifest:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    return ExperimentManifest.create(
        experiment_id=experiment_id,
        created_at="2026-09-21T00:00:00Z",
        harness_revision="product-eval/v1",
        environment_revision="windows11-stata18/v1",
        stata_revision="stata18-mp",
        scenarios=(scenario,),
        system_under_test=SystemUnderTestSnapshot(
            "candidate",
            "test-provider",
            "test-model",
            "a" * 64,
            "system-prompt/v1",
            "b" * 64,
            "tool-catalog/v1",
            "rag-policy/v1",
            "memory-policy/v1",
            "context-policy/v1",
        ),
    )


def test_runner_executes_real_memory_trial_and_persists_result(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest("memory-eval")
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry((ObservationContractGrader(),)),
        (MemoryCorrectionEvaluationAdapter(),),
    )

    result = runner.run_trial(manifest, scenario, 1)

    assert result.status is TrialStatus.COMPLETED
    assert result.task_outcome is TaskOutcome.PASS
    persisted = json.loads(
        (
            experiment
            / "trials"
            / result.trial_id
            / "trial-result.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted["task_outcome"] == "pass"
    assert persisted["scores"][0]["role"] == "hard_gate"
    integrity = json.loads(
        (
            experiment
            / "trials"
            / result.trial_id
            / "artifact-integrity.json"
        ).read_text(encoding="utf-8")
    )
    assert {artifact["role"] for artifact in integrity["artifacts"]} == {
        "workspace_database",
        "trace",
        "adapter_artifact_manifest",
        "evaluation_payload:memory-report",
    }
    assert all(len(artifact["sha256"]) == 64 for artifact in integrity["artifacts"])


def test_experiment_and_trial_identities_are_immutable(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest("immutable-eval")
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    store.create_experiment(manifest)
    with pytest.raises(ProductEvaluationError, match="already exists"):
        store.create_experiment(manifest)

    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry((ObservationContractGrader(),)),
        (MemoryCorrectionEvaluationAdapter(),),
    )
    runner.run_trial(manifest, scenario, 1)
    with pytest.raises(ProductEvaluationError, match="trial identity already exists"):
        runner.run_trial(manifest, scenario, 1)


def test_missing_adapter_is_visible_infrastructure_error(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest("missing-adapter")
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry((ObservationContractGrader(),)),
        (),
    )

    result = runner.run_trial(manifest, scenario, 1)

    assert result.status is TrialStatus.INFRASTRUCTURE_ERROR
    assert result.task_outcome is TaskOutcome.UNSCORED
    assert result.failure_codes == ("SCENARIO_ADAPTER_UNAVAILABLE",)
    persisted = json.loads(
        (
            experiment
            / "trials"
            / result.trial_id
            / "trial-result.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted["status"] == "infrastructure_error"
    assert persisted["cost_amount"] is None


def test_reserved_trial_is_not_visible_as_a_formal_result(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest("incomplete-eval")
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)

    trial_id, staging, relative = store.reserve_trial(manifest, scenario, 1)

    assert relative == f"trials/{trial_id}"
    assert staging == experiment / ".staging" / trial_id
    assert staging.is_dir()
    assert not (experiment / "trials" / trial_id).exists()


def test_manifest_tampering_blocks_new_trials(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest("tampered-eval")
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    manifest_path = experiment / "experiment-manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["system_under_test"]["code_revision"] = "different"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry((ObservationContractGrader(),)),
        (MemoryCorrectionEvaluationAdapter(),),
    )
    with pytest.raises(ProductEvaluationError, match="identity mismatch"):
        runner.run_trial(manifest, scenario, 1)
