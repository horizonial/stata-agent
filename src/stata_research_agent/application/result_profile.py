"""Commands and immutable output identities for formal Stata Result qualification."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stata_research_agent.domain.identifiers import (
    CommandId,
    EnvironmentSnapshotId,
    EstimationSampleManifestId,
    OperationId,
    PlanNodeId,
    PlanRevisionId,
    ResearchCommandInstanceId,
    ResearchPathId,
    ResultCandidateId,
    ResultCapturePointId,
    ResultCaptureSnapshotId,
    ResultContractId,
    ResultElementId,
    ResultId,
    ResultQualificationReportId,
    ResultSourceLocatorId,
    RunId,
    TrustedDerivationReceiptId,
    TurnId,
)
from stata_research_agent.domain.result_profile import QualificationVerdict
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class RegisterRegressProfileCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId


@dataclass(frozen=True, slots=True)
class RegisterGenericStataResultProfileCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId


@dataclass(frozen=True, slots=True)
class PromoteStataResultCommand:
    command_id: CommandId
    operation_id: OperationId
    created_by_turn_id: TurnId
    research_path_id: ResearchPathId
    selected_source_keys: tuple[str, ...]
    result_summary: str
    plan_revision_id: PlanRevisionId | None = None
    plan_node_id: PlanNodeId | None = None

    def __post_init__(self) -> None:
        if not self.selected_source_keys or any(
            not key.strip() for key in self.selected_source_keys
        ):
            raise ValueError("at least one non-empty Stata source key is required")
        if len(self.selected_source_keys) != len(set(self.selected_source_keys)):
            raise ValueError("selected Stata source keys must be unique")
        if not self.result_summary.strip():
            raise ValueError("result summary is required")
        if (self.plan_revision_id is None) != (self.plan_node_id is None):
            raise ValueError("Plan Revision and Plan Node binding must be supplied together")


@dataclass(frozen=True, slots=True)
class QualifyRegressResultCommand:
    command_id: CommandId
    operation_id: OperationId
    created_by_turn_id: TurnId
    research_path_id: ResearchPathId
    expected_dependent_variable: str
    expected_terms: tuple[str, ...]
    plan_revision_id: PlanRevisionId | None = None
    plan_node_id: PlanNodeId | None = None
    estimator: str = "regress"
    expected_absorbed_effects: tuple[str, ...] = ()
    expected_cluster_variables: tuple[str, ...] = ()
    expected_vce: str = "ols"
    expected_endogenous_variables: tuple[str, ...] = ()
    expected_included_exogenous_variables: tuple[str, ...] = ()
    expected_excluded_instruments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.expected_dependent_variable.strip() or not self.expected_terms:
            raise ValueError("a dependent variable and target terms are required")
        if len(self.expected_terms) != len(set(self.expected_terms)):
            raise ValueError("expected terms must be unique")
        if self.estimator not in {"regress", "logit", "reghdfe", "ivregress"}:
            raise ValueError("estimator is not supported by an active Result Profile")
        if len(self.expected_absorbed_effects) != len(set(self.expected_absorbed_effects)):
            raise ValueError("expected absorbed effects must be unique")
        if len(self.expected_cluster_variables) != len(set(self.expected_cluster_variables)):
            raise ValueError("expected cluster variables must be unique")
        if self.estimator == "reghdfe" and not self.expected_absorbed_effects:
            raise ValueError("reghdfe qualification requires absorbed effects")
        if self.estimator == "ivregress" and (
            not self.expected_endogenous_variables or not self.expected_excluded_instruments
        ):
            raise ValueError(
                "ivregress qualification requires endogenous variables and instruments"
            )
        for values, label in (
            (self.expected_endogenous_variables, "endogenous variables"),
            (
                self.expected_included_exogenous_variables,
                "included exogenous variables",
            ),
            (self.expected_excluded_instruments, "excluded instruments"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"expected {label} must be unique")
        if not self.expected_vce.strip():
            raise ValueError("expected VCE is required")
        if (self.plan_revision_id is None) != (self.plan_node_id is None):
            raise ValueError("Plan Revision and Plan Node binding must be supplied together")


@dataclass(frozen=True, slots=True)
class RegressOperationFacts:
    operation_id: str
    attempt_id: str
    manifest_id: str
    execution_status: str
    structured_result_status: str
    structured: Mapping[str, Any] | None
    receipt: Mapping[str, Any]
    input_data_version_id: str | None
    input_data_slot_key: str | None
    input_verification_receipt_id: str | None
    input_is_currently_verified: bool
    source_data_state_operation_id: str | None
    expected_data_state_token: str | None
    expected_session_generation: int | None
    executable_source_id: str
    command_text: str
    command_sha256: str
    session_id: str
    session_generation: int
    execution_purpose: str = "formal_estimation"
    source_execution_purpose: str | None = None
    source_has_formal_result: bool = False
    source_exec_seq: int | None = None


@dataclass(frozen=True, slots=True)
class PreparedElementIdentity:
    element_id: ResultElementId
    locator_id: ResultSourceLocatorId
    primitive_locator_ids: tuple[ResultSourceLocatorId, ...]
    derivation_receipt_id: TrustedDerivationReceiptId | None


@dataclass(frozen=True, slots=True)
class ResultPromotionIdentity:
    environment_snapshot_id: EnvironmentSnapshotId
    run_id: RunId
    command_instance_id: ResearchCommandInstanceId
    contract_id: ResultContractId
    capture_point_id: ResultCapturePointId
    snapshot_id: ResultCaptureSnapshotId
    sample_manifest_id: EstimationSampleManifestId
    candidate_id: ResultCandidateId
    qualification_report_id: ResultQualificationReportId
    result_id: ResultId
    elements: tuple[PreparedElementIdentity, ...]


@dataclass(frozen=True, slots=True)
class ResultQualificationOutcome:
    run_id: RunId
    candidate_id: ResultCandidateId
    qualification_report_id: ResultQualificationReportId
    verdict: QualificationVerdict
    findings: tuple[str, ...]
    result_id: ResultId | None
    element_count: int
    commit_revision: WorkspaceRevision
    replayed: bool
