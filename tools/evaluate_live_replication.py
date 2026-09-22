"""Evaluate a live Workspace against a hidden official replication oracle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _scalar(connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> int:
    return int(connection.execute(sql, parameters).fetchone()[0])


def _adopted_values(connection: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, float]]:
    rows = connection.execute(
        """
        SELECT slot.canonical_key, adoption.target_result_id, element.semantic_key,
               element.canonical_decimal_text
        FROM result_slots AS slot
        JOIN path_result_adoptions AS adoption USING (result_slot_id)
        JOIN result_elements AS element ON element.result_id = adoption.target_result_id
        WHERE slot.lifecycle = 'active' AND element.value_kind = 'finite'
          AND element.estimate_status = 'estimated'
        """
    ).fetchall()
    return {
        (str(row["canonical_key"]), str(row["semantic_key"])): (
            str(row["target_result_id"]),
            float(row["canonical_decimal_text"]),
        )
        for row in rows
    }


def _observed_oracle(
    values: dict[tuple[str, str], tuple[str, float]], benchmark_id: str, oracle_key: str
) -> tuple[str | None, float | None]:
    direct_keys: dict[str, dict[str, tuple[str, str | None]]] = {
        "radical-reform-aer-2011": {
            "N": ("scalar.N", None),
            "NUMBER_OF_STATES": ("scalar.N_clust", None),
            "DF_ABSORBED": ("scalar.df_a", None),
            "R2": ("scalar.r2", None),
            "JOINT_P": ("scalar.p", "joint"),
        },
        "jel-did-practitioners-guide": {
            "DID_UNWEIGHTED": ("scalar.estimate", "did_unweighted"),
            "DID_WEIGHTED": ("return.scalar.estimate", "did_weighted"),
            "REGRESSION_DID": (
                "term.c.Treat#c.Post.coefficient",
                "reg_interaction_unweighted",
            ),
            "REGRESSION_DID_SE": (
                "term.c.Treat#c.Post.se",
                "reg_interaction_unweighted",
            ),
            "REGRESSION_N": ("scalar.N", "reg_interaction_unweighted"),
            "U02013": (
                "term.c.crude_rate_20_64@0bn.Treat#0bn.Post.coefficient",
                "cell_means_unweighted",
            ),
            "U02014": (
                "term.c.crude_rate_20_64@0bn.Treat#1.Post.coefficient",
                "cell_means_unweighted",
            ),
            "U12013": (
                "term.c.crude_rate_20_64@1.Treat#0bn.Post.coefficient",
                "cell_means_unweighted",
            ),
            "U12014": (
                "term.c.crude_rate_20_64@1.Treat#1.Post.coefficient",
                "cell_means_unweighted",
            ),
            "W02013": (
                "term.c.crude_rate_20_64@0bn.Treat#0bn.Post.coefficient",
                "cell_means_weighted",
            ),
            "W02014": (
                "term.c.crude_rate_20_64@0bn.Treat#1.Post.coefficient",
                "cell_means_weighted",
            ),
            "W12013": (
                "term.c.crude_rate_20_64@1.Treat#0bn.Post.coefficient",
                "cell_means_weighted",
            ),
            "W12014": (
                "term.c.crude_rate_20_64@1.Treat#1.Post.coefficient",
                "cell_means_weighted",
            ),
        },
        "powerful-experiments-wp-2025": {
            "EMPLOYEE_N": ("scalar.N", "employee_summary"),
            "EMPLOYEE_MEAN": ("scalar.mean", "employee_summary"),
            "EMPLOYEE_P50": ("scalar.p50", "employee_summary"),
            "EMPLOYEE_P95": ("scalar.p95", "employee_summary"),
            "ZERO_EXPORTS": ("scalar.N", "zero_exports"),
            "EXPORT_MEAN": ("scalar.mean", "export_summary"),
            "EXPORT_P50": ("scalar.p50", "export_summary"),
            "EXPORT_P95": ("scalar.p95", "export_summary"),
            "WINSORIZED_EXPORT_MEAN": (
                "scalar.mean",
                "winsorized_export_summary",
            ),
            "SHARE_EXPORT_OVER_100": ("scalar.mean", "export_binary_summary"),
        },
    }
    mapping = direct_keys.get(benchmark_id, {}).get(oracle_key)
    semantic_key = None if mapping is None else mapping[0]
    slot_hint = None if mapping is None else mapping[1]
    if oracle_key.startswith("B_"):
        semantic_key = f"term.{oracle_key[2:]}.coefficient"
    elif oracle_key.startswith("SE_"):
        semantic_key = f"term.{oracle_key[3:]}.se"
    if semantic_key is None:
        return None, None
    matches = [
        (slot, result_id, value)
        for (slot, key), (result_id, value) in values.items()
        if key == semantic_key and (slot_hint is None or slot_hint in slot)
    ]
    if not matches:
        return None, None
    slot, result_id, value = sorted(matches)[0]
    del slot
    return result_id, value


def evaluate(run_root: Path, reference_path: Path) -> dict[str, Any]:
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    database_path = run_root / "workspaces" / "ws_live_product" / "workspace.sqlite3"
    workspace_root = database_path.parent
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        values = _adopted_values(connection)
        tolerance = reference["oracle"]["numeric_tolerance"]
        checks: dict[str, dict[str, object]] = {}
        result_ids: set[str] = set()
        for key, expected_raw in reference["oracle"]["values"].items():
            expected = float(expected_raw)
            result_id, observed = _observed_oracle(values, reference["benchmark_id"], key)
            matched = observed is not None and math.isclose(
                observed,
                expected,
                rel_tol=float(tolerance["relative"]),
                abs_tol=float(tolerance["absolute"]),
            )
            item: dict[str, object] = {
                "expected": expected,
                "observed": observed,
                "matched": matched,
            }
            if result_id is not None:
                result_ids.add(result_id)
                item["result_id"] = result_id
            if observed is None:
                item["reason"] = "no adopted formal Result element matched the oracle key"
            checks[key] = item

        document = connection.execute(
            """
            SELECT revision.document_revision_id, revision.docx_artifact_id,
                   artifact.size_bytes, artifact.content_hash, state.availability,
                   location.managed_handle, gate.verdict
            FROM document_revisions AS revision
            JOIN artifacts AS artifact ON artifact.artifact_id = revision.docx_artifact_id
            JOIN artifact_states AS state ON state.artifact_id = artifact.artifact_id
            JOIN artifact_locations AS location ON location.artifact_id = artifact.artifact_id
            JOIN delivery_gate_reports AS gate
              ON gate.document_revision_id = revision.document_revision_id
            ORDER BY revision.created_revision DESC LIMIT 1
            """
        ).fetchone()
        delivery: dict[str, object] = {"delivered": False}
        if document is not None:
            payload_path = workspace_root / str(document["managed_handle"])
            payload = payload_path.read_bytes() if payload_path.is_file() else b""
            integrity = (
                len(payload) == int(document["size_bytes"])
                and hashlib.sha256(payload).hexdigest() == str(document["content_hash"])
            )
            evidence_receipts = _scalar(
                connection,
                """
                SELECT count(*) FROM statistical_evidence_use_validation_receipts
                WHERE purpose = 'document_delivery' AND verdict = 'eligible'
                """,
            )
            delivery = {
                "delivered": (
                    str(document["verdict"]) == "pass"
                    and str(document["availability"]) == "available"
                    and integrity
                ),
                "document_revision_id": str(document["document_revision_id"]),
                "docx_artifact_id": str(document["docx_artifact_id"]),
                "delivery_gate": str(document["verdict"]),
                "availability": str(document["availability"]),
                "payload_integrity": integrity,
                "evidence_validation_receipts": evidence_receipts,
                "managed_handle": str(document["managed_handle"]),
            }

        tools = [
            {
                "tool": str(row["requested_tool_name"]),
                "result_kind": row["result_kind"],
                "summary": row["summary"],
            }
            for row in connection.execute(
                """
                SELECT call.requested_tool_name, result.result_kind, result.summary
                FROM tool_calls AS call
                LEFT JOIN canonical_tool_results AS result USING (tool_call_id)
                ORDER BY call.created_revision, call.call_ordinal
                """
            )
        ]
        turn = connection.execute(
            """
            SELECT turn_id, status, turn_revision
            FROM turns ORDER BY created_revision DESC LIMIT 1
            """
        ).fetchone()
        matched_count = sum(bool(item["matched"]) for item in checks.values())
        required_count = len(checks)
        required_artifacts = tuple(str(name) for name in reference.get("required_artifacts", ()))
        artifact_checks: dict[str, dict[str, object]] = {}
        for filename in required_artifacts:
            ledger = connection.execute(
                """
                SELECT artifact.artifact_id, artifact.media_type, artifact.size_bytes,
                       artifact.content_hash, artifact.created_revision,
                       state.availability, location.managed_handle,
                       manifest.output_slot, manifest.producer_locator
                FROM artifacts AS artifact
                JOIN artifact_states AS state USING (artifact_id)
                JOIN artifact_locations AS location USING (artifact_id)
                JOIN artifact_candidate_sources AS source USING (artifact_id)
                JOIN completion_manifest_artifacts AS manifest USING (artifact_candidate_id)
                WHERE replace(manifest.relative_staging_path, '\\', '/') = ?
                   OR replace(manifest.relative_staging_path, '\\', '/') LIKE ?
                ORDER BY artifact.created_revision DESC
                LIMIT 1
                """,
                (filename, f"%/{filename}"),
            ).fetchone()
            artifact_payload_path = (
                None
                if ledger is None
                else (workspace_root / str(ledger["managed_handle"])).resolve()
            )
            payload = (
                b""
                if artifact_payload_path is None or not artifact_payload_path.is_file()
                else artifact_payload_path.read_bytes()
            )
            payload_integrity = (
                ledger is not None
                and len(payload) == int(ledger["size_bytes"])
                and hashlib.sha256(payload).hexdigest() == str(ledger["content_hash"])
            )
            format_signature = (
                payload.startswith(b"\x89PNG\r\n\x1a\n")
                if filename.casefold().endswith(".png")
                else payload.startswith(b"%PDF-")
                if filename.casefold().endswith(".pdf")
                else len(payload) > 256
            )
            artifact_checks[filename] = {
                "ledger_registered": ledger is not None,
                "artifact_id": None if ledger is None else str(ledger["artifact_id"]),
                "output_slot": None if ledger is None else str(ledger["output_slot"]),
                "availability": None if ledger is None else str(ledger["availability"]),
                "size_bytes": None if ledger is None else int(ledger["size_bytes"]),
                "payload_integrity": payload_integrity,
                "format_signature": format_signature,
            }
        artifacts_passed = all(
            bool(check["ledger_registered"])
            and check["availability"] == "available"
            and bool(check["payload_integrity"])
            and bool(check["format_signature"])
            for check in artifact_checks.values()
        )
        passed = (
            matched_count == required_count
            and bool(delivery["delivered"])
            and artifacts_passed
        )
        return {
            "schema_version": "stata-research-agent.live-replication-report/v2",
            "benchmark_id": reference["benchmark_id"],
            "created_at": datetime.now(UTC).isoformat(),
            "input_policy": {
                "official_data_visible": True,
                "official_do_visible": True,
                "harness_visible": False,
                "oracle_visible": False,
            },
            "counts": {
                table: _scalar(connection, f"SELECT count(*) FROM {table}")
                for table in (
                    "steps",
                    "model_invocations",
                    "provider_attempts",
                    "stata_runs",
                    "results",
                    "document_revisions",
                )
            },
            "hidden_oracle": {
                "checks": checks,
                "matched_count": matched_count,
                "required_count": required_count,
                "result_ids": sorted(result_ids),
            },
            "delivery": delivery,
            "required_artifacts": {
                "checks": artifact_checks,
                "matched_count": sum(
                    bool(check["ledger_registered"])
                    and check["availability"] == "available"
                    and bool(check["payload_integrity"])
                    and bool(check["format_signature"])
                    for check in artifact_checks.values()
                ),
                "required_count": len(artifact_checks),
            },
            "tool_sequence": tools,
            "turn": None
            if turn is None
            else {
                "turn_id": str(turn["turn_id"]),
                "status": str(turn["status"]),
                "revision": int(turn["turn_revision"]),
            },
            "verdict": "pass" if passed else "fail",
            "verdict_reason": (
                "Every hidden official oracle value matched an adopted Stata Result and the "
                "delivered DOCX and required generated artifacts passed their integrity checks."
                if passed
                else "One or more hidden oracle or document-delivery checks failed."
            ),
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.run_root.resolve(), args.reference.resolve())
    output = args.output or args.run_root / "live-replication-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
