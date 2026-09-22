"""Render a verified Stata RTF table into an immutable, deliverable DOCX revision."""

from __future__ import annotations

import hashlib
import json

from stata_research_agent.domain.identifiers import (
    ArtifactId,
    ArtifactLocationId,
    ArtifactStateObservationId,
    CommandId,
    DeliveryGateReportId,
    DocumentId,
    DocumentManifestId,
    DocumentParseReceiptId,
    DocumentRevisionId,
    DocumentSlotId,
    EvidenceValidationReceiptId,
    OperationAttemptId,
    OperationId,
)

from .document_delivery import (
    DeliverEsttabDocumentCommand,
    DocumentArtifactIdentity,
    DocumentDeliveryIdentity,
    DocumentDeliveryOutcome,
    PublishedDocumentArtifact,
)
from .ports.artifact_data import ManagedArtifactStore, ManagedPayload
from .ports.document_delivery import DocumentDeliveryRepository, DocumentRenderer
from .ports.identity import IdentityGenerator
from .sensitive_output import SensitiveOutputGate


class DocumentDeliveryService:
    def __init__(
        self,
        repository: DocumentDeliveryRepository,
        managed_store: ManagedArtifactStore,
        identities: IdentityGenerator,
        renderer: DocumentRenderer,
        sensitive_output_gate: SensitiveOutputGate | None = None,
    ) -> None:
        self._repository = repository
        self._managed_store = managed_store
        self._identities = identities
        self._renderer = renderer
        self._sensitive_output_gate = sensitive_output_gate or SensitiveOutputGate()

    def deliver(self, command: DeliverEsttabDocumentCommand) -> DocumentDeliveryOutcome:
        document_id = self._identities.new(DocumentId)
        operation_id = self._identities.new(OperationId)
        attempt_id = self._identities.new(OperationAttemptId)
        prepared = self._repository.prepare_render(command, document_id, operation_id, attempt_id)
        identities = DocumentDeliveryIdentity(
            self._identities.new(DocumentRevisionId),
            self._identities.new(DocumentManifestId),
            self._identities.new(DocumentParseReceiptId),
            self._identities.new(DeliveryGateReportId),
            self._identities.new(DocumentSlotId),
            self._identities.new(DocumentSlotId),
            DocumentArtifactIdentity(
                self._identities.new(ArtifactId),
                self._identities.new(ArtifactStateObservationId),
                self._identities.new(ArtifactLocationId),
            ),
            DocumentArtifactIdentity(
                self._identities.new(ArtifactId),
                self._identities.new(ArtifactStateObservationId),
                self._identities.new(ArtifactLocationId),
            ),
            tuple(self._identities.new(EvidenceValidationReceiptId) for _ in prepared.cells),
        )
        rtf_payload = self._managed_store.read_small_payload(
            prepared.table_managed_handle, max_bytes=10 * 1024 * 1024
        )
        if (
            self._sensitive_output_gate.assert_clean_bytes(
                "document.source_table", rtf_payload
            ).verdict
            != "safe"
        ):
            raise RuntimeError("CREDENTIAL_OUTPUT_BLOCKED")
        staging = self._managed_store.prepare_attempt_staging(prepared.attempt_id)
        docx_path = self._renderer.render(
            rtf_payload, staging_root=staging, manuscript=command.manuscript
        )
        unmarked_payload = docx_path.read_bytes()
        docx_payload, marker_tags = self._renderer.mark_evidence(
            unmarked_payload,
            prepared.cells,
            document_revision_id=identities.document_revision_id.value,
        )
        if (
            self._sensitive_output_gate.assert_clean_archive_bytes(
                "document.delivery", docx_payload
            ).verdict
            != "safe"
        ):
            raise RuntimeError("CREDENTIAL_OUTPUT_BLOCKED")
        marked_docx_path = self._managed_store.write_staging_bytes(
            prepared.attempt_id,
            "document-marked.docx",
            docx_payload,
        )
        inspection = self._renderer.inspect(
            docx_payload, tuple(cell.rendered_text for cell in prepared.cells)
        )
        manifest_value = {
            "schema_version": "stata-agent.document-manifest/v1",
            "document_id": prepared.document_id.value,
            "document_revision_id": identities.document_revision_id.value,
            "origin_kind": "agent_generated",
            "renderer_profile": self._renderer.profile_id,
            "table_render_receipt_id": command.table_render_receipt_id.value,
            "table_coverage_manifest_id": prepared.table_coverage_manifest_id,
            "manuscript_sections": (
                None if command.manuscript is None else command.manuscript.as_mapping()
            ),
            "cells": [
                {
                    "table_cell_use_id": cell.table_cell_use_id,
                    "semantic_cell_slot": cell.semantic_cell_slot,
                    "rendered_text": cell.rendered_text,
                    "evidence_record_id": cell.evidence_record_id,
                    "result_element_id": cell.result_element_id,
                    "marker_tag": marker_tag,
                }
                for cell, marker_tag in zip(prepared.cells, marker_tags, strict=True)
            ],
            "docx_sha256": inspection.package_sha256,
        }
        manifest_json = json.dumps(
            manifest_value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (
            self._sensitive_output_gate.assert_clean_bytes(
                "document.manifest", manifest_json.encode("utf-8")
            ).verdict
            != "safe"
        ):
            raise RuntimeError("CREDENTIAL_OUTPUT_BLOCKED")
        manifest_path = self._managed_store.write_staging_bytes(
            prepared.attempt_id,
            "document-manifest.json",
            manifest_json.encode("utf-8"),
        )
        docx_payload_info = self._managed_store.publish_candidate(
            marked_docx_path,
            artifact_id=identities.docx.artifact_id,
            attempt_id=prepared.attempt_id,
        )
        manifest_payload_info = self._managed_store.publish_candidate(
            manifest_path,
            artifact_id=identities.manifest.artifact_id,
            attempt_id=prepared.attempt_id,
        )
        if docx_payload_info.sha256 != hashlib.sha256(docx_payload).hexdigest():
            raise RuntimeError("DOCX changed between inspection and managed publish")
        return self._repository.finalize_delivery(
            command,
            prepared,
            identities,
            self._published(
                identities.docx,
                "document",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                docx_payload_info,
            ),
            self._published(
                identities.manifest,
                "document",
                "application/json",
                manifest_payload_info,
            ),
            manifest_json,
            inspection,
            self._identities.new(CommandId),
        )

    @staticmethod
    def _published(
        identity: DocumentArtifactIdentity,
        kind: str,
        media_type: str,
        payload: ManagedPayload,
    ) -> PublishedDocumentArtifact:
        return PublishedDocumentArtifact(
            identity.artifact_id,
            identity.state_observation_id,
            identity.location_id,
            kind,
            media_type,
            payload.managed_handle,
            payload.size_bytes,
            payload.sha256,
        )
