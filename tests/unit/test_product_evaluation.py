"""Product-evaluation contracts remain independent, strict, and comparable."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from stata_research_agent.application.product_evaluation import (
    ComparisonDisposition,
    EvaluationScenario,
    ExperimentManifest,
    GraderKind,
    GraderRole,
    ObservationContractGrader,
    ProductEvaluationError,
    ScoreVerdict,
    SystemUnderTestSnapshot,
    TaskOutcome,
    TrialBundle,
    TrialGraderRegistry,
    TrialResult,
    TrialStatus,
    aggregate_case_trials,
    compare_case_metrics,
)
from stata_research_agent.interfaces.evaluation_scenario import EvaluationScenarioLoader
from stata_research_agent.interfaces.memory_evaluation_adapter import (
    MemoryCorrectionEvaluationAdapter,
)

ROOT = Path(__file__).parents[2]
SCENARIO_PATH = (
    ROOT / "verification" / "evaluation-scenarios" / "memory-correction-v1.json"
)


def _system_under_test(*, code_revision: str = "candidate") -> SystemUnderTestSnapshot:
    return SystemUnderTestSnapshot(
        code_revision,
        "deepseek-default",
        "deepseek-chat",
        "a" * 64,
        "system-prompt/v1",
        "b" * 64,
        "tool-catalog/v1",
        "rag-policy/v1",
        "memory-policy/v1",
        "context-policy/v1",
    )


def _bundle(tmp_path: Path, scenario: object, observations: tuple[str, ...]) -> TrialBundle:
    return TrialBundle(
        "trial-001",
        scenario.scenario_id,
        scenario.revision,
        scenario.scenario_sha256,
        tmp_path / "workspace",
        tmp_path / "workspace" / "workspace.sqlite3",
        tmp_path / "trace.jsonl",
        tmp_path / "artifact-manifest.json",
        observations,
        ("journal:1",),
    )


def _trial(
    scenario: object,
    ordinal: int,
    *,
    passed: bool,
    duration: float = 1.0,
) -> TrialResult:
    score = ObservationContractGrader().grade(
        scenario,
        scenario.graders[0],
        TrialBundle(
            f"bundle-{ordinal}",
            scenario.scenario_id,
            scenario.revision,
            scenario.scenario_sha256,
            Path(f"workspace-{ordinal}"),
            Path(f"workspace-{ordinal}/workspace.sqlite3"),
            Path(f"trace-{ordinal}.jsonl"),
            Path(f"artifact-{ordinal}.json"),
            scenario.required_outcomes if passed else (),
        ),
    )
    return TrialResult(
        f"trial-{ordinal}",
        "experiment-1",
        scenario.scenario_id,
        scenario.revision,
        scenario.scenario_sha256,
        ordinal,
        TrialStatus.COMPLETED,
        TaskOutcome.PASS if passed else TaskOutcome.FAIL,
        (score,),
        () if passed else ("OUTCOME_CONTRACT_FAILED",),
        duration,
        Decimal("0.01"),
        "USD",
        f"runs/trial-{ordinal}",
    )


def test_checked_in_memory_scenario_is_strict_and_canonically_hashed(tmp_path: Path) -> None:
    loader = EvaluationScenarioLoader()
    scenario = loader.load(SCENARIO_PATH)
    assert scenario.scenario_id == "agent.memory.correction"
    assert scenario.level.value == "subsystem"
    assert scenario.subsystem_mode is not None
    assert scenario.subsystem_mode.value == "intrinsic"
    assert scenario.reference.status == "verified"
    assert len(scenario.scenario_sha256) == 64

    raw = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    reformatted = tmp_path / "reformatted.json"
    reformatted.write_text(json.dumps(raw, ensure_ascii=False, indent=7), encoding="utf-8")
    assert loader.load(reformatted).scenario_sha256 == scenario.scenario_sha256


def test_scenario_loader_rejects_unknown_nested_fields_and_unsafe_locators(
    tmp_path: Path,
) -> None:
    raw = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    raw["graders"][0]["surprise"] = True
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProductEvaluationError, match="unknown or missing"):
        EvaluationScenarioLoader().load(path)

    raw["graders"][0].pop("surprise")
    raw["fixtures"][0]["locator"] = "C:/private/gold.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProductEvaluationError, match="safe relative"):
        EvaluationScenarioLoader().load(path)


def test_scenario_policy_and_reference_fail_closed(tmp_path: Path) -> None:
    raw = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "invalid.json"
    raw["trial_policy"]["reliability_k"] = 4
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProductEvaluationError, match="cannot exceed"):
        EvaluationScenarioLoader().load(path)

    raw["trial_policy"]["reliability_k"] = 3
    raw["reference"]["status"] = "pending"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProductEvaluationError, match="verified reference"):
        EvaluationScenarioLoader().load(path)


def test_experiment_manifest_freezes_contract_but_allows_system_comparison() -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    baseline = ExperimentManifest.create(
        experiment_id="baseline",
        created_at="2026-09-21T00:00:00Z",
        harness_revision="product-eval/v1",
        environment_revision="windows11-stata18/v1",
        stata_revision="stata18-mp",
        scenarios=(scenario,),
        system_under_test=_system_under_test(code_revision="baseline"),
    )
    candidate = ExperimentManifest.create(
        experiment_id="candidate",
        created_at="2026-09-21T01:00:00Z",
        harness_revision="product-eval/v1",
        environment_revision="windows11-stata18/v1",
        stata_revision="stata18-mp",
        scenarios=(scenario,),
        system_under_test=_system_under_test(code_revision="candidate"),
    )
    assert baseline.comparable_with(candidate)
    assert baseline.comparison_contract_sha256 == candidate.comparison_contract_sha256
    assert baseline.manifest_sha256 != candidate.manifest_sha256

    incompatible = ExperimentManifest.create(
        experiment_id="other-environment",
        created_at="2026-09-21T02:00:00Z",
        harness_revision="product-eval/v1",
        environment_revision="windows11-stata19/v1",
        stata_revision="stata19-mp",
        scenarios=(scenario,),
        system_under_test=_system_under_test(),
    )
    assert not baseline.comparable_with(incompatible)
    serialized = json.loads(baseline.to_json())
    assert serialized["manifest_sha256"] == baseline.manifest_sha256
    assert serialized["system_under_test"]["code_revision"] == "baseline"


def test_experiment_manifest_rejects_conflicting_grader_bindings() -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    conflicting_grader = replace(
        scenario.graders[0],
        config_json=json.dumps({"required_outcomes": "different"}),
    )
    conflicting_scenario = replace(
        scenario,
        scenario_id="agent.memory.correction.variant",
        graders=(conflicting_grader,),
        scenario_sha256="f" * 64,
    )
    assert isinstance(conflicting_scenario, EvaluationScenario)
    with pytest.raises(ProductEvaluationError, match="conflicting experiment bindings"):
        ExperimentManifest.create(
            experiment_id="conflict",
            created_at="2026-09-21T00:00:00Z",
            harness_revision="product-eval/v1",
            environment_revision="windows11-stata18/v1",
            stata_revision="stata18-mp",
            scenarios=(scenario, conflicting_scenario),
            system_under_test=_system_under_test(),
        )


def test_grader_registry_enforces_identity_kind_and_hard_gate(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    registry = TrialGraderRegistry((ObservationContractGrader(),))
    passed = registry.grade(scenario, _bundle(tmp_path, scenario, scenario.required_outcomes))
    assert len(passed) == 1
    assert passed[0].verdict is ScoreVerdict.PASS
    assert passed[0].role is GraderRole.HARD_GATE

    failed = registry.grade(
        scenario,
        _bundle(tmp_path, scenario, (scenario.forbidden_outcomes[0],)),
    )
    assert failed[0].verdict is ScoreVerdict.FAIL
    with pytest.raises(ProductEvaluationError, match="hard-gate failure"):
        TrialResult(
            "invalid-pass",
            "experiment-1",
            scenario.scenario_id,
            scenario.revision,
            scenario.scenario_sha256,
            1,
            TrialStatus.COMPLETED,
            TaskOutcome.PASS,
            failed,
            (),
            1.0,
            None,
            None,
            "runs/invalid-pass",
        )


def test_case_metrics_report_pass_at_k_pass_power_k_and_comparison() -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    candidate = aggregate_case_trials(
        scenario,
        (
            _trial(scenario, 1, passed=True),
            _trial(scenario, 2, passed=True),
            _trial(scenario, 3, passed=False),
        ),
    )
    assert candidate.pass_at_1 == pytest.approx(2 / 3)
    assert candidate.pass_at_k == 1.0
    assert candidate.pass_power_k == 0.0
    assert candidate.hard_gate_failure_count == 1
    assert candidate.known_mean_cost == Decimal("0.01")

    baseline = replace(
        candidate,
        passed_trials=1,
        hard_gate_failure_count=2,
        pass_at_1=1 / 3,
    )
    comparison = compare_case_metrics(baseline, candidate)
    assert comparison.disposition is ComparisonDisposition.IMPROVED
    assert comparison.pass_at_1_delta == pytest.approx(1 / 3)
    assert comparison.hard_gate_failure_delta == -1

    changed = replace(candidate, scenario_sha256="f" * 64)
    assert (
        compare_case_metrics(candidate, changed).disposition
        is ComparisonDisposition.INCOMPARABLE
    )


def test_infrastructure_errors_remain_visible_and_unscored() -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    completed = _trial(scenario, 1, passed=True)
    infrastructure = TrialResult(
        "trial-2",
        "experiment-1",
        scenario.scenario_id,
        scenario.revision,
        scenario.scenario_sha256,
        2,
        TrialStatus.INFRASTRUCTURE_ERROR,
        TaskOutcome.UNSCORED,
        (),
        ("STATA_NOT_AVAILABLE",),
        0.5,
        None,
        None,
        "runs/trial-2",
    )
    cancelled = replace(
        infrastructure,
        trial_id="trial-3",
        trial_ordinal=3,
        status=TrialStatus.CANCELLED,
        failure_codes=("USER_CANCELLED",),
        bundle_locator="runs/trial-3",
    )
    metrics = aggregate_case_trials(scenario, (completed, infrastructure, cancelled))
    assert metrics.completed_trials == 1
    assert metrics.infrastructure_errors == 1
    assert metrics.pass_at_1 == 1.0
    assert metrics.pass_at_k is None
    assert metrics.pass_power_k is None


def test_grader_kind_mismatch_is_not_silently_accepted(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)

    class _WrongKindGrader(ObservationContractGrader):
        kind = GraderKind.MODEL

    with pytest.raises(ProductEvaluationError, match="kind does not match"):
        TrialGraderRegistry((_WrongKindGrader(),)).grade(
            scenario,
            _bundle(tmp_path, scenario, scenario.required_outcomes),
        )


def test_memory_scenario_executes_real_authoritative_memory_store(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    bundle = MemoryCorrectionEvaluationAdapter().execute(
        scenario,
        trial_id="memory-correction-001",
        trial_root=tmp_path / "trial-001",
    )
    assert set(scenario.required_outcomes) <= set(bundle.observations)
    assert not set(scenario.forbidden_outcomes) & set(bundle.observations)
    assert bundle.workspace_database.is_file()
    assert bundle.trace_export.read_text(encoding="utf-8").strip()
    assert bundle.artifact_manifest.is_file()

    scores = TrialGraderRegistry((ObservationContractGrader(),)).grade(scenario, bundle)
    assert scores[0].verdict is ScoreVerdict.PASS
