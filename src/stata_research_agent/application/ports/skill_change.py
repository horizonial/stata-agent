"""Ports for human-gated evaluation-to-Skill changes."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from stata_research_agent.application.skill_change import (
    ActivateSkillChangeCommand,
    MaterializeSkillChangeCommand,
    RejectSkillChangeCommand,
    SkillChangeCandidateOutcome,
    SkillChangePublicationPlan,
)
from stata_research_agent.application.skill_evolution import PublishedSkillReceipt
from stata_research_agent.domain.identifiers import (
    SkillAdoptionHistoryId,
    SkillChangeCandidateId,
    SkillChangeStateHistoryId,
    SkillPublicationManifestId,
    SkillVersionId,
)


class SkillChangeRepository(Protocol):
    def materialization_replay(
        self, command: MaterializeSkillChangeCommand
    ) -> SkillChangeCandidateOutcome | None: ...

    def activation_replay(
        self, command: ActivateSkillChangeCommand
    ) -> SkillChangeCandidateOutcome | None: ...

    def rejection_replay(
        self, command: RejectSkillChangeCommand
    ) -> SkillChangeCandidateOutcome | None: ...

    def materialize(
        self,
        command: MaterializeSkillChangeCommand,
        candidate_id: SkillChangeCandidateId,
        history_id: SkillChangeStateHistoryId,
    ) -> SkillChangeCandidateOutcome: ...

    def prepare_activation(
        self, command: ActivateSkillChangeCommand
    ) -> SkillChangePublicationPlan: ...

    def finalize_activation(
        self,
        *,
        plan: SkillChangePublicationPlan,
        receipt: PublishedSkillReceipt,
        history_id: SkillChangeStateHistoryId,
        version_id: SkillVersionId,
        adoption_history_id: SkillAdoptionHistoryId,
        publication_manifest_id: SkillPublicationManifestId,
    ) -> SkillChangeCandidateOutcome: ...

    def reject(
        self, command: RejectSkillChangeCommand, history_id: SkillChangeStateHistoryId
    ) -> SkillChangeCandidateOutcome: ...


class SkillChangePublisher(Protocol):
    def publish_change(
        self, workspace_root: Path, plan: SkillChangePublicationPlan
    ) -> PublishedSkillReceipt: ...
