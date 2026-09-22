"""Blind Agent challenge inputs stay separated from replication judges."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stata_research_agent.interfaces.evaluation_scenario import EvaluationScenarioLoader
from stata_research_agent.interfaces.replication_challenge import (
    JEL_DID_AGENT_CHALLENGE,
    POWERFUL_EXPERIMENTS_AGENT_CHALLENGE,
    RADICAL_REFORM_AGENT_CHALLENGE,
    ReplicationChallengeMaterializer,
    ReplicationChallengeSpec,
)

ROOT = Path(__file__).parents[2]
SCENARIO_ROOT = ROOT / "verification" / "evaluation-scenarios"
if not (ROOT / "verification" / "replication-benchmarks").is_dir():
    pytest.skip(
        "optional frozen public replication packages are not installed",
        allow_module_level=True,
    )


@pytest.mark.parametrize(
    ("scenario_filename", "challenge"),
    (
        (
            "replication-radical-reform-table3-column1-v1.json",
            RADICAL_REFORM_AGENT_CHALLENGE,
        ),
        (
            "replication-powerful-experiments-full-package-v1.json",
            POWERFUL_EXPERIMENTS_AGENT_CHALLENGE,
        ),
        (
            "replication-jel-did-table2-simple-did-v1.json",
            JEL_DID_AGENT_CHALLENGE,
        ),
    ),
)
def test_materialized_challenge_excludes_harness_and_oracle(
    tmp_path: Path,
    scenario_filename: str,
    challenge: ReplicationChallengeSpec,
) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO_ROOT / scenario_filename)

    materialized = ReplicationChallengeMaterializer(ROOT).materialize(
        scenario,
        challenge,
        tmp_path / "challenge",
    )

    manifest = json.loads(materialized.manifest_path.read_text(encoding="utf-8"))
    manifest_roles = {item["role"] for item in manifest["inputs"]}
    assert manifest_roles == set(challenge.visible_fixture_destinations)
    assert "replication_harness" not in manifest_roles
    assert "replication_reference" not in manifest_roles
    assert not tuple(materialized.root.rglob("*reference*.json"))
    assert not tuple(materialized.root.rglob("harness"))
    assert all(
        (materialized.root / relative).is_file()
        for relative in challenge.visible_fixture_destinations.values()
    )
