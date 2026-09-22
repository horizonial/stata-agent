"""Authority port for explicit post-Turn outcome feedback."""

from typing import Protocol

from stata_research_agent.application.outcome_feedback import (
    RecordTurnOutcomeFeedbackCommand,
    TurnOutcomeFeedbackOutcome,
)
from stata_research_agent.domain.identifiers import TurnOutcomeFeedbackId


class TurnOutcomeFeedbackRepository(Protocol):
    def record(
        self,
        command: RecordTurnOutcomeFeedbackCommand,
        feedback_id: TurnOutcomeFeedbackId,
    ) -> TurnOutcomeFeedbackOutcome: ...
