"""Typed runtime-evaluation, goal-coverage, and stop-guard contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from stata_research_agent.domain.identifiers import (
    CommandId,
    CompletionContractRevisionId,
    CompletionObligationId,
    EvaluationFindingId,
    EvaluationPolicySnapshotId,
    EvaluationReportId,
    EvaluationRequestId,
    EvidenceScopeManifestId,
    GoalCoverageId,
    ObligationObservationId,
    StepId,
    StopGuardDecisionRecordId,
    TurnId,
    WaitingRequestId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.status import (
    ContinueDirective,
    StopGuardDecision,
    TerminalDisposition,
    WaitReason,
)


class ObligationProvenance(StrEnum):
    USER_EXPLICIT = "user_explicit"
    PLAN_DERIVED = "plan_derived"
    USER_DECISION = "user_decision"
    AGENT_NORMALIZATION = "agent_normalization"


class RequirementLevel(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class EvaluationDimension(StrEnum):
    ELIGIBILITY = "eligibility"
    QUALITY = "quality"
    COMPLETION = "completion"
    LOOP_HEALTH = "loop_health"


class EvaluationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class ContractObligationCandidate:
    stable_key: str
    label: str
    provenance: ObligationProvenance
    source_object_type: str
    source_object_id: str
    requirement_level: RequirementLevel
    acceptance_criterion: str

    def __post_init__(self) -> None:
        required = (
            self.stable_key,
            self.label,
            self.source_object_type,
            self.source_object_id,
            self.acceptance_criterion,
        )
        if any(not value.strip() for value in required):
            raise ValueError("obligation fields must be non-empty")
        if (
            self.requirement_level is RequirementLevel.REQUIRED
            and self.provenance is ObligationProvenance.AGENT_NORMALIZATION
        ):
            raise ValueError("agent normalization cannot create a required obligation")


@dataclass(frozen=True, slots=True)
class NormalizeCompletionContractCommand:
    command_id: CommandId
    turn_id: TurnId
    expected_turn_revision: int
    goal_summary: str
    obligations: tuple[ContractObligationCandidate, ...]

    def __post_init__(self) -> None:
        if not self.goal_summary.strip():
            raise ValueError("goal_summary is required")
        keys = [item.stable_key for item in self.obligations]
        if len(keys) != len(set(keys)):
            raise ValueError("obligation stable_key values must be unique")


@dataclass(frozen=True, slots=True)
class NormalizedContractIdentity:
    revision_id: CompletionContractRevisionId
    obligation_ids: tuple[CompletionObligationId, ...]


@dataclass(frozen=True, slots=True)
class NormalizeContractOutcome:
    turn_id: TurnId
    completion_contract_revision_id: CompletionContractRevisionId
    turn_revision: int
    obligation_ids: tuple[CompletionObligationId, ...]
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class RecordObligationStateCommand:
    command_id: CommandId
    turn_id: TurnId
    completion_obligation_id: CompletionObligationId
    observed_state: str
    evidence_references: tuple[str, ...] = ()
    actor_kind: str = "system_fact"
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.observed_state not in {"unsatisfied", "satisfied", "waived", "deferred"}:
            raise ValueError("invalid obligation state")
        if self.actor_kind not in {"system_fact", "user_decision"}:
            raise ValueError("invalid obligation actor")
        if self.observed_state == "waived" and self.actor_kind != "user_decision":
            raise ValueError("only a user decision can waive an obligation")


@dataclass(frozen=True, slots=True)
class ObligationStateOutcome:
    obligation_observation_id: ObligationObservationId
    observed_state: str
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class EvidenceDependency:
    object_type: str
    object_id: str
    object_revision: str


@dataclass(frozen=True, slots=True)
class EvaluationFindingCandidate:
    finding_code: str
    severity: str
    message: str
    confidence: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in {"info", "warn", "error"}:
            raise ValueError("invalid evaluation finding severity")
        if self.confidence not in {None, "high", "medium", "low"}:
            raise ValueError("invalid evaluation finding confidence")
        if not self.finding_code.strip() or not self.message.strip():
            raise ValueError("finding code and message are required")


@dataclass(frozen=True, slots=True)
class RecordEvaluationCommand:
    command_id: CommandId
    turn_id: TurnId
    expected_turn_revision: int
    step_id: StepId | None
    evaluation_kind: str
    dimension: EvaluationDimension
    trigger_reason: str
    subject_type: str
    subject_id: str
    subject_revision: str
    dependencies: tuple[EvidenceDependency, ...]
    permitted_slices: tuple[str, ...]
    excluded_scope: tuple[str, ...]
    grader_kind: str
    grader_version: str
    verdict: EvaluationVerdict
    findings: tuple[EvaluationFindingCandidate, ...]
    unknowns: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    suggested_actions: tuple[str, ...] = ()
    policy_revision: str = "evaluation-v0.1"

    def __post_init__(self) -> None:
        if self.grader_kind not in {"rule", "statistical", "model", "human_adapter"}:
            raise ValueError("invalid grader_kind")
        required = (
            self.evaluation_kind,
            self.trigger_reason,
            self.subject_type,
            self.subject_id,
            self.subject_revision,
            self.grader_version,
            self.policy_revision,
        )
        if any(not value.strip() for value in required):
            raise ValueError("evaluation identity fields are required")


@dataclass(frozen=True, slots=True)
class EvaluationIdentity:
    policy_snapshot_id: EvaluationPolicySnapshotId
    evidence_scope_manifest_id: EvidenceScopeManifestId
    evaluation_request_id: EvaluationRequestId
    evaluation_report_id: EvaluationReportId
    finding_ids: tuple[EvaluationFindingId, ...]


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    evaluation_request_id: EvaluationRequestId
    evaluation_report_id: EvaluationReportId
    verdict: EvaluationVerdict
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class DecideNaturalStopCommand:
    command_id: CommandId
    turn_id: TurnId
    expected_turn_revision: int
    evaluation_report_id: EvaluationReportId
    requested_disposition: TerminalDisposition = TerminalDisposition.SUCCEED


@dataclass(frozen=True, slots=True)
class StopGuardIdentity:
    goal_coverage_id: GoalCoverageId
    decision_id: StopGuardDecisionRecordId
    waiting_request_id: WaitingRequestId


@dataclass(frozen=True, slots=True)
class StopGuardOutcome:
    decision_id: StopGuardDecisionRecordId
    goal_coverage_id: GoalCoverageId
    decision: StopGuardDecision
    directive: ContinueDirective | None
    wait_reason: WaitReason | None
    terminal_disposition: TerminalDisposition | None
    reason_code: str
    blockers: tuple[str, ...]
    turn_revision: int
    turn_status: str
    waiting_request_id: WaitingRequestId | None
    commit_revision: WorkspaceRevision
    replayed: bool
