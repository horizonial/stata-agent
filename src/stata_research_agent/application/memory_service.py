"""Application orchestration for Project Memory."""

from __future__ import annotations

from dataclasses import replace

from stata_research_agent.domain.identifiers import (
    MemoryCompactionCheckpointId,
    MemoryEpisodeId,
    MemoryItemId,
    MemoryRetentionHistoryId,
    MemoryRevisionId,
    MemoryRevisionSourceId,
    MemoryStateHistoryId,
)

from .memory import (
    ActivateMemoryCommand,
    ConversationMemoryPolicyOutcome,
    CreateMemoryCommand,
    MemoryCompactionCheckpointOutcome,
    MemoryEpisodeOutcome,
    MemoryIdentity,
    MemoryOutcome,
    MemoryRetentionIdentity,
    MemoryRetentionOutcome,
    MemoryRevisionIdentity,
    RecordMemoryCompactionCheckpointCommand,
    RecordMemoryEpisodeCommand,
    RetractMemoryCommand,
    ReviseMemoryCommand,
    SetConversationMemoryPolicyCommand,
    SetMemoryAccessTierCommand,
    SupersedeMemoryCommand,
)
from .ports.identity import IdentityGenerator
from .ports.memory import MemoryRepository
from .sensitive_output import SensitiveOutputGate


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        identities: IdentityGenerator,
        sensitive_output_gate: SensitiveOutputGate | None = None,
    ) -> None:
        self._repository = repository
        self._identities = identities
        self._sensitive = sensitive_output_gate or SensitiveOutputGate()

    def create(self, command: CreateMemoryCommand) -> MemoryOutcome:
        command = replace(
            command,
            title=str(self._sensitive.inspect_text("memory.title", command.title).safe_value),
            content=str(self._sensitive.inspect_text("memory.content", command.content).safe_value),
        )
        return self._repository.create(
            command,
            MemoryIdentity(
                self._identities.new(MemoryItemId),
                self._identities.new(MemoryRevisionId),
                tuple(self._identities.new(MemoryRevisionSourceId) for _ in command.sources),
                self._identities.new(MemoryStateHistoryId),
            ),
        )

    def revise(self, command: ReviseMemoryCommand) -> MemoryOutcome:
        command = replace(
            command,
            title=str(self._sensitive.inspect_text("memory.title", command.title).safe_value),
            content=str(self._sensitive.inspect_text("memory.content", command.content).safe_value),
        )
        return self._repository.revise(
            command,
            MemoryRevisionIdentity(
                self._identities.new(MemoryRevisionId),
                tuple(self._identities.new(MemoryRevisionSourceId) for _ in command.sources),
                self._identities.new(MemoryStateHistoryId),
            ),
        )

    def activate(self, command: ActivateMemoryCommand) -> MemoryOutcome:
        return self._repository.activate(command, self._identities.new(MemoryStateHistoryId))

    def retract(self, command: RetractMemoryCommand) -> MemoryOutcome:
        return self._repository.retract(command, self._identities.new(MemoryStateHistoryId))

    def set_access_tier(self, command: SetMemoryAccessTierCommand) -> MemoryRetentionOutcome:
        return self._repository.set_access_tier(
            command,
            MemoryRetentionIdentity(self._identities.new(MemoryRetentionHistoryId)),
        )

    def supersede(self, command: SupersedeMemoryCommand) -> MemoryRetentionOutcome:
        return self._repository.supersede(
            command,
            MemoryRetentionIdentity(self._identities.new(MemoryRetentionHistoryId)),
        )

    def set_conversation_policy(
        self, command: SetConversationMemoryPolicyCommand
    ) -> ConversationMemoryPolicyOutcome:
        return self._repository.set_conversation_policy(command)

    def record_episode(self, command: RecordMemoryEpisodeCommand) -> MemoryEpisodeOutcome:
        command = replace(
            command,
            summary=str(self._sensitive.inspect_text("memory.episode", command.summary).safe_value),
        )
        return self._repository.record_episode(command, self._identities.new(MemoryEpisodeId))

    def record_compaction_checkpoint(
        self, command: RecordMemoryCompactionCheckpointCommand
    ) -> MemoryCompactionCheckpointOutcome:
        return self._repository.record_compaction_checkpoint(
            command,
            self._identities.new(MemoryCompactionCheckpointId),
        )
