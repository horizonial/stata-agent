"""ID allocation and orchestration for runtime evaluation."""

from stata_research_agent.domain.identifiers import (
    CompletionContractRevisionId,
    CompletionObligationId,
    EvaluationFindingId,
    EvaluationPolicySnapshotId,
    EvaluationReportId,
    EvaluationRequestId,
    EvidenceScopeManifestId,
    GoalCoverageId,
    ObligationObservationId,
    StopGuardDecisionRecordId,
    WaitingRequestId,
)

from .evaluation import (
    DecideNaturalStopCommand,
    EvaluationIdentity,
    EvaluationOutcome,
    NormalizeCompletionContractCommand,
    NormalizeContractOutcome,
    NormalizedContractIdentity,
    ObligationStateOutcome,
    RecordEvaluationCommand,
    RecordObligationStateCommand,
    StopGuardIdentity,
    StopGuardOutcome,
)
from .ports.evaluation import EvaluationRepository
from .ports.identity import IdentityGenerator


class RuntimeEvaluationService:
    def __init__(self, repository: EvaluationRepository, identities: IdentityGenerator) -> None:
        self._repository = repository
        self._identities = identities

    def normalize_contract(
        self, command: NormalizeCompletionContractCommand
    ) -> NormalizeContractOutcome:
        return self._repository.normalize_contract(
            command,
            NormalizedContractIdentity(
                self._identities.new(CompletionContractRevisionId),
                tuple(self._identities.new(CompletionObligationId) for _ in command.obligations),
            ),
        )

    def record_obligation_state(
        self, command: RecordObligationStateCommand
    ) -> ObligationStateOutcome:
        return self._repository.record_obligation_state(
            command, self._identities.new(ObligationObservationId)
        )

    def record_evaluation(self, command: RecordEvaluationCommand) -> EvaluationOutcome:
        return self._repository.record_evaluation(
            command,
            EvaluationIdentity(
                self._identities.new(EvaluationPolicySnapshotId),
                self._identities.new(EvidenceScopeManifestId),
                self._identities.new(EvaluationRequestId),
                self._identities.new(EvaluationReportId),
                tuple(self._identities.new(EvaluationFindingId) for _ in command.findings),
            ),
        )

    def decide_natural_stop(self, command: DecideNaturalStopCommand) -> StopGuardOutcome:
        return self._repository.decide_natural_stop(
            command,
            StopGuardIdentity(
                self._identities.new(GoalCoverageId),
                self._identities.new(StopGuardDecisionRecordId),
                self._identities.new(WaitingRequestId),
            ),
        )
