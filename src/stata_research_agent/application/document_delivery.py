"""Commands and immutable facts for the minimal Word delivery vertical."""

from __future__ import annotations

from dataclasses import dataclass

from stata_research_agent.domain.evidence import NUMERIC_PATTERN
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
    ResearchPathId,
    TableRenderReceiptId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class ManuscriptSections:
    title: str
    abstract: str
    research_question: str
    data_and_methods: str
    results: str
    limitations: str
    conclusion: str

    def __post_init__(self) -> None:
        for name, value in self.as_mapping().items():
            if not value.strip():
                raise ValueError(f"manuscript section {name} is required")
            if len(value) > 20_000:
                raise ValueError(f"manuscript section {name} exceeds the size limit")
            if NUMERIC_PATTERN.search(value):
                raise ValueError(
                    f"manuscript section {name} contains an unbound numeric occurrence"
                )

    def as_mapping(self) -> dict[str, str]:
        return {
            "title": self.title,
            "abstract": self.abstract,
            "research_question": self.research_question,
            "data_and_methods": self.data_and_methods,
            "results": self.results,
            "limitations": self.limitations,
            "conclusion": self.conclusion,
        }


@dataclass(frozen=True, slots=True)
class DeliverEsttabDocumentCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId
    research_path_id: ResearchPathId
    table_render_receipt_id: TableRenderReceiptId
    expected_working_pointer_revision: int = 0
    expected_delivery_pointer_revision: int = 0
    document_key: str = "manuscript.main"
    manuscript: ManuscriptSections | None = None


@dataclass(frozen=True, slots=True)
class DocumentTableCell:
    table_cell_use_id: str
    semantic_cell_slot: str
    rendered_text: str
    evidence_record_id: str
    result_element_id: str


@dataclass(frozen=True, slots=True)
class PreparedDocumentRender:
    document_id: DocumentId
    operation_id: OperationId
    attempt_id: OperationAttemptId
    table_artifact_id: ArtifactId
    table_managed_handle: str
    table_coverage_manifest_id: str
    cells: tuple[DocumentTableCell, ...]
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class DocumentArtifactIdentity:
    artifact_id: ArtifactId
    state_observation_id: ArtifactStateObservationId
    location_id: ArtifactLocationId


@dataclass(frozen=True, slots=True)
class DocumentDeliveryIdentity:
    document_revision_id: DocumentRevisionId
    document_manifest_id: DocumentManifestId
    parse_receipt_id: DocumentParseReceiptId
    delivery_gate_report_id: DeliveryGateReportId
    working_slot_id: DocumentSlotId
    delivery_slot_id: DocumentSlotId
    docx: DocumentArtifactIdentity
    manifest: DocumentArtifactIdentity
    evidence_validation_receipt_ids: tuple[EvidenceValidationReceiptId, ...]


@dataclass(frozen=True, slots=True)
class PublishedDocumentArtifact:
    artifact_id: ArtifactId
    state_observation_id: ArtifactStateObservationId
    location_id: ArtifactLocationId
    artifact_kind: str
    media_type: str
    managed_handle: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DocxInspection:
    """Fail-closed inspection facts returned by a document-rendering adapter."""

    package_sha256: str
    visible_text: str
    findings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DocumentDeliveryOutcome:
    document_id: DocumentId
    document_revision_id: DocumentRevisionId
    docx_artifact_id: ArtifactId
    manifest_artifact_id: ArtifactId
    delivery_gate_report_id: DeliveryGateReportId
    verdict: str
    working_pointer_revision: int
    delivery_pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool
