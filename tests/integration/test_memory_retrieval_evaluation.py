"""Product Evaluation for exact, recommendation-style Project Memory recall."""

from __future__ import annotations

import json
from pathlib import Path

from stata_research_agent.application.product_evaluation import (
    ObservationContractGrader,
    ScoreVerdict,
    TrialGraderRegistry,
)
from stata_research_agent.interfaces.evaluation_scenario import EvaluationScenarioLoader
from stata_research_agent.interfaces.memory_retrieval_evaluation_adapter import (
    MemoryRetrievalEvaluationAdapter,
    MemoryRetrievalQualityGrader,
)

ROOT = Path(__file__).parents[2]
SCENARIO = ROOT / "verification/evaluation-scenarios/memory-retrieval-v2.json"


def test_memory_retrieval_v2_runs_production_search_and_exact_open(tmp_path: Path) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO)
    adapter = MemoryRetrievalEvaluationAdapter()
    bundle = adapter.execute(
        scenario,
        trial_id="memory-retrieval-v2-001",
        trial_root=tmp_path / "trial",
    )

    assert set(scenario.required_outcomes) <= set(bundle.observations)
    assert not set(scenario.forbidden_outcomes).intersection(bundle.observations)
    report_ref = next(
        item
        for item in bundle.evaluation_payloads
        if item.payload_id == "memory-retrieval-report"
    )
    report = json.loads(report_ref.path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "stata-research-agent.memory-retrieval-report/v2"
    assert report["metrics"]["recall_at_k"] == 1.0
    assert report["metrics"]["mean_reciprocal_rank"] == 1.0
    assert report["metrics"]["irrelevant_silence_rate"] == 1.0
    assert report["metrics"]["supersession_forwarding_accuracy"] == 1.0
    assert report["metrics"]["cross_path_precision"] == 1.0
    assert report["metrics"]["exact_open_accuracy"] == 1.0
    assert report["metrics"]["hybrid_semantic_recall"] == 1.0
    assert report["metrics"]["lexical_semantic_recall"] == 0.0
    assert report["metrics"]["source_neighbor_recall"] == 1.0
    assert report["metrics"]["tampered_open_rejection_rate"] == 1.0

    scores = TrialGraderRegistry(
        (ObservationContractGrader(), MemoryRetrievalQualityGrader())
    ).grade(scenario, bundle)
    assert [score.verdict for score in scores] == [ScoreVerdict.PASS, ScoreVerdict.PASS]


def test_memory_retrieval_report_retains_case_level_ranks_reasons_and_latency(
    tmp_path: Path,
) -> None:
    scenario = EvaluationScenarioLoader().load(SCENARIO)
    bundle = MemoryRetrievalEvaluationAdapter().execute(
        scenario,
        trial_id="memory-retrieval-v2-details",
        trial_root=tmp_path / "trial",
    )
    report = json.loads(bundle.evaluation_payloads[0].path.read_text(encoding="utf-8"))
    cases = {item["case_id"]: item for item in report["cases"]}

    assert cases["superseded-tail-rule"]["returned_keys"][0] == "current_tail_rule"
    assert "superseded_match_forwarded" in cases["superseded-tail-rule"][
        "retrieval_reasons"
    ]["current_tail_rule"]
    assert "branch_outcome" not in cases["cross-path-isolation"]["returned_keys"]
    assert cases["irrelevant-abstention"]["candidate_count"] == 0
    assert all(item["latency_ms"] >= 0 for item in cases.values())
