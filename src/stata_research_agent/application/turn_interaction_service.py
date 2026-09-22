"""Application orchestration for Waiting and user pause."""

from stata_research_agent.domain.identifiers import (
    MessageId,
    PauseIntentId,
    ToolResultId,
    TurnContinuationId,
    TurnId,
    WaitingAnswerId,
    WaitingRequestId,
)

from .ports.identity import IdentityGenerator
from .ports.turn_interaction import TurnInteractionRepository
from .turn_interaction import (
    AnswerWaitingCommand,
    ContinuationIdentity,
    ContinuationOutcome,
    ContinuePausedTurnCommand,
    ConvergePauseCommand,
    OpenWaitingCommand,
    PauseIdentity,
    PauseOutcome,
    RequestPauseCommand,
    WaitingAnswerIdentity,
    WaitingIdentity,
    WaitingOutcome,
)


class TurnInteractionService:
    def __init__(
        self, repository: TurnInteractionRepository, identities: IdentityGenerator
    ) -> None:
        self._repository = repository
        self._identities = identities

    def open_waiting(self, command: OpenWaitingCommand) -> WaitingOutcome:
        return self._repository.open_waiting(
            command, WaitingIdentity(self._identities.new(WaitingRequestId))
        )

    def answer_waiting(self, command: AnswerWaitingCommand) -> WaitingOutcome:
        return self._repository.answer_waiting(
            command,
            WaitingAnswerIdentity(
                self._identities.new(WaitingAnswerId),
                self._identities.new(MessageId),
                self._identities.new(ToolResultId),
            ),
        )

    def request_pause(self, command: RequestPauseCommand) -> PauseOutcome:
        return self._repository.request_pause(
            command,
            PauseIdentity(self._identities.new(PauseIntentId), ()),
        )

    def converge_pause(self, command: ConvergePauseCommand) -> PauseOutcome:
        return self._repository.converge_pause(command, lambda: self._identities.new(ToolResultId))

    def continue_paused_turn(self, command: ContinuePausedTurnCommand) -> ContinuationOutcome:
        return self._repository.continue_paused_turn(
            command,
            ContinuationIdentity(
                self._identities.new(TurnContinuationId),
                self._identities.new(TurnId),
                self._identities.new(MessageId),
            ),
        )
