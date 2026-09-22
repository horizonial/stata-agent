"""Independent, advisory evaluation contracts for adopted Skill versions.

Skill evaluation is deliberately separate from the Turn Runtime Evaluator. It observes
exact version use and explicit user feedback, but never treats co-occurrence as causation
and never changes an adopted Skill by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillEvaluationAttemptId,
    SkillEvaluationRunId,
    SkillImprovementProposalId,
    SkillVersionId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

SkillEvaluationVerdict = Literal[
    "insufficient_evidence",
    "candidate_preferred",
    "baseline_preferred",
    "mixed",
    "no_material_difference",
]
SkillImprovementKind = Literal["revise", "merge", "retire", "keep_observing"]


@dataclass(frozen=True, slots=True)
class EvaluateSkillCommand:
    command_id: CommandId
    skill_name: str
    candidate_skill_version_id: SkillVersionId | None = None
    baseline_skill_version_id: SkillVersionId | None = None

    def __post_init__(self) -> None:
        if not self.skill_name.strip():
            raise ValueError("Skill name is required")
        if self.candidate_skill_version_id == self.baseline_skill_version_id:
            if self.candidate_skill_version_id is not None:
                raise ValueError("candidate and baseline Skill versions must differ")


@dataclass(frozen=True, slots=True)
class SkillEvaluationPreparation:
    run_id: SkillEvaluationRunId
    attempt_id: SkillEvaluationAttemptId
    skill_name: str
    candidate_skill_version_id: SkillVersionId
    baseline_skill_version_id: SkillVersionId | None
    evaluation_kind: str
    source_start_revision: int
    source_end_revision: int
    evidence_json: str
    request_json: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class SkillImprovementProposalDraft:
    kind: SkillImprovementKind
    title: str
    rationale: str
    suggested_instruction_body: str | None = None
    merge_target_skill_name: str | None = None

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.rationale.strip():
            raise ValueError("Skill improvement proposal title and rationale are required")
        if self.kind == "merge" and not (self.merge_target_skill_name or "").strip():
            raise ValueError("merge proposal requires a target Skill name")
        if self.kind in {"revise", "merge"} and not (self.suggested_instruction_body or "").strip():
            raise ValueError("revise/merge proposal requires full replacement instructions")


@dataclass(frozen=True, slots=True)
class SkillEvaluationJudgment:
    verdict: SkillEvaluationVerdict
    rationale: str
    limitations: tuple[str, ...]
    proposals: tuple[SkillImprovementProposalDraft, ...]

    def __post_init__(self) -> None:
        if not self.rationale.strip() or not self.limitations:
            raise ValueError("Skill evaluation rationale and limitations are required")
        if len(self.proposals) > 4:
            raise ValueError("Skill evaluation proposal limit exceeded")


@dataclass(frozen=True, slots=True)
class SkillImprovementProposal:
    proposal_id: SkillImprovementProposalId
    kind: SkillImprovementKind
    title: str
    rationale: str
    suggested_instruction_body: str | None
    merge_target_skill_name: str | None


@dataclass(frozen=True, slots=True)
class SkillEvaluationOutcome:
    run_id: SkillEvaluationRunId
    status: str
    skill_name: str
    candidate_skill_version_id: SkillVersionId
    baseline_skill_version_id: SkillVersionId | None
    evaluation_kind: str
    verdict: SkillEvaluationVerdict | None
    rationale: str | None
    limitations: tuple[str, ...]
    proposals: tuple[SkillImprovementProposal, ...]
    commit_revision: WorkspaceRevision
    replayed: bool
