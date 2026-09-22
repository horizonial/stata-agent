"""Persistence port for Project Memory commands."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.memory import (
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
from stata_research_agent.domain.identifiers import (
    MemoryCompactionCheckpointId,
    MemoryEpisodeId,
    MemoryStateHistoryId,
)


class MemoryRepository(Protocol):
    def create(self, command: CreateMemoryCommand, identity: MemoryIdentity) -> MemoryOutcome: ...

    def revise(
        self, command: ReviseMemoryCommand, identity: MemoryRevisionIdentity
    ) -> MemoryOutcome: ...

    def activate(
        self, command: ActivateMemoryCommand, state_history_id: MemoryStateHistoryId
    ) -> MemoryOutcome: ...

    def retract(
        self, command: RetractMemoryCommand, state_history_id: MemoryStateHistoryId
    ) -> MemoryOutcome: ...

    def set_access_tier(
        self, command: SetMemoryAccessTierCommand, identity: MemoryRetentionIdentity
    ) -> MemoryRetentionOutcome: ...

    def supersede(
        self, command: SupersedeMemoryCommand, identity: MemoryRetentionIdentity
    ) -> MemoryRetentionOutcome: ...

    def set_conversation_policy(
        self, command: SetConversationMemoryPolicyCommand
    ) -> ConversationMemoryPolicyOutcome: ...

    def record_episode(
        self, command: RecordMemoryEpisodeCommand, memory_episode_id: MemoryEpisodeId
    ) -> MemoryEpisodeOutcome: ...

    def record_compaction_checkpoint(
        self,
        command: RecordMemoryCompactionCheckpointCommand,
        checkpoint_id: MemoryCompactionCheckpointId,
    ) -> MemoryCompactionCheckpointOutcome: ...
