"""Commands and immutable facts for human Word roundtrips."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stata_research_agent.domain.identifiers import (
    ArtifactId,
    CommandId,
    DeliveryGateReportId,
    DocumentDiffId,
    DocumentId,
    DocumentManifestId,
    DocumentMergeReceiptId,
    DocumentParseReceiptId,
    DocumentReturnReportId,
    DocumentRevisionId,
    DocumentRevisionViewPolicyId,
    EvidenceValidationReceiptId,
    OperationAttemptId,
    OperationId,
    ResearchPathId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .document_delivery import DocumentArtifactIdentity, PublishedDocumentArtifact


@dataclass(frozen=True, slots=True)
class ImportReturnedDocumentCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId
    research_path_id: ResearchPathId
    base_document_revision_id: DocumentRevisionId
    returned_path: Path
    expected_working_pointer_revision: int
    expected_delivery_pointer_revision: int


@dataclass(frozen=True, slots=True)
class PreparedDocumentReturn:
    document_id: DocumentId
    operation_id: OperationId
    attempt_id: OperationAttemptId
    base_docx_managed_handle: str
    base_document_manifest_id: DocumentManifestId
    table_render_receipt_id: str
    table_coverage_manifest_id: str
    base_delivery_verdict: str
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class DocumentReturnIdentity:
    returned_revision_id: DocumentRevisionId
    returned_manifest_id: DocumentManifestId
    parse_receipt_id: DocumentParseReceiptId
    delivery_gate_report_id: DeliveryGateReportId
    view_policy_id: DocumentRevisionViewPolicyId
    document_diff_id: DocumentDiffId
    return_report_id: DocumentReturnReportId
    docx: DocumentArtifactIdentity
    manifest: DocumentArtifactIdentity
    evidence_validation_receipt_ids: tuple[EvidenceValidationReceiptId, ...]


@dataclass(frozen=True, slots=True)
class DocumentReturnOutcome:
    returned_revision_id: DocumentRevisionId
    document_diff_id: DocumentDiffId
    return_report_id: DocumentReturnReportId
    classification: str
    findings: tuple[str, ...]
    working_pointer_revision: int
    delivery_pointer_revision: int
    delivery_advanced: bool
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class RejectedDocumentReturn:
    raw_artifact_id: ArtifactId
    return_report_id: DocumentReturnReportId
    finding_code: str
    detail: str
    working_pointer_revision: int
    delivery_pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class FinalizedDocumentReturn:
    docx: PublishedDocumentArtifact
    manifest: PublishedDocumentArtifact
    manifest_json: str


@dataclass(frozen=True, slots=True)
class MergeDocumentRevisionsCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId
    research_path_id: ResearchPathId
    common_base_revision_id: DocumentRevisionId
    human_revision_id: DocumentRevisionId
    agent_revision_id: DocumentRevisionId
    expected_working_pointer_revision: int
    expected_delivery_pointer_revision: int


@dataclass(frozen=True, slots=True)
class PreparedDocumentMerge:
    document_id: DocumentId
    operation_id: OperationId
    attempt_id: OperationAttemptId
    base_docx_managed_handle: str
    human_docx_managed_handle: str
    agent_docx_managed_handle: str
    agent_document_manifest_id: DocumentManifestId
    table_render_receipt_id: str
    table_coverage_manifest_id: str
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class DocumentMergeIdentity:
    merged_revision_id: DocumentRevisionId
    merged_manifest_id: DocumentManifestId
    parse_receipt_id: DocumentParseReceiptId
    delivery_gate_report_id: DeliveryGateReportId
    merge_receipt_id: DocumentMergeReceiptId
    docx: DocumentArtifactIdentity
    manifest: DocumentArtifactIdentity
    evidence_validation_receipt_ids: tuple[EvidenceValidationReceiptId, ...]


@dataclass(frozen=True, slots=True)
class DocumentMergeOutcome:
    merged_revision_id: DocumentRevisionId
    merge_receipt_id: DocumentMergeReceiptId
    decisions: tuple[str, ...]
    working_pointer_revision: int
    delivery_pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class MarkerObservation:
    marker_tag: str
    semantic_slot: str
    visible_value: str
    tracked_change_inside: bool


@dataclass(frozen=True, slots=True)
class DocumentSemanticSnapshot:
    package_sha256: str
    normalized_sha256: str
    prose_sha256: str
    table_sha256: str
    markers: tuple[MarkerObservation, ...]
    tracked_change_count: int


@dataclass(frozen=True, slots=True)
class DocumentSemanticDiff:
    classification: str
    findings: tuple[str, ...]
    base: DocumentSemanticSnapshot
    returned: DocumentSemanticSnapshot

    @property
    def delivery_eligible(self) -> bool:
        return self.classification in {"no_semantic_change", "prose_only"}


@dataclass(frozen=True, slots=True)
class DocumentThreeWayMerge:
    payload: bytes
    decisions: tuple[str, ...]
    snapshot: DocumentSemanticSnapshot
