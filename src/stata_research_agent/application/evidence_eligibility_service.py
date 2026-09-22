"""Application facade for Evidence eligibility."""

from stata_research_agent.domain.identifiers import EvidenceValidationReceiptId

from .evidence_eligibility import (
    EvidenceEligibility,
    EvidenceEligibilityQuery,
    EvidenceProjectionRebuild,
    EvidenceUseValidation,
    ValidateEvidenceUseCommand,
)
from .ports.evidence_eligibility import EvidenceEligibilityRepository
from .ports.identity import IdentityGenerator


class EvidenceEligibilityService:
    def __init__(
        self,
        repository: EvidenceEligibilityRepository,
        identities: IdentityGenerator,
    ) -> None:
        self._repository = repository
        self._identities = identities

    def query(self, query: EvidenceEligibilityQuery) -> EvidenceEligibility:
        return self._repository.query(query)

    def validate_for_use(self, command: ValidateEvidenceUseCommand) -> EvidenceUseValidation:
        return self._repository.validate_for_use(
            command, self._identities.new(EvidenceValidationReceiptId)
        )

    def rebuild_projection(self) -> EvidenceProjectionRebuild:
        return self._repository.rebuild_projection()
