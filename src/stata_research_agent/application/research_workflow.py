"""A narrow M2 vertical that composes trusted M1 services into a manuscript outcome."""

from __future__ import annotations

from dataclasses import dataclass

from stata_research_agent.domain.identifiers import (
    ArtifactVerificationReceiptId,
    DataVersionId,
    DocumentRevisionId,
    PlanNodeId,
    PlanRevisionId,
    ResearchPathId,
    ResultId,
    TableRenderReceiptId,
    TurnId,
)


@dataclass(frozen=True, slots=True)
class RunResearchToWordCommand:
    requested_by_turn_id: TurnId
    research_path_id: ResearchPathId
    data_version_id: DataVersionId
    verification_receipt_id: ArtifactVerificationReceiptId
    managed_data_handle: str
    session_id: str
    dependent_variable: str
    terms: tuple[str, ...]
    plan_revision_id: PlanRevisionId | None = None
    plan_node_id: PlanNodeId | None = None
    result_slot_key: str = "baseline.primary"
    data_slot_key: str = "analysis.primary"
    document_title: str = "Baseline regression"
    expected_data_pointer_revision: int = 0
    expected_result_pointer_revision: int = 0
    expected_working_document_pointer_revision: int = 0
    expected_delivery_document_pointer_revision: int = 0

    def __post_init__(self) -> None:
        if self.plan_revision_id is None or self.plan_node_id is None:
            raise ValueError(
                "autonomous formal research requires an adopted Plan Revision and Node"
            )
        if (
            min(
                self.expected_data_pointer_revision,
                self.expected_result_pointer_revision,
                self.expected_working_document_pointer_revision,
                self.expected_delivery_document_pointer_revision,
            )
            < 0
        ):
            raise ValueError("expected pointer revisions cannot be negative")


@dataclass(frozen=True, slots=True)
class ResearchToWordOutcome:
    result_id: ResultId
    table_render_receipt_id: TableRenderReceiptId
    document_revision_id: DocumentRevisionId
    docx_artifact_id: str
    delivery_verdict: str
