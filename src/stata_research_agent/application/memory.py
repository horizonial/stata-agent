"""Project Memory contracts.

Memory preserves project continuity. It is never a Result, Evidence source, Current Pointer,
or substitute for live Research State. Stable items receive immutable revisions; correction
and forgetting move a separately recorded current-state pointer without rewriting history.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from stata_research_agent.domain.identifiers import (
    CommandId,
    ConversationId,
    MemoryCompactionCheckpointId,
    MemoryEpisodeId,
    MemoryItemId,
    MemoryRetentionHistoryId,
    MemoryRevisionId,
    MemoryRevisionSourceId,
    MemoryStateHistoryId,
    ResearchPathId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


class MemoryScopeKind(StrEnum):
    WORKSPACE = "workspace"
    RESEARCH_PATH = "research_path"


class MemoryKind(StrEnum):
    USER_PREFERENCE = "user_preference"
    RESEARCH_DECISION = "research_decision"
    RESEARCH_CONSTRAINT = "research_constraint"
    FEEDBACK = "feedback"
    UNRESOLVED_QUESTION = "unresolved_question"
    REFERENCE_POINTER = "reference_pointer"
    PROJECT_PROCEDURE = "project_procedure"


class MemoryOriginKind(StrEnum):
    EXPLICIT_USER = "explicit_user"
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    IMPORTED = "imported"


class MemoryLifecycle(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    RETRACTED = "retracted"


class MemoryAccessTier(StrEnum):
    """Recall accessibility, deliberately separate from semantic validity."""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    ARCHIVED = "archived"


class MemorySourceRole(StrEnum):
    USER_STATEMENT = "user_statement"
    USER_CONFIRMATION = "user_confirmation"
    ASSISTANT_INFERENCE = "assistant_inference"
    RESEARCH_FACT_REFERENCE = "research_fact_reference"
    EXTERNAL_REFERENCE = "external_reference"
    IMPORT = "import"


@dataclass(frozen=True, slots=True)
class MemorySource:
    object_type: str
    object_id: str
    object_revision: str
    role: MemorySourceRole

    def __post_init__(self) -> None:
        if not all(
            value.strip() for value in (self.object_type, self.object_id, self.object_revision)
        ):
            raise ValueError("Memory source identity is required")


@dataclass(frozen=True, slots=True)
class CreateMemoryCommand:
    command_id: CommandId
    kind: MemoryKind
    title: str
    content: str
    origin: MemoryOriginKind
    sources: tuple[MemorySource, ...]
    scope_kind: MemoryScopeKind = MemoryScopeKind.WORKSPACE
    research_path_id: ResearchPathId | None = None
    requested_lifecycle: MemoryLifecycle | None = None

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.content.strip():
            raise ValueError("Memory title and content are required")
        if not self.sources:
            raise ValueError("Memory requires at least one source")
        if (self.scope_kind is MemoryScopeKind.RESEARCH_PATH) != (
            self.research_path_id is not None
        ):
            raise ValueError("Research Path Memory requires an exact Research Path")
        if self.requested_lifecycle is MemoryLifecycle.RETRACTED:
            raise ValueError("new Memory cannot start retracted")
        if (
            self.origin is MemoryOriginKind.INFERRED
            and self.requested_lifecycle is MemoryLifecycle.ACTIVE
        ):
            raise ValueError("inferred Memory must be proposed before activation")
        _validate_origin_sources(self.origin, self.sources)


@dataclass(frozen=True, slots=True)
class ReviseMemoryCommand:
    command_id: CommandId
    memory_item_id: MemoryItemId
    expected_pointer_revision: int
    title: str
    content: str
    origin: MemoryOriginKind
    sources: tuple[MemorySource, ...]
    requested_lifecycle: MemoryLifecycle = MemoryLifecycle.ACTIVE

    def __post_init__(self) -> None:
        if self.expected_pointer_revision < 1:
            raise ValueError("expected Memory pointer revision must be positive")
        if not self.title.strip() or not self.content.strip() or not self.sources:
            raise ValueError("Memory revision requires title, content, and sources")
        if self.requested_lifecycle is MemoryLifecycle.RETRACTED:
            raise ValueError("use RetractMemoryCommand to retract Memory")
        if (
            self.origin is MemoryOriginKind.INFERRED
            and self.requested_lifecycle is MemoryLifecycle.ACTIVE
        ):
            raise ValueError("inferred Memory must be proposed before activation")
        _validate_origin_sources(self.origin, self.sources)


@dataclass(frozen=True, slots=True)
class ActivateMemoryCommand:
    command_id: CommandId
    memory_item_id: MemoryItemId
    memory_revision_id: MemoryRevisionId
    expected_pointer_revision: int


@dataclass(frozen=True, slots=True)
class RetractMemoryCommand:
    command_id: CommandId
    memory_item_id: MemoryItemId
    expected_pointer_revision: int
    reason: str

    def __post_init__(self) -> None:
        if self.expected_pointer_revision < 1 or not self.reason.strip():
            raise ValueError("Memory retraction requires pointer revision and reason")


@dataclass(frozen=True, slots=True)
class SetMemoryAccessTierCommand:
    command_id: CommandId
    memory_item_id: MemoryItemId
    expected_retention_revision: int
    access_tier: MemoryAccessTier
    reason: str

    def __post_init__(self) -> None:
        if self.expected_retention_revision < 1 or not self.reason.strip():
            raise ValueError("Memory access transition requires revision and reason")


@dataclass(frozen=True, slots=True)
class SupersedeMemoryCommand:
    command_id: CommandId
    memory_item_id: MemoryItemId
    successor_memory_item_id: MemoryItemId
    expected_retention_revision: int
    reason: str

    def __post_init__(self) -> None:
        if self.memory_item_id == self.successor_memory_item_id:
            raise ValueError("Memory cannot supersede itself")
        if self.expected_retention_revision < 1 or not self.reason.strip():
            raise ValueError("Memory supersession requires revision and reason")


@dataclass(frozen=True, slots=True)
class SetConversationMemoryPolicyCommand:
    command_id: CommandId
    conversation_id: ConversationId
    use_memory: bool
    contribute_memory: bool
    expected_policy_revision: int | None = None


@dataclass(frozen=True, slots=True)
class RecordMemoryEpisodeCommand:
    command_id: CommandId
    conversation_id: ConversationId
    source_start_revision: int
    source_end_revision: int
    summary: str
    extractor_kind: str
    extractor_revision: str

    def __post_init__(self) -> None:
        if (
            self.source_start_revision < 1
            or self.source_end_revision < self.source_start_revision
            or not self.summary.strip()
            or not self.extractor_kind.strip()
            or not self.extractor_revision.strip()
        ):
            raise ValueError("invalid Memory Episode")


@dataclass(frozen=True, slots=True)
class RecordMemoryCompactionCheckpointCommand:
    command_id: CommandId
    conversation_id: ConversationId
    source_start_revision: int
    source_end_revision: int
    required_source_message_ids: tuple[str, ...]
    policy_revision: str = "memory-compaction-v1"

    def __post_init__(self) -> None:
        if (
            self.source_start_revision < 1
            or self.source_end_revision < self.source_start_revision
            or not self.policy_revision.strip()
            or any(not value.startswith("msg_") for value in self.required_source_message_ids)
        ):
            raise ValueError("invalid Memory compaction checkpoint")


@dataclass(frozen=True, slots=True)
class MemoryIdentity:
    memory_item_id: MemoryItemId
    memory_revision_id: MemoryRevisionId
    source_ids: tuple[MemoryRevisionSourceId, ...]
    state_history_id: MemoryStateHistoryId


@dataclass(frozen=True, slots=True)
class MemoryRevisionIdentity:
    memory_revision_id: MemoryRevisionId
    source_ids: tuple[MemoryRevisionSourceId, ...]
    state_history_id: MemoryStateHistoryId


@dataclass(frozen=True, slots=True)
class MemoryOutcome:
    memory_item_id: MemoryItemId
    memory_revision_id: MemoryRevisionId
    lifecycle: MemoryLifecycle
    pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class MemoryRetentionIdentity:
    history_id: MemoryRetentionHistoryId


@dataclass(frozen=True, slots=True)
class MemoryRetentionOutcome:
    memory_item_id: MemoryItemId
    access_tier: MemoryAccessTier
    retention_revision: int
    superseded_by_memory_item_id: MemoryItemId | None
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class ConversationMemoryPolicyOutcome:
    conversation_id: ConversationId
    use_memory: bool
    contribute_memory: bool
    policy_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class MemoryEpisodeOutcome:
    memory_episode_id: MemoryEpisodeId
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class MemoryCompactionCheckpointOutcome:
    checkpoint_id: MemoryCompactionCheckpointId
    disposition: str
    uncovered_source_message_ids: tuple[str, ...]
    commit_revision: WorkspaceRevision
    replayed: bool


def _validate_origin_sources(origin: MemoryOriginKind, sources: tuple[MemorySource, ...]) -> None:
    roles = {source.role for source in sources}
    required = {
        MemoryOriginKind.EXPLICIT_USER: {
            MemorySourceRole.USER_STATEMENT,
            MemorySourceRole.USER_CONFIRMATION,
        },
        MemoryOriginKind.CONFIRMED: {
            MemorySourceRole.USER_STATEMENT,
            MemorySourceRole.USER_CONFIRMATION,
        },
        MemoryOriginKind.INFERRED: {MemorySourceRole.ASSISTANT_INFERENCE},
        MemoryOriginKind.IMPORTED: {MemorySourceRole.IMPORT},
    }[origin]
    if not roles.intersection(required):
        raise ValueError(f"{origin.value} Memory lacks an appropriate source role")
