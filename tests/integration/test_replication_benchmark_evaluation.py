"""Real public-paper replication benchmark through Stata MCP and Product Evaluation."""

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
    RadicalReformReplicationAdapter,
    ReplicationOracleGrader,
)

ROOT = Path(__file__).parents[2]
if not (ROOT / "verification" / "replication-benchmarks").is_dir():
    pytest.skip(
        "optional frozen public replication package is not installed",
        allow_module_level=True,
    )
SCENARIO_PATH = (
    ROOT
    / "verification"
    / "evaluation-scenarios"
    / "replication-radical-reform-table3-column1-v1.json"
)
MCP_ROOT = Path.home() / "stata-mcp"
MCP_PYTHON = MCP_ROOT / ".venv-uv" / "Scripts" / "python.exe"
STATA_HOME = Path(r"C:\Program Files\Stata18")


def _manifest() -> ExperimentManifest:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    return ExperimentManifest.create(
        experiment_id="replication-radical-reform-eval",
        created_at="2026-09-21T00:00:00Z",
        harness_revision="product-eval/v1",
        environment_revision="windows11-stata18/v1",
        stata_revision="stata18-mp-version-9.2",
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


def test_public_paper_replication_matches_numeric_and_rtf_oracle(tmp_path: Path) -> None:
    if not (MCP_PYTHON.is_file() and STATA_HOME.is_dir()):
        pytest.skip("certified local Stata MCP environment is not installed")
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)
    manifest = _manifest()
    store = FilesystemEvaluationRunStore(tmp_path / "runs")
    experiment = store.create_experiment(manifest)
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry((ObservationContractGrader(), ReplicationOracleGrader())),
        (
            RadicalReformReplicationAdapter(
                ROOT,
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
    trial_root = experiment / "trials" / result.trial_id
    report = json.loads(
        (trial_root / "bundle" / "replication-report.json").read_text(encoding="utf-8")
    )
    assert report["receipt"]["execution_status"] == "succeeded"
    assert report["receipt"]["rc"] == 0
    assert report["markers"]["N"] == 74
    assert report["markers"]["NUMBER_OF_STATES"] == 13
    assert report["markers"]["B_fpresence1900"] == pytest.approx(0.6338688525)
    assert report["rtf"]["header_valid"] is True
    assert report["rtf"]["size_bytes"] >= 512


def test_replication_scenario_is_frozen_to_public_source_hashes() -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_PATH)

    fixture_roles = {fixture.role for fixture in scenario.fixtures}
    assert fixture_roles == {
        "replication_do",
        "replication_main_data",
        "replication_table5_data",
        "replication_harness",
        "replication_reference",
    }
    assert scenario.reference.status == "verified"
    assert scenario.trial_policy.trials == 1
