"""Human-gated contracts for turning evaluation proposals into Skill versions."""

from __future__ import annotations

from dataclasses import dataclass

from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillChangeCandidateId,
    SkillImprovementProposalId,
    SkillVersionId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class MaterializeSkillChangeCommand:
    command_id: CommandId
    proposal_id: SkillImprovementProposalId


@dataclass(frozen=True, slots=True)
class ActivateSkillChangeCommand:
    command_id: CommandId
    candidate_id: SkillChangeCandidateId
    expected_pointer_revision: int

    def __post_init__(self) -> None:
        if self.expected_pointer_revision < 1:
            raise ValueError("expected Skill change pointer revision must be positive")


@dataclass(frozen=True, slots=True)
class RejectSkillChangeCommand:
    command_id: CommandId
    candidate_id: SkillChangeCandidateId
    expected_pointer_revision: int
    reason: str

    def __post_init__(self) -> None:
        if self.expected_pointer_revision < 1 or not self.reason.strip():
            raise ValueError("Skill change rejection requires revision and reason")


@dataclass(frozen=True, slots=True)
class SkillChangePublicationPlan:
    command_id: CommandId
    candidate_id: SkillChangeCandidateId
    skill_name: str
    base_skill_version_id: SkillVersionId
    base_skill_sha256: str
    merge_source_skill_version_id: SkillVersionId | None
    proposed_version: str
    skill_markdown: str
    skill_sha256: str
    expected_candidate_pointer_revision: int
    expected_adoption_pointer_revision: int


@dataclass(frozen=True, slots=True)
class SkillChangeCandidateOutcome:
    candidate_id: SkillChangeCandidateId
    lifecycle: str
    pointer_revision: int
    skill_name: str
    base_skill_version_id: SkillVersionId
    merge_source_skill_version_id: SkillVersionId | None
    proposed_version: str
    validation_status: str
    validation_findings: tuple[str, ...]
    commit_revision: WorkspaceRevision
    replayed: bool
    activated_skill_version_id: SkillVersionId | None = None
