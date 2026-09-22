"""Ports for path-aware Evidence eligibility and its rebuildable projection."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.evidence_eligibility import (
    EvidenceEligibility,
    EvidenceEligibilityQuery,
    EvidenceProjectionRebuild,
    EvidenceUseValidation,
    ValidateEvidenceUseCommand,
)
from stata_research_agent.domain.identifiers import EvidenceValidationReceiptId


class EvidenceEligibilityRepository(Protocol):
    def query(self, query: EvidenceEligibilityQuery) -> EvidenceEligibility: ...

    def validate_for_use(
        self,
        command: ValidateEvidenceUseCommand,
        receipt_id: EvidenceValidationReceiptId,
    ) -> EvidenceUseValidation: ...

    def rebuild_projection(self) -> EvidenceProjectionRebuild: ...
