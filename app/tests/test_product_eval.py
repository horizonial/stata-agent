"""Product-level, offline evaluation gates.

These tests intentionally call the same command implementation used by CI so
the release gate cannot drift into a test-only helper.
"""

from __future__ import annotations

import json
from pathlib import Path

from stata_agent.eval.runner import load_golden, main, run_product_eval


def test_product_eval_all_scenarios_pass() -> None:
    report = run_product_eval()
    assert report["ok"] is True, report
    assert report["summary"] == {"total": 7, "passed": 7, "failed": 0}
    assert set(report["scenarios"]) == {
        "L1_TOOL_ROUTING",
        "L2_RUN_FSM_RECOVERY",
        "L3_EVIDENCE_GROUNDING",
        "L4_FAKE_TO_DRAFT",
        "L5_CONTEXT_MEMORY_QUALITY",
        "L6_PRODUCT_TRUST_OPERATIONS",
        "L7_OPERATOR_GOVERNANCE",
    }


def test_product_eval_json_cli_has_machine_exit_and_summary(capsys) -> None:
    exit_code = main(["--json", "--scenario", "L4_FAKE_TO_DRAFT"])
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["summary"] == {"total": 1, "passed": 1, "failed": 0}
    assert payload["scenarios"]["L4_FAKE_TO_DRAFT"]["observed"]["draft_written"] is True


def test_l5_context_memory_quality_gate_is_green_and_repeatable() -> None:
    first = run_product_eval(["L5_CONTEXT_MEMORY_QUALITY"])
    second = run_product_eval(["L5_CONTEXT_MEMORY_QUALITY"])
    assert first == second
    observed = first["scenarios"]["L5_CONTEXT_MEMORY_QUALITY"]["observed"]
    assert observed["ledger_event_count"] >= 100
    assert observed["compaction_count"] >= 2
    assert observed["budget_compliance"] == 100
    assert observed["must_keep_recall"] == 100
    assert observed["forbidden_injection"] == 0
    assert observed["semantic_unit_completeness"] == 100
    assert observed["checkpoint_fidelity"] == 100
    assert observed["checkpoint_recovery"] == 100
    assert observed["memory_precision"] == 100
    assert observed["provider_overflow_retries"] == 1
    assert observed["review_duplicate_count"] == 0


def test_l6_product_trust_operations_gate_is_green_and_repeatable() -> None:
    first = run_product_eval(["L6_PRODUCT_TRUST_OPERATIONS"])
    second = run_product_eval(["L6_PRODUCT_TRUST_OPERATIONS"])
    assert first == second
    observed = first["scenarios"]["L6_PRODUCT_TRUST_OPERATIONS"]["observed"]
    assert observed["provider_unknown_denied"] is True
    assert observed["provider_attempt_cap"] is True
    assert observed["provider_attempts"] == 2
    assert observed["attachment_ready_only"] is True
    assert observed["attachment_non_ready_blocked"] is True
    assert observed["attachment_foreign_hidden"] is True
    assert observed["attachment_cross_workspace_blocked"] is True
    assert observed["attachment_safe_projection"] is True
    assert observed["backup_verified"] is True
    assert observed["backup_tamper_rejected"] is True


def test_l7_operator_governance_gate_is_green_and_repeatable() -> None:
    first = run_product_eval(["L7_OPERATOR_GOVERNANCE"])
    second = run_product_eval(["L7_OPERATOR_GOVERNANCE"])
    assert first == second
    observed = first["scenarios"]["L7_OPERATOR_GOVERNANCE"]["observed"]
    assert observed["tool_card_count"] == 2
    assert observed["tool_correlation_ok"] is True
    assert observed["tool_failure_code"] == "timeout"
    assert observed["attachment_safe_projection"] is True
    assert observed["failure_safe_projection"] is True
    assert observed["skill_staged"] is True
    assert observed["skill_wrong_hash_rejected"] is True
    assert observed["skill_active"] is True
    assert observed["skill_tamper_rejected"] is True
    assert observed["skill_tamper_rejection_code"] == "candidate_integrity"


def test_golden_is_structured_and_excludes_run_metadata() -> None:
    payload = load_golden()
    assert payload["schema_version"] == 1
    assert set(payload["scenarios"]) == {
        "L1_TOOL_ROUTING",
        "L2_RUN_FSM_RECOVERY",
        "L3_EVIDENCE_GROUNDING",
        "L4_FAKE_TO_DRAFT",
        "L5_CONTEXT_MEMORY_QUALITY",
        "L6_PRODUCT_TRUST_OPERATIONS",
        "L7_OPERATOR_GOVERNANCE",
    }
    text = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in ("created_at", "event_id", "absolute_path", "timestamp", "uuid"):
        assert forbidden not in text


def test_packaged_golden_matches_checkout_copy() -> None:
    app_root = Path(__file__).resolve().parents[1]
    checkout = json.loads(
        (app_root / "eval_golden" / "scenarios.json").read_text(encoding="utf-8")
    )
    packaged = json.loads(
        (app_root / "src" / "stata_agent" / "eval" / "golden.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkout == packaged
