"""Application orchestration for Analysis Output promotion."""

from stata_research_agent.domain.identifiers import (
    AnalysisDocumentEligibilityId,
    AnalysisOutputAdoptionId,
    AnalysisOutputClassificationId,
    AnalysisOutputId,
    EnvironmentSnapshotId,
    EvidenceRecordId,
)

from .analysis_output import (
    AdoptAnalysisOutputCommand,
    AdoptedAnalysisOutput,
    AnalysisAdoptionIdentity,
    AnalysisOutputIdentity,
    ClassifiedAnalysisOutput,
    ClassifyAnalysisOutputCommand,
)
from .ports.analysis_output import AnalysisOutputRepository
from .ports.identity import IdentityGenerator


class AnalysisOutputService:
    def __init__(self, repository: AnalysisOutputRepository, identities: IdentityGenerator) -> None:
        self._repository = repository
        self._identities = identities

    def classify(self, command: ClassifyAnalysisOutputCommand) -> ClassifiedAnalysisOutput:
        return self._repository.classify(
            command,
            AnalysisOutputIdentity(
                self._identities.new(AnalysisOutputId),
                self._identities.new(EnvironmentSnapshotId),
                self._identities.new(AnalysisOutputClassificationId),
            ),
        )

    def adopt(self, command: AdoptAnalysisOutputCommand) -> AdoptedAnalysisOutput:
        return self._repository.adopt(
            command,
            AnalysisAdoptionIdentity(
                self._identities.new(AnalysisOutputAdoptionId),
                self._identities.new(EvidenceRecordId),
                self._identities.new(AnalysisDocumentEligibilityId),
            ),
        )
