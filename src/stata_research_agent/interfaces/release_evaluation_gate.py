"""Unified, machine-readable Research Agent release evaluation gate."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EvaluationDimension:
    name: str
    test_nodes: tuple[str, ...]
    allow_skips: bool = False


CORE_DIMENSIONS = (
    EvaluationDimension(
        "autonomous_research",
        (
            "tests/vertical/test_agent_turn_driver_vertical.py::test_autonomous_two_step_turn_reaches_stop_guard_success",
            "tests/integration/test_agent_research_to_word_vertical.py::test_real_agent_turn_delivers_traceable_word_without_user_intervention",
        ),
    ),
    EvaluationDimension(
        "researcher_control",
        (
            "tests/vertical/test_agent_turn_driver_vertical.py::test_supervised_turn_waits_when_evaluation_requires_researcher_decision",
            "tests/vertical/test_tool_broker_vertical.py::test_user_pause_blocks_new_admission_and_cancels_pre_handoff_operation",
        ),
    ),
    EvaluationDimension(
        "data_fidelity_and_lineage",
        (
            "tests/vertical/test_broker_stata_bridge_vertical.py::test_admitted_stata_operation_is_handed_off_finalized_and_resolved",
            "tests/vertical/test_result_profile_vertical.py::test_formal_evidence_render_has_complete_numeric_coverage_and_lineage",
            "tests/vertical/test_result_profile_vertical.py::test_unbound_numeric_literal_cannot_commit_formal_block",
            "tests/vertical/test_analysis_output_promotion_vertical.py::test_python_regression_requires_exact_later_user_confirmation_before_adoption",
        ),
    ),
    EvaluationDimension(
        "crash_recovery",
        (
            "tests/vertical/test_stata_operation_vertical.py::test_finalization_response_loss_replays_committed_manifest_without_reexecution",
            "tests/vertical/test_stata_operation_vertical.py::test_recovery_scan_classifies_published_completion_without_finalization",
        ),
    ),
    EvaluationDimension(
        "retrieval_grounding_and_safety",
        (
            "tests/vertical/test_model_gateway_vertical.py::test_untrusted_retrieval_is_frozen_as_data_only_context",
            "tests/unit/test_rag_evaluation.py::test_grounded_answer_requires_valid_support_and_does_not_treat_style_as_fact",
            "tests/unit/test_rag_evaluation.py::test_multi_hop_trace_requires_novel_evidence_and_explicit_completion",
        ),
    ),
    EvaluationDimension(
        "runtime_resilience",
        (
            "tests/vertical/test_agent_turn_driver_vertical.py::test_turn_deadline_blocks_new_tool_admission_after_model_returns",
            "tests/integration/test_turn_runtime_budget.py::test_runtime_budget_is_frozen_accumulated_and_reloaded",
            "tests/vertical/test_model_gateway_vertical.py::test_retry_after_jitter_and_fallback_route_remain_one_invocation",
            "tests/vertical/test_model_gateway_vertical.py::test_open_circuit_fails_without_a_second_network_dispatch",
            "tests/vertical/test_model_gateway_vertical.py::test_delivery_unknown_is_terminal_and_never_automatically_retried",
        ),
    ),
    EvaluationDimension(
        "memory_tool_and_judge_quality",
        (
            "tests/unit/test_agent_benchmarks.py::test_memory_quality_benchmark_scores_activation_sources_and_false_facts",
            "tests/integration/test_memory_quality_lifecycle.py::test_memory_remains_precise_across_correction_and_many_conversations",
            "tests/integration/test_project_memory.py::test_inferred_memory_requires_activation",
            "tests/unit/test_agent_benchmarks.py::test_tool_selection_benchmark_accepts_equivalent_registered_tool_choice",
            "tests/unit/test_production_tool_catalog.py::test_production_tool_schemas_include_use_boundaries",
            "tests/unit/test_agent_benchmarks.py::test_dataset_loader_rejects_cross_split_prompt_leakage",
            "tests/unit/test_agent_benchmarks.py::test_judge_stability_requires_repeatable_majority_and_unanimity",
        ),
    ),
    EvaluationDimension(
        "retrieval_index_upgrade",
        (
            "tests/unit/test_dense_index_upgrade.py::test_dense_upgrade_requires_no_case_regression_before_adoption",
        ),
    ),
    EvaluationDimension(
        "provider_delta_streaming",
        (
            "tests/unit/test_openai_compatible_transport.py::test_streaming_transport_emits_ephemeral_deltas_and_returns_final_output",
            "tests/unit/test_ephemeral_model_delta_hub.py::test_ephemeral_delta_hub_is_scoped_and_drops_oldest_under_backpressure",
        ),
    ),
    EvaluationDimension(
        "multilingual_contract_integrity",
        (
            "tests/unit/test_openai_compatible_transport.py::test_transport_preserves_mixed_language_research_context_and_output",
            "tests/unit/test_table_document_rules.py::test_manuscript_roundtrip_preserves_chinese_and_latin_identifiers",
        ),
    ),
)

FULL_ONLY_DIMENSIONS = (
    EvaluationDimension(
        "real_stata_session",
        (
            "tests/integration/test_real_stata_mcp_runtime.py::test_real_stdio_runtime_executes_and_closes_one_stata_session",
        ),
    ),
    EvaluationDimension(
        "real_data_fidelity",
        (
            "tests/integration/test_real_auto_capture.py::test_real_stata18_auto_dta_is_captured_byte_for_byte",
        ),
    ),
    EvaluationDimension(
        "real_stata_table_export",
        (
            "tests/integration/test_real_stata_mcp_runtime.py::test_real_esttab_nested_artifact_contract_accepts_stored_estimate",
        ),
    ),
    EvaluationDimension(
        "real_parallel_workspaces",
        (
            "tests/integration/test_cross_workspace_real_stata.py::test_two_workspaces_can_hold_isolated_real_stata_sessions_and_workdirs",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class DimensionResult:
    name: str
    status: str
    tests: int
    failures: int
    errors: int
    skipped: int
    duration_seconds: float
    command: tuple[str, ...]
    output_tail: str


@dataclass(frozen=True, slots=True)
class ReleaseEvaluationReport:
    schema_version: str
    profile: str
    manifest_sha256: str
    passed: bool
    dimensions: tuple[DimensionResult, ...]
    duration_seconds: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True)


class ReleaseEvaluationGate:
    def __init__(self, project_root: Path, *, python_executable: Path | None = None) -> None:
        self._project_root = project_root.resolve()
        self._python = (python_executable or Path(sys.executable)).resolve()

    def run(self, profile: str = "core") -> ReleaseEvaluationReport:
        if profile not in {"core", "full"}:
            raise ValueError("release evaluation profile must be core or full")
        dimensions = CORE_DIMENSIONS + (FULL_ONLY_DIMENSIONS if profile == "full" else ())
        manifest = json.dumps(
            [asdict(item) for item in dimensions],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="stata-agent-release-gate-") as temporary:
            output_root = Path(temporary)
            results = tuple(
                self._run_dimension(item, output_root / f"{item.name}.xml")
                for item in dimensions
            )
        return ReleaseEvaluationReport(
            "research-agent-release-evaluation/v1",
            profile,
            hashlib.sha256(manifest).hexdigest(),
            all(item.status == "passed" for item in results),
            results,
            round(time.monotonic() - started, 3),
        )

    def _run_dimension(self, dimension: EvaluationDimension, junit_path: Path) -> DimensionResult:
        command = (
            str(self._python),
            "-m",
            "pytest",
            "-q",
            *dimension.test_nodes,
            f"--junitxml={junit_path}",
        )
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=self._project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        duration = round(time.monotonic() - started, 3)
        tests = failures = errors = skipped = 0
        if junit_path.exists():
            root = ET.parse(junit_path).getroot()
            cases = tuple(root.iter("testcase"))
            tests = len(cases)
            failures = sum(case.find("failure") is not None for case in cases)
            errors = sum(case.find("error") is not None for case in cases)
            skipped = sum(case.find("skipped") is not None for case in cases)
        passed = (
            completed.returncode == 0
            and tests == len(dimension.test_nodes)
            and failures == 0
            and errors == 0
            and (dimension.allow_skips or skipped == 0)
        )
        combined_output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        return DimensionResult(
            dimension.name,
            "passed" if passed else "failed",
            tests,
            failures,
            errors,
            skipped,
            duration,
            command,
            combined_output[-12_000:],
        )
