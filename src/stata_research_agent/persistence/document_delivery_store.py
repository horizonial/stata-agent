"""SQLite Unit of Work for minimal DOCX revision, gate, and dual adoption."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from stata_research_agent.application.document_delivery import (
    DeliverEsttabDocumentCommand,
    DocumentDeliveryIdentity,
    DocumentDeliveryOutcome,
    DocumentTableCell,
    DocxInspection,
    PreparedDocumentRender,
    PublishedDocumentArtifact,
)
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    CommandId,
    DocumentId,
    EvidenceRecordId,
    OperationAttemptId,
    OperationId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)
from .evidence_eligibility_store import SqliteEvidenceEligibilityRepository


class SqliteDocumentDeliveryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def prepare_render(
        self,
        command: DeliverEsttabDocumentCommand,
        document_id: DocumentId,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
    ) -> PreparedDocumentRender:
        request = {
            "research_path_id": command.research_path_id.value,
            "table_render_receipt_id": command.table_render_receipt_id.value,
            "document_key": command.document_key,
            "manuscript_sections": (
                None if command.manuscript is None else command.manuscript.as_mapping()
            ),
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_turn_path(connection, command)
            source = self._delivery_source(connection, command)
            existing_document = connection.execute(
                """
                SELECT document_id FROM documents
                WHERE research_path_id = ? AND canonical_key = ?
                """,
                (command.research_path_id.value, command.document_key),
            ).fetchone()
            actual_document_id = (
                str(existing_document["document_id"])
                if existing_document is not None
                else document_id.value
            )
            if existing_document is None:
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?)",
                    (
                        actual_document_id,
                        command.research_path_id.value,
                        command.document_key,
                        revision.value,
                    ),
                )
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
            cells = connection.execute(
                """
                SELECT table_cell_evidence_use_id, semantic_cell_slot,
                       rendered_text, evidence_record_id, result_element_id
                FROM table_cell_evidence_uses
                WHERE table_render_receipt_id = ?
                ORDER BY rtf_byte_start
                """,
                (command.table_render_receipt_id.value,),
            ).fetchall()
            expected_cell_count = int(source["controlled_cell_count"])
            if expected_cell_count < 1 or len(cells) != expected_cell_count:
                raise ValueError(
                    "DOCX delivery requires every controlled table cell to have Evidence"
                )
            response = {
                "document_id": actual_document_id,
                "operation_id": operation_id.value,
                "attempt_id": attempt_id.value,
                "table_artifact_id": str(source["table_artifact_id"]),
                "table_managed_handle": str(source["managed_handle"]),
                "table_coverage_manifest_id": str(source["table_coverage_manifest_id"]),
                "cells": [dict(row) for row in cells],
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "document_render.handoff_committed",
                        "operation",
                        operation_id.value,
                        {"table_render_receipt_id": (command.table_render_receipt_id.value)},
                    ),
                ),
                (OutboxDraft("document_render.started", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="document_delivery.prepare",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return PreparedDocumentRender(
            DocumentId(str(response["document_id"])),
            OperationId(str(response["operation_id"])),
            OperationAttemptId(str(response["attempt_id"])),
            self._artifact_id(str(response["table_artifact_id"])),
            str(response["table_managed_handle"]),
            str(response["table_coverage_manifest_id"]),
            tuple(
                DocumentTableCell(
                    str(item["table_cell_evidence_use_id"]),
                    str(item["semantic_cell_slot"]),
                    str(item["rendered_text"]),
                    str(item["evidence_record_id"]),
                    str(item["result_element_id"]),
                )
                for item in response["cells"]
            ),
            receipt.commit_revision,
            receipt.replayed,
        )

    def finalize_delivery(
        self,
        command: DeliverEsttabDocumentCommand,
        prepared: PreparedDocumentRender,
        identities: DocumentDeliveryIdentity,
        docx: PublishedDocumentArtifact,
        manifest: PublishedDocumentArtifact,
        manifest_json: str,
        inspection: DocxInspection,
        finalization_command_id: CommandId,
    ) -> DocumentDeliveryOutcome:
        request = {
            "operation_id": prepared.operation_id.value,
            "document_id": prepared.document_id.value,
            "document_revision_id": identities.document_revision_id.value,
            "docx_sha256": docx.sha256,
            "manifest_sha256": manifest.sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_turn_path(connection, command)
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
                raise ValueError("Document Render is not awaiting finalization")
            self._delivery_source(connection, command)
            for artifact in (docx, manifest):
                self._insert_artifact(
                    connection,
                    revision,
                    prepared.attempt_id,
                    artifact,
                )
            if len(identities.evidence_validation_receipt_ids) != len(prepared.cells):
                raise ValueError("Document Evidence validation identity count changed")
            evidence_gate = SqliteEvidenceEligibilityRepository(connection)
            for cell, receipt_id in zip(
                prepared.cells,
                identities.evidence_validation_receipt_ids,
                strict=True,
            ):
                evidence_gate.require_statistical_use_in_uow(
                    connection,
                    revision=revision,
                    receipt_id=receipt_id,
                    evidence_record_id=EvidenceRecordId(cell.evidence_record_id),
                    research_path_id=command.research_path_id,
                )
            manifest_sha = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
            if manifest_sha != manifest.sha256:
                raise ValueError("Document Manifest payload changed before commit")
            connection.execute(
                "INSERT INTO document_manifests VALUES (?, ?, ?, ?, ?, 'word.rtf-to-docx.v1', ?)",
                (
                    identities.document_manifest_id.value,
                    command.table_render_receipt_id.value,
                    prepared.table_coverage_manifest_id,
                    manifest_json,
                    manifest_sha,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO document_revisions VALUES (
                    ?, ?, 'agent_generated', NULL, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    identities.document_revision_id.value,
                    prepared.document_id.value,
                    docx.artifact_id.value,
                    manifest.artifact_id.value,
                    identities.document_manifest_id.value,
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
                    identities.parse_receipt_id.value,
                    identities.document_revision_id.value,
                    inspection.package_sha256,
                    revision.value,
                ),
            )
            frozen = {
                "table_render_receipt_id": command.table_render_receipt_id.value,
                "table_coverage_manifest_id": prepared.table_coverage_manifest_id,
                "docx_sha256": docx.sha256,
                "manifest_sha256": manifest.sha256,
                "renderer_profile": "word.rtf-to-docx.v1",
                "parser_profile": "ooxml.safe-reader.v1",
            }
            connection.execute(
                "INSERT INTO delivery_gate_reports VALUES (?, ?, 'pass', ?, '[]', ?)",
                (
                    identities.delivery_gate_report_id.value,
                    identities.document_revision_id.value,
                    canonical_json(frozen),
                    revision.value,
                ),
            )
            working_revision = self._adopt_slot(
                connection,
                revision,
                command,
                identities.working_slot_id.value,
                "manuscript.main.working",
                identities.document_revision_id.value,
                command.expected_working_pointer_revision,
                None,
            )
            delivery_revision = self._adopt_slot(
                connection,
                revision,
                command,
                identities.delivery_slot_id.value,
                "manuscript.main.delivery",
                identities.document_revision_id.value,
                command.expected_delivery_pointer_revision,
                identities.delivery_gate_report_id.value,
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
                "document_id": prepared.document_id.value,
                "document_revision_id": identities.document_revision_id.value,
                "docx_artifact_id": docx.artifact_id.value,
                "manifest_artifact_id": manifest.artifact_id.value,
                "delivery_gate_report_id": identities.delivery_gate_report_id.value,
                "verdict": "pass",
                "working_pointer_revision": working_revision,
                "delivery_pointer_revision": delivery_revision,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "document_revision.delivered",
                        "document_revision",
                        identities.document_revision_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("document_revision.delivered", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=finalization_command_id,
            command_type="document_delivery.finalize",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return DocumentDeliveryOutcome(
            DocumentId(str(response["document_id"])),
            identities.document_revision_id,
            self._artifact_id(str(response["docx_artifact_id"])),
            self._artifact_id(str(response["manifest_artifact_id"])),
            identities.delivery_gate_report_id,
            str(response["verdict"]),
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
            VALUES (?, ?, 'available', 'document_render_verified', ?, ?, ?, ?)
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
    def _adopt_slot(
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        command: DeliverEsttabDocumentCommand,
        slot_candidate_id: str,
        key: str,
        document_revision_id: str,
        expected_pointer: int,
        gate_report_id: str | None,
    ) -> int:
        slot = connection.execute(
            """
            SELECT document_slot_id FROM document_slots
            WHERE research_path_id = ? AND canonical_key = ?
            """,
            (command.research_path_id.value, key),
        ).fetchone()
        slot_id = str(slot["document_slot_id"]) if slot else slot_candidate_id
        if slot is None:
            connection.execute(
                "INSERT INTO document_slots VALUES (?, ?, ?, 'active', ?)",
                (slot_id, command.research_path_id.value, key, revision.value),
            )
        current = connection.execute(
            "SELECT pointer_revision FROM path_document_adoptions WHERE document_slot_id = ?",
            (slot_id,),
        ).fetchone()
        current_revision = int(current["pointer_revision"]) if current else 0
        if current_revision != expected_pointer:
            raise ValueError(f"Document slot pointer changed: {key}")
        next_revision = current_revision + 1
        values = (
            slot_id,
            next_revision,
            document_revision_id,
            command.requested_by_turn_id.value,
            gate_report_id,
            revision.value,
        )
        connection.execute(
            "INSERT INTO path_document_adoption_history VALUES (?, ?, ?, ?, ?, ?)",
            values,
        )
        connection.execute(
            """
            INSERT INTO path_document_adoptions VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_slot_id) DO UPDATE SET
                target_document_revision_id = excluded.target_document_revision_id,
                pointer_revision = excluded.pointer_revision,
                updated_by_turn_id = excluded.updated_by_turn_id,
                delivery_gate_report_id = excluded.delivery_gate_report_id,
                commit_revision = excluded.commit_revision
            """,
            (
                slot_id,
                document_revision_id,
                next_revision,
                command.requested_by_turn_id.value,
                gate_report_id,
                revision.value,
            ),
        )
        return next_revision

    @staticmethod
    def _assert_turn_path(
        connection: sqlite3.Connection, command: DeliverEsttabDocumentCommand
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM turns WHERE turn_id = ? AND research_path_id = ?
              AND status IN ('running', 'waiting')
            """,
            (command.requested_by_turn_id.value, command.research_path_id.value),
        ).fetchone()
        if row is None:
            raise ValueError("Document Delivery Turn is not active on this Research Path")

    @staticmethod
    def _delivery_source(
        connection: sqlite3.Connection, command: DeliverEsttabDocumentCommand
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT export.table_artifact_id, location.managed_handle,
                   coverage.table_coverage_manifest_id,
                   export.estimation_state_gate, render.cell_evidence_gate,
                   shape.controlled_cell_count,
                   coverage.coverage_status, input.research_path_id,
                   input.source_result_id, adoption.target_result_id
            FROM table_render_receipts AS render
            JOIN table_export_manifests AS export
              ON export.table_export_manifest_id = render.table_export_manifest_id
            JOIN table_export_input_manifests AS input
              ON input.table_export_input_manifest_id =
                 export.table_export_input_manifest_id
            JOIN table_numeric_coverage_manifests AS coverage
              ON coverage.table_render_receipt_id = render.table_render_receipt_id
            JOIN table_render_shape_manifests AS shape
              ON shape.table_render_receipt_id = render.table_render_receipt_id
            JOIN artifact_states AS state ON state.artifact_id = export.table_artifact_id
             AND state.availability = 'available'
            JOIN artifact_locations AS location
              ON location.artifact_id = export.table_artifact_id
            JOIN path_result_adoptions AS adoption
              ON adoption.result_slot_id = input.result_slot_id
            WHERE render.table_render_receipt_id = ?
              AND input.research_path_id = ?
            """,
            (
                command.table_render_receipt_id.value,
                command.research_path_id.value,
            ),
        ).fetchone()
        if row is None or tuple(
            row[key]
            for key in (
                "estimation_state_gate",
                "cell_evidence_gate",
                "coverage_status",
            )
        ) != ("pass", "pass", "complete"):
            raise ValueError("Table is not eligible for Document Delivery")
        if str(row["source_result_id"]) != str(row["target_result_id"]):
            raise ValueError("Table source Result is no longer current")
        if not isinstance(row, sqlite3.Row):
            raise TypeError("SQLite row factory contract is not active")
        return row

    @staticmethod
    def _artifact_id(value: str) -> ArtifactId:
        return ArtifactId(value)
