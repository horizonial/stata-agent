"""Commands and immutable facts for Research Path and Plan adoption."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from stata_research_agent.domain.identifiers import (
    ArtifactVerificationReceiptId,
    CommandId,
    DataVersionId,
    DocumentSlotId,
    PathBranchManifestId,
    PathDataSlotId,
    PlanId,
    PlanNodeId,
    PlanRevisionId,
    ResearchPathId,
    ResultId,
    ResultSlotId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

JsonObject = dict[str, Any]
# Research methods are open-ended.  Persistence keeps a small storage category for
# navigation, while the Agent's semantic node kind is preserved in the specification.
PlanNodeKind = str


@dataclass(frozen=True, slots=True)
class PlanNodeCandidate:
    canonical_key: str
    node_kind: PlanNodeKind
    specification: JsonObject
    retained_plan_node_id: PlanNodeId | None = None

    def __post_init__(self) -> None:
        if not self.canonical_key.strip() or not self.node_kind.strip():
            raise ValueError("Plan Node canonical key and kind are required")


@dataclass(frozen=True, slots=True)
class PlanDependencyCandidate:
    upstream_node_key: str
    downstream_node_key: str
    dependency_kind: Literal["data", "control", "evidence"]

    def __post_init__(self) -> None:
        if (
            not self.upstream_node_key.strip()
            or not self.downstream_node_key.strip()
            or self.upstream_node_key == self.downstream_node_key
        ):
            raise ValueError("Plan dependency requires two distinct node keys")


@dataclass(frozen=True, slots=True)
class CreatePlanRevisionCommand:
    command_id: CommandId
    created_by_turn_id: TurnId
    canonical_key: str
    summary: str
    specification: JsonObject
    nodes: tuple[PlanNodeCandidate, ...]
    dependencies: tuple[PlanDependencyCandidate, ...] = ()
    plan_id: PlanId | None = None
    parent_revision_ids: tuple[PlanRevisionId, ...] = ()

    def __post_init__(self) -> None:
        if not self.canonical_key.strip() or not self.summary.strip():
            raise ValueError("Plan canonical key and summary are required")
        keys = [node.canonical_key for node in self.nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("Plan Node canonical keys must be unique per revision")
        known = set(keys)
        if any(
            edge.upstream_node_key not in known or edge.downstream_node_key not in known
            for edge in self.dependencies
        ):
            raise ValueError("Plan dependency references an unknown node key")
        if self.plan_id is None and self.parent_revision_ids:
            raise ValueError("a new Plan cannot have parent revisions")


@dataclass(frozen=True, slots=True)
class PlanRevisionIdentity:
    plan_id: PlanId
    plan_revision_id: PlanRevisionId
    node_ids: tuple[PlanNodeId, ...]


@dataclass(frozen=True, slots=True)
class PlanRevisionOutcome:
    plan_id: PlanId
    plan_revision_id: PlanRevisionId
    revision_number: int
    content_sha256: str
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class CurrentPlanAdoption:
    """Authoritative current Plan identity exposed without leaking persistence details."""

    plan_id: PlanId
    plan_revision_id: PlanRevisionId
    canonical_key: str
    pointer_revision: int


@dataclass(frozen=True, slots=True)
class PlanCommandBinding:
    """One semantic node in the currently adopted Plan revision."""

    plan_revision_id: PlanRevisionId
    plan_node_id: PlanNodeId
    canonical_key: str


@dataclass(frozen=True, slots=True)
class AdoptPlanRevisionCommand:
    command_id: CommandId
    research_path_id: ResearchPathId
    target_plan_revision_id: PlanRevisionId
    updated_by_turn_id: TurnId
    expected_pointer_revision: int

    def __post_init__(self) -> None:
        if self.expected_pointer_revision < 0:
            raise ValueError("expected Plan pointer revision cannot be negative")


@dataclass(frozen=True, slots=True)
class PlanAdoptionOutcome:
    research_path_id: ResearchPathId
    target_plan_revision_id: PlanRevisionId
    pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class DataSlotAdoptionTarget:
    path_data_slot_id: PathDataSlotId
    data_version_id: DataVersionId
    verification_receipt_id: ArtifactVerificationReceiptId
    expected_pointer_revision: int

    def __post_init__(self) -> None:
        if self.expected_pointer_revision < 0:
            raise ValueError("expected Data pointer revision cannot be negative")


@dataclass(frozen=True, slots=True)
class ResultSlotAdoptionTarget:
    result_slot_id: ResultSlotId
    result_id: ResultId
    expected_pointer_revision: int

    def __post_init__(self) -> None:
        if self.expected_pointer_revision < 0:
            raise ValueError("expected Result pointer revision cannot be negative")


@dataclass(frozen=True, slots=True)
class AdoptResearchBundleCommand:
    command_id: CommandId
    research_path_id: ResearchPathId
    updated_by_turn_id: TurnId
    data_targets: tuple[DataSlotAdoptionTarget, ...] = ()
    result_targets: tuple[ResultSlotAdoptionTarget, ...] = ()
    plan_revision_id: PlanRevisionId | None = None
    expected_plan_pointer_revision: int | None = None

    def __post_init__(self) -> None:
        if not (self.data_targets or self.result_targets or self.plan_revision_id):
            raise ValueError("Research adoption bundle cannot be empty")
        if (self.plan_revision_id is None) != (self.expected_plan_pointer_revision is None):
            raise ValueError("Plan target and expected pointer must be supplied together")
        if (
            self.expected_plan_pointer_revision is not None
            and self.expected_plan_pointer_revision < 0
        ):
            raise ValueError("expected Plan pointer revision cannot be negative")
        data_slots = [target.path_data_slot_id for target in self.data_targets]
        result_slots = [target.result_slot_id for target in self.result_targets]
        if len(data_slots) != len(set(data_slots)) or len(result_slots) != len(set(result_slots)):
            raise ValueError("Research adoption bundle contains duplicate Slots")


@dataclass(frozen=True, slots=True)
class ResearchBundleAdoptionOutcome:
    research_path_id: ResearchPathId
    data_pointer_revisions: dict[str, int]
    result_pointer_revisions: dict[str, int]
    plan_pointer_revision: int | None
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class CreateResearchPathBranchCommand:
    command_id: CommandId
    source_research_path_id: ResearchPathId
    created_by_turn_id: TurnId
    canonical_key: str
    display_name: str
    branch_reason: str
    source_workspace_revision: int
    expected_workspace_revision: int

    def __post_init__(self) -> None:
        if not all(
            value.strip() for value in (self.canonical_key, self.display_name, self.branch_reason)
        ):
            raise ValueError("Path branch identity, name, and reason are required")
        if min(self.source_workspace_revision, self.expected_workspace_revision) < 1:
            raise ValueError("Path branch revisions must be positive")
        if self.source_workspace_revision != self.expected_workspace_revision:
            raise ValueError("V0.1 branches require an exact current Workspace source revision")


@dataclass(frozen=True, slots=True)
class BranchSourceSlot:
    slot_id: str
    canonical_key: str


@dataclass(frozen=True, slots=True)
class BranchSourceSnapshot:
    data_slots: tuple[BranchSourceSlot, ...]
    result_slots: tuple[BranchSourceSlot, ...]
    document_slots: tuple[BranchSourceSlot, ...]


@dataclass(frozen=True, slots=True)
class ResearchPathBranchIdentity:
    research_path_id: ResearchPathId
    manifest_id: PathBranchManifestId
    data_slot_ids: tuple[PathDataSlotId, ...]
    result_slot_ids: tuple[ResultSlotId, ...]
    document_slot_ids: tuple[DocumentSlotId, ...]


@dataclass(frozen=True, slots=True)
class ResearchPathBranchOutcome:
    research_path_id: ResearchPathId
    parent_research_path_id: ResearchPathId
    source_workspace_revision: int
    copied_data_adoptions: int
    copied_result_adoptions: int
    copied_document_adoptions: int
    copied_plan_adoption: bool
    commit_revision: WorkspaceRevision
    replayed: bool
