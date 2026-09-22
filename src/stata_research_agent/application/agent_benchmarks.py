"""Versioned quality benchmarks for Memory, Tool selection, and model judges."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


@dataclass(frozen=True, slots=True)
class BenchmarkDataset:
    schema_version: str
    dataset_id: str
    cases: tuple[dict[str, Any], ...]
    content_sha256: str

    @classmethod
    def load(cls, path: Path, expected_schema: str) -> BenchmarkDataset:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != expected_schema:
            raise ValueError("benchmark dataset schema mismatch")
        dataset_id = raw.get("dataset_id")
        cases = raw.get("cases")
        if not isinstance(dataset_id, str) or not dataset_id.strip() or not isinstance(cases, list):
            raise ValueError("invalid benchmark dataset envelope")
        if not cases or any(not isinstance(item, dict) for item in cases):
            raise ValueError("benchmark cases must be non-empty objects")
        case_ids = [item.get("case_id") for item in cases]
        if any(not isinstance(item, str) or not item for item in case_ids):
            raise ValueError("benchmark case_id is required")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("benchmark case_id must be unique")
        cls._assert_split_isolation(cases)
        return cls(
            expected_schema,
            dataset_id,
            tuple(cases),
            hashlib.sha256(_canonical(raw)).hexdigest(),
        )

    @staticmethod
    def _assert_split_isolation(cases: list[dict[str, Any]]) -> None:
        fingerprints: dict[str, str] = {}
        for case in cases:
            split = case.get("split")
            prompt = case.get("prompt")
            if split not in {"train", "test"} or not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("benchmark case requires train/test split and prompt")
            fingerprint = hashlib.sha256(_normalized(prompt).encode("utf-8")).hexdigest()
            previous = fingerprints.get(fingerprint)
            if previous is not None and previous != split:
                raise ValueError("benchmark prompt leakage across train/test splits")
            fingerprints[fingerprint] = split


@dataclass(frozen=True, slots=True)
class MemoryBenchmarkMetrics:
    precision: float
    recall: float
    activation_precision: float
    source_accuracy: float
    forbidden_fact_count: int
    passed: bool


def evaluate_memory_predictions(
    dataset: BenchmarkDataset,
    predictions: dict[str, list[dict[str, Any]]],
    *,
    split: str = "test",
) -> MemoryBenchmarkMetrics:
    true_positive = predicted_count = expected_count = 0
    active_correct = active_count = source_correct = matched_count = forbidden = 0
    for case in dataset.cases:
        if case["split"] != split:
            continue
        expected = case.get("expected_memories", [])
        predicted = predictions.get(str(case["case_id"]), [])
        if not isinstance(expected, list) or not isinstance(predicted, list):
            raise ValueError("memory benchmark expectations and predictions must be arrays")
        expected_count += len(expected)
        predicted_count += len(predicted)
        matched_expected: set[int] = set()
        for prediction in predicted:
            if not isinstance(prediction, dict):
                continue
            content = _normalized(str(prediction.get("content", "")))
            lifecycle = str(prediction.get("lifecycle", ""))
            if lifecycle == "active":
                active_count += 1
            forbidden += sum(
                _normalized(str(marker)) in content for marker in case.get("forbidden_markers", [])
            )
            match_index = next(
                (
                    index
                    for index, item in enumerate(expected)
                    if index not in matched_expected
                    and str(prediction.get("kind")) == str(item.get("kind"))
                    and all(
                        _normalized(str(marker)) in content
                        for marker in item.get("required_markers", [])
                    )
                ),
                None,
            )
            if match_index is None:
                continue
            matched_expected.add(match_index)
            true_positive += 1
            matched_count += 1
            target = expected[match_index]
            if lifecycle == "active" and lifecycle == target.get("lifecycle"):
                active_correct += 1
            if prediction.get("source_message_id") == target.get("source_message_id"):
                source_correct += 1
    precision = true_positive / predicted_count if predicted_count else float(expected_count == 0)
    recall = true_positive / expected_count if expected_count else 1.0
    activation_precision = active_correct / active_count if active_count else 1.0
    source_accuracy = (
        source_correct / matched_count if matched_count else float(expected_count == 0)
    )
    passed = (
        precision >= 0.9
        and recall >= 0.9
        and activation_precision >= 0.95
        and source_accuracy >= 0.95
        and forbidden == 0
    )
    return MemoryBenchmarkMetrics(
        precision, recall, activation_precision, source_accuracy, forbidden, passed
    )


@dataclass(frozen=True, slots=True)
class ToolSelectionMetrics:
    decision_accuracy: float
    tool_accuracy: float
    argument_contract_accuracy: float
    unsafe_tool_count: int
    passed: bool


def evaluate_tool_predictions(
    dataset: BenchmarkDataset,
    predictions: dict[str, dict[str, Any]],
    *,
    split: str = "test",
) -> ToolSelectionMetrics:
    total = decision_correct = tool_cases = tool_correct = argument_correct = unsafe = 0
    for case in dataset.cases:
        if case["split"] != split:
            continue
        total += 1
        expected = case["expected"]
        prediction = predictions.get(str(case["case_id"]), {})
        decision = prediction.get("decision")
        if decision == expected.get("decision"):
            decision_correct += 1
        selected_tool = prediction.get("tool_name")
        if selected_tool in case.get("forbidden_tools", []):
            unsafe += 1
        if expected.get("decision") != "tool":
            continue
        tool_cases += 1
        allowed_tools = expected.get("allowed_tools", [])
        if selected_tool in allowed_tools:
            tool_correct += 1
        arguments = prediction.get("arguments", {})
        required_keys = expected.get("required_argument_keys", [])
        if isinstance(arguments, dict) and all(key in arguments for key in required_keys):
            argument_correct += 1
    decision_accuracy = decision_correct / total if total else 1.0
    tool_accuracy = tool_correct / tool_cases if tool_cases else 1.0
    argument_accuracy = argument_correct / tool_cases if tool_cases else 1.0
    passed = (
        decision_accuracy >= 0.9
        and tool_accuracy >= 0.9
        and argument_accuracy >= 0.9
        and unsafe == 0
    )
    return ToolSelectionMetrics(decision_accuracy, tool_accuracy, argument_accuracy, unsafe, passed)


@dataclass(frozen=True, slots=True)
class JudgeStabilityMetrics:
    unanimous_rate: float
    majority_rate: float
    passed: bool


def evaluate_judge_stability(
    repeated_labels: dict[str, tuple[str, ...]], *, minimum_unanimous_rate: float = 0.8
) -> JudgeStabilityMetrics:
    if not repeated_labels or any(len(labels) < 3 for labels in repeated_labels.values()):
        raise ValueError("judge stability requires at least three labels per case")
    unanimous = majority = 0
    for labels in repeated_labels.values():
        counts = {label: labels.count(label) for label in set(labels)}
        unanimous += len(counts) == 1
        majority += max(counts.values()) > len(labels) / 2
    size = len(repeated_labels)
    unanimous_rate = unanimous / size
    majority_rate = majority / size
    return JudgeStabilityMetrics(
        unanimous_rate,
        majority_rate,
        unanimous_rate >= minimum_unanimous_rate and majority_rate == 1.0,
    )
