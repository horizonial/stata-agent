"""Application orchestration for versioned plans and non-destructive path branches."""

from __future__ import annotations

from stata_research_agent.domain.identifiers import (
    DocumentSlotId,
    PathBranchManifestId,
    PathDataSlotId,
    PlanId,
    PlanNodeId,
    PlanRevisionId,
    ResearchPathId,
    ResultSlotId,
)

from .ports.identity import IdentityGenerator
from .ports.research_state import ResearchStateRepository
from .research_state import (
    AdoptPlanRevisionCommand,
    AdoptResearchBundleCommand,
    CreatePlanRevisionCommand,
    CreateResearchPathBranchCommand,
    CurrentPlanAdoption,
    PlanAdoptionOutcome,
    PlanCommandBinding,
    PlanRevisionIdentity,
    PlanRevisionOutcome,
    ResearchBundleAdoptionOutcome,
    ResearchPathBranchIdentity,
    ResearchPathBranchOutcome,
)


class ResearchStateService:
    def __init__(
        self,
        repository: ResearchStateRepository,
        identities: IdentityGenerator,
    ) -> None:
        self._repository = repository
        self._identities = identities

    def current_plan_adoption(
        self, research_path_id: ResearchPathId
    ) -> CurrentPlanAdoption | None:
        return self._repository.current_plan_adoption(research_path_id)

    def retained_plan_nodes(self, plan_id: PlanId) -> dict[str, PlanNodeId]:
        return self._repository.retained_plan_nodes(plan_id)

    def current_plan_node_for_command(
        self, research_path_id: ResearchPathId, command: str
    ) -> PlanCommandBinding | None:
        return self._repository.current_plan_node_for_command(research_path_id, command)

    def current_plan_node_for_key(
        self, research_path_id: ResearchPathId, canonical_key: str
    ) -> PlanCommandBinding | None:
        return self._repository.current_plan_node_for_key(research_path_id, canonical_key)

    def current_plan_nodes(
        self, research_path_id: ResearchPathId
    ) -> tuple[PlanCommandBinding, ...]:
        return self._repository.current_plan_nodes(research_path_id)

    def current_plan_semantic_digest(self, research_path_id: ResearchPathId) -> str | None:
        return self._repository.current_plan_semantic_digest(research_path_id)

    def create_plan_revision(self, command: CreatePlanRevisionCommand) -> PlanRevisionOutcome:
        node_ids = tuple(
            node.retained_plan_node_id or self._identities.new(PlanNodeId) for node in command.nodes
        )
        return self._repository.create_plan_revision(
            command,
            PlanRevisionIdentity(
                command.plan_id or self._identities.new(PlanId),
                self._identities.new(PlanRevisionId),
                node_ids,
            ),
        )

    def adopt_plan_revision(self, command: AdoptPlanRevisionCommand) -> PlanAdoptionOutcome:
        return self._repository.adopt_plan_revision(command)

    def adopt_research_bundle(
        self, command: AdoptResearchBundleCommand
    ) -> ResearchBundleAdoptionOutcome:
        return self._repository.adopt_research_bundle(command)

    def create_path_branch(
        self, command: CreateResearchPathBranchCommand
    ) -> ResearchPathBranchOutcome:
        source = self._repository.describe_branch_source(command)
        identity = ResearchPathBranchIdentity(
            self._identities.new(ResearchPathId),
            self._identities.new(PathBranchManifestId),
            tuple(self._identities.new(PathDataSlotId) for _ in source.data_slots),
            tuple(self._identities.new(ResultSlotId) for _ in source.result_slots),
            tuple(self._identities.new(DocumentSlotId) for _ in source.document_slots),
        )
        return self._repository.create_path_branch(command, identity)
