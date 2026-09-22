"""Strict Founder Scenario manifest parsing and candidate binding semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stata_research_agent.application.founder_acceptance import FounderAcceptanceError
from stata_research_agent.interfaces.founder_scenario import FounderScenarioLoader

PROJECT_ROOT = Path(__file__).parents[2]
SCENARIO = PROJECT_ROOT / "verification" / "scenarios" / "founder-autonomous-auto-v1.json"


def test_checked_in_autonomous_founder_scenario_is_versioned_and_exact() -> None:
    scenario = FounderScenarioLoader().load(SCENARIO)
    assert scenario.scenario_id == "founder.autonomous.auto"
    assert scenario.version == 1
    assert scenario.exact_candidate_required
    assert scenario.applicability.release_gate == "G7"
    assert len(scenario.scenario_sha256) == 64
    assert "word.numeric_coverage_complete" in scenario.required_observations


def test_founder_scenario_rejects_unknown_fields_and_unbound_candidate(
    tmp_path: Path,
) -> None:
    raw = json.loads(SCENARIO.read_text(encoding="utf-8"))
    raw["surprise"] = True
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(FounderAcceptanceError, match="unknown or missing"):
        FounderScenarioLoader().load(path)

    raw.pop("surprise")
    raw["exact_candidate_required"] = False
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(FounderAcceptanceError, match="exact candidate"):
        FounderScenarioLoader().load(path)
