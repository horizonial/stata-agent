"""Capture, compare, and commit a human-returned Word revision."""

from __future__ import annotations

import hashlib
import json
import zipfile
from xml.etree import ElementTree

from stata_research_agent.domain.identifiers import (
    ArtifactId,
    ArtifactLocationId,
    ArtifactStateObservationId,
    DeliveryGateReportId,
    DocumentDiffId,
    DocumentManifestId,
    DocumentMergeReceiptId,
    DocumentParseReceiptId,
    DocumentReturnReportId,
    DocumentRevisionId,
    DocumentRevisionViewPolicyId,
    EvidenceValidationReceiptId,
    OperationAttemptId,
    OperationId,
)

from .document_delivery import DocumentArtifactIdentity, PublishedDocumentArtifact
from .document_roundtrip import (
    DocumentMergeIdentity,
    DocumentMergeOutcome,
    DocumentReturnIdentity,
    DocumentReturnOutcome,
    FinalizedDocumentReturn,
    ImportReturnedDocumentCommand,
    MergeDocumentRevisionsCommand,
    RejectedDocumentReturn,
)
from .ports.artifact_data import ManagedArtifactStore, ManagedPayload
from .ports.document_roundtrip import (
    DocumentRoundtripEngine,
    DocumentRoundtripRepository,
)
from .ports.identity import IdentityGenerator


