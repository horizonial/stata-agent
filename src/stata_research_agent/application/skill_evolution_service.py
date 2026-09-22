"""Application orchestration for user-approved Skill evolution."""

from __future__ import annotations

from pathlib import Path

from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillActivationManifestId,
    SkillAdoptionHistoryId,
    SkillEvolutionStateHistoryId,
    SkillPublicationManifestId,
    SkillVersionId,
)

from .ports.identity import IdentityGenerator
from .ports.skill_evolution import SkillEvolutionRepository, SkillPublisher
from .skill_evolution import (
    ApproveSkillEvolutionCommand,
    DeactivateSkillCommand,
    RejectSkillEvolutionCommand,
    RollbackSkillCommand,
    SkillAdoptionOutcome,
    SkillEvolutionOutcome,
)


class SkillEvolutionService:
    def __init__(
        self,
        repository: SkillEvolutionRepository,
        publisher: SkillPublisher,
        identities: IdentityGenerator,
        workspace_root: Path,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._identities = identities
        self._workspace_root = workspace_root

    def approve_and_activate(
        self, command: ApproveSkillEvolutionCommand
    ) -> SkillEvolutionOutcome:
        plan = self._repository.approve(
            command, self._identities.new(SkillEvolutionStateHistoryId)
        )
        receipt = self._publisher.publish(self._workspace_root, plan)
        return self._repository.finalize_activation(
            command_id=self._identities.new(CommandId),
            plan=plan,
            receipt=receipt,
            state_history_id=self._identities.new(SkillEvolutionStateHistoryId),
            manifest_id=self._identities.new(SkillActivationManifestId),
            version_id=self._identities.new(SkillVersionId),
            adoption_history_id=self._identities.new(SkillAdoptionHistoryId),
            publication_manifest_id=self._identities.new(SkillPublicationManifestId),
        )

    def reject(self, command: RejectSkillEvolutionCommand) -> SkillEvolutionOutcome:
        return self._repository.reject(
            command, self._identities.new(SkillEvolutionStateHistoryId)
        )

    def rollback(self, command: RollbackSkillCommand) -> SkillAdoptionOutcome:
        replay = self._repository.rollback_replay(command)
        if replay is not None:
            return replay
        plan = self._repository.prepare_rollback(command)
        receipt = self._publisher.publish_version(self._workspace_root, plan)
        return self._repository.finalize_rollback(
            plan=plan,
            receipt=receipt,
            history_id=self._identities.new(SkillAdoptionHistoryId),
            publication_manifest_id=self._identities.new(SkillPublicationManifestId),
        )

    def deactivate(self, command: DeactivateSkillCommand) -> SkillAdoptionOutcome:
        replay = self._repository.deactivation_replay(command)
        if replay is not None:
            return replay
        plan = self._repository.prepare_deactivation(command)
        receipt = self._publisher.deactivate(self._workspace_root, plan)
        return self._repository.finalize_deactivation(
            plan=plan,
            receipt=receipt,
            history_id=self._identities.new(SkillAdoptionHistoryId),
            publication_manifest_id=self._identities.new(SkillPublicationManifestId),
        )
