"""SQLite current Evidence eligibility and rebuildable last-known projection."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from stata_research_agent.application.evidence_eligibility import (
    EligibilityVerdict,
    EvidenceEligibility,
    EvidenceEligibilityQuery,
    EvidenceProjectionRebuild,
    EvidenceUseContext,
    EvidenceUsePurpose,
    EvidenceUseValidation,
    ValidateEvidenceUseCommand,
)
from stata_research_agent.domain.identifiers import (
    EvidenceRecordId,
    EvidenceValidationReceiptId,
    ResearchPathId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


@dataclass(frozen=True, slots=True)
class _Assessment:
    eligibility: EvidenceEligibility
    source_kind: str
    result_id: str | None = None
    result_slot_id: str | None = None
    result_pointer_revision: int | None = None
    plan_revision_id: str | None = None
    plan_pointer_revision: int | None = None
    analysis_output_id: str | None = None
    analysis_output_adoption_id: str | None = None


class SqliteEvidenceEligibilityRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def query(self, query: EvidenceEligibilityQuery) -> EvidenceEligibility:
        owns_snapshot = not self._connection.in_transaction
        if owns_snapshot:
            self._connection.execute("BEGIN DEFERRED")
        try:
            revision = self._authoritative_revision(self._connection)
            return self._assess(self._connection, query, revision).eligibility
        finally:
            if owns_snapshot:
                self._connection.rollback()

    def validate_for_use(
        self,
        command: ValidateEvidenceUseCommand,
        receipt_id: EvidenceValidationReceiptId,
    ) -> EvidenceUseValidation:
        request = {
            "requested_by_turn_id": command.requested_by_turn_id.value,
            "evidence_record_id": command.query.evidence_record_id.value,
            "research_path_id": command.query.research_path_id.value,
            "use_context_kind": command.query.use_context_kind.value,
            "purpose": command.query.purpose.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            active = connection.execute(
                """
                SELECT 1 FROM turns WHERE turn_id = ? AND research_path_id = ?
                  AND status IN ('running', 'waiting')
                """,
                (
                    command.requested_by_turn_id.value,
                    command.query.research_path_id.value,
                ),
            ).fetchone()
            if active is None:
                raise ValueError("Evidence validation Turn is not active on this Research Path")
            assessment = self._assess(connection, command.query, revision)
            eligibility = assessment.eligibility
            if assessment.source_kind == "statistical_element":
                if assessment.result_id is None:
                    raise RuntimeError("statistical assessment omitted Result identity")
                connection.execute(
                    """
                    INSERT INTO statistical_evidence_use_validation_receipts
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        receipt_id.value,
                        eligibility.evidence_record_id.value,
                        eligibility.research_path_id.value,
                        eligibility.use_context_kind.value,
                        eligibility.purpose.value,
                        eligibility.verdict.value,
                        canonical_json(list(eligibility.reason_codes)),
                        assessment.result_id,
                        assessment.result_slot_id,
                        assessment.result_pointer_revision,
                        assessment.plan_revision_id,
                        assessment.plan_pointer_revision,
                        canonical_json(list(eligibility.dependency_snapshot)),
                        revision.value,
                        revision.value,
                    ),
                )
            else:
                if (
                    assessment.analysis_output_id is None
                    or assessment.analysis_output_adoption_id is None
                ):
                    raise RuntimeError("analysis assessment omitted adoption identity")
                connection.execute(
                    """
                    INSERT INTO analysis_evidence_use_validation_receipts
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        receipt_id.value,
                        eligibility.evidence_record_id.value,
                        eligibility.research_path_id.value,
                        eligibility.use_context_kind.value,
                        eligibility.purpose.value,
                        eligibility.verdict.value,
                        canonical_json(list(eligibility.reason_codes)),
                        assessment.analysis_output_id,
                        assessment.analysis_output_adoption_id,
                        canonical_json(list(eligibility.dependency_snapshot)),
                        revision.value,
                        revision.value,
                    ),
                )
            response = self._response(receipt_id, eligibility)
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "evidence.use_validated",
                        "evidence_validation_receipt",
                        receipt_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("evidence.validation.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="evidence.validate_for_use",
            request=request,
            mutation=mutate,
        )
        return EvidenceUseValidation(
            receipt_id,
            self._eligibility_from_response(receipt.response),
            receipt.commit_revision,
            receipt.replayed,
        )

    def rebuild_projection(self) -> EvidenceProjectionRebuild:
        if self._connection.in_transaction:
            raise RuntimeError("Projection rebuild requires its own transaction")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            revision = self._authoritative_revision(self._connection)
            statistical = self._connection.execute(
                "SELECT evidence_record_id FROM evidence_records ORDER BY evidence_record_id"
            ).fetchall()
            analysis = self._connection.execute(
                """
                SELECT evidence_record_id FROM analysis_evidence_records
                ORDER BY evidence_record_id
                """
            ).fetchall()
            self._connection.execute("DELETE FROM statistical_evidence_current_states")
            self._connection.execute("DELETE FROM analysis_evidence_current_states")
            for row in statistical:
                state, reason = self._statistical_source_state(
                    self._connection, str(row["evidence_record_id"])
                )
                self._connection.execute(
                    "INSERT INTO statistical_evidence_current_states VALUES (?, ?, ?, ?)",
                    (str(row["evidence_record_id"]), state, reason, revision.value),
                )
            for row in analysis:
                state, reason, _ = self._analysis_source_state(
                    self._connection, str(row["evidence_record_id"])
                )
                self._connection.execute(
                    "INSERT INTO analysis_evidence_current_states VALUES (?, ?, ?, ?)",
                    (str(row["evidence_record_id"]), state, reason, revision.value),
                )
            self._connection.execute(
                """
                UPDATE evidence_projection_checkpoints
                SET projection_revision = ?, rebuilt_at = ?
                WHERE projection_name = 'evidence_current_state'
                """,
                (revision.value, datetime.now(UTC).isoformat()),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return EvidenceProjectionRebuild(revision, len(statistical), len(analysis))

    def require_statistical_use_in_uow(
        self,
        connection: sqlite3.Connection,
        *,
        revision: WorkspaceRevision,
        receipt_id: EvidenceValidationReceiptId,
        evidence_record_id: EvidenceRecordId,
        research_path_id: ResearchPathId,
    ) -> None:
        """Revalidate and receipt a formal Document use inside its owning UoW."""

        query = EvidenceEligibilityQuery(
            evidence_record_id,
            research_path_id,
            EvidenceUseContext.CURRENT_ADOPTED_RESULT,
            EvidenceUsePurpose.DOCUMENT_DELIVERY,
        )
        assessment = self._assess_statistical(connection, query, revision)
        eligibility = assessment.eligibility
        if eligibility.verdict is not EligibilityVerdict.ELIGIBLE:
            raise ValueError(
                "Evidence is not currently eligible for Document Delivery: "
                + ",".join(eligibility.reason_codes)
            )
        if assessment.result_id is None:
            raise RuntimeError("statistical assessment omitted Result identity")
        connection.execute(
            """
            INSERT INTO statistical_evidence_use_validation_receipts
            VALUES (?, ?, ?, ?, ?, 'eligible', '[]', ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                receipt_id.value,
                evidence_record_id.value,
                research_path_id.value,
                EvidenceUseContext.CURRENT_ADOPTED_RESULT.value,
                EvidenceUsePurpose.DOCUMENT_DELIVERY.value,
                assessment.result_id,
                assessment.result_slot_id,
                assessment.result_pointer_revision,
                assessment.plan_revision_id,
                assessment.plan_pointer_revision,
                canonical_json(list(eligibility.dependency_snapshot)),
                revision.value,
                revision.value,
            ),
        )

    def _assess(
        self,
        connection: sqlite3.Connection,
        query: EvidenceEligibilityQuery,
        revision: WorkspaceRevision,
    ) -> _Assessment:
        if connection.execute(
            "SELECT 1 FROM evidence_records WHERE evidence_record_id = ?",
            (query.evidence_record_id.value,),
        ).fetchone():
            return self._assess_statistical(connection, query, revision)
        if connection.execute(
            "SELECT 1 FROM analysis_evidence_records WHERE evidence_record_id = ?",
            (query.evidence_record_id.value,),
        ).fetchone():
            return self._assess_analysis(connection, query, revision)
        raise ValueError("EvidenceRecord does not exist")

    def _assess_statistical(
        self,
        connection: sqlite3.Connection,
        query: EvidenceEligibilityQuery,
        revision: WorkspaceRevision,
    ) -> _Assessment:
        source = connection.execute(
            """
            SELECT result.result_id, run.stata_run_id, state.availability,
                   binding.plan_revision_id, binding.adherence_verdict,
                   plan.target_plan_revision_id AS current_plan_revision_id,
                   plan.pointer_revision AS plan_pointer_revision
            FROM evidence_statistical_sources AS evidence_source
            JOIN result_elements AS element
              ON element.result_element_id = evidence_source.result_element_id
            JOIN results AS result ON result.result_id = element.result_id
            JOIN stata_runs AS run ON run.stata_run_id = result.producing_stata_run_id
            JOIN data_versions AS data
              ON data.data_version_id = run.input_data_version_id
            LEFT JOIN artifact_states AS state
              ON state.artifact_id = data.canonical_artifact_id
            LEFT JOIN stata_run_plan_bindings AS binding
              ON binding.stata_run_id = run.stata_run_id
            LEFT JOIN path_plan_adoptions AS plan
              ON plan.research_path_id = ?
            WHERE evidence_source.evidence_record_id = ?
            """,
            (query.research_path_id.value, query.evidence_record_id.value),
        ).fetchone()
        if source is None:
            raise ValueError("Statistical Evidence source chain is incomplete")
        reasons: list[str] = []
        availability = source["availability"]
        if availability is None:
            reasons.append("SOURCE_STATE_UNKNOWN")
        elif str(availability) != "available":
            reasons.append("SOURCE_UNAVAILABLE")

        result_id = str(source["result_id"])
        current = connection.execute(
            """
            SELECT slot.result_slot_id, adoption.pointer_revision
            FROM result_slots AS slot
            JOIN path_result_adoptions AS adoption
              ON adoption.result_slot_id = slot.result_slot_id
            WHERE slot.research_path_id = ? AND slot.lifecycle = 'active'
              AND adoption.target_result_id = ?
            ORDER BY slot.result_slot_id LIMIT 1
            """,
            (query.research_path_id.value, result_id),
        ).fetchone()
        dependencies = connection.execute(
            """
            SELECT dependency.input_data_slot_key, dependency.data_version_id,
                   slot.path_data_slot_id, slot.lifecycle,
                   adoption.target_data_version_id, adoption.pointer_revision
            FROM result_required_data_dependencies AS dependency
            LEFT JOIN path_data_slots AS slot
              ON slot.research_path_id = ?
             AND slot.canonical_key = dependency.input_data_slot_key
            LEFT JOIN path_data_adoptions AS adoption
              ON adoption.path_data_slot_id = slot.path_data_slot_id
            WHERE dependency.result_id = ?
            ORDER BY dependency.input_data_slot_key
            """,
            (query.research_path_id.value, result_id),
        ).fetchall()
        dependency_snapshot = cast(
            tuple[dict[str, object], ...],
            tuple(
                {
                    "slot_key": str(row["input_data_slot_key"]),
                    "required_data_version_id": str(row["data_version_id"]),
                    "path_data_slot_id": (
                        None if row["path_data_slot_id"] is None else str(row["path_data_slot_id"])
                    ),
                    "lifecycle": (None if row["lifecycle"] is None else str(row["lifecycle"])),
                    "adopted_data_version_id": (
                        None
                        if row["target_data_version_id"] is None
                        else str(row["target_data_version_id"])
                    ),
                    "pointer_revision": (
                        None if row["pointer_revision"] is None else int(row["pointer_revision"])
                    ),
                }
                for row in dependencies
            ),
        )
        if query.use_context_kind is EvidenceUseContext.CURRENT_ADOPTED_RESULT:
            if current is None:
                reasons.append("RESULT_NOT_CURRENT_ON_PATH")
            if not dependencies:
                reasons.append("REQUIRED_DATA_DEPENDENCY_UNKNOWN")
            for item in dependency_snapshot:
                if item["lifecycle"] != "active":
                    reasons.append("REQUIRED_DATA_SLOT_NOT_ACTIVE")
                if item["adopted_data_version_id"] != item["required_data_version_id"]:
                    reasons.append("REQUIRED_DATA_NOT_CURRENT")
            if source["plan_revision_id"] is None and source["current_plan_revision_id"] is None:
                pass
            elif source["plan_revision_id"] is None:
                reasons.append("PLAN_BINDING_UNKNOWN")
            elif str(source["adherence_verdict"]) != "matches":
                reasons.append("PLAN_ADHERENCE_FAILED")
            elif source["current_plan_revision_id"] is None:
                reasons.append("CURRENT_PLAN_UNKNOWN")
            elif str(source["plan_revision_id"]) != str(source["current_plan_revision_id"]):
                reasons.append("PLAN_NOT_CURRENT_ON_PATH")
        projection_revision, projected_state, projected_reason = self._projection_state(
            connection, query.evidence_record_id.value, "statistical"
        )
        eligibility = EvidenceEligibility(
            query.evidence_record_id,
            "statistical_element",
            query.research_path_id,
            query.use_context_kind,
            query.purpose,
            self._verdict(reasons),
            tuple(sorted(set(reasons))),
            projected_state,
            projected_reason,
            revision,
            projection_revision,
            dependency_snapshot,
        )
        return _Assessment(
            eligibility,
            "statistical_element",
            result_id=result_id,
            result_slot_id=(None if current is None else str(current["result_slot_id"])),
            result_pointer_revision=(None if current is None else int(current["pointer_revision"])),
            plan_revision_id=(
                None if source["plan_revision_id"] is None else str(source["plan_revision_id"])
            ),
            plan_pointer_revision=(
                None
                if source["plan_pointer_revision"] is None
                else int(source["plan_pointer_revision"])
            ),
        )

    def _assess_analysis(
        self,
        connection: sqlite3.Connection,
        query: EvidenceEligibilityQuery,
        revision: WorkspaceRevision,
    ) -> _Assessment:
        source = connection.execute(
            """
            SELECT evidence.analysis_output_id,
                   evidence.analysis_output_adoption_id,
                   turn.research_path_id AS adoption_path_id,
                   eligibility.eligibility_status
            FROM analysis_evidence_records AS evidence
            JOIN analysis_output_adoptions AS adoption
              ON adoption.analysis_output_adoption_id =
                 evidence.analysis_output_adoption_id
            JOIN turns AS turn ON turn.turn_id = adoption.adopted_by_turn_id
            LEFT JOIN analysis_document_eligibility_receipts AS eligibility
              ON eligibility.evidence_record_id = evidence.evidence_record_id
            WHERE evidence.evidence_record_id = ?
            """,
            (query.evidence_record_id.value,),
        ).fetchone()
        if source is None:
            raise ValueError("Analysis Evidence source chain is incomplete")
        state, _, artifact_snapshot = self._analysis_source_state(
            connection, query.evidence_record_id.value
        )
        reasons: list[str] = []
        if state == "unknown":
            reasons.append("SOURCE_STATE_UNKNOWN")
        elif state != "available":
            reasons.append("SOURCE_UNAVAILABLE")
        if source["eligibility_status"] != "eligible":
            reasons.append("DOCUMENT_ELIGIBILITY_UNKNOWN")
        if (
            query.use_context_kind is EvidenceUseContext.CURRENT_ADOPTED_RESULT
            and str(source["adoption_path_id"]) != query.research_path_id.value
        ):
            reasons.append("ANALYSIS_OUTPUT_NOT_ADOPTED_ON_PATH")
        projection_revision, projected_state, projected_reason = self._projection_state(
            connection, query.evidence_record_id.value, "analysis"
        )
        eligibility = EvidenceEligibility(
            query.evidence_record_id,
            "adopted_analysis_output",
            query.research_path_id,
            query.use_context_kind,
            query.purpose,
            self._verdict(reasons),
            tuple(sorted(set(reasons))),
            projected_state,
            projected_reason,
            revision,
            projection_revision,
            artifact_snapshot,
        )
        return _Assessment(
            eligibility,
            "adopted_analysis_output",
            analysis_output_id=str(source["analysis_output_id"]),
            analysis_output_adoption_id=str(source["analysis_output_adoption_id"]),
        )

    @staticmethod
    def _statistical_source_state(
        connection: sqlite3.Connection, evidence_record_id: str
    ) -> tuple[str, str]:
        row = connection.execute(
            """
            SELECT state.availability
            FROM evidence_statistical_sources AS source
            JOIN result_elements AS element
              ON element.result_element_id = source.result_element_id
            JOIN results AS result ON result.result_id = element.result_id
            JOIN stata_runs AS run ON run.stata_run_id = result.producing_stata_run_id
            JOIN data_versions AS data
              ON data.data_version_id = run.input_data_version_id
            LEFT JOIN artifact_states AS state
              ON state.artifact_id = data.canonical_artifact_id
            WHERE source.evidence_record_id = ?
            """,
            (evidence_record_id,),
        ).fetchone()
        if row is None or row["availability"] is None:
            return "unknown", "SOURCE_STATE_UNKNOWN"
        if str(row["availability"]) != "available":
            return "source_unavailable", "SOURCE_UNAVAILABLE"
        return "available", "SOURCE_AVAILABLE"

    @staticmethod
    def _analysis_source_state(
        connection: sqlite3.Connection, evidence_record_id: str
    ) -> tuple[str, str, tuple[dict[str, object], ...]]:
        rows = connection.execute(
            """
            WITH source_artifacts(artifact_id, role) AS (
                SELECT output.executable_artifact_id, 'executable'
                FROM analysis_evidence_records AS evidence
                JOIN analysis_outputs AS output
                  ON output.analysis_output_id = evidence.analysis_output_id
                WHERE evidence.evidence_record_id = ?
                UNION ALL
                SELECT input.artifact_id, 'input'
                FROM analysis_evidence_records AS evidence
                JOIN analysis_output_inputs AS input
                  ON input.analysis_output_id = evidence.analysis_output_id
                WHERE evidence.evidence_record_id = ?
                UNION ALL
                SELECT artifact.artifact_id, artifact.role
                FROM analysis_evidence_records AS evidence
                JOIN analysis_output_artifacts AS artifact
                  ON artifact.analysis_output_id = evidence.analysis_output_id
                WHERE evidence.evidence_record_id = ?
            )
            SELECT source_artifacts.artifact_id, source_artifacts.role,
                   state.availability, artifact.content_hash,
                   artifact.created_revision AS artifact_identity_revision
            FROM source_artifacts
            LEFT JOIN artifacts AS artifact USING (artifact_id)
            LEFT JOIN artifact_states AS state USING (artifact_id)
            ORDER BY source_artifacts.role, source_artifacts.artifact_id
            """,
            (evidence_record_id, evidence_record_id, evidence_record_id),
        ).fetchall()
        snapshot = cast(
            tuple[dict[str, object], ...],
            tuple(
                {
                    "artifact_id": str(row["artifact_id"]),
                    "role": str(row["role"]),
                    "availability": (
                        None if row["availability"] is None else str(row["availability"])
                    ),
                    "content_hash": (
                        None if row["content_hash"] is None else str(row["content_hash"])
                    ),
                    "artifact_identity_revision": (
                        None
                        if row["artifact_identity_revision"] is None
                        else int(row["artifact_identity_revision"])
                    ),
                }
                for row in rows
            ),
        )
        if not rows or any(row["availability"] is None for row in rows):
            return "unknown", "SOURCE_STATE_UNKNOWN", snapshot
        if any(str(row["availability"]) != "available" for row in rows):
            return "source_unavailable", "SOURCE_UNAVAILABLE", snapshot
        return "available", "SOURCE_AVAILABLE", snapshot

    @staticmethod
    def _verdict(reasons: list[str]) -> EligibilityVerdict:
        if any(
            reason
            in {
                "SOURCE_UNAVAILABLE",
                "RESULT_NOT_CURRENT_ON_PATH",
                "REQUIRED_DATA_SLOT_NOT_ACTIVE",
                "REQUIRED_DATA_NOT_CURRENT",
                "PLAN_ADHERENCE_FAILED",
                "PLAN_NOT_CURRENT_ON_PATH",
                "ANALYSIS_OUTPUT_NOT_ADOPTED_ON_PATH",
            }
            for reason in reasons
        ):
            return EligibilityVerdict.INELIGIBLE
        if reasons:
            return EligibilityVerdict.UNKNOWN
        return EligibilityVerdict.ELIGIBLE

    @staticmethod
    def _authoritative_revision(connection: sqlite3.Connection) -> WorkspaceRevision:
        return WorkspaceRevision(
            int(
                connection.execute(
                    "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                ).fetchone()[0]
            )
        )

    @staticmethod
    def _projection_state(
        connection: sqlite3.Connection, evidence_record_id: str, source_kind: str
    ) -> tuple[WorkspaceRevision, str, str]:
        checkpoint = int(
            connection.execute(
                """
                SELECT projection_revision FROM evidence_projection_checkpoints
                WHERE projection_name = 'evidence_current_state'
                """
            ).fetchone()[0]
        )
        table = (
            "statistical_evidence_current_states"
            if source_kind == "statistical"
            else "analysis_evidence_current_states"
        )
        row = connection.execute(
            f"SELECT source_state, reason_code FROM {table} WHERE evidence_record_id = ?",
            (evidence_record_id,),
        ).fetchone()
        if row is None:
            return WorkspaceRevision(checkpoint), "unknown", "PROJECTION_NOT_BUILT"
        return (
            WorkspaceRevision(checkpoint),
            str(row["source_state"]),
            str(row["reason_code"]),
        )

    @staticmethod
    def _response(
        receipt_id: EvidenceValidationReceiptId, eligibility: EvidenceEligibility
    ) -> dict[str, object]:
        return {
            "evidence_validation_receipt_id": receipt_id.value,
            "evidence_record_id": eligibility.evidence_record_id.value,
            "evidence_kind": eligibility.evidence_kind,
            "research_path_id": eligibility.research_path_id.value,
            "use_context_kind": eligibility.use_context_kind.value,
            "purpose": eligibility.purpose.value,
            "verdict": eligibility.verdict.value,
            "reason_codes": list(eligibility.reason_codes),
            "source_state": eligibility.source_state,
            "source_state_reason": eligibility.source_state_reason,
            "authoritative_revision": eligibility.authoritative_revision.value,
            "projection_revision": eligibility.projection_revision.value,
            "dependency_snapshot": list(eligibility.dependency_snapshot),
        }

    @staticmethod
    def _eligibility_from_response(
        response: Mapping[str, Any],
    ) -> EvidenceEligibility:
        return EvidenceEligibility(
            EvidenceRecordId(str(response["evidence_record_id"])),
            str(response["evidence_kind"]),
            ResearchPathId(str(response["research_path_id"])),
            EvidenceUseContext(str(response["use_context_kind"])),
            EvidenceUsePurpose(str(response["purpose"])),
            EligibilityVerdict(str(response["verdict"])),
            tuple(str(item) for item in response["reason_codes"]),
            str(response["source_state"]),
            str(response["source_state_reason"]),
            WorkspaceRevision(int(response["authoritative_revision"])),
            WorkspaceRevision(int(response["projection_revision"])),
            tuple(dict(item) for item in response["dependency_snapshot"]),
        )
