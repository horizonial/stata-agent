"""SQLite UoWs for immutable human Word return revisions."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from stata_research_agent.application.document_delivery import PublishedDocumentArtifact
from stata_research_agent.application.document_roundtrip import (
    DocumentMergeIdentity,
    DocumentMergeOutcome,
    DocumentReturnIdentity,
    DocumentReturnOutcome,
    DocumentSemanticDiff,
    DocumentThreeWayMerge,
    FinalizedDocumentReturn,
    ImportReturnedDocumentCommand,
    MergeDocumentRevisionsCommand,
    PreparedDocumentMerge,
    PreparedDocumentReturn,
    RejectedDocumentReturn,
)
from stata_research_agent.domain.identifiers import (
    DocumentId,
    DocumentManifestId,
    DocumentReturnReportId,
    DocumentRevisionId,
    EvidenceRecordId,
    EvidenceValidationReceiptId,
    OperationAttemptId,
    OperationId,
    ResearchPathId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    CrashInjector,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)
from .evidence_eligibility_store import SqliteEvidenceEligibilityRepository

_VIEW_POLICY = {
    "policy_version": 1,
    "managed_marker": "wordprocessingml.sdt.tag/sa:v1",
    "tracked_changes": "preserve_and_block_delivery",
    "missing_or_duplicate_marker": "block_evidence_inheritance",
    "table_edit": "block_delivery",
    "prose_edit": "preserve_human_content",
}


class SqliteDocumentRoundtripRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        finalization_crash_injector: CrashInjector | None = None,
    ) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)
        self._finalization_crash_injector = finalization_crash_injector

    def prepare_return(
        self,
        command: ImportReturnedDocumentCommand,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
    ) -> PreparedDocumentReturn:
        request = {
            "requested_by_turn_id": command.requested_by_turn_id.value,
            "research_path_id": command.research_path_id.value,
            "base_document_revision_id": command.base_document_revision_id.value,
            "returned_locator": command.returned_path.name,
            "expected_working_pointer_revision": command.expected_working_pointer_revision,
            "expected_delivery_pointer_revision": command.expected_delivery_pointer_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_active_turn(connection, command)
            base = self._base_revision(connection, command)
            if int(base["working_pointer_revision"]) != command.expected_working_pointer_revision:
                raise ValueError("Working Document pointer changed before return capture")
            connection.execute(
                """
                INSERT INTO operations VALUES (
                    ?, 'document.render', ?, NULL, 'handoff_committed', ?, ?, NULL
                )
                """,
                (
                    operation_id.value,
                    command.requested_by_turn_id.value,
                    command.command_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO operation_attempts VALUES (
                    ?, ?, 1, NULL, 'handoff_committed', ?, NULL
                )
                """,
                (attempt_id.value, operation_id.value, revision.value),
            )
            response = {
                "document_id": str(base["document_id"]),
                "operation_id": operation_id.value,
                "attempt_id": attempt_id.value,
                "base_docx_managed_handle": str(base["managed_handle"]),
                "base_document_manifest_id": str(base["document_manifest_id"]),
                "table_render_receipt_id": str(base["table_render_receipt_id"]),
                "table_coverage_manifest_id": str(base["table_coverage_manifest_id"]),
                "base_delivery_verdict": str(base["base_delivery_verdict"]),
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "document_return.handoff_committed",
                        "operation",
                        operation_id.value,
                        {"base_document_revision_id": (command.base_document_revision_id.value)},
                    ),
                ),
                (OutboxDraft("document_return.started", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="document_roundtrip.prepare",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return PreparedDocumentReturn(
            DocumentId(str(response["document_id"])),
            OperationId(str(response["operation_id"])),
            OperationAttemptId(str(response["attempt_id"])),
            str(response["base_docx_managed_handle"]),
            DocumentManifestId(str(response["base_document_manifest_id"])),
            str(response["table_render_receipt_id"]),
            str(response["table_coverage_manifest_id"]),
            str(response["base_delivery_verdict"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def finalize_return(
        self,
        command: ImportReturnedDocumentCommand,
        prepared: PreparedDocumentReturn,
        identity: DocumentReturnIdentity,
        payloads: FinalizedDocumentReturn,
        diff: DocumentSemanticDiff,
    ) -> DocumentReturnOutcome:
        finalization_command_id = type(command.command_id)(
            "cmd_"
            + hashlib.sha256((command.command_id.value + ":finalize").encode("utf-8")).hexdigest()[
                :28
            ]
        )
        request = {
            "operation_id": prepared.operation_id.value,
            "attempt_id": prepared.attempt_id.value,
            "base_document_revision_id": command.base_document_revision_id.value,
            "returned_document_revision_id": identity.returned_revision_id.value,
            "docx_sha256": payloads.docx.sha256,
            "manifest_sha256": payloads.manifest.sha256,
            "classification": diff.classification,
            "findings": list(diff.findings),
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_active_turn(connection, command)
            operation = connection.execute(
                """
                SELECT operation.status, attempt.status AS attempt_status
                FROM operations AS operation
                JOIN operation_attempts AS attempt
                  ON attempt.operation_id = operation.operation_id
                WHERE operation.operation_id = ? AND attempt.operation_attempt_id = ?
                """,
                (prepared.operation_id.value, prepared.attempt_id.value),
            ).fetchone()
            if operation is None or tuple(operation) != (
                "handoff_committed",
                "handoff_committed",
            ):
                raise ValueError("Document Return is not awaiting finalization")
            base = self._base_revision(connection, command)
            if int(base["working_pointer_revision"]) != command.expected_working_pointer_revision:
                raise ValueError("Working Document pointer changed during return capture")
            for artifact in (payloads.docx, payloads.manifest):
                self._insert_artifact(connection, revision, prepared.attempt_id, artifact)
            manifest_sha = hashlib.sha256(payloads.manifest_json.encode("utf-8")).hexdigest()
            if manifest_sha != payloads.manifest.sha256:
                raise ValueError("Returned Document Manifest changed before commit")
            connection.execute(
                "INSERT INTO document_manifests VALUES (?, ?, ?, ?, ?, 'word.rtf-to-docx.v1', ?)",
                (
                    identity.returned_manifest_id.value,
                    prepared.table_render_receipt_id,
                    prepared.table_coverage_manifest_id,
                    payloads.manifest_json,
                    manifest_sha,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO document_revisions VALUES (
                    ?, ?, 'user_returned', ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    identity.returned_revision_id.value,
                    prepared.document_id.value,
                    command.base_document_revision_id.value,
                    payloads.docx.artifact_id.value,
                    payloads.manifest.artifact_id.value,
                    identity.returned_manifest_id.value,
                    command.requested_by_turn_id.value,
                    prepared.operation_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO document_parse_receipts
                VALUES (?, ?, ?, 'pass', ?, 'ooxml.safe-reader.v1', ?)
                """,
                (
                    identity.parse_receipt_id.value,
                    identity.returned_revision_id.value,
                    diff.returned.package_sha256,
                    canonical_json(list(diff.findings)),
                    revision.value,
                ),
            )
            policy_json = canonical_json(_VIEW_POLICY)
            policy_sha256 = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
            policy = connection.execute(
                """
                SELECT document_revision_view_policy_id
                FROM document_revision_view_policies WHERE policy_sha256 = ?
                """,
                (policy_sha256,),
            ).fetchone()
            policy_id = (
                str(policy["document_revision_view_policy_id"])
                if policy is not None
                else identity.view_policy_id.value
            )
            if policy is None:
                connection.execute(
                    "INSERT INTO document_revision_view_policies VALUES (?, 1, ?, ?, ?)",
                    (policy_id, policy_json, policy_sha256, revision.value),
                )
            connection.execute(
                "INSERT INTO document_diffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identity.document_diff_id.value,
                    command.base_document_revision_id.value,
                    identity.returned_revision_id.value,
                    policy_id,
                    diff.classification,
                    canonical_json(list(diff.findings)),
                    diff.base.normalized_sha256,
                    diff.returned.normalized_sha256,
                    revision.value,
                ),
            )
            eligible = diff.delivery_eligible and prepared.base_delivery_verdict == "pass"
            effective_findings = list(diff.findings)
            if prepared.base_delivery_verdict != "pass":
                effective_findings.append("BASE_REVISION_NOT_DELIVERY_ELIGIBLE")
            if eligible:
                self._validate_table_evidence(
                    connection,
                    revision,
                    command.research_path_id,
                    prepared.table_render_receipt_id,
                    identity.evidence_validation_receipt_ids,
                )
            gate_verdict = "pass" if eligible else "fail"
            frozen = {
                "base_document_revision_id": command.base_document_revision_id.value,
                "base_document_manifest_id": prepared.base_document_manifest_id.value,
                "document_diff_id": identity.document_diff_id.value,
                "document_revision_view_policy_id": policy_id,
                "docx_sha256": payloads.docx.sha256,
                "manifest_sha256": payloads.manifest.sha256,
            }
            connection.execute(
                "INSERT INTO delivery_gate_reports VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identity.delivery_gate_report_id.value,
                    identity.returned_revision_id.value,
                    gate_verdict,
                    canonical_json(frozen),
                    canonical_json(sorted(set(effective_findings))),
                    revision.value,
                ),
            )
            observations = [
                {
                    "marker_tag": marker.marker_tag,
                    "semantic_slot": marker.semantic_slot,
                    "visible_value": marker.visible_value,
                    "tracked_change_inside": marker.tracked_change_inside,
                }
                for marker in diff.returned.markers
            ]
            connection.execute(
                "INSERT INTO document_return_reports VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identity.return_report_id.value,
                    command.base_document_revision_id.value,
                    identity.returned_revision_id.value,
                    identity.document_diff_id.value,
                    "delivery_eligible" if eligible else "conflict",
                    canonical_json(observations),
                    revision.value,
                ),
            )
            working_pointer = self._adopt_slot(
                connection,
                revision,
                command,
                "manuscript.main.working",
                identity.returned_revision_id.value,
                command.expected_working_pointer_revision,
                identity.delivery_gate_report_id.value,
            )
            if eligible:
                delivery_pointer = self._adopt_slot(
                    connection,
                    revision,
                    command,
                    "manuscript.main.delivery",
                    identity.returned_revision_id.value,
                    command.expected_delivery_pointer_revision,
                    identity.delivery_gate_report_id.value,
                )
            else:
                delivery_pointer = self._current_pointer(
                    connection, command, "manuscript.main.delivery"
                )
            connection.execute(
                """
                UPDATE operation_attempts SET status = 'completed', terminal_revision = ?
                WHERE operation_attempt_id = ?
                """,
                (revision.value, prepared.attempt_id.value),
            )
            connection.execute(
                """
                UPDATE operations SET status = 'completed', terminal_revision = ?
                WHERE operation_id = ?
                """,
                (revision.value, prepared.operation_id.value),
            )
            response = {
                "returned_document_revision_id": identity.returned_revision_id.value,
                "document_diff_id": identity.document_diff_id.value,
                "document_return_report_id": identity.return_report_id.value,
                "classification": diff.classification,
                "findings": sorted(set(effective_findings)),
                "working_pointer_revision": working_pointer,
                "delivery_pointer_revision": delivery_pointer,
                "delivery_advanced": eligible,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "document_return.finalized",
                        "document_revision",
                        identity.returned_revision_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("document_return.finalized", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=finalization_command_id,
            command_type="document_roundtrip.finalize",
            request=request,
            mutation=mutate,
            crash_injector=self._finalization_crash_injector,
        )
        response = receipt.response
        return DocumentReturnOutcome(
            DocumentRevisionId(str(response["returned_document_revision_id"])),
            identity.document_diff_id,
            identity.return_report_id,
            str(response["classification"]),
            tuple(str(item) for item in response["findings"]),
            int(response["working_pointer_revision"]),
            int(response["delivery_pointer_revision"]),
            bool(response["delivery_advanced"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def prepare_merge(
        self,
        command: MergeDocumentRevisionsCommand,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
    ) -> PreparedDocumentMerge:
        request = {
            "requested_by_turn_id": command.requested_by_turn_id.value,
            "research_path_id": command.research_path_id.value,
            "common_base_revision_id": command.common_base_revision_id.value,
            "human_revision_id": command.human_revision_id.value,
            "agent_revision_id": command.agent_revision_id.value,
            "expected_working_pointer_revision": command.expected_working_pointer_revision,
            "expected_delivery_pointer_revision": command.expected_delivery_pointer_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_merge_turn(connection, command)
            sources = self._merge_sources(connection, command)
            if int(sources["working_pointer_revision"]) != (
                command.expected_working_pointer_revision
            ):
                raise ValueError("Working Document pointer changed before merge")
            connection.execute(
                """
                INSERT INTO operations VALUES (
                    ?, 'document.render', ?, NULL, 'handoff_committed', ?, ?, NULL
                )
                """,
                (
                    operation_id.value,
                    command.requested_by_turn_id.value,
                    command.command_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO operation_attempts VALUES (
                    ?, ?, 1, NULL, 'handoff_committed', ?, NULL
                )
                """,
                (attempt_id.value, operation_id.value, revision.value),
            )
            response = {
                "document_id": str(sources["document_id"]),
                "operation_id": operation_id.value,
                "attempt_id": attempt_id.value,
                "base_docx_managed_handle": str(sources["base_handle"]),
                "human_docx_managed_handle": str(sources["human_handle"]),
                "agent_docx_managed_handle": str(sources["agent_handle"]),
                "agent_document_manifest_id": str(sources["agent_document_manifest_id"]),
                "table_render_receipt_id": str(sources["table_render_receipt_id"]),
                "table_coverage_manifest_id": str(sources["table_coverage_manifest_id"]),
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "document_merge.handoff_committed",
                        "operation",
                        operation_id.value,
                        request,
                    ),
                ),
                (OutboxDraft("document_merge.started", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="document_roundtrip.merge.prepare",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return PreparedDocumentMerge(
            DocumentId(str(response["document_id"])),
            OperationId(str(response["operation_id"])),
            OperationAttemptId(str(response["attempt_id"])),
            str(response["base_docx_managed_handle"]),
            str(response["human_docx_managed_handle"]),
            str(response["agent_docx_managed_handle"]),
            DocumentManifestId(str(response["agent_document_manifest_id"])),
            str(response["table_render_receipt_id"]),
            str(response["table_coverage_manifest_id"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def reject_return(
        self,
        command: ImportReturnedDocumentCommand,
        prepared: PreparedDocumentReturn,
        return_report_id: DocumentReturnReportId,
        raw_docx: PublishedDocumentArtifact,
        finding_code: str,
        detail: str,
    ) -> RejectedDocumentReturn:
        finalization_command_id = type(command.command_id)(
            "cmd_"
            + hashlib.sha256((command.command_id.value + ":reject").encode("utf-8")).hexdigest()[
                :28
            ]
        )
        request = {
            "operation_id": prepared.operation_id.value,
            "attempt_id": prepared.attempt_id.value,
            "base_document_revision_id": command.base_document_revision_id.value,
            "raw_docx_artifact_id": raw_docx.artifact_id.value,
            "raw_docx_sha256": raw_docx.sha256,
            "finding_code": finding_code,
            "detail": detail,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_active_turn(connection, command)
            operation = connection.execute(
                """
                SELECT operation.status, attempt.status AS attempt_status
                FROM operations AS operation
                JOIN operation_attempts AS attempt
                  ON attempt.operation_id = operation.operation_id
                WHERE operation.operation_id = ? AND attempt.operation_attempt_id = ?
                """,
                (prepared.operation_id.value, prepared.attempt_id.value),
            ).fetchone()
            if operation is None or tuple(operation) != (
                "handoff_committed",
                "handoff_committed",
            ):
                raise ValueError("Document Return is not awaiting rejection finalization")
            self._insert_artifact(connection, revision, prepared.attempt_id, raw_docx)
            connection.execute(
                """
                INSERT INTO document_raw_return_reports
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    return_report_id.value,
                    command.base_document_revision_id.value,
                    raw_docx.artifact_id.value,
                    prepared.operation_id.value,
                    prepared.attempt_id.value,
                    finding_code,
                    detail,
                    revision.value,
                ),
            )
            connection.execute(
                """
                UPDATE operation_attempts SET status = 'completed', terminal_revision = ?
                WHERE operation_attempt_id = ?
                """,
                (revision.value, prepared.attempt_id.value),
            )
            connection.execute(
                """
                UPDATE operations SET status = 'completed', terminal_revision = ?
                WHERE operation_id = ?
                """,
                (revision.value, prepared.operation_id.value),
            )
            working_pointer = self._current_pointer(connection, command, "manuscript.main.working")
            delivery_pointer = self._current_pointer(
                connection, command, "manuscript.main.delivery"
            )
            response = {
                "raw_docx_artifact_id": raw_docx.artifact_id.value,
                "document_return_report_id": return_report_id.value,
                "finding_code": finding_code,
                "detail": detail,
                "working_pointer_revision": working_pointer,
                "delivery_pointer_revision": delivery_pointer,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "document_return.rejected",
                        "artifact",
                        raw_docx.artifact_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("document_return.rejected", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=finalization_command_id,
            command_type="document_roundtrip.reject",
            request=request,
            mutation=mutate,
            crash_injector=self._finalization_crash_injector,
        )
        response = receipt.response
        return RejectedDocumentReturn(
            raw_docx.artifact_id,
            return_report_id,
            str(response["finding_code"]),
            str(response["detail"]),
            int(response["working_pointer_revision"]),
            int(response["delivery_pointer_revision"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def finalize_merge(
        self,
        command: MergeDocumentRevisionsCommand,
        prepared: PreparedDocumentMerge,
        identity: DocumentMergeIdentity,
        payloads: FinalizedDocumentReturn,
        merge: DocumentThreeWayMerge,
    ) -> DocumentMergeOutcome:
        finalization_command_id = type(command.command_id)(
            "cmd_"
            + hashlib.sha256((command.command_id.value + ":finalize").encode("utf-8")).hexdigest()[
                :28
            ]
        )
        request = {
            "operation_id": prepared.operation_id.value,
            "attempt_id": prepared.attempt_id.value,
            "common_base_revision_id": command.common_base_revision_id.value,
            "human_revision_id": command.human_revision_id.value,
            "agent_revision_id": command.agent_revision_id.value,
            "merged_revision_id": identity.merged_revision_id.value,
            "docx_sha256": payloads.docx.sha256,
            "manifest_sha256": payloads.manifest.sha256,
            "decisions": list(merge.decisions),
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_merge_turn(connection, command)
            operation = connection.execute(
                """
                SELECT operation.status, attempt.status AS attempt_status
                FROM operations AS operation
                JOIN operation_attempts AS attempt
                  ON attempt.operation_id = operation.operation_id
                WHERE operation.operation_id = ? AND attempt.operation_attempt_id = ?
                """,
                (prepared.operation_id.value, prepared.attempt_id.value),
            ).fetchone()
            if operation is None or tuple(operation) != (
                "handoff_committed",
                "handoff_committed",
            ):
                raise ValueError("Document Merge is not awaiting finalization")
            sources = self._merge_sources(connection, command)
            if int(sources["working_pointer_revision"]) != (
                command.expected_working_pointer_revision
            ):
                raise ValueError("Working Document pointer changed during merge")
            if (
                self._merge_current_pointer(connection, command, "manuscript.main.delivery")
                != command.expected_delivery_pointer_revision
            ):
                raise ValueError("Delivery Document pointer changed during merge")
            for artifact in (payloads.docx, payloads.manifest):
                self._insert_artifact(connection, revision, prepared.attempt_id, artifact)
            manifest_sha = hashlib.sha256(payloads.manifest_json.encode("utf-8")).hexdigest()
            if manifest_sha != payloads.manifest.sha256:
                raise ValueError("Merged Document Manifest changed before commit")
            connection.execute(
                "INSERT INTO document_manifests VALUES (?, ?, ?, ?, ?, 'word.rtf-to-docx.v1', ?)",
                (
                    identity.merged_manifest_id.value,
                    prepared.table_render_receipt_id,
                    prepared.table_coverage_manifest_id,
                    payloads.manifest_json,
                    manifest_sha,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO document_revisions VALUES (
                    ?, ?, 'system_merged', ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    identity.merged_revision_id.value,
                    prepared.document_id.value,
                    command.human_revision_id.value,
                    payloads.docx.artifact_id.value,
                    payloads.manifest.artifact_id.value,
                    identity.merged_manifest_id.value,
                    command.requested_by_turn_id.value,
                    prepared.operation_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO document_parse_receipts
                VALUES (?, ?, ?, 'pass', '[]', 'ooxml.safe-reader.v1', ?)
                """,
                (
                    identity.parse_receipt_id.value,
                    identity.merged_revision_id.value,
                    merge.snapshot.package_sha256,
                    revision.value,
                ),
            )
            frozen = {
                "common_base_revision_id": command.common_base_revision_id.value,
                "human_revision_id": command.human_revision_id.value,
                "agent_revision_id": command.agent_revision_id.value,
                "agent_document_manifest_id": (prepared.agent_document_manifest_id.value),
                "docx_sha256": payloads.docx.sha256,
                "manifest_sha256": payloads.manifest.sha256,
            }
            self._validate_table_evidence(
                connection,
                revision,
                command.research_path_id,
                prepared.table_render_receipt_id,
                identity.evidence_validation_receipt_ids,
            )
            connection.execute(
                "INSERT INTO delivery_gate_reports VALUES (?, ?, 'pass', ?, '[]', ?)",
                (
                    identity.delivery_gate_report_id.value,
                    identity.merged_revision_id.value,
                    canonical_json(frozen),
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO document_merge_receipts VALUES (?, ?, ?, ?, ?, 'merged', ?, ?)",
                (
                    identity.merge_receipt_id.value,
                    command.common_base_revision_id.value,
                    command.human_revision_id.value,
                    command.agent_revision_id.value,
                    identity.merged_revision_id.value,
                    canonical_json(list(merge.decisions)),
                    revision.value,
                ),
            )
            connection.executemany(
                "INSERT INTO document_revision_merge_inputs VALUES (?, ?, ?)",
                (
                    (
                        identity.merged_revision_id.value,
                        command.human_revision_id.value,
                        "human",
                    ),
                    (
                        identity.merged_revision_id.value,
                        command.agent_revision_id.value,
                        "agent",
                    ),
                ),
            )
            working_pointer = self._merge_adopt_slot(
                connection,
                revision,
                command,
                "manuscript.main.working",
                identity.merged_revision_id.value,
                command.expected_working_pointer_revision,
                identity.delivery_gate_report_id.value,
            )
            delivery_pointer = self._merge_adopt_slot(
                connection,
                revision,
                command,
                "manuscript.main.delivery",
                identity.merged_revision_id.value,
                command.expected_delivery_pointer_revision,
                identity.delivery_gate_report_id.value,
            )
            connection.execute(
                """
                UPDATE operation_attempts SET status = 'completed', terminal_revision = ?
                WHERE operation_attempt_id = ?
                """,
                (revision.value, prepared.attempt_id.value),
            )
            connection.execute(
                """
                UPDATE operations SET status = 'completed', terminal_revision = ?
                WHERE operation_id = ?
                """,
                (revision.value, prepared.operation_id.value),
            )
            response = {
                "merged_document_revision_id": identity.merged_revision_id.value,
                "document_merge_receipt_id": identity.merge_receipt_id.value,
                "decisions": list(merge.decisions),
                "working_pointer_revision": working_pointer,
                "delivery_pointer_revision": delivery_pointer,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "document_merge.finalized",
                        "document_revision",
                        identity.merged_revision_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("document_merge.finalized", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=finalization_command_id,
            command_type="document_roundtrip.merge.finalize",
            request=request,
            mutation=mutate,
            crash_injector=self._finalization_crash_injector,
        )
        response = receipt.response
        return DocumentMergeOutcome(
            DocumentRevisionId(str(response["merged_document_revision_id"])),
            identity.merge_receipt_id,
            tuple(str(item) for item in response["decisions"]),
            int(response["working_pointer_revision"]),
            int(response["delivery_pointer_revision"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _insert_artifact(
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        attempt_id: OperationAttemptId,
        artifact: PublishedDocumentArtifact,
    ) -> None:
        observed_at = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, 'sha256', ?, ?, 'internal_artifact', ?)",
            (
                artifact.artifact_id.value,
                artifact.artifact_kind,
                artifact.media_type,
                artifact.size_bytes,
                artifact.sha256,
                attempt_id.value,
                revision.value,
            ),
        )
        connection.execute(
            """
            INSERT INTO artifact_state_history
            VALUES (?, ?, 'available', 'document_return_verified', ?, ?, ?, ?)
            """,
            (
                artifact.state_observation_id.value,
                artifact.artifact_id.value,
                artifact.size_bytes,
                artifact.sha256,
                observed_at,
                revision.value,
            ),
        )
        connection.execute(
            "INSERT INTO artifact_states VALUES (?, ?, 'available', ?, ?)",
            (
                artifact.artifact_id.value,
                artifact.state_observation_id.value,
                observed_at,
                revision.value,
            ),
        )
        connection.execute(
            "INSERT INTO artifact_location_history VALUES (?, ?, 1, 'installed', ?, ?)",
            (
                artifact.location_id.value,
                artifact.artifact_id.value,
                artifact.managed_handle,
                revision.value,
            ),
        )
        connection.execute(
            "INSERT INTO artifact_locations VALUES (?, ?, 1, ?, ?)",
            (
                artifact.artifact_id.value,
                artifact.location_id.value,
                artifact.managed_handle,
                revision.value,
            ),
        )

    @staticmethod
    def _validate_table_evidence(
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        research_path_id: ResearchPathId,
        table_render_receipt_id: str,
        receipt_ids: tuple[EvidenceValidationReceiptId, ...],
    ) -> None:
        rows = connection.execute(
            """
            SELECT evidence_record_id FROM table_cell_evidence_uses
            WHERE table_render_receipt_id = ? ORDER BY rtf_byte_start
            """,
            (table_render_receipt_id,),
        ).fetchall()
        if len(rows) != len(receipt_ids):
            raise ValueError("Document Evidence validation coverage changed")
        gate = SqliteEvidenceEligibilityRepository(connection)
        for row, receipt_id in zip(rows, receipt_ids, strict=True):
            gate.require_statistical_use_in_uow(
                connection,
                revision=revision,
                receipt_id=receipt_id,
                evidence_record_id=EvidenceRecordId(str(row["evidence_record_id"])),
                research_path_id=research_path_id,
            )

    @staticmethod
    def _assert_merge_turn(
        connection: sqlite3.Connection, command: MergeDocumentRevisionsCommand
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM turns WHERE turn_id = ? AND research_path_id = ?
              AND status IN ('running', 'waiting')
            """,
            (command.requested_by_turn_id.value, command.research_path_id.value),
        ).fetchone()
        if row is None:
            raise ValueError("Document Merge Turn is not active on this Research Path")

    @staticmethod
    def _merge_sources(
        connection: sqlite3.Connection, command: MergeDocumentRevisionsCommand
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT base.document_id,
                   base_location.managed_handle AS base_handle,
                   human_location.managed_handle AS human_handle,
                   agent_location.managed_handle AS agent_handle,
                   human.primary_parent_revision_id AS human_parent,
                   agent.primary_parent_revision_id AS agent_parent,
                   agent.document_manifest_id AS agent_document_manifest_id,
                   agent_manifest.table_render_receipt_id,
                   agent_manifest.table_coverage_manifest_id,
                   working.pointer_revision AS working_pointer_revision
            FROM document_revisions AS base
            JOIN documents AS document ON document.document_id = base.document_id
            JOIN document_revisions AS human
              ON human.document_revision_id = ? AND human.document_id = base.document_id
            JOIN document_revisions AS agent
              ON agent.document_revision_id = ? AND agent.document_id = base.document_id
            JOIN document_manifests AS agent_manifest
              ON agent_manifest.document_manifest_id = agent.document_manifest_id
            JOIN artifact_locations AS base_location
              ON base_location.artifact_id = base.docx_artifact_id
            JOIN artifact_locations AS human_location
              ON human_location.artifact_id = human.docx_artifact_id
            JOIN artifact_locations AS agent_location
              ON agent_location.artifact_id = agent.docx_artifact_id
            JOIN document_slots AS working_slot
              ON working_slot.research_path_id = document.research_path_id
             AND working_slot.canonical_key = 'manuscript.main.working'
            JOIN path_document_adoptions AS working
              ON working.document_slot_id = working_slot.document_slot_id
             AND working.target_document_revision_id = human.document_revision_id
            WHERE base.document_revision_id = ? AND document.research_path_id = ?
            """,
            (
                command.human_revision_id.value,
                command.agent_revision_id.value,
                command.common_base_revision_id.value,
                command.research_path_id.value,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("Merge inputs are not a valid current same-Document set")
        if str(row["human_parent"]) != command.common_base_revision_id.value:
            raise ValueError("Human revision does not descend from the common base")
        if str(row["agent_parent"]) != command.common_base_revision_id.value:
            raise ValueError("Agent revision does not descend from the common base")
        if not isinstance(row, sqlite3.Row):
            raise TypeError("SQLite row factory contract is not active")
        return row

    @staticmethod
    def _merge_current_pointer(
        connection: sqlite3.Connection,
        command: MergeDocumentRevisionsCommand,
        key: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT adoption.pointer_revision
            FROM document_slots AS slot
            JOIN path_document_adoptions AS adoption
              ON adoption.document_slot_id = slot.document_slot_id
            WHERE slot.research_path_id = ? AND slot.canonical_key = ?
            """,
            (command.research_path_id.value, key),
        ).fetchone()
        if row is None:
            raise ValueError(f"Document slot is missing: {key}")
        return int(row["pointer_revision"])

    @staticmethod
    def _merge_adopt_slot(
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        command: MergeDocumentRevisionsCommand,
        key: str,
        target_revision_id: str,
        expected_pointer: int,
        gate_report_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT slot.document_slot_id, adoption.pointer_revision
            FROM document_slots AS slot
            JOIN path_document_adoptions AS adoption
              ON adoption.document_slot_id = slot.document_slot_id
            WHERE slot.research_path_id = ? AND slot.canonical_key = ?
            """,
            (command.research_path_id.value, key),
        ).fetchone()
        if row is None or int(row["pointer_revision"]) != expected_pointer:
            raise ValueError(f"Document slot pointer changed: {key}")
        slot_id = str(row["document_slot_id"])
        next_pointer = expected_pointer + 1
        connection.execute(
            "INSERT INTO path_document_adoption_history VALUES (?, ?, ?, ?, ?, ?)",
            (
                slot_id,
                next_pointer,
                target_revision_id,
                command.requested_by_turn_id.value,
                gate_report_id,
                revision.value,
            ),
        )
        cursor = connection.execute(
            """
            UPDATE path_document_adoptions SET
                target_document_revision_id = ?, pointer_revision = ?,
                updated_by_turn_id = ?, delivery_gate_report_id = ?, commit_revision = ?
            WHERE document_slot_id = ? AND pointer_revision = ?
            """,
            (
                target_revision_id,
                next_pointer,
                command.requested_by_turn_id.value,
                gate_report_id,
                revision.value,
                slot_id,
                expected_pointer,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Document slot CAS failed: {key}")
        return next_pointer

    @staticmethod
    def _assert_active_turn(
        connection: sqlite3.Connection, command: ImportReturnedDocumentCommand
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM turns WHERE turn_id = ? AND research_path_id = ?
              AND status IN ('running', 'waiting')
            """,
            (command.requested_by_turn_id.value, command.research_path_id.value),
        ).fetchone()
        if row is None:
            raise ValueError("Document Return Turn is not active on this Research Path")

    @staticmethod
    def _base_revision(
        connection: sqlite3.Connection, command: ImportReturnedDocumentCommand
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT revision.document_id, location.managed_handle,
                   revision.document_manifest_id,
                   manifest.table_render_receipt_id,
                   manifest.table_coverage_manifest_id,
                   adoption.pointer_revision AS working_pointer_revision,
                   gate.verdict AS base_delivery_verdict
            FROM document_revisions AS revision
            JOIN documents AS document ON document.document_id = revision.document_id
            JOIN document_manifests AS manifest
              ON manifest.document_manifest_id = revision.document_manifest_id
            JOIN artifact_locations AS location
              ON location.artifact_id = revision.docx_artifact_id
            JOIN document_slots AS slot
              ON slot.research_path_id = document.research_path_id
             AND slot.canonical_key = 'manuscript.main.working'
            JOIN path_document_adoptions AS adoption
              ON adoption.document_slot_id = slot.document_slot_id
             AND adoption.target_document_revision_id = revision.document_revision_id
            JOIN delivery_gate_reports AS gate
              ON gate.document_revision_id = revision.document_revision_id
            WHERE revision.document_revision_id = ?
              AND document.research_path_id = ?
            """,
            (
                command.base_document_revision_id.value,
                command.research_path_id.value,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("Base revision is not the current Working Document")
        if not isinstance(row, sqlite3.Row):
            raise TypeError("SQLite row factory contract is not active")
        return row

    @staticmethod
    def _current_pointer(
        connection: sqlite3.Connection,
        command: ImportReturnedDocumentCommand,
        key: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT adoption.pointer_revision
            FROM document_slots AS slot
            JOIN path_document_adoptions AS adoption
              ON adoption.document_slot_id = slot.document_slot_id
            WHERE slot.research_path_id = ? AND slot.canonical_key = ?
            """,
            (command.research_path_id.value, key),
        ).fetchone()
        if row is None:
            raise ValueError(f"Document slot is missing: {key}")
        return int(row["pointer_revision"])

    @classmethod
    def _adopt_slot(
        cls,
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        command: ImportReturnedDocumentCommand,
        key: str,
        target_revision_id: str,
        expected_pointer: int,
        gate_report_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT slot.document_slot_id, adoption.pointer_revision
            FROM document_slots AS slot
            JOIN path_document_adoptions AS adoption
              ON adoption.document_slot_id = slot.document_slot_id
            WHERE slot.research_path_id = ? AND slot.canonical_key = ?
            """,
            (command.research_path_id.value, key),
        ).fetchone()
        if row is None or int(row["pointer_revision"]) != expected_pointer:
            raise ValueError(f"Document slot pointer changed: {key}")
        next_pointer = expected_pointer + 1
        slot_id = str(row["document_slot_id"])
        connection.execute(
            "INSERT INTO path_document_adoption_history VALUES (?, ?, ?, ?, ?, ?)",
            (
                slot_id,
                next_pointer,
                target_revision_id,
                command.requested_by_turn_id.value,
                gate_report_id,
                revision.value,
            ),
        )
        connection.execute(
            """
            UPDATE path_document_adoptions SET
                target_document_revision_id = ?, pointer_revision = ?,
                updated_by_turn_id = ?, delivery_gate_report_id = ?, commit_revision = ?
            WHERE document_slot_id = ? AND pointer_revision = ?
            """,
            (
                target_revision_id,
                next_pointer,
                command.requested_by_turn_id.value,
                gate_report_id,
                revision.value,
                slot_id,
                expected_pointer,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError(f"Document slot CAS failed: {key}")
        return next_pointer
