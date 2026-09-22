"""Authority port for Waiting and user-pause control."""

from collections.abc import Callable
from typing import Protocol

from stata_research_agent.application.turn_interaction import (
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
from stata_research_agent.domain.identifiers import ToolResultId


class TurnInteractionRepository(Protocol):
    def open_waiting(
        self, command: OpenWaitingCommand, identity: WaitingIdentity
    ) -> WaitingOutcome: ...

    def answer_waiting(
        self, command: AnswerWaitingCommand, identity: WaitingAnswerIdentity
    ) -> WaitingOutcome: ...

    def request_pause(
        self, command: RequestPauseCommand, identity: PauseIdentity
    ) -> PauseOutcome: ...

    def converge_pause(
        self,
        command: ConvergePauseCommand,
        result_identity_factory: Callable[[], ToolResultId],
    ) -> PauseOutcome: ...

    def continue_paused_turn(
        self, command: ContinuePausedTurnCommand, identity: ContinuationIdentity
    ) -> ContinuationOutcome: ...
