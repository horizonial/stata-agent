"""Orchestration for deterministic read-only assessment and Recovery UoW."""

from stata_research_agent.domain.identifiers import RecoveryReportId

from .ports.identity import IdentityGenerator
from .ports.recovery import RecoveryAssessmentSource, RecoveryRepository
from .recovery import RecoverOperationCommand, RecoveryAssessment, RecoveryOutcome


class RecoveryService:
    def __init__(
        self,
        assessments: RecoveryAssessmentSource,
        repository: RecoveryRepository,
        identities: IdentityGenerator,
    ) -> None:
        self._assessments = assessments
        self._repository = repository
        self._identities = identities

    def assess(self, command: RecoverOperationCommand) -> RecoveryAssessment:
        return self._assessments.assess(command.operation_id)

    def recover(self, command: RecoverOperationCommand) -> RecoveryOutcome:
        assessment = self.assess(command)
        return self._repository.record(
            command,
            assessment,
            self._identities.new(RecoveryReportId),
        )
