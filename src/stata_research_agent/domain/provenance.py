"""Closed provenance reference union; it is not a polymorphic persistence relation."""

from dataclasses import dataclass
from typing import Literal

from .identifiers import CommandId, MessageId, OperationAttemptId, TurnId


@dataclass(frozen=True, slots=True)
class UserMessageProvenance:
    kind: Literal["user_message"]
    message_id: MessageId


@dataclass(frozen=True, slots=True)
class AgentTurnProvenance:
    kind: Literal["agent_turn"]
    turn_id: TurnId


@dataclass(frozen=True, slots=True)
class OperationAttemptProvenance:
    kind: Literal["operation_attempt"]
    operation_attempt_id: OperationAttemptId


@dataclass(frozen=True, slots=True)
class MigrationCommandProvenance:
    kind: Literal["migration_command"]
    command_id: CommandId


type ProvenanceRef = (
    UserMessageProvenance
    | AgentTurnProvenance
    | OperationAttemptProvenance
    | MigrationCommandProvenance
)
