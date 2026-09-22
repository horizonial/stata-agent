"""Application service for voluntary user outcome feedback."""

from stata_research_agent.application.outcome_feedback import (
    RecordTurnOutcomeFeedbackCommand,
    TurnOutcomeFeedbackOutcome,
)
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.ports.outcome_feedback import TurnOutcomeFeedbackRepository
from stata_research_agent.domain.identifiers import TurnOutcomeFeedbackId


class TurnOutcomeFeedbackService:
    def __init__(
        self,
        repository: TurnOutcomeFeedbackRepository,
        identities: IdentityGenerator,
    ) -> None:
        self._repository = repository
        self._identities = identities

    def record(self, command: RecordTurnOutcomeFeedbackCommand) -> TurnOutcomeFeedbackOutcome:
        return self._repository.record(
            command,
            self._identities.new(TurnOutcomeFeedbackId),
        )
