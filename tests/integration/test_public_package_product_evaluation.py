"""Unified Product Evaluation for two frozen public replication packages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from stata_research_agent.interfaces.replication_evaluation_adapter import (
    JEL_DID_SPEC,
    POWERFUL_EXPERIMENTS_SPEC,
    PackageReplicationOracleGrader,
    PackageReplicationSpec,
    PublicPackageReplicationAdapter,
)

ROOT = Path(__file__).parents[2]
SCENARIO_ROOT = ROOT / "verification" / "evaluation-scenarios"
if not (ROOT / "verification" / "replication-benchmarks").is_dir():
    pytest.skip(
        "optional frozen public replication packages are not installed",
        allow_module_level=True,
    )
MCP_ROOT = Path.home() / "stata-mcp"
MCP_PYTHON = MCP_ROOT / ".venv-uv" / "Scripts" / "python.exe"
STATA_HOME = Path(r"C:\Program Files\Stata18")


def _manifest(scenario: object, experiment_id: str) -> ExperimentManifest:
    return ExperimentManifest.create(
        experiment_id=experiment_id,
        created_at="2026-09-21T00:00:00Z",
        harness_revision="product-eval/v1",
        environment_revision="windows11-stata18/v1",
        stata_revision="stata18-mp",
        scenarios=(scenario,),  # type: ignore[arg-type]
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


@pytest.mark.parametrize(
    ("scenario_filename", "package_spec", "expected_marker", "expected_value"),
    (
        (
            "replication-powerful-experiments-full-package-v1.json",
            POWERFUL_EXPERIMENTS_SPEC,
            "EMPLOYEE_N",
            196,
        ),
        (
            "replication-jel-did-table2-simple-did-v1.json",
            JEL_DID_SPEC,
            "REGRESSION_N",
            4400,
        ),
    ),
)
def test_public_package_runs_through_product_evaluation(
    tmp_path: Path,
    scenario_filename: str,
    package_spec: PackageReplicationSpec,
    expected_marker: str,
    expected_value: int,
) -> None:
    if not (MCP_PYTHON.is_file() and STATA_HOME.is_dir()):
        pytest.skip("certified local Stata MCP environment is not installed")
    scenario = EvaluationScenarioLoader().load(SCENARIO_ROOT / scenario_filename)
    manifest = _manifest(
        scenario,
        f"{package_spec.scenario_id.removeprefix('agent.replication.').replace('.', '-')}-eval",
    )
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry(
            (
                ObservationContractGrader(),
                PackageReplicationOracleGrader((POWERFUL_EXPERIMENTS_SPEC, JEL_DID_SPEC)),
            )
        ),
        (
            PublicPackageReplicationAdapter(
                ROOT,
                POWERFUL_EXPERIMENTS_SPEC,
                mcp_python=MCP_PYTHON,
                mcp_source_root=MCP_ROOT / "src",
                stata_home=STATA_HOME,
            ),
            PublicPackageReplicationAdapter(
                ROOT,
                JEL_DID_SPEC,
                mcp_python=MCP_PYTHON,
                mcp_source_root=MCP_ROOT / "src",
                stata_home=STATA_HOME,
            ),
        ),
    )

    result = runner.run_trial(manifest, scenario, 1)

    assert result.status is TrialStatus.COMPLETED
    assert result.task_outcome is TaskOutcome.PASS
    assert all(score.verdict.value == "pass" for score in result.scores)
    report = json.loads(
        (
            experiment
            / "trials"
            / result.trial_id
            / "bundle"
            / "replication-report.json"
        ).read_text(encoding="utf-8")
    )
    assert report["receipt"]["execution_status"] == "succeeded"
    assert report["receipt"]["rc"] == 0
    assert report["markers"][expected_marker] == expected_value
    assert all(output["exists"] for output in report["outputs"].values())


@pytest.mark.parametrize(
    "scenario_filename",
    (
        "replication-powerful-experiments-full-package-v1.json",
        "replication-jel-did-table2-simple-did-v1.json",
    ),
)
def test_public_package_scenarios_freeze_every_fixture(scenario_filename: str) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_ROOT / scenario_filename)

    assert scenario.reference.status == "verified"
    assert all(len(fixture.sha256) == 64 for fixture in scenario.fixtures)
    assert "replication.required_artifacts_created" in scenario.required_outcomes
