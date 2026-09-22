"""Commit model Plan proposals before any formal research side effect is admitted."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.research_state import (
    AdoptPlanRevisionCommand,
    CreatePlanRevisionCommand,
    CurrentPlanAdoption,
    PlanDependencyCandidate,
    PlanNodeCandidate,
)
from stata_research_agent.application.research_state_service import ResearchStateService
from stata_research_agent.domain.identifiers import (
    CommandId,
    PlanNodeId,
    PlanRevisionId,
    ResearchPathId,
    TurnId,
)

from .ipc_contract import PlanProposal, ToolProposal


@dataclass(frozen=True, slots=True)
class CommittedPlan:
    plan_revision_id: str
    pointer_revision: int


class ResearchPlanCoordinator:
    """Normalize, version, and adopt a proposed plan before Tool Admission."""

    _NODE_KINDS = {
        "data_preparation",
        "variable_construction",
        "research_design",
        "estimation",
        "robustness",
        "heterogeneity",
        "mechanism",
        "visualization",
        "document",
    }

    def __init__(
        self,
        service: ResearchStateService,
        identities: IdentityGenerator,
    ) -> None:
        self._service = service
        self._identities = identities

    def commit_proposal(
        self,
        turn_id: TurnId,
        research_path_id: ResearchPathId,
        proposal: PlanProposal,
    ) -> CommittedPlan:
        nodes, dependencies = self._parse(proposal.structured_plan)
        return self._commit(
            turn_id,
            research_path_id,
            proposal.summary,
            dict(proposal.structured_plan),
            nodes,
            dependencies,
        )

    def bind_formal_tools(
        self,
        research_path_id: ResearchPathId,
        proposals: Sequence[ToolProposal],
    ) -> tuple[ToolProposal, ...]:
        """Freeze semantic Plan identity into formal Call arguments before Admission."""

        bound: list[ToolProposal] = []
        current_nodes = self._service.current_plan_nodes(research_path_id)
        for proposal in proposals:
            if not (
                proposal.tool_name == "stata.execute"
                and proposal.arguments.get("execution_role")
                in {"formal_result_candidate", "formal_post_estimation"}
            ):
                bound.append(proposal)
                continue
            node_key = str(proposal.arguments.get("plan_node_key", "")).strip()
            if not node_key:
                if len(current_nodes) != 1:
                    raise ValueError(
                        "formal Stata execution requires plan_node_key when the Plan has "
                        "multiple nodes"
                    )
                node_key = current_nodes[0].canonical_key
            binding = self._service.current_plan_node_for_key(research_path_id, node_key)
            if binding is None:
                raise ValueError(
                    "formal Stata execution must reference a node in the adopted semantic Plan"
                )
            arguments = dict(proposal.arguments)
            arguments["plan_node_key"] = node_key
            arguments["plan_revision_id"] = binding.plan_revision_id.value
            arguments["plan_node_id"] = binding.plan_node_id.value
            bound.append(proposal.model_copy(update={"arguments": arguments}))
        return tuple(bound)

    def ensure_minimal_formal_plan(
        self,
        turn_id: TurnId,
        research_path_id: ResearchPathId,
        proposals: Sequence[ToolProposal],
    ) -> CommittedPlan | None:
        """Create a non-command fallback only when no semantic Plan exists at all.

        This compatibility path never replaces an existing future Plan and never treats code as
        research intent.  Current models are instructed to propose the richer Plan themselves.
        """

        formal = tuple(
            proposal
            for proposal in proposals
            if proposal.tool_name == "stata.execute"
            and proposal.arguments.get("execution_role")
            in {"formal_result_candidate", "formal_post_estimation"}
        )
        if not formal:
            return None
        current = self._current(research_path_id)
        if current is not None:
            return CommittedPlan(current.plan_revision_id.value, current.pointer_revision)
        nodes = tuple(
            PlanNodeCandidate(
                f"formal_result.candidate.{index}",
                "estimation",
                {
                    "objective": "Evaluate and retain a traceable formal Stata result candidate",
                    "origin": "runtime_minimal_plan_fallback",
                    "tool_call_ordinal": proposal.call_ordinal,
                },
            )
            for index, proposal in enumerate(formal, start=1)
        )
        return self._commit(
            turn_id,
            research_path_id,
            "Provisional formal-result plan; refine it when research intent is available",
            {
                "change_kind": "runtime_fallback",
                "trigger_references": [proposal.message_id for proposal in formal],
                "origin": "runtime_minimal_plan_fallback",
            },
            nodes,
            (),
        )

    def current_plan_node_for_command(
        self, research_path_id: ResearchPathId, command: str
    ) -> tuple[PlanRevisionId, PlanNodeId] | None:
        binding = self._service.current_plan_node_for_command(research_path_id, command)
        if binding is None:
            return None
        return binding.plan_revision_id, binding.plan_node_id

    def current_plan_node_for_key(
        self, research_path_id: ResearchPathId, canonical_key: str
    ) -> tuple[PlanRevisionId, PlanNodeId] | None:
        binding = self._service.current_plan_node_for_key(research_path_id, canonical_key)
        if binding is None:
            return None
        return binding.plan_revision_id, binding.plan_node_id

    def _commit(
        self,
        turn_id: TurnId,
        research_path_id: ResearchPathId,
        summary: str,
        specification: dict[str, Any],
        nodes: tuple[PlanNodeCandidate, ...],
        dependencies: tuple[PlanDependencyCandidate, ...],
    ) -> CommittedPlan:
        if not nodes:
            raise ValueError("a research Plan proposal requires at least one node")
        current = self._current(research_path_id)
        semantic_content = {
            "summary": summary.strip(),
            "specification": {
                key: value for key, value in specification.items() if key != "_adaptive_plan"
            },
            "nodes": [
                {
                    "canonical_key": node.canonical_key,
                    "node_kind": node.specification.get("semantic_node_kind", node.node_kind),
                    "specification": node.specification,
                }
                for node in nodes
            ],
            "dependencies": [
                {
                    "upstream": edge.upstream_node_key,
                    "downstream": edge.downstream_node_key,
                    "kind": edge.dependency_kind,
                }
                for edge in dependencies
            ],
        }
        semantic_digest = hashlib.sha256(
            json.dumps(
                semantic_content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        if (
            current is not None
            and self._service.current_plan_semantic_digest(research_path_id) == semantic_digest
        ):
            return CommittedPlan(current.plan_revision_id.value, current.pointer_revision)
        specification = dict(specification)
        specification["_adaptive_plan"] = {
            "schema_version": "adaptive-plan/v1",
            "semantic_digest": semantic_digest,
            "change_kind": (
                "initial" if current is None else str(specification.get("change_kind", "refine"))
            ),
            "trigger_references": specification.get("trigger_references", ["current_turn"]),
        }
        retained = (
            {} if current is None else self._service.retained_plan_nodes(current.plan_id)
        )
        prepared_nodes = tuple(
            PlanNodeCandidate(
                node.canonical_key,
                node.node_kind,
                node.specification,
                retained.get(node.canonical_key),
            )
            for node in nodes
        )
        if current is None:
            canonical_key = f"study.{research_path_id.value}"
            plan_id = None
            parents: tuple[PlanRevisionId, ...] = ()
            pointer_revision = 0
        else:
            plan_id = current.plan_id
            canonical_key = current.canonical_key
            pointer_revision = current.pointer_revision
            parents = (current.plan_revision_id,)
        created = self._service.create_plan_revision(
            CreatePlanRevisionCommand(
                self._identities.new(CommandId),
                turn_id,
                canonical_key,
                summary.strip(),
                specification,
                prepared_nodes,
                dependencies,
                plan_id=plan_id,
                parent_revision_ids=parents,
            )
        )
        adopted = self._service.adopt_plan_revision(
            AdoptPlanRevisionCommand(
                self._identities.new(CommandId),
                research_path_id,
                created.plan_revision_id,
                turn_id,
                pointer_revision,
            )
        )
        return CommittedPlan(created.plan_revision_id.value, adopted.pointer_revision)

    def _current(self, research_path_id: ResearchPathId) -> CurrentPlanAdoption | None:
        return self._service.current_plan_adoption(research_path_id)

    def _parse(
        self, value: Mapping[str, Any]
    ) -> tuple[tuple[PlanNodeCandidate, ...], tuple[PlanDependencyCandidate, ...]]:
        raw_nodes = value.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ValueError("structured Plan requires a non-empty nodes array")
        nodes: list[PlanNodeCandidate] = []
        for raw in raw_nodes:
            if not isinstance(raw, Mapping):
                raise ValueError("Plan nodes must be objects")
            kind = str(raw.get("node_kind", "")).strip()
            canonical_key = raw.get("canonical_key", "")
            specification = raw.get("specification")
            if (
                re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", kind) is None
                or not isinstance(canonical_key, str)
                or not canonical_key.strip()
                or not isinstance(specification, dict)
            ):
                raise ValueError("Plan node kind/specification is invalid")
            stored_kind = kind if kind in self._NODE_KINDS else "research_design"
            normalized_specification = dict(specification)
            if stored_kind != kind:
                normalized_specification["semantic_node_kind"] = kind
            nodes.append(
                PlanNodeCandidate(
                    canonical_key,
                    stored_kind,
                    normalized_specification,
                )
            )
        raw_dependencies = value.get("dependencies", [])
        if not isinstance(raw_dependencies, list):
            raise ValueError("Plan dependencies must be an array")
        dependencies: list[PlanDependencyCandidate] = []
        for raw in raw_dependencies:
            if not isinstance(raw, Mapping):
                raise ValueError("Plan dependencies must be objects")
            dependency_kind = str(raw.get("dependency_kind", ""))
            if dependency_kind not in {"data", "control", "evidence"}:
                raise ValueError("invalid Plan dependency kind")
            dependencies.append(
                PlanDependencyCandidate(
                    str(raw.get("upstream_node_key", "")),
                    str(raw.get("downstream_node_key", "")),
                    cast(Any, dependency_kind),
                )
            )
        return tuple(nodes), tuple(dependencies)
