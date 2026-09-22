"""Contracts for asynchronous Project Memory extraction and consolidation.

The Curator is deliberately outside the Turn reasoning loop.  Its model output is an
untrusted proposal; only the repository's deterministic finalizer may create Memory facts.
"""

from __future__ import annotations

from dataclasses import dataclass

from stata_research_agent.application.memory import MemoryKind, MemoryLifecycle
from stata_research_agent.domain.identifiers import (
    ConversationId,
    MemoryMaintenanceJobId,
    MemoryProviderAttemptId,
    MessageId,
)


@dataclass(frozen=True, slots=True)
class MemorySourceMessage:
    message_id: MessageId
    created_revision: int
    content: str
    research_path_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryMaintenanceWindow:
    conversation_id: ConversationId
    source_start_revision: int
    source_end_revision: int
    messages: tuple[MemorySourceMessage, ...]
    assistant_texts: tuple[str, ...]
    active_memory_index: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemoryMaintenanceJob:
    job_id: MemoryMaintenanceJobId
    window: MemoryMaintenanceWindow
    curator_revision: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class MemoryCandidateProposal:
    source_message_id: MessageId
    kind: MemoryKind
    title: str
    content: str
    supporting_quote: str
    suggested_lifecycle: MemoryLifecycle

    def __post_init__(self) -> None:
        if self.suggested_lifecycle is MemoryLifecycle.RETRACTED:
            raise ValueError("Curator cannot propose retracted Memory")
        if not all(value.strip() for value in (self.title, self.content, self.supporting_quote)):
            raise ValueError("Memory candidate text is required")


@dataclass(frozen=True, slots=True)
class SkillEvolutionProposal:
    skill_name: str
    description: str
    instruction_body: str
    rationale: str
    source_memory_item_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.skill_name,
                self.description,
                self.instruction_body,
                self.rationale,
            )
        ):
            raise ValueError("Skill evolution proposal fields are required")
        if len(set(self.source_memory_item_ids)) < 2:
            raise ValueError("Skill evolution requires at least two distinct Memory sources")


@dataclass(frozen=True, slots=True)
class MemoryExtractionOutput:
    episode_summary: str
    candidates: tuple[MemoryCandidateProposal, ...]
    skill_candidates: tuple[SkillEvolutionProposal, ...] = ()

    def __post_init__(self) -> None:
        if not self.episode_summary.strip():
            raise ValueError("Memory extraction summary is required")
        if len(self.candidates) > 24:
            raise ValueError("Memory extraction candidate limit exceeded")
        if len(self.skill_candidates) > 4:
            raise ValueError("Skill evolution candidate limit exceeded")


@dataclass(frozen=True, slots=True)
class MemoryAttemptPreparation:
    attempt_id: MemoryProviderAttemptId
    attempt_ordinal: int


@dataclass(frozen=True, slots=True)
class MemoryMaintenanceOutcome:
    job_id: MemoryMaintenanceJobId
    status: str
    created_active: int = 0
    created_proposed: int = 0
    deduplicated: int = 0
    rejected: int = 0
    skill_candidates_proposed: int = 0
