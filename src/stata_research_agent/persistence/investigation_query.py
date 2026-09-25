"""Deterministic read models for locating Agent failures across execution layers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from stata_research_agent.application.investigation import (
    InvestigationFinding,
    InvestigationLayer,
    ToolDecisionTrace,
    ToolOperationTrace,
    ToolStatusObservation,
    TurnInvestigationSnapshot,
)


def _json_object(value: object | None) -> dict[str, Any] | None:
    if value is None:
        return None
    decoded = json.loads(str(value))
    return dict(decoded) if isinstance(decoded, dict) else None


def _json_array(value: object | None) -> tuple[Any, ...]:
    if value is None:
        return ()
    decoded = json.loads(str(value))
    return tuple(decoded) if isinstance(decoded, list) else ()


class SqliteInvestigationQuery:
    """Join immutable facts without promoting diagnostics or heuristics to authority."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def turn(self, turn_id: str) -> TurnInvestigationSnapshot:
        turn = self._connection.execute(
            "SELECT status, turn_revision FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if turn is None:
            raise ValueError("Turn does not exist")
        authoritative_revision = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
            ).fetchone()[0]
        )
        last = self._connection.execute(
            """
            SELECT event_type, workspace_revision
            FROM journal_entries
            WHERE object_id = ? OR json_extract(payload_json, '$.turn_id') = ?
            ORDER BY workspace_revision DESC, ordinal DESC
            LIMIT 1
            """,
            (turn_id, turn_id),
        ).fetchone()
        findings: list[InvestigationFinding] = []

        turn_status = str(turn["status"])
        if turn_status in {"failed", "paused"}:
            findings.append(
                self._finding(
                    "control",
                    f"turn_{turn_status}",
                    "error" if turn_status == "failed" else "warn",
                    f"Turn is {turn_status}; inspect its last authoritative transitions.",
                    "turn",
                    turn_id,
                    f"/journal-entries?turn_id={turn_id}",
                )
            )
        waiting = self._connection.execute(
            """
            SELECT waiting_request_id, wait_reason
            FROM waiting_requests
            WHERE turn_id = ? AND status = 'open'
            ORDER BY created_revision DESC LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        if waiting is not None:
            findings.append(
                self._finding(
                    "control",
                    "waiting_for_user",
                    "info",
                    f"Turn is waiting for user input ({waiting['wait_reason']}).",
                    "waiting_request",
                    str(waiting["waiting_request_id"]),
                    f"/journal-entries?turn_id={turn_id}&event_type=turn.waiting",
                )
            )

        for row in self._connection.execute(
            """
            SELECT attempt.provider_attempt_id, attempt.state, attempt.error_code
            FROM provider_attempts AS attempt
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ?
              AND attempt.state IN ('failed', 'delivery_unknown', 'blocked', 'cancelled')
            ORDER BY step.step_ordinal, attempt.attempt_ordinal
            """,
            (turn_id,),
        ).fetchall():
            state = str(row["state"])
            findings.append(
                self._finding(
                    "provider",
                    str(row["error_code"] or f"provider_{state}"),
                    "error" if state in {"failed", "delivery_unknown"} else "warn",
                    f"Provider Attempt ended as {state}.",
                    "provider_attempt",
                    str(row["provider_attempt_id"]),
                    f"/journal-entries?turn_id={turn_id}&attempt_id={row['provider_attempt_id']}",
                )
            )

        tool_call_ids = tuple(
            str(row["tool_call_id"])
            for row in self._connection.execute(
                """
                SELECT call.tool_call_id
                FROM tool_calls AS call
                JOIN assistant_outputs AS output USING (assistant_output_id)
                JOIN model_invocations AS invocation USING (model_invocation_id)
                JOIN steps AS step USING (step_id)
                WHERE step.turn_id = ?
                ORDER BY step.step_ordinal, call.call_ordinal
                """,
                (turn_id,),
            ).fetchall()
        )
        for tool_call_id in tool_call_ids:
            trace = self.tool_decision(turn_id, tool_call_id)
            if trace.structural_diagnosis == "no_structural_failure":
                continue
            layer: InvestigationLayer = "tool_selection"
            severity: str = "warn"
            if trace.structural_diagnosis.startswith("admission_blocked"):
                layer = "tool_admission"
            elif trace.structural_diagnosis.startswith("execution_failed"):
                layer = "tool_execution"
                severity = "error"
            elif trace.structural_diagnosis.startswith("execution_uncertain"):
                layer = "recovery"
                severity = "error"
            elif trace.structural_diagnosis.startswith("tool_result_error"):
                layer = "tool_execution"
                severity = "error"
            findings.append(
                self._finding(
                    layer,
                    trace.structural_diagnosis.split(":", 1)[0],
                    cast(Any, severity),
                    trace.structural_diagnosis,
                    "tool_call",
                    tool_call_id,
                    f"/turns/{turn_id}/tool-decisions/{tool_call_id}",
                )
            )

        for row in self._connection.execute(
            """
            SELECT finding.evaluation_finding_id, finding.finding_code,
                   finding.severity, finding.message
            FROM evaluation_findings AS finding
            JOIN evaluation_reports AS report USING (evaluation_report_id)
            JOIN evaluation_requests AS request USING (evaluation_request_id)
            WHERE request.turn_id = ? AND finding.severity IN ('warn', 'error')
            ORDER BY report.created_revision, finding.finding_ordinal
            """,
            (turn_id,),
        ).fetchall():
            findings.append(
                self._finding(
                    "evaluation",
                    str(row["finding_code"]),
                    cast(Any, str(row["severity"])),
                    str(row["message"]),
                    "evaluation_finding",
                    str(row["evaluation_finding_id"]),
                    f"/turns/{turn_id}/evaluation",
                )
            )

        return TurnInvestigationSnapshot(
            turn_id,
            turn_status,
            int(turn["turn_revision"]),
            authoritative_revision,
            None if last is None else str(last["event_type"]),
            None if last is None else int(last["workspace_revision"]),
            tuple(findings),
            tool_call_ids,
            (
                "No deterministic structural fault was found. Review Tool Decision traces for "
                "semantic tool appropriateness; hidden chain-of-thought is neither required nor "
                "available."
                if not findings
                else "Inspect findings in order, then drill into the linked authoritative query."
            ),
        )

    def tool_decision(self, turn_id: str, tool_call_id: str) -> ToolDecisionTrace:
        row = self._connection.execute(
            """
            SELECT step.turn_id, step.step_id, step.step_ordinal,
                   manifest.context_manifest_id,
                   invocation.model_invocation_id,
                   invocation.status AS invocation_status,
                   output.producing_provider_attempt_id,
                   output.assistant_output_id, output.output_json,
                   call.tool_call_id, call.call_ordinal, call.requested_tool_name,
                   call.provider_tool_call_id, call.proposal_status,
                   raw.raw_arguments_text,
                   canonical.arguments_json, canonical.normalization_diff_json,
                   contract.tool_contract_id, contract.tool_version,
                   contract.operation_kind, contract.effect_class, contract.execution_owner,
                   plan.dispatch_plan_id, entry.execution_batch_ordinal,
                   admission.tool_admission_id, admission.admission_policy_revision,
                   result.result_kind, result.summary AS result_summary,
                   result.structured_payload_json, result.artifact_references_json
            FROM tool_calls AS call
            JOIN assistant_outputs AS output USING (assistant_output_id)
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            JOIN context_manifests AS manifest USING (step_id)
            JOIN raw_tool_argument_snapshots AS raw USING (raw_arguments_snapshot_id)
            LEFT JOIN canonical_tool_argument_snapshots AS canonical
              USING (canonical_arguments_snapshot_id)
            LEFT JOIN tool_contracts AS contract
              ON contract.tool_contract_id = call.resolved_tool_contract_id
            LEFT JOIN tool_dispatch_plan_entries AS entry USING (tool_call_id)
            LEFT JOIN tool_dispatch_plans AS plan USING (dispatch_plan_id)
            LEFT JOIN tool_admissions AS admission USING (tool_call_id)
            LEFT JOIN canonical_tool_results AS result USING (tool_call_id)
            WHERE step.turn_id = ? AND call.tool_call_id = ?
            """,
            (turn_id, tool_call_id),
        ).fetchone()
        if row is None:
            raise ValueError("Tool Call does not belong to the requested Turn")
        output = _json_object(row["output_json"]) or {}
        assistant_text = output.get("text")
        status_history = tuple(
            ToolStatusObservation(
                int(item["status_ordinal"]),
                str(item["proposal_status"]),
                str(item["reason_code"]),
                int(item["commit_revision"]),
            )
            for item in self._connection.execute(
                """
                SELECT status_ordinal, proposal_status, reason_code, commit_revision
                FROM tool_call_status_history
                WHERE tool_call_id = ? ORDER BY status_ordinal
                """,
                (tool_call_id,),
            ).fetchall()
        )
        operations: list[ToolOperationTrace] = []
        operation_ids: list[str] = []
        for operation in self._connection.execute(
            """
            SELECT operation_id, operation_kind, status, created_revision, terminal_revision
            FROM operations WHERE tool_call_id = ? ORDER BY created_revision
            """,
            (tool_call_id,),
        ).fetchall():
            operation_id = str(operation["operation_id"])
            operation_ids.append(operation_id)
            attempts = tuple(
                {
                    "attempt_id": str(attempt["operation_attempt_id"]),
                    "attempt_number": int(attempt["attempt_number"]),
                    "session_generation": (
                        None
                        if attempt["session_generation"] is None
                        else int(attempt["session_generation"])
                    ),
                    "status": str(attempt["status"]),
                    "created_revision": int(attempt["created_revision"]),
                    "terminal_revision": (
                        None
                        if attempt["terminal_revision"] is None
                        else int(attempt["terminal_revision"])
                    ),
                }
                for attempt in self._connection.execute(
                    """
                    SELECT operation_attempt_id, attempt_number, session_generation,
                           status, created_revision, terminal_revision
                    FROM operation_attempts
                    WHERE operation_id = ? ORDER BY attempt_number
                    """,
                    (operation_id,),
                ).fetchall()
            )
            operations.append(
                ToolOperationTrace(
                    operation_id,
                    str(operation["operation_kind"]),
                    str(operation["status"]),
                    int(operation["created_revision"]),
                    (
                        None
                        if operation["terminal_revision"] is None
                        else int(operation["terminal_revision"])
                    ),
                    attempts,
                )
            )
        evaluation_findings = tuple(
            {
                "finding_id": str(item["evaluation_finding_id"]),
                "code": str(item["finding_code"]),
                "severity": str(item["severity"]),
                "message": str(item["message"]),
                "control_effect": str(item["control_effect"]),
            }
            for item in self._connection.execute(
                """
                SELECT finding.evaluation_finding_id, finding.finding_code,
                       finding.severity, finding.message, finding.control_effect
                FROM evaluation_findings AS finding
                JOIN evaluation_reports AS report USING (evaluation_report_id)
                JOIN evaluation_requests AS request USING (evaluation_request_id)
                WHERE request.step_id = ?
                ORDER BY report.created_revision, finding.finding_ordinal
                """,
                (str(row["step_id"]),),
            ).fetchall()
        )
        references = [tool_call_id, *operation_ids]
        placeholders = ",".join("?" for _ in references)
        journal_references = tuple(
            {
                "journal_entry_id": str(item["journal_entry_id"]),
                "workspace_revision": int(item["workspace_revision"]),
                "ordinal": int(item["ordinal"]),
                "event_type": str(item["event_type"]),
                "object_type": str(item["object_type"]),
                "object_id": str(item["object_id"]),
            }
            for item in self._connection.execute(
                f"""
                SELECT journal_entry_id, workspace_revision, ordinal,
                       event_type, object_type, object_id
                FROM journal_entries
                WHERE object_id IN ({placeholders})
                   OR json_extract(payload_json, '$.tool_call_id') = ?
                ORDER BY workspace_revision, ordinal
                """,
                (*references, tool_call_id),
            ).fetchall()
        )
        diagnosis = self._diagnose_tool(
            str(row["proposal_status"]),
            status_history,
            tuple(operations),
            None if row["result_kind"] is None else str(row["result_kind"]),
        )
        return ToolDecisionTrace(
            str(row["turn_id"]),
            str(row["step_id"]),
            int(row["step_ordinal"]),
            str(row["context_manifest_id"]),
            str(row["model_invocation_id"]),
            str(row["invocation_status"]),
            str(row["producing_provider_attempt_id"]),
            str(row["assistant_output_id"]),
            assistant_text if isinstance(assistant_text, str) else None,
            str(row["tool_call_id"]),
            int(row["call_ordinal"]),
            str(row["requested_tool_name"]),
            None if row["provider_tool_call_id"] is None else str(row["provider_tool_call_id"]),
            str(row["proposal_status"]),
            str(row["raw_arguments_text"]),
            _json_object(row["arguments_json"]),
            _json_object(row["normalization_diff_json"]),
            None if row["tool_contract_id"] is None else str(row["tool_contract_id"]),
            None if row["tool_version"] is None else str(row["tool_version"]),
            None if row["operation_kind"] is None else str(row["operation_kind"]),
            None if row["effect_class"] is None else str(row["effect_class"]),
            None if row["execution_owner"] is None else str(row["execution_owner"]),
            None if row["dispatch_plan_id"] is None else str(row["dispatch_plan_id"]),
            (
                None
                if row["execution_batch_ordinal"] is None
                else int(row["execution_batch_ordinal"])
            ),
            None if row["tool_admission_id"] is None else str(row["tool_admission_id"]),
            (
                None
                if row["admission_policy_revision"] is None
                else str(row["admission_policy_revision"])
            ),
            status_history,
            tuple(operations),
            None if row["result_kind"] is None else str(row["result_kind"]),
            None if row["result_summary"] is None else str(row["result_summary"]),
            (
                None
                if row["structured_payload_json"] is None
                else json.loads(str(row["structured_payload_json"]))
            ),
            tuple(str(item) for item in _json_array(row["artifact_references_json"])),
            evaluation_findings,
            journal_references,
            diagnosis,
        )

    @staticmethod
    def _diagnose_tool(
        proposal_status: str,
        history: tuple[ToolStatusObservation, ...],
        operations: tuple[ToolOperationTrace, ...],
        result_kind: str | None,
    ) -> str:
        if proposal_status == "rejected":
            reason = history[-1].reason_code if history else "unknown"
            return f"preflight_rejected:{reason}"
        blocked = [
            item.reason_code
            for item in history
            if item.reason_code not in {"preflight_passed", "jit_passed", "success", "error"}
        ]
        if blocked and not operations:
            return f"admission_blocked:{blocked[-1]}"
        for operation in operations:
            if operation.status in {"outcome_unknown", "completed_unreconciled", "interrupted"}:
                return f"execution_uncertain:{operation.status}"
            if operation.status in {"failed", "integrity_violation"}:
                return f"execution_failed:{operation.status}"
        if result_kind in {"error", "rejected", "denied", "cancelled", "blocked"}:
            return f"tool_result_error:{result_kind}"
        if proposal_status in {"scheduled", "admitted"}:
            return f"incomplete_tool_call:{proposal_status}"
        return "no_structural_failure"

    @staticmethod
    def _finding(
        layer: InvestigationLayer,
        code: str,
        severity: Any,
        summary: str,
        object_type: str,
        object_id: str,
        next_query: str,
    ) -> InvestigationFinding:
        return InvestigationFinding(
            layer,
            code,
            cast(Any, severity),
            summary,
            object_type,
            object_id,
            next_query,
        )
