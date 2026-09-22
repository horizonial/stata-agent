from __future__ import annotations

import json
from pathlib import Path

import pytest

from stata_research_agent.application.agent_benchmarks import (
    BenchmarkDataset,
    evaluate_judge_stability,
    evaluate_memory_predictions,
    evaluate_tool_predictions,
)

ROOT = Path(__file__).parents[2]


def test_memory_quality_benchmark_scores_activation_sources_and_false_facts() -> None:
    dataset = BenchmarkDataset.load(
        ROOT / "verification" / "memory-quality-gold.v1.json",
        "memory-quality-benchmark/v1",
    )
    predictions = {
        "memory-test-decision": [
            {
                "kind": "research_decision",
                "content": "Price is the primary outcome.",
                "lifecycle": "active",
                "source_message_id": "msg_test_decision",
            }
        ],
        "memory-test-tentative": [
            {
                "kind": "user_preference",
                "content": "The user may prefer shorter tables.",
                "lifecycle": "proposed",
                "source_message_id": "msg_test_tentative",
            }
        ],
        "memory-test-no-assistant-invention": [],
        "memory-test-correction": [
            {
                "kind": "research_decision",
                "content": "Keep price in levels.",
                "lifecycle": "active",
                "source_message_id": "msg_test_correction",
            }
        ],
    }
    metrics = evaluate_memory_predictions(dataset, predictions)
    assert metrics.passed
    assert metrics.precision == metrics.recall == 1.0
    assert metrics.activation_precision == metrics.source_accuracy == 1.0


def test_tool_selection_benchmark_accepts_equivalent_registered_tool_choice() -> None:
    dataset = BenchmarkDataset.load(
        ROOT / "verification" / "tool-selection-gold.v2.json",
        "tool-selection-benchmark/v2",
    )
    predictions = {
        "tool-test-formal-stata": {
            "decision": "tool",
            "tool_name": "stata.execute",
            "arguments": {
                "code": "reg price weight mpg",
                "dataset_relative_path": "auto.dta",
                "execution_role": "formal_result_candidate",
            },
        },
        "tool-test-python-exploration": {
            "decision": "tool",
            "tool_name": "python.run",
            "arguments": {"code": "fit_exploratory_model()"},
        },
        "tool-test-python-adoption": {
            "decision": "tool",
            "tool_name": "research.adopt_analysis_output",
            "arguments": {
                "analysis_output_id": "analysisoutput_1",
                "expected_output_fingerprint": "a" * 64,
                "preview_artifact_id": "artifact_1",
                "confirmation_summary": "The user confirmed this exact preview.",
            },
        },
        "tool-test-literature": {
            "decision": "tool",
            "tool_name": "research.search_knowledge",
            "arguments": {"query": "parallel trends"},
        },
        "tool-test-help": {
            "decision": "tool",
            "tool_name": "stata.explain_run_error",
            "arguments": {"query": "unknown option"},
        },
        "tool-test-confirm": {"decision": "wait"},
        "tool-test-complete": {"decision": "complete"},
    }
    metrics = evaluate_tool_predictions(dataset, predictions)
    assert metrics.passed
    assert metrics.decision_accuracy == metrics.tool_accuracy == 1.0
    assert metrics.argument_contract_accuracy == 1.0


def test_dataset_loader_rejects_cross_split_prompt_leakage(tmp_path: Path) -> None:
    path = tmp_path / "leaked.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "memory-quality-benchmark/v1",
                "dataset_id": "leaked",
                "cases": [
                    {"case_id": "a", "split": "train", "prompt": "same"},
                    {"case_id": "b", "split": "test", "prompt": " SAME "},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="leakage"):
        BenchmarkDataset.load(path, "memory-quality-benchmark/v1")


def test_judge_stability_requires_repeatable_majority_and_unanimity() -> None:
    stable = evaluate_judge_stability(
        {"a": ("pass", "pass", "pass"), "b": ("fail", "fail", "fail")}
    )
    assert stable.passed
    unstable = evaluate_judge_stability(
        {"a": ("pass", "warn", "pass"), "b": ("fail", "pass", "warn")}
    )
    assert not unstable.passed
