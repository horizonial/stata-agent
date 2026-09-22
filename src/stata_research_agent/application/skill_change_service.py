"""Orchestration for human-gated evaluation-to-Skill changes."""

from __future__ import annotations

from pathlib import Path

from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.ports.skill_change import (
    SkillChangePublisher,
    SkillChangeRepository,
)
from stata_research_agent.application.skill_change import (
    ActivateSkillChangeCommand,
    MaterializeSkillChangeCommand,
    RejectSkillChangeCommand,
    SkillChangeCandidateOutcome,
)
from stata_research_agent.domain.identifiers import (
    SkillAdoptionHistoryId,
    SkillChangeCandidateId,
    SkillChangeStateHistoryId,
    SkillPublicationManifestId,
    SkillVersionId,
)


class SkillChangeService:
    def __init__(
        self,
        repository: SkillChangeRepository,
        publisher: SkillChangePublisher,
        identities: IdentityGenerator,
        workspace_root: Path,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._identities = identities
        self._workspace_root = workspace_root

    def materialize(self, command: MaterializeSkillChangeCommand) -> SkillChangeCandidateOutcome:
        replay = self._repository.materialization_replay(command)
        if replay is not None:
            return replay
        return self._repository.materialize(
            command,
            self._identities.new(SkillChangeCandidateId),
            self._identities.new(SkillChangeStateHistoryId),
        )

    def activate(self, command: ActivateSkillChangeCommand) -> SkillChangeCandidateOutcome:
        replay = self._repository.activation_replay(command)
        if replay is not None:
            return replay
        plan = self._repository.prepare_activation(command)
        receipt = self._publisher.publish_change(self._workspace_root, plan)
        return self._repository.finalize_activation(
            plan=plan,
            receipt=receipt,
            history_id=self._identities.new(SkillChangeStateHistoryId),
            version_id=self._identities.new(SkillVersionId),
            adoption_history_id=self._identities.new(SkillAdoptionHistoryId),
            publication_manifest_id=self._identities.new(SkillPublicationManifestId),
        )

    def reject(self, command: RejectSkillChangeCommand) -> SkillChangeCandidateOutcome:
        replay = self._repository.rejection_replay(command)
        if replay is not None:
            return replay
        return self._repository.reject(command, self._identities.new(SkillChangeStateHistoryId))