class DocumentRoundtripService:
    def __init__(
        self,
        repository: DocumentRoundtripRepository,
        managed_store: ManagedArtifactStore,
        identities: IdentityGenerator,
        roundtrip: DocumentRoundtripEngine,
    ) -> None:
        self._repository = repository
        self._managed_store = managed_store
        self._identities = identities
        self._roundtrip = roundtrip

    def import_returned(
        self, command: ImportReturnedDocumentCommand
    ) -> DocumentReturnOutcome | RejectedDocumentReturn:
        operation_id = self._identities.new(OperationId)
        attempt_id = self._identities.new(OperationAttemptId)
        prepared = self._repository.prepare_return(command, operation_id, attempt_id)
        identity = DocumentReturnIdentity(
            self._identities.new(DocumentRevisionId),
            self._identities.new(DocumentManifestId),
            self._identities.new(DocumentParseReceiptId),
            self._identities.new(DeliveryGateReportId),
            self._identities.new(DocumentRevisionViewPolicyId),
            self._identities.new(DocumentDiffId),
            self._identities.new(DocumentReturnReportId),
            self._artifact_identity(),
            self._artifact_identity(),
            tuple(self._identities.new(EvidenceValidationReceiptId) for _ in range(8)),
        )
        captured = self._managed_store.capture(
            command.returned_path,
            artifact_id=identity.docx.artifact_id,
            attempt_id=prepared.attempt_id,
        )
        raw_docx = self._published(
            identity.docx,
            "document",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            captured,
        )
        try:
            returned_payload = self._managed_store.read_small_payload(
                captured.managed_handle, max_bytes=50 * 1024 * 1024
            )
            base_payload = self._managed_store.read_small_payload(
                prepared.base_docx_managed_handle, max_bytes=50 * 1024 * 1024
            )
            diff = self._roundtrip.compare(base_payload, returned_payload)
        except (ValueError, zipfile.BadZipFile, ElementTree.ParseError) as error:
            return self._repository.reject_return(
                command,
                prepared,
                identity.return_report_id,
                raw_docx,
                self._package_finding(str(error)),
                str(error),
            )
        marker_observations = [
            {
                "marker_tag": marker.marker_tag,
                "semantic_slot": marker.semantic_slot,
                "visible_value": marker.visible_value,
                "tracked_change_inside": marker.tracked_change_inside,
            }
            for marker in diff.returned.markers
        ]
        effective_findings = list(diff.findings)
        if prepared.base_delivery_verdict != "pass":
            effective_findings.append("BASE_REVISION_NOT_DELIVERY_ELIGIBLE")
        inherits_evidence = diff.delivery_eligible and prepared.base_delivery_verdict == "pass"
        manifest_value = {
            "schema_version": "stata-agent.document-manifest/v2",
            "document_id": prepared.document_id.value,
            "document_revision_id": identity.returned_revision_id.value,
            "origin_kind": "user_returned",
            "base_document_revision_id": command.base_document_revision_id.value,
            "semantic_change_classification": diff.classification,
            "findings": sorted(set(effective_findings)),
            "evidence_inheritance": "preserved" if inherits_evidence else "blocked",
            "marker_observations": marker_observations,
            "docx_sha256": captured.sha256,
        }
        manifest_json = json.dumps(
            manifest_value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest_path = self._managed_store.write_staging_bytes(
            prepared.attempt_id,
            "returned-document-manifest.json",
            manifest_json.encode("utf-8"),
        )
        manifest_payload = self._managed_store.publish_candidate(
            manifest_path,
            artifact_id=identity.manifest.artifact_id,
            attempt_id=prepared.attempt_id,
        )
        if captured.sha256 != hashlib.sha256(returned_payload).hexdigest():
            raise RuntimeError("returned DOCX changed after stable capture")
        return self._repository.finalize_return(
            command,
            prepared,
            identity,
            FinalizedDocumentReturn(
                raw_docx,
                self._published(
                    identity.manifest,
                    "document",
                    "application/json",
                    manifest_payload,
                ),
                manifest_json,
            ),
            diff,
        )

    @staticmethod
    def _package_finding(detail: str) -> str:
        lowered = detail.lower()
        if "external relationship" in lowered:
            return "DOCX_EXTERNAL_DEPENDENCY"
        if "active or embedded" in lowered:
            return "DOCX_ACTIVE_CONTENT_BLOCKED"
        if "unsupported" in lowered or "size limit" in lowered:
            return "DOCX_UNSUPPORTED_FEATURE"
        return "DOCX_PACKAGE_INVALID"

    def merge(self, command: MergeDocumentRevisionsCommand) -> DocumentMergeOutcome:
        prepared = self._repository.prepare_merge(
            command,
            self._identities.new(OperationId),
            self._identities.new(OperationAttemptId),
        )
        identity = DocumentMergeIdentity(
            self._identities.new(DocumentRevisionId),
            self._identities.new(DocumentManifestId),
            self._identities.new(DocumentParseReceiptId),
            self._identities.new(DeliveryGateReportId),
            self._identities.new(DocumentMergeReceiptId),
            self._artifact_identity(),
            self._artifact_identity(),
            tuple(self._identities.new(EvidenceValidationReceiptId) for _ in range(8)),
        )
        base_payload = self._managed_store.read_small_payload(
            prepared.base_docx_managed_handle, max_bytes=50 * 1024 * 1024
        )
        human_payload = self._managed_store.read_small_payload(
            prepared.human_docx_managed_handle, max_bytes=50 * 1024 * 1024
        )
        agent_payload = self._managed_store.read_small_payload(
            prepared.agent_docx_managed_handle, max_bytes=50 * 1024 * 1024
        )
        merge = self._roundtrip.merge(base_payload, human_payload, agent_payload)
        docx_path = self._managed_store.write_staging_bytes(
            prepared.attempt_id, "merged-document.docx", merge.payload
        )
        docx_payload = self._managed_store.publish_candidate(
            docx_path,
            artifact_id=identity.docx.artifact_id,
            attempt_id=prepared.attempt_id,
        )
        manifest_value = {
            "schema_version": "stata-agent.document-manifest/v2",
            "document_id": prepared.document_id.value,
            "document_revision_id": identity.merged_revision_id.value,
            "origin_kind": "system_merged",
            "common_base_revision_id": command.common_base_revision_id.value,
            "human_revision_id": command.human_revision_id.value,
            "agent_revision_id": command.agent_revision_id.value,
            "evidence_source_document_manifest_id": (prepared.agent_document_manifest_id.value),
            "decisions": list(merge.decisions),
            "marker_observations": [
                {
                    "marker_tag": marker.marker_tag,
                    "semantic_slot": marker.semantic_slot,
                    "visible_value": marker.visible_value,
                    "tracked_change_inside": marker.tracked_change_inside,
                }
                for marker in merge.snapshot.markers
            ],
            "docx_sha256": docx_payload.sha256,
        }
        manifest_json = json.dumps(
            manifest_value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest_path = self._managed_store.write_staging_bytes(
            prepared.attempt_id,
            "merged-document-manifest.json",
            manifest_json.encode("utf-8"),
        )
        manifest_payload = self._managed_store.publish_candidate(
            manifest_path,
            artifact_id=identity.manifest.artifact_id,
            attempt_id=prepared.attempt_id,
        )
        return self._repository.finalize_merge(
            command,
            prepared,
            identity,
            FinalizedDocumentReturn(
                self._published(
                    identity.docx,
                    "document",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    docx_payload,
                ),
                self._published(
                    identity.manifest,
                    "document",
                    "application/json",
                    manifest_payload,
                ),
                manifest_json,
            ),
            merge,
        )

    def _artifact_identity(self) -> DocumentArtifactIdentity:
        return DocumentArtifactIdentity(
            self._identities.new(ArtifactId),
            self._identities.new(ArtifactStateObservationId),
            self._identities.new(ArtifactLocationId),
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
