"""Persistence port for Research Path, Plan, and adoption commands."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.research_state import (
    AdoptPlanRevisionCommand,
    AdoptResearchBundleCommand,
    BranchSourceSnapshot,
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
from stata_research_agent.domain.identifiers import PlanId, PlanNodeId, ResearchPathId


class ResearchStateRepository(Protocol):
    def current_plan_adoption(
        self, research_path_id: ResearchPathId
    ) -> CurrentPlanAdoption | None: ...

    def retained_plan_nodes(self, plan_id: PlanId) -> dict[str, PlanNodeId]: ...

    def current_plan_node_for_command(
        self, research_path_id: ResearchPathId, command: str
    ) -> PlanCommandBinding | None: ...

    def current_plan_node_for_key(
        self, research_path_id: ResearchPathId, canonical_key: str
    ) -> PlanCommandBinding | None: ...

    def current_plan_nodes(
        self, research_path_id: ResearchPathId
    ) -> tuple[PlanCommandBinding, ...]: ...

    def current_plan_semantic_digest(self, research_path_id: ResearchPathId) -> str | None: ...

    def create_plan_revision(
        self,
        command: CreatePlanRevisionCommand,
        identity: PlanRevisionIdentity,
    ) -> PlanRevisionOutcome: ...

    def adopt_plan_revision(self, command: AdoptPlanRevisionCommand) -> PlanAdoptionOutcome: ...

    def adopt_research_bundle(
        self, command: AdoptResearchBundleCommand
    ) -> ResearchBundleAdoptionOutcome: ...

    def describe_branch_source(
        self, command: CreateResearchPathBranchCommand
    ) -> BranchSourceSnapshot: ...

    def create_path_branch(
        self,
        command: CreateResearchPathBranchCommand,
        identity: ResearchPathBranchIdentity,
    ) -> ResearchPathBranchOutcome: ...
