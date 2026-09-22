"""Ports for user-authorized Skill evolution."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from stata_research_agent.application.skill_evolution import (
    ApproveSkillEvolutionCommand,
    DeactivatedSkillReceipt,
    DeactivateSkillCommand,
    PublishedSkillReceipt,
    RejectSkillEvolutionCommand,
    RollbackSkillCommand,
    SkillActivationPlan,
    SkillAdoptionOutcome,
    SkillDeactivationPlan,
    SkillEvolutionOutcome,
    SkillVersionAdoptionPlan,
)
from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillActivationManifestId,
    SkillAdoptionHistoryId,
    SkillEvolutionStateHistoryId,
    SkillPublicationManifestId,
    SkillVersionId,
)


class SkillEvolutionRepository(Protocol):
    def rollback_replay(self, command: RollbackSkillCommand) -> SkillAdoptionOutcome | None: ...

    def deactivation_replay(
        self, command: DeactivateSkillCommand
    ) -> SkillAdoptionOutcome | None: ...

    def approve(
        self,
        command: ApproveSkillEvolutionCommand,
        state_history_id: SkillEvolutionStateHistoryId,
    ) -> SkillActivationPlan: ...

    def finalize_activation(
        self,
        *,
        command_id: CommandId,
        plan: SkillActivationPlan,
        receipt: PublishedSkillReceipt,
        state_history_id: SkillEvolutionStateHistoryId,
        manifest_id: SkillActivationManifestId,
        version_id: SkillVersionId,
        adoption_history_id: SkillAdoptionHistoryId,
        publication_manifest_id: SkillPublicationManifestId,
    ) -> SkillEvolutionOutcome: ...

    def reject(
        self,
        command: RejectSkillEvolutionCommand,
        state_history_id: SkillEvolutionStateHistoryId,
    ) -> SkillEvolutionOutcome: ...

    def prepare_rollback(self, command: RollbackSkillCommand) -> SkillVersionAdoptionPlan: ...

    def finalize_rollback(
        self,
        *,
        plan: SkillVersionAdoptionPlan,
        receipt: PublishedSkillReceipt,
        history_id: SkillAdoptionHistoryId,
        publication_manifest_id: SkillPublicationManifestId,
    ) -> SkillAdoptionOutcome: ...

    def prepare_deactivation(self, command: DeactivateSkillCommand) -> SkillDeactivationPlan: ...

    def finalize_deactivation(
        self,
        *,
        plan: SkillDeactivationPlan,
        receipt: DeactivatedSkillReceipt,
        history_id: SkillAdoptionHistoryId,
        publication_manifest_id: SkillPublicationManifestId,
    ) -> SkillAdoptionOutcome: ...


class SkillPublisher(Protocol):
    def publish(self, workspace_root: Path, plan: SkillActivationPlan) -> PublishedSkillReceipt: ...

    def publish_version(
        self, workspace_root: Path, plan: SkillVersionAdoptionPlan
    ) -> PublishedSkillReceipt: ...

    def deactivate(
        self, workspace_root: Path, plan: SkillDeactivationPlan
    ) -> DeactivatedSkillReceipt: ...
