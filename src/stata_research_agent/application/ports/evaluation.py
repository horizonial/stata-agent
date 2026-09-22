"""Authority port for runtime evaluation and deterministic stop decisions."""

from typing import Protocol

from stata_research_agent.application.evaluation import (
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
from stata_research_agent.domain.identifiers import ObligationObservationId


class EvaluationRepository(Protocol):
    def normalize_contract(
        self,
        command: NormalizeCompletionContractCommand,
        identity: NormalizedContractIdentity,
    ) -> NormalizeContractOutcome: ...

    def record_obligation_state(
        self,
        command: RecordObligationStateCommand,
        observation_id: ObligationObservationId,
    ) -> ObligationStateOutcome: ...

    def record_evaluation(
        self, command: RecordEvaluationCommand, identity: EvaluationIdentity
    ) -> EvaluationOutcome: ...

    def decide_natural_stop(
        self, command: DecideNaturalStopCommand, identity: StopGuardIdentity
    ) -> StopGuardOutcome: ...
