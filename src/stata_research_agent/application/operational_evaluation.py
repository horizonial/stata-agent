"""Operational evaluation contracts computed from ordinary product usage.

These snapshots never turn missing denominators into a perfect score.  Metrics are either
observed, deterministically gated, not applicable, or unknown.  Benchmark gold data belongs to
the separate Product Evaluation harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OperationalMetricStatus = Literal["pass", "warn", "fail", "observed", "not_applicable", "unknown"]
OperationalLayer = Literal["L1", "L2", "L3"]


@dataclass(frozen=True, slots=True)
class OperationalMetric:
    metric_id: str
    layer: OperationalLayer
    subsystem: str
    status: OperationalMetricStatus
    value: float | None
    numerator: float | None
    denominator: float | None
    unit: str
    explanation: str
    source_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperationalLayerSnapshot:
    layer: OperationalLayer
    status: Literal["pass", "warn", "fail", "observed", "not_applicable"]
    metrics: tuple[OperationalMetric, ...]
    hard_failure_count: int
    warning_count: int


@dataclass(frozen=True, slots=True)
class OperationalBreakdownItem:
    key: str
    count: int


@dataclass(frozen=True, slots=True)
class OperationalBreakdown:
    breakdown_id: str
    layer: OperationalLayer
    subsystem: str
    items: tuple[OperationalBreakdownItem, ...]
    explanation: str
    source_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperationalConfigurationSlice:
    system_prompt_revision: str | None
    main_skill_name: str | None
    main_skill_revision: str | None
    tool_catalog_revision: str | None
    model_policy_revisions: tuple[str, ...]
    provider_kinds: tuple[str, ...]
    model_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TurnOperationalEvaluationSnapshot:
    schema_version: str
    policy_revision: str
    authoritative_revision: int
    workspace_id: str
    turn_id: str
    turn_status: str
    research_path_id: str
    created_at: str
    terminal_at: str | None
    configuration: OperationalConfigurationSlice
    layers: tuple[OperationalLayerSnapshot, ...]
    breakdowns: tuple[OperationalBreakdown, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceOperationalEvaluationSnapshot:
    schema_version: str
    policy_revision: str
    authoritative_revision: int
    workspace_id: str
    turns: tuple[TurnOperationalEvaluationSnapshot, ...]
    layers: tuple[OperationalLayerSnapshot, ...]
    breakdowns: tuple[OperationalBreakdown, ...]
