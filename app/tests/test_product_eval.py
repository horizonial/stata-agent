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
    assert report["summary"] == {"total": 4, "passed": 4, "failed": 0}
    assert set(report["scenarios"]) == {
        "L1_TOOL_ROUTING",
        "L2_RUN_FSM_RECOVERY",
        "L3_EVIDENCE_GROUNDING",
        "L4_FAKE_TO_DRAFT",
    }


def test_product_eval_json_cli_has_machine_exit_and_summary(capsys) -> None:
    exit_code = main(["--json", "--scenario", "L4_FAKE_TO_DRAFT"])
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["summary"] == {"total": 1, "passed": 1, "failed": 0}
    assert payload["scenarios"]["L4_FAKE_TO_DRAFT"]["observed"]["draft_written"] is True


def test_golden_is_structured_and_excludes_run_metadata() -> None:
    payload = load_golden()
    assert payload["schema_version"] == 1
    assert set(payload["scenarios"]) == {
        "L1_TOOL_ROUTING",
        "L2_RUN_FSM_RECOVERY",
        "L3_EVIDENCE_GROUNDING",
        "L4_FAKE_TO_DRAFT",
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
