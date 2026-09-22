"""Path-aware Evidence eligibility and last-known source-state projection DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from stata_research_agent.domain.identifiers import (
    CommandId,
    EvidenceRecordId,
    EvidenceValidationReceiptId,
    ResearchPathId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


class EvidenceUseContext(StrEnum):
    CURRENT_ADOPTED_RESULT = "current_adopted_result"
    EXPLICIT_HISTORICAL_COMPARISON = "explicit_historical_comparison"
    AUDIT_OR_EXPLANATION = "audit_or_explanation"


class EvidenceUsePurpose(StrEnum):
    MESSAGE_AUTHORING = "message_authoring"
    DOCUMENT_DELIVERY = "document_delivery"
    LINEAGE_PREVIEW = "lineage_preview"


class EligibilityVerdict(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EvidenceEligibilityQuery:
    evidence_record_id: EvidenceRecordId
    research_path_id: ResearchPathId
    use_context_kind: EvidenceUseContext
    purpose: EvidenceUsePurpose


@dataclass(frozen=True, slots=True)
class EvidenceEligibility:
    evidence_record_id: EvidenceRecordId
    evidence_kind: str
    research_path_id: ResearchPathId
    use_context_kind: EvidenceUseContext
    purpose: EvidenceUsePurpose
    verdict: EligibilityVerdict
    reason_codes: tuple[str, ...]
    source_state: str
    source_state_reason: str
    authoritative_revision: WorkspaceRevision
    projection_revision: WorkspaceRevision
    dependency_snapshot: tuple[dict[str, object], ...]

    @property
    def projection_lag(self) -> int:
        return self.authoritative_revision.value - self.projection_revision.value


@dataclass(frozen=True, slots=True)
class ValidateEvidenceUseCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId
    query: EvidenceEligibilityQuery


@dataclass(frozen=True, slots=True)
class EvidenceUseValidation:
    receipt_id: EvidenceValidationReceiptId
    eligibility: EvidenceEligibility
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class EvidenceProjectionRebuild:
    projection_revision: WorkspaceRevision
    statistical_count: int
    analysis_count: int
