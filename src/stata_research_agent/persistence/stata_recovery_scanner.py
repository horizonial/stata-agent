"""Deterministic, read-only classification of interrupted Stata operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from stata_research_agent.application.ports.completion_manifest import CompletionManifestStore
from stata_research_agent.application.recovery import RecoveryAssessment
from stata_research_agent.domain.identifiers import OperationAttemptId, OperationId, TurnId
from stata_research_agent.domain.status import RecoveryClassification

StataRecoveryAssessment = RecoveryAssessment


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


class SqliteStataRecoveryScanner:
    """Observe SQLite and isolated completion payloads without mutating either."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        completion_store: CompletionManifestStore,
    ) -> None:
        self._connection = connection
        self._completion_store = completion_store

    def assess(self, operation_id: OperationId | str) -> RecoveryAssessment:
        operation_value = (
            operation_id.value if isinstance(operation_id, OperationId) else operation_id
        )
        row = self._connection.execute(
            """
            SELECT o.operation_id, o.status AS operation_status,
                   o.requested_by_turn_id, t.status AS turn_status,
                   t.execution_mode,
                   a.operation_attempt_id, a.status AS attempt_status,
                   a.session_generation,
                   r.session_id
            FROM operations AS o
            JOIN turns AS t ON t.turn_id = o.requested_by_turn_id
            LEFT JOIN operation_attempts AS a ON a.operation_id = o.operation_id
            LEFT JOIN stata_operation_requests AS r ON r.operation_id = o.operation_id
            WHERE o.operation_id = ?
            ORDER BY a.attempt_number DESC
            LIMIT 1
            """,
            (operation_value,),
        ).fetchone()
        if row is None:
            raise KeyError(operation_value)

        attempt_value = (
            str(row["operation_attempt_id"]) if row["operation_attempt_id"] is not None else None
        )
        lane = self._connection.execute(
            """
            SELECT active_write_turn_id, lane_revision
            FROM workspace_write_lane WHERE singleton_id = 1
            """
        ).fetchone()
        lane_owner = (
            str(lane["active_write_turn_id"])
            if lane is not None and lane["active_write_turn_id"] is not None
            else None
        )
        lane_revision = int(lane["lane_revision"]) if lane is not None else 0
        handoff_event = self._connection.execute(
            """
            SELECT 1 FROM journal_entries
            WHERE event_type = 'tool.handoff_committed'
              AND object_type = 'operation' AND object_id = ?
            LIMIT 1
            """,
            (operation_value,),
        ).fetchone()
        committed = None
        if attempt_value is not None:
            committed = self._connection.execute(
                """
                SELECT completion_manifest_id, session_generation, execution_status
                FROM completion_manifests WHERE operation_attempt_id = ?
                """,
                (attempt_value,),
            ).fetchone()

        if committed is not None:
            journal_boundary = "finalization_committed"
        elif attempt_value is not None or handoff_event is not None:
            journal_boundary = "handoff_committed"
        else:
            journal_boundary = "admission_only"

        manifest: dict[str, object] | None = None
        manifest_error: str | None = None
        if attempt_value is not None:
            try:
                manifest = self._completion_store.verify_attempt(attempt_value)
            except Exception as error:  # integrity failures are classification input
                manifest_error = f"{type(error).__name__}: {error}"

        manifest_summary: dict[str, Any] = {
            "verdict": (
                "failed"
                if manifest_error is not None
                else "verified"
                if manifest is not None
                else "absent"
            ),
            "error": manifest_error,
            "completion_manifest_id": (
                str(manifest.get("completion_manifest_id")) if manifest is not None else None
            ),
            "operation_id": (str(manifest.get("operation_id")) if manifest is not None else None),
            "operation_attempt_id": (
                str(manifest.get("operation_attempt_id")) if manifest is not None else None
            ),
            "session_generation": (
                manifest.get("session_generation") if manifest is not None else None
            ),
            "execution_status": (
                manifest.get("execution_status") if manifest is not None else None
            ),
        }
        raw_artifacts = manifest.get("artifacts") if manifest is not None else None
        artifact_entries = raw_artifacts if isinstance(raw_artifacts, list) else []
        artifact_summary: dict[str, Any] = {
            "verdict": manifest_summary["verdict"],
            "count": len(artifact_entries),
            "manifest_entries_sha256": hashlib.sha256(
                _canonical(artifact_entries).encode("utf-8")
            ).hexdigest(),
        }

        integrity_reasons = self._integrity_reasons(
            row=row,
            committed=committed,
            manifest=manifest,
            manifest_error=manifest_error,
            operation_id=operation_value,
            attempt_id=attempt_value,
            lane_owner=lane_owner,
        )
        if integrity_reasons:
            classification = RecoveryClassification.INTEGRITY_VIOLATION
            reason = "; ".join(integrity_reasons)
        elif committed is not None:
            classification = RecoveryClassification.COMMITTED
            reason = "authoritative Finalization and matching Completion Manifest are committed"
        elif journal_boundary == "admission_only":
            classification = RecoveryClassification.DEFINITELY_NOT_STARTED
            reason = "no Handoff boundary or execution Attempt was committed"
        elif manifest is not None:
            classification = RecoveryClassification.COMPLETED_UNRECONCILED
            reason = "isolated completion is valid but Finalization is absent"
        else:
            classification = RecoveryClassification.OUTCOME_UNKNOWN
            reason = "Handoff crossed the side-effect boundary without a valid completion"

        actions = {
            RecoveryClassification.DEFINITELY_NOT_STARTED: ("inspect", "continue"),
            RecoveryClassification.OUTCOME_UNKNOWN: ("inspect", "decide_new_work"),
            RecoveryClassification.COMPLETED_UNRECONCILED: (
                "inspect",
                "continue",
                "reconcile",
            ),
            RecoveryClassification.COMMITTED: ("inspect", "continue"),
            RecoveryClassification.INTEGRITY_VIOLATION: ("inspect", "repair"),
        }[classification]
        fingerprint_input = {
            "schema_version": "recovery-assessment/v1",
            "operation_id": operation_value,
            "attempt_id": attempt_value,
            "operation_status": str(row["operation_status"]),
            "attempt_status": (
                str(row["attempt_status"]) if row["attempt_status"] is not None else None
            ),
            "turn_id": str(row["requested_by_turn_id"]),
            "turn_status": str(row["turn_status"]),
            "lane_owner": lane_owner,
            "lane_revision": lane_revision,
            "journal_boundary": journal_boundary,
            "manifest": manifest_summary,
            "artifacts": artifact_summary,
            "classification": classification.value,
        }
        input_fingerprint = hashlib.sha256(
            _canonical(fingerprint_input).encode("utf-8")
        ).hexdigest()
        prior = self._connection.execute(
            """
            SELECT classification, completion_manifest_id,
                   manifest_verification_json, artifact_verification_json,
                   input_fingerprint
            FROM recovery_reports
            WHERE operation_id = ?
            ORDER BY created_revision DESC
            LIMIT 1
            """,
            (operation_value,),
        ).fetchone()
        if (
            prior is not None
            and str(prior["classification"]) == classification.value
            and prior["completion_manifest_id"]
            == (
                manifest.get("completion_manifest_id")
                if manifest is not None
                else (committed["completion_manifest_id"] if committed is not None else None)
            )
            and json.loads(str(prior["manifest_verification_json"])) == manifest_summary
            and json.loads(str(prior["artifact_verification_json"])) == artifact_summary
        ):
            input_fingerprint = str(prior["input_fingerprint"])

        return RecoveryAssessment(
            operation_id=OperationId(operation_value),
            attempt_id=(OperationAttemptId(attempt_value) if attempt_value is not None else None),
            requested_by_turn_id=TurnId(str(row["requested_by_turn_id"])),
            classification=classification,
            manifest_id=(
                str(manifest.get("completion_manifest_id"))
                if manifest is not None
                else (str(committed["completion_manifest_id"]) if committed is not None else None)
            ),
            reason=reason,
            observed_operation_status=str(row["operation_status"]),
            observed_attempt_status=(
                str(row["attempt_status"]) if row["attempt_status"] is not None else None
            ),
            observed_turn_status=str(row["turn_status"]),
            observed_lane_owner_turn_id=lane_owner,
            observed_lane_revision=lane_revision,
            observed_journal_boundary=journal_boundary,
            manifest_verification=manifest_summary,
            artifact_verification=artifact_summary,
            blocked_actions=("automatic_reexecution", "automatic_finalization", "pointer_move"),
            available_user_actions=actions,
            input_fingerprint=input_fingerprint,
        )

    def _integrity_reasons(
        self,
        *,
        row: sqlite3.Row,
        committed: sqlite3.Row | None,
        manifest: dict[str, object] | None,
        manifest_error: str | None,
        operation_id: str,
        attempt_id: str | None,
        lane_owner: str | None,
    ) -> list[str]:
        reasons: list[str] = []
        if manifest_error is not None:
            reasons.append("Completion Manifest or captured Artifact verification failed")
        if str(row["operation_status"]) in {"completed", "failed"} and committed is None:
            reasons.append("Operation claims Finalization without a committed manifest")
        if committed is not None and manifest is None:
            reasons.append("committed Finalization has no verifiable isolated manifest")
        if manifest is not None:
            if manifest.get("operation_id") != operation_id:
                reasons.append("Completion Manifest operation identity mismatch")
            if manifest.get("operation_attempt_id") != attempt_id:
                reasons.append("Completion Manifest Attempt identity mismatch")
            if (
                row["session_generation"] is not None
                and manifest.get("session_generation") != row["session_generation"]
            ):
                reasons.append("Completion Manifest session generation mismatch")
            receipt = manifest.get("execution_receipt")
            if not isinstance(receipt, dict) or receipt.get("session_id") != row["session_id"]:
                reasons.append("Completion Manifest session identity mismatch")
        if committed is not None and manifest is not None:
            if manifest.get("completion_manifest_id") != committed["completion_manifest_id"]:
                reasons.append("SQLite and isolated Completion Manifest identity mismatch")
            if manifest.get("session_generation") != committed["session_generation"]:
                reasons.append("SQLite and isolated session generation mismatch")
            if manifest.get("execution_status") != committed["execution_status"]:
                reasons.append("SQLite and isolated execution status mismatch")
            expected = {
                str(item["output_slot"]): (
                    int(item["size_bytes"]),
                    str(item["content_hash"]),
                )
                for item in self._connection.execute(
                    """
                    SELECT output_slot, size_bytes, content_hash
                    FROM completion_manifest_artifacts
                    WHERE completion_manifest_id = ?
                    """,
                    (str(committed["completion_manifest_id"]),),
                ).fetchall()
            }
            raw_artifacts = manifest.get("artifacts")
            manifest_artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
            actual = {
                str(item.get("output_slot")): (
                    int(item.get("size_bytes", -1)),
                    str(item.get("sha256")),
                )
                for item in manifest_artifacts
                if isinstance(item, dict)
            }
            if expected != actual:
                reasons.append("SQLite and isolated Artifact manifests differ")
        if (
            str(row["execution_mode"]) == "write"
            and str(row["turn_status"]) in {"running", "waiting"}
            and lane_owner not in {None, str(row["requested_by_turn_id"])}
        ):
            reasons.append("active Turn conflicts with Workspace write-lane owner")
        return reasons
