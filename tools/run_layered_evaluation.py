"""Run and persist the current L1/L2 evaluation suite plus the retained L3 record.

This command intentionally reports coverage gaps as ``not_evaluated``.  Unit tests and
release-gate test counts are not converted into Agent quality scores.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from statistics import fmean
from typing import Any

from stata_research_agent.application.product_evaluation import (
    ExperimentManifest,
    ObservationContractGrader,
    SystemUnderTestSnapshot,
    TrialGraderRegistry,
)
from stata_research_agent.interfaces.context_evaluation_adapter import (
    ContextCompilerEvaluationAdapter,
    ContextCompilerReportGrader,
)
from stata_research_agent.interfaces.evaluation_scenario import EvaluationScenarioLoader
from stata_research_agent.interfaces.memory_evaluation_adapter import (
    MemoryCorrectionEvaluationAdapter,
    MemoryIntrinsicQualityGrader,
)
from stata_research_agent.interfaces.product_evaluation_runner import (
    FilesystemEvaluationRunStore,
    ProductEvaluationTrialRunner,
)
from stata_research_agent.interfaces.rag_evaluation_adapter import (
    RagIntrinsicEvaluationAdapter,
    RagRetrievalReportGrader,
)
from stata_research_agent.interfaces.trajectory_evaluation_adapter import (
    TurnLoopTrajectoryEvaluationAdapter,
    TurnLoopTrajectoryGrader,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = (
    ROOT / "verification/evaluation-scenarios/memory-correction-v1.json",
    ROOT / "verification/evaluation-scenarios/rag-corpus-isolation-v1.json",
    ROOT / "verification/evaluation-scenarios/context-compiler-v1.json",
    ROOT / "verification/evaluation-scenarios/turn-loop-stop-guard-success-v1.json",
)
L3_REPORTS = (
    ROOT / "verification/runs/live-replication-radical-20260921-v7/live-replication-report.json",
    ROOT / "verification/runs/live-replication-jel-did-20260921-v4/live-replication-report.json",
    ROOT / "verification/runs/live-replication-powerful-20260921-v10/live-replication-report.json",
)


def _digest(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _mean(values: list[float]) -> float:
    return fmean(values) if values else 0.0


def _trial_reports(experiment_root: Path, filename: str) -> list[dict[str, Any]]:
    return [
        _read_json(path)
        for path in sorted((experiment_root / "trials").glob(f"*/bundle/{filename}"))
    ]


def _case_metrics(experiment_root: Path, scenario_id: str) -> dict[str, Any]:
    return _read_json(experiment_root / "case-metrics" / f"{scenario_id}.json")


def _l1_report(experiment_root: Path) -> dict[str, Any]:
    memory = _trial_reports(experiment_root, "memory-report.json")
    rag = _trial_reports(experiment_root, "rag-report.json")
    context = _trial_reports(experiment_root, "context-report.json")
    evaluated = {
        "memory": {
            "status": "partial",
            "evaluated_case_status": "pass",
            "coverage_note": "one intrinsic correction case; no extrinsic ablation yet",
            "trials": len(memory),
            "pass_at_1": _case_metrics(experiment_root, "agent.memory.correction")["pass_at_1"],
            "pass_power_k": _case_metrics(experiment_root, "agent.memory.correction")[
                "pass_power_k"
            ],
            "metrics": {
                key: _mean([float(item["metrics"][key]) for item in memory])
                for key in (
                    "activation_precision",
                    "source_accuracy",
                    "stale_suppression_rate",
                    "false_active_memory_count",
                )
            },
        },
        "rag": {
            "status": "partial",
            "evaluated_case_status": "pass",
            "coverage_note": "one intrinsic corpus-isolation case; no L3 ablation yet",
            "trials": len(rag),
            "pass_at_1": _case_metrics(experiment_root, "agent.rag.corpus-isolation")["pass_at_1"],
            "pass_power_k": _case_metrics(experiment_root, "agent.rag.corpus-isolation")[
                "pass_power_k"
            ],
            "metrics": {
                key: _mean([float(item[key]) for item in rag])
                for key in (
                    "macro_recall_at_k",
                    "macro_precision_at_k",
                    "mean_reciprocal_rank",
                    "macro_evidence_group_coverage",
                    "total_role_leaks",
                    "no_answer_hit_count",
                )
            },
        },
        "context": {
            "status": "partial",
            "evaluated_case_status": "pass",
            "coverage_note": "one intrinsic compiler-boundary case; no long-context ablation yet",
            "trials": len(context),
            "pass_at_1": _case_metrics(experiment_root, "agent.context.compiler-boundaries")[
                "pass_at_1"
            ],
            "pass_power_k": _case_metrics(experiment_root, "agent.context.compiler-boundaries")[
                "pass_power_k"
            ],
            "metrics": {
                "check_pass_rate": {
                    key: _mean([1.0 if item["checks"][key] else 0.0 for item in context])
                    for key in context[0]["checks"]
                },
                "mean_estimated_input_tokens": _mean(
                    [float(item["estimated_input_tokens"]) for item in context]
                ),
            },
        },
    }
    for subsystem in ("plan", "tool", "evaluator", "gateway"):
        evaluated[subsystem] = {
            "status": "not_evaluated",
            "reason": "no dedicated L1 benchmark and retained metric dataset yet",
        }
    return {
        "status": "partial",
        "definition": "Memory / RAG / Context / Plan / Tool / Evaluator / Gateway",
        "coverage": {"evaluated": 3, "required": 7, "ratio": 3 / 7},
        "subsystems": evaluated,
    }


def _l2_report(experiment_root: Path) -> dict[str, Any]:
    reports = _trial_reports(experiment_root, "trajectory-report.json")
    case = _case_metrics(experiment_root, "agent.trajectory.turn-loop-success")
    return {
        "status": "partial",
        "definition": "Turn → Step → Tool → Observation → Replan → Stop",
        "coverage": {
            "evaluated_cases": 1,
            "covered_slices": ["happy_path", "tool_admission", "evidence_then_stop"],
            "missing_slices": [
                "tool_failure_then_replan",
                "waiting_and_resume",
                "pause_and_continuation",
                "no_progress_termination",
                "provider_retry_and_fallback",
                "crash_recovery_and_reconciliation",
            ],
        },
        "metrics": {
            "trials": len(reports),
            "task_success_rate": case["pass_at_1"],
            "continuous_success_rate_k": case["pass_power_k"],
            "infrastructure_error_rate": case["infrastructure_errors"] / case["requested_trials"],
            "mean_steps_to_success": _mean(
                [float(item["outcome"]["executed_steps"]) for item in reports]
            ),
            "mean_tool_executions": _mean(
                [float(item["outcome"]["tool_executions"]) for item in reports]
            ),
            "premature_stop_rate": _mean(
                [
                    0.0
                    if item["stop_guard"]["terminal_disposition"] == "succeed"
                    and item["goal_coverage"]["coverage_status"] == "satisfied"
                    and item["tool_results"]
                    else 1.0
                    for item in reports
                ]
            ),
            "admission_before_execution_rate": _mean(
                [
                    1.0 if len(item["admissions"]) == len(item["operations"]) == 1 else 0.0
                    for item in reports
                ]
            ),
        },
    }


def _l3_report() -> dict[str, Any]:
    retained = [_read_json(path) for path in L3_REPORTS if path.is_file()]
    required = sum(int(item["hidden_oracle"]["required_count"]) for item in retained)
    matched = sum(int(item["hidden_oracle"]["matched_count"]) for item in retained)
    delivered = sum(bool(item["delivery"]["delivered"]) for item in retained)
    retained_cases_pass = len(retained) == 3 and all(
        item["verdict"] == "pass" for item in retained
    )
    return {
        "status": "partial",
        "evaluated_case_status": "pass" if retained_cases_pass else "partial",
        "definition": "idea + data → controllable, faithful, reproducible research + Word",
        "verified_dimensions": [
            "live_agent_completion",
            "hidden_numeric_oracle",
            "stata_result_provenance",
            "word_delivery",
        ],
        "not_yet_common_dimensions": [
            "researcher_control_interventions",
            "clean_environment_reproduction",
            "multi_trial_reliability",
        ],
        "metrics": {
            "retained_live_scenarios": len(retained),
            "scenario_success_rate": (
                sum(item["verdict"] == "pass" for item in retained) / len(retained)
                if retained
                else 0.0
            ),
            "hidden_oracle_accuracy": matched / required if required else 0.0,
            "hidden_oracle_matched": matched,
            "hidden_oracle_required": required,
            "word_delivery_rate": delivered / len(retained) if retained else 0.0,
            "total_steps": sum(int(item["counts"]["steps"]) for item in retained),
            "total_provider_attempts": sum(
                int(item["counts"]["provider_attempts"]) for item in retained
            ),
            "total_stata_runs": sum(int(item["counts"]["stata_runs"]) for item in retained),
        },
        "source_reports": [str(path.relative_to(ROOT)) for path in L3_REPORTS if path.is_file()],
        "limitations": [
            "three retained live scenarios are one trial each, so reliability is not established",
            (
                "control interventions and clean-environment rerun are not yet common "
                "metrics across all three"
            ),
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=ROOT / "verification/runs")
    parser.add_argument("--experiment-id")
    args = parser.parse_args()
    now = datetime.now(UTC)
    experiment_id = args.experiment_id or now.strftime("layered-evaluation-%Y%m%dT%H%M%SZ")
    scenarios = tuple(EvaluationScenarioLoader().load(path) for path in SCENARIOS)
    manifest = ExperimentManifest.create(
        experiment_id=experiment_id,
        created_at=now.isoformat().replace("+00:00", "Z"),
        harness_revision="layered-evaluation/v1",
        environment_revision="windows11-local/v1",
        stata_revision="stata18-mp",
        scenarios=scenarios,
        system_under_test=SystemUnderTestSnapshot(
            "working-tree",
            "mixed-deterministic-and-retained-live",
            "deterministic-fixtures",
            _digest("layered-evaluation-model-parameters/v1"),
            "system-prompt/current",
            _digest("skill-snapshot/current"),
            "tool-catalog/current",
            "rag-policy/current",
            "memory-policy/current",
            "context-policy/current",
        ),
    )
    store = FilesystemEvaluationRunStore(args.output_root)
    experiment_root = store.create_experiment(manifest)
    python = ROOT / ".venv/Scripts/python.exe"
    runner = ProductEvaluationTrialRunner(
        store,
        TrialGraderRegistry(
            (
                ObservationContractGrader(),
                MemoryIntrinsicQualityGrader(),
                RagRetrievalReportGrader(),
                ContextCompilerReportGrader(),
                TurnLoopTrajectoryGrader(),
            )
        ),
        (
            MemoryCorrectionEvaluationAdapter(),
            RagIntrinsicEvaluationAdapter(ROOT),
            ContextCompilerEvaluationAdapter(ROOT),
            TurnLoopTrajectoryEvaluationAdapter(ROOT, python),
        ),
    )
    runner.run_experiment(manifest, scenarios)
    layered = {
        "schema_version": "stata-research-agent.layered-evaluation-report/v1",
        "experiment_id": experiment_id,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "overall_status": "incomplete",
        "status_reason": (
            "L1 lacks dedicated Plan/Tool/Evaluator/Gateway benchmarks and L2 only covers "
            "the successful trajectory; passing tests are not counted as evaluation metrics."
        ),
        "layers": {
            "L1": _l1_report(experiment_root),
            "L2": _l2_report(experiment_root),
            "L3": _l3_report(),
        },
    }
    report_path = experiment_root / "layered-evaluation-report.json"
    report_path.write_text(
        json.dumps(layered, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(report_path)
    print(json.dumps(layered, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
