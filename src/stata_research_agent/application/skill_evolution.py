"""Controlled Memory-to-Skill evolution contracts."""

from __future__ import annotations

from dataclasses import dataclass

from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillActivationManifestId,
    SkillAdoptionHistoryId,
    SkillEvolutionCandidateId,
    SkillEvolutionStateHistoryId,
    SkillPublicationManifestId,
    SkillVersionId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class ApproveSkillEvolutionCommand:
    command_id: CommandId
    candidate_id: SkillEvolutionCandidateId
    expected_pointer_revision: int

    def __post_init__(self) -> None:
        if self.expected_pointer_revision < 1:
            raise ValueError("expected Skill candidate pointer revision must be positive")


@dataclass(frozen=True, slots=True)
class RejectSkillEvolutionCommand:
    command_id: CommandId
    candidate_id: SkillEvolutionCandidateId
    expected_pointer_revision: int
    reason: str

    def __post_init__(self) -> None:
        if self.expected_pointer_revision < 1:
            raise ValueError("expected Skill candidate pointer revision must be positive")
        if not self.reason.strip():
            raise ValueError("Skill candidate rejection reason is required")


@dataclass(frozen=True, slots=True)
class SkillActivationPlan:
    candidate_id: SkillEvolutionCandidateId
    skill_name: str
    proposed_version: str
    skill_markdown: str
    skill_sha256: str
    pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class PublishedSkillReceipt:
    relative_skill_path: str
    installed_sha256: str
    prior_sha256: str | None
    activation_kind: str


@dataclass(frozen=True, slots=True)
class RollbackSkillCommand:
    command_id: CommandId
    skill_name: str
    target_skill_version_id: SkillVersionId
    expected_pointer_revision: int
    reason: str

    def __post_init__(self) -> None:
        if not self.skill_name.strip() or not self.reason.strip():
            raise ValueError("Skill rollback name and reason are required")
        if self.expected_pointer_revision < 1:
            raise ValueError("expected Skill adoption pointer revision must be positive")


@dataclass(frozen=True, slots=True)
class DeactivateSkillCommand:
    command_id: CommandId
    skill_name: str
    expected_pointer_revision: int
    reason: str

    def __post_init__(self) -> None:
        if not self.skill_name.strip() or not self.reason.strip():
            raise ValueError("Skill deactivation name and reason are required")
        if self.expected_pointer_revision < 1:
            raise ValueError("expected Skill adoption pointer revision must be positive")


@dataclass(frozen=True, slots=True)
class SkillVersionAdoptionPlan:
    command_id: CommandId
    skill_name: str
    target_skill_version_id: SkillVersionId
    version_label: str
    skill_markdown: str
    skill_sha256: str
    expected_pointer_revision: int
    reason: str


@dataclass(frozen=True, slots=True)
class SkillDeactivationPlan:
    command_id: CommandId
    skill_name: str
    current_skill_version_id: SkillVersionId
    current_sha256: str
    expected_pointer_revision: int
    reason: str


@dataclass(frozen=True, slots=True)
class DeactivatedSkillReceipt:
    relative_skill_path: str
    prior_sha256: str
    publication_result: str


@dataclass(frozen=True, slots=True)
class SkillEvolutionOutcome:
    candidate_id: SkillEvolutionCandidateId
    lifecycle: str
    pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool
    activation_manifest_id: SkillActivationManifestId | None = None
    skill_version_id: SkillVersionId | None = None
    adoption_pointer_revision: int | None = None


@dataclass(frozen=True, slots=True)
class SkillAdoptionOutcome:
    skill_name: str
    lifecycle: str
    current_skill_version_id: SkillVersionId | None
    pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool
    publication_manifest_id: SkillPublicationManifestId


@dataclass(frozen=True, slots=True)
class SkillEvolutionIdentity:
    state_history_id: SkillEvolutionStateHistoryId


@dataclass(frozen=True, slots=True)
class SkillAdoptionIdentity:
    history_id: SkillAdoptionHistoryId
    publication_manifest_id: SkillPublicationManifestId
