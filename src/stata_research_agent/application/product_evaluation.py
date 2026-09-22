"""Versioned product-evaluation contracts, graders, and trial aggregation.

This module is deliberately outside the in-product Runtime Evaluator.  Runtime evaluation helps a
Turn decide whether to continue; product evaluation independently measures a frozen system under
test against versioned scenarios.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ProductEvaluationError(f"{field_name} cannot be empty")


def _require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ProductEvaluationError(f"{field_name} must be a lowercase sha256")


def _require_relative_locator(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    locator = PurePosixPath(value)
    windows_locator = PureWindowsPath(value)
    if (
        "\\" in value
        or locator.is_absolute()
        or windows_locator.is_absolute()
        or bool(windows_locator.drive)
        or ".." in locator.parts
    ):
        raise ProductEvaluationError(f"{field_name} must be a safe relative POSIX locator")


class ProductEvaluationError(ValueError):
    """Raised when an evaluation contract or result is internally inconsistent."""


class EvaluationSuiteKind(StrEnum):
    SMOKE = "smoke"
    REGRESSION = "regression"
    CAPABILITY = "capability"
    ADVERSARIAL = "adversarial"


class EvaluationSplit(StrEnum):
    DEVELOPMENT = "development"
    HOLDOUT = "holdout"


class EvaluationLevel(StrEnum):
    SUBSYSTEM = "subsystem"
    AGENT_LOOP = "agent_loop"
    PRODUCT = "product"


class SubsystemEvaluationMode(StrEnum):
    INTRINSIC = "intrinsic"
    EXTRINSIC = "extrinsic"


class GraderKind(StrEnum):
    INVARIANT = "invariant"
    OUTCOME = "outcome"
    REPRODUCIBILITY = "reproducibility"
    TRACE = "trace"
    MODEL = "model"
    HUMAN_ADAPTER = "human_adapter"


class GraderRole(StrEnum):
    HARD_GATE = "hard_gate"
    QUALITY = "quality"
    DIAGNOSTIC = "diagnostic"


class ScoreVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    UNSCORED = "unscored"


class TrialStatus(StrEnum):
    COMPLETED = "completed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    CANCELLED = "cancelled"


class TaskOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNSCORED = "unscored"


class ComparisonDisposition(StrEnum):
    IMPROVED = "improved"
    REGRESSED = "regressed"
    MIXED = "mixed"
    UNCHANGED = "unchanged"
    INCOMPARABLE = "incomparable"


@dataclass(frozen=True, slots=True)
class EvaluationFixture:
    fixture_id: str
    role: str
    locator: str
    media_type: str
    sha256: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.fixture_id, "fixture_id"),
            (self.role, "fixture role"),
            (self.media_type, "fixture media_type"),
        ):
            _require_text(value, field_name)
        _require_relative_locator(self.locator, "fixture locator")
        _require_sha256(self.sha256, "fixture sha256")


@dataclass(frozen=True, slots=True)
class ScenarioInteractionEvent:
    event_id: str
    event_kind: str
    content: str

    def __post_init__(self) -> None:
        _require_text(self.event_id, "interaction event_id")
        _require_text(self.event_kind, "interaction event_kind")
        _require_text(self.content, "interaction content")


@dataclass(frozen=True, slots=True)
class ScenarioFault:
    fault_id: str
    fault_kind: str
    trigger: str
    parameters_json: str

    def __post_init__(self) -> None:
        _require_text(self.fault_id, "fault_id")
        _require_text(self.fault_kind, "fault_kind")
        _require_text(self.trigger, "fault trigger")
        try:
            parameters = json.loads(self.parameters_json)
        except json.JSONDecodeError as error:
            raise ProductEvaluationError("fault parameters_json is invalid") from error
        if not isinstance(parameters, dict):
            raise ProductEvaluationError("fault parameters_json must contain an object")


@dataclass(frozen=True, slots=True)
class EvaluationReference:
    status: str
    locator: str
    sha256: str

    def __post_init__(self) -> None:
        if self.status not in {"verified", "pending"}:
            raise ProductEvaluationError("reference status must be verified or pending")
        _require_relative_locator(self.locator, "reference locator")
        _require_sha256(self.sha256, "reference sha256")


@dataclass(frozen=True, slots=True)
class GraderSpec:
    grader_id: str
    grader_kind: GraderKind
    role: GraderRole
    revision: str
    required: bool
    config_json: str

    def __post_init__(self) -> None:
        _require_text(self.grader_id, "grader_id")
        _require_text(self.revision, "grader revision")
        try:
            config = json.loads(self.config_json)
        except json.JSONDecodeError as error:
            raise ProductEvaluationError("grader config_json is invalid") from error
        if not isinstance(config, dict):
            raise ProductEvaluationError("grader config_json must contain an object")
        if self.role is GraderRole.HARD_GATE and not self.required:
            raise ProductEvaluationError("hard-gate graders must be required")


@dataclass(frozen=True, slots=True)
class TrialPolicy:
    trials: int
    reliability_k: int
    max_steps: int
    max_tool_calls: int
    max_provider_attempts: int
    max_wall_clock_seconds: float

    def __post_init__(self) -> None:
        integers = (
            self.trials,
            self.reliability_k,
            self.max_steps,
            self.max_tool_calls,
            self.max_provider_attempts,
        )
        if any(isinstance(value, bool) or value < 1 for value in integers):
            raise ProductEvaluationError("trial policy counts must be positive integers")
        if self.reliability_k > self.trials:
            raise ProductEvaluationError("reliability_k cannot exceed trials")
        if self.max_wall_clock_seconds <= 0:
            raise ProductEvaluationError("max_wall_clock_seconds must be positive")


@dataclass(frozen=True, slots=True)
class EvaluationScenario:
    schema_version: str
    scenario_id: str
    revision: int
    title: str
    suite: EvaluationSuiteKind
    split: EvaluationSplit
    level: EvaluationLevel
    subsystem_mode: SubsystemEvaluationMode | None
    subsystems: tuple[str, ...]
    tags: tuple[str, ...]
    interaction_mode: str
    initial_user_message: str
    fixtures: tuple[EvaluationFixture, ...]
    interaction: tuple[ScenarioInteractionEvent, ...]
    faults: tuple[ScenarioFault, ...]
    required_outcomes: tuple[str, ...]
    forbidden_outcomes: tuple[str, ...]
    permitted_freedom: tuple[str, ...]
    graders: tuple[GraderSpec, ...]
    trial_policy: TrialPolicy
    reference: EvaluationReference
    scenario_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != "stata-research-agent.evaluation-scenario/v1":
            raise ProductEvaluationError("unsupported evaluation scenario schema")
        _require_text(self.scenario_id, "scenario_id")
        _require_text(self.title, "scenario title")
        _require_text(self.interaction_mode, "interaction_mode")
        _require_text(self.initial_user_message, "initial_user_message")
        if isinstance(self.revision, bool) or self.revision < 1:
            raise ProductEvaluationError("scenario revision must be a positive integer")
        if self.interaction_mode not in {"autonomous", "supervised"}:
            raise ProductEvaluationError("interaction_mode must be autonomous or supervised")
        if self.level is EvaluationLevel.SUBSYSTEM and self.subsystem_mode is None:
            raise ProductEvaluationError("subsystem scenarios require subsystem_mode")
        if self.level is not EvaluationLevel.SUBSYSTEM and self.subsystem_mode is not None:
            raise ProductEvaluationError("only subsystem scenarios may set subsystem_mode")
        if not self.subsystems:
            raise ProductEvaluationError("scenario must identify at least one subsystem")
        if not self.required_outcomes:
            raise ProductEvaluationError("scenario must define a required outcome")
        if set(self.required_outcomes) & set(self.forbidden_outcomes):
            raise ProductEvaluationError("required and forbidden outcomes must be disjoint")
        if not self.graders:
            raise ProductEvaluationError("scenario must define at least one grader")
        identifiers = [fixture.fixture_id for fixture in self.fixtures]
        identifiers.extend(event.event_id for event in self.interaction)
        identifiers.extend(fault.fault_id for fault in self.faults)
        if len(identifiers) != len(set(identifiers)):
            raise ProductEvaluationError("scenario fixture/event/fault identities must be unique")
        grader_keys = [(grader.grader_id, grader.revision) for grader in self.graders]
        if len(grader_keys) != len(set(grader_keys)):
            raise ProductEvaluationError("scenario grader identities must be unique")
        for values, field_name in (
            (self.subsystems, "subsystems"),
            (self.tags, "tags"),
            (self.required_outcomes, "required_outcomes"),
            (self.forbidden_outcomes, "forbidden_outcomes"),
            (self.permitted_freedom, "permitted_freedom"),
        ):
            if any(not value.strip() for value in values) or len(values) != len(set(values)):
                raise ProductEvaluationError(f"{field_name} must contain unique non-empty strings")
        if self.suite is EvaluationSuiteKind.REGRESSION and self.reference.status != "verified":
            raise ProductEvaluationError("regression scenarios require a verified reference")
        _require_sha256(self.scenario_sha256, "scenario_sha256")


@dataclass(frozen=True, slots=True)
class SystemUnderTestSnapshot:
    code_revision: str
    provider_profile: str
    model_name: str
    model_parameters_sha256: str
    system_prompt_revision: str
    skill_snapshot_sha256: str
    tool_catalog_revision: str
    rag_policy_revision: str
    memory_policy_revision: str
    context_policy_revision: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.code_revision, "code_revision"),
            (self.provider_profile, "provider_profile"),
            (self.model_name, "model_name"),
            (self.system_prompt_revision, "system_prompt_revision"),
            (self.tool_catalog_revision, "tool_catalog_revision"),
            (self.rag_policy_revision, "rag_policy_revision"),
            (self.memory_policy_revision, "memory_policy_revision"),
            (self.context_policy_revision, "context_policy_revision"),
        ):
            _require_text(value, field_name)
        _require_sha256(self.model_parameters_sha256, "model_parameters_sha256")
        _require_sha256(self.skill_snapshot_sha256, "skill_snapshot_sha256")


@dataclass(frozen=True, slots=True)
class ScenarioBinding:
    scenario_id: str
    revision: int
    scenario_sha256: str


@dataclass(frozen=True, slots=True)
class GraderBinding:
    grader_id: str
    grader_kind: str
    role: str
    revision: str
    config_sha256: str


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    schema_version: str
    experiment_id: str
    created_at: str
    harness_revision: str
    environment_revision: str
    stata_revision: str
    scenarios: tuple[ScenarioBinding, ...]
    graders: tuple[GraderBinding, ...]
    system_under_test: SystemUnderTestSnapshot
    comparison_contract_sha256: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != "stata-research-agent.experiment-manifest/v1":
            raise ProductEvaluationError("unsupported experiment manifest schema")
        for value, field_name in (
            (self.experiment_id, "experiment_id"),
            (self.created_at, "created_at"),
            (self.harness_revision, "harness_revision"),
            (self.environment_revision, "environment_revision"),
            (self.stata_revision, "stata_revision"),
        ):
            _require_text(value, field_name)
        if not self.scenarios:
            raise ProductEvaluationError("experiment must contain a scenario")
        if len({item.scenario_id for item in self.scenarios}) != len(self.scenarios):
            raise ProductEvaluationError("experiment scenario_id values must be unique")
        grader_keys = [(item.grader_id, item.revision) for item in self.graders]
        if len(grader_keys) != len(set(grader_keys)):
            raise ProductEvaluationError(
                "experiment grader identity cannot have conflicting bindings"
            )
        _require_sha256(
            self.comparison_contract_sha256,
            "comparison_contract_sha256",
        )
        _require_sha256(self.manifest_sha256, "manifest_sha256")

    @classmethod
    def create(
        cls,
        *,
        experiment_id: str,
        created_at: str,
        harness_revision: str,
        environment_revision: str,
        stata_revision: str,
        scenarios: tuple[EvaluationScenario, ...],
        system_under_test: SystemUnderTestSnapshot,
    ) -> ExperimentManifest:
        for value, field_name in (
            (experiment_id, "experiment_id"),
            (created_at, "created_at"),
            (harness_revision, "harness_revision"),
            (environment_revision, "environment_revision"),
            (stata_revision, "stata_revision"),
        ):
            _require_text(value, field_name)
        if not scenarios:
            raise ProductEvaluationError("experiment must contain a scenario")
        scenario_bindings = tuple(
            sorted(
                (
                    ScenarioBinding(
                        scenario.scenario_id,
                        scenario.revision,
                        scenario.scenario_sha256,
                    )
                    for scenario in scenarios
                ),
                key=lambda item: (item.scenario_id, item.revision),
            )
        )
        if len({item.scenario_id for item in scenario_bindings}) != len(scenario_bindings):
            raise ProductEvaluationError("experiment scenario_id values must be unique")
        grader_bindings_by_identity: dict[tuple[str, str], GraderBinding] = {}
        for scenario in scenarios:
            for grader in scenario.graders:
                binding = GraderBinding(
                    grader.grader_id,
                    grader.grader_kind.value,
                    grader.role.value,
                    grader.revision,
                    _sha256(json.loads(grader.config_json)),
                )
                identity = (binding.grader_id, binding.revision)
                existing = grader_bindings_by_identity.get(identity)
                if existing is not None and existing != binding:
                    raise ProductEvaluationError(
                        "the same grader identity has conflicting experiment bindings"
                    )
                grader_bindings_by_identity[identity] = binding
        grader_bindings = tuple(
            sorted(
                grader_bindings_by_identity.values(),
                key=lambda item: (item.grader_id, item.revision),
            )
        )
        contract = {
            "schema_version": "stata-research-agent.evaluation-contract/v1",
            "harness_revision": harness_revision,
            "environment_revision": environment_revision,
            "stata_revision": stata_revision,
            "scenarios": [asdict(item) for item in scenario_bindings],
            "graders": [asdict(item) for item in grader_bindings],
        }
        contract_sha256 = _sha256(contract)
        body = {
            "schema_version": "stata-research-agent.experiment-manifest/v1",
            "experiment_id": experiment_id,
            "created_at": created_at,
            "harness_revision": harness_revision,
            "environment_revision": environment_revision,
            "stata_revision": stata_revision,
            "scenarios": [asdict(item) for item in scenario_bindings],
            "graders": [asdict(item) for item in grader_bindings],
            "system_under_test": asdict(system_under_test),
            "comparison_contract_sha256": contract_sha256,
        }
        return cls(
            "stata-research-agent.experiment-manifest/v1",
            experiment_id,
            created_at,
            harness_revision,
            environment_revision,
            stata_revision,
            scenario_bindings,
            grader_bindings,
            system_under_test,
            contract_sha256,
            _sha256(body),
        )

    def comparable_with(self, other: ExperimentManifest) -> bool:
        return self.comparison_contract_sha256 == other.comparison_contract_sha256

    def to_dict(self) -> dict[str, object]:
        """Return the canonical public payload, including its integrity digest."""

        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "created_at": self.created_at,
            "harness_revision": self.harness_revision,
            "environment_revision": self.environment_revision,
            "stata_revision": self.stata_revision,
            "scenarios": [asdict(item) for item in self.scenarios],
            "graders": [asdict(item) for item in self.graders],
            "system_under_test": asdict(self.system_under_test),
            "comparison_contract_sha256": self.comparison_contract_sha256,
            "manifest_sha256": self.manifest_sha256,
        }

    def to_json(self) -> str:
        """Serialize deterministically so the persisted manifest is diffable."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ) + "\n"


@dataclass(frozen=True, slots=True)
class EvaluationPayloadReference:
    payload_id: str
    media_type: str
    path: Path
    sha256: str

    def __post_init__(self) -> None:
        _require_text(self.payload_id, "evaluation payload_id")
        _require_text(self.media_type, "evaluation payload media_type")
        _require_sha256(self.sha256, "evaluation payload sha256")


@dataclass(frozen=True, slots=True)
class TrialBundle:
    trial_id: str
    scenario_id: str
    scenario_revision: int
    scenario_sha256: str
    workspace_root: Path
    workspace_database: Path
    trace_export: Path
    artifact_manifest: Path
    observations: tuple[str, ...]
    evidence_references: tuple[str, ...] = ()
    evaluation_payloads: tuple[EvaluationPayloadReference, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.trial_id, "trial_id")
        _require_text(self.scenario_id, "scenario_id")
        _require_sha256(self.scenario_sha256, "trial scenario_sha256")
        if self.scenario_revision < 1:
            raise ProductEvaluationError("trial scenario_revision must be positive")
        if len(self.observations) != len(set(self.observations)):
            raise ProductEvaluationError("trial observations must be unique")
        payload_ids = [payload.payload_id for payload in self.evaluation_payloads]
        if len(payload_ids) != len(set(payload_ids)):
            raise ProductEvaluationError("evaluation payload identities must be unique")


@dataclass(frozen=True, slots=True)
class EvaluationScore:
    grader_id: str
    grader_revision: str
    grader_kind: GraderKind
    role: GraderRole
    verdict: ScoreVerdict
    value: float | bool | str | None
    explanation: str
    evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.grader_id, "score grader_id")
        _require_text(self.grader_revision, "score grader_revision")
        _require_text(self.explanation, "score explanation")


class TrialGrader(Protocol):
    grader_id: str
    revision: str
    kind: GraderKind

    def grade(
        self,
        scenario: EvaluationScenario,
        spec: GraderSpec,
        bundle: TrialBundle,
    ) -> EvaluationScore: ...


class TrialGraderRegistry:
    def __init__(self, graders: tuple[TrialGrader, ...]) -> None:
        keys = [(grader.grader_id, grader.revision) for grader in graders]
        if len(keys) != len(set(keys)):
            raise ProductEvaluationError("registered grader identities must be unique")
        self._graders = {key: grader for key, grader in zip(keys, graders, strict=True)}

    def grade(
        self,
        scenario: EvaluationScenario,
        bundle: TrialBundle,
    ) -> tuple[EvaluationScore, ...]:
        if (
            bundle.scenario_id != scenario.scenario_id
            or bundle.scenario_revision != scenario.revision
            or bundle.scenario_sha256 != scenario.scenario_sha256
        ):
            raise ProductEvaluationError("trial bundle targets another scenario revision")
        scores: list[EvaluationScore] = []
        for spec in scenario.graders:
            grader = self._graders.get((spec.grader_id, spec.revision))
            if grader is None:
                if spec.required:
                    raise ProductEvaluationError(
                        f"required grader is not registered: {spec.grader_id}@{spec.revision}"
                    )
                continue
            if grader.kind is not spec.grader_kind:
                raise ProductEvaluationError("registered grader kind does not match scenario")
            scores.append(grader.grade(scenario, spec, bundle))
        return tuple(scores)


class ObservationContractGrader:
    grader_id = "core.observation_contract"
    revision = "observation-contract/v1"
    kind = GraderKind.INVARIANT

    def grade(
        self,
        scenario: EvaluationScenario,
        spec: GraderSpec,
        bundle: TrialBundle,
    ) -> EvaluationScore:
        observed = set(bundle.observations)
        missing = sorted(set(scenario.required_outcomes) - observed)
        forbidden = sorted(set(scenario.forbidden_outcomes) & observed)
        passed = not missing and not forbidden
        explanation = _canonical_json({"missing": missing, "forbidden": forbidden})
        return EvaluationScore(
            self.grader_id,
            self.revision,
            self.kind,
            spec.role,
            ScoreVerdict.PASS if passed else ScoreVerdict.FAIL,
            passed,
            explanation,
            bundle.evidence_references,
        )


@dataclass(frozen=True, slots=True)
class TrialResult:
    trial_id: str
    experiment_id: str
    scenario_id: str
    scenario_revision: int
    scenario_sha256: str
    trial_ordinal: int
    status: TrialStatus
    task_outcome: TaskOutcome
    scores: tuple[EvaluationScore, ...]
    failure_codes: tuple[str, ...]
    duration_seconds: float
    cost_amount: Decimal | None
    cost_currency: str | None
    bundle_locator: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.trial_id, "trial_id"),
            (self.experiment_id, "experiment_id"),
            (self.scenario_id, "scenario_id"),
            (self.bundle_locator, "bundle_locator"),
        ):
            _require_text(value, field_name)
        _require_sha256(self.scenario_sha256, "trial result scenario_sha256")
        if self.trial_ordinal < 1 or self.scenario_revision < 1:
            raise ProductEvaluationError("trial and scenario ordinals must be positive")
        if self.duration_seconds < 0:
            raise ProductEvaluationError("trial duration cannot be negative")
        if self.cost_amount is not None and self.cost_amount < 0:
            raise ProductEvaluationError("trial cost cannot be negative")
        if (self.cost_amount is None) != (self.cost_currency is None):
            raise ProductEvaluationError("trial cost amount and currency must appear together")
        if (
            self.status is not TrialStatus.COMPLETED
            and self.task_outcome is not TaskOutcome.UNSCORED
        ):
            raise ProductEvaluationError("incomplete trials must remain unscored")
        hard_failures = tuple(
            score
            for score in self.scores
            if score.role is GraderRole.HARD_GATE and score.verdict is not ScoreVerdict.PASS
        )
        if hard_failures and self.task_outcome is TaskOutcome.PASS:
            raise ProductEvaluationError("a trial with a hard-gate failure cannot pass")


@dataclass(frozen=True, slots=True)
class CaseMetrics:
    scenario_id: str
    scenario_revision: int
    scenario_sha256: str
    requested_trials: int
    completed_trials: int
    infrastructure_errors: int
    passed_trials: int
    hard_gate_failure_count: int
    pass_at_1: float | None
    pass_at_k: float | None
    pass_power_k: float | None
    reliability_k: int
    mean_duration_seconds: float | None
    known_mean_cost: Decimal | None
    cost_currency: str | None


@dataclass(frozen=True, slots=True)
class ExperimentMetrics:
    experiment_id: str
    comparison_contract_sha256: str
    cases: tuple[CaseMetrics, ...]
    requested_trials: int
    completed_trials: int
    infrastructure_errors: int
    hard_gate_failure_count: int

    def __post_init__(self) -> None:
        _require_text(self.experiment_id, "experiment metrics experiment_id")
        _require_sha256(
            self.comparison_contract_sha256,
            "experiment metrics comparison_contract_sha256",
        )
        identities = [(case.scenario_id, case.scenario_revision) for case in self.cases]
        if not self.cases or len(identities) != len(set(identities)):
            raise ProductEvaluationError("Experiment Metrics require unique Cases")


def aggregate_experiment_cases(
    manifest: ExperimentManifest,
    cases: tuple[CaseMetrics, ...],
) -> ExperimentMetrics:
    expected = {
        (binding.scenario_id, binding.revision, binding.scenario_sha256)
        for binding in manifest.scenarios
    }
    actual = {
        (case.scenario_id, case.scenario_revision, case.scenario_sha256)
        for case in cases
    }
    if actual != expected:
        raise ProductEvaluationError("Experiment Metrics do not cover the frozen Scenario set")
    return ExperimentMetrics(
        manifest.experiment_id,
        manifest.comparison_contract_sha256,
        tuple(sorted(cases, key=lambda case: (case.scenario_id, case.scenario_revision))),
        sum(case.requested_trials for case in cases),
        sum(case.completed_trials for case in cases),
        sum(case.infrastructure_errors for case in cases),
        sum(case.hard_gate_failure_count for case in cases),
    )


def aggregate_case_trials(
    scenario: EvaluationScenario, trials: tuple[TrialResult, ...]
) -> CaseMetrics:
    if len(trials) != scenario.trial_policy.trials:
        raise ProductEvaluationError("trial count does not match the scenario policy")
    ordinals = [trial.trial_ordinal for trial in trials]
    if set(ordinals) != set(range(1, scenario.trial_policy.trials + 1)):
        raise ProductEvaluationError("trial ordinals must exactly cover the scenario policy")
    if any(
        trial.scenario_id != scenario.scenario_id
        or trial.scenario_revision != scenario.revision
        or trial.scenario_sha256 != scenario.scenario_sha256
        for trial in trials
    ):
        raise ProductEvaluationError("case aggregation received another scenario revision")

    completed = tuple(trial for trial in trials if trial.status is TrialStatus.COMPLETED)
    passed = sum(trial.task_outcome is TaskOutcome.PASS for trial in completed)
    hard_gate_failures = sum(
        score.role is GraderRole.HARD_GATE and score.verdict is not ScoreVerdict.PASS
        for trial in completed
        for score in trial.scores
    )
    sample_size = len(completed)
    k = scenario.trial_policy.reliability_k
    pass_at_1: float | None = passed / sample_size if sample_size else None
    pass_at_k: float | None = None
    pass_power_k: float | None = None
    if sample_size >= k:
        denominator = math.comb(sample_size, k)
        failures = sample_size - passed
        pass_at_k = 1 - math.comb(failures, k) / denominator if failures >= k else 1.0
        pass_power_k = math.comb(passed, k) / denominator if passed >= k else 0.0

    costs = tuple(trial.cost_amount for trial in completed if trial.cost_amount is not None)
    currencies = {trial.cost_currency for trial in completed if trial.cost_currency is not None}
    if len(currencies) > 1:
        raise ProductEvaluationError("case trials use incompatible cost currencies")
    known_mean_cost = sum(costs, Decimal(0)) / len(costs) if costs else None
    return CaseMetrics(
        scenario.scenario_id,
        scenario.revision,
        scenario.scenario_sha256,
        scenario.trial_policy.trials,
        sample_size,
        sum(trial.status is TrialStatus.INFRASTRUCTURE_ERROR for trial in trials),
        passed,
        hard_gate_failures,
        pass_at_1,
        pass_at_k,
        pass_power_k,
        k,
        sum(trial.duration_seconds for trial in completed) / sample_size if sample_size else None,
        known_mean_cost,
        next(iter(currencies)) if currencies else None,
    )


@dataclass(frozen=True, slots=True)
class CaseComparison:
    scenario_id: str
    disposition: ComparisonDisposition
    pass_at_1_delta: float | None
    hard_gate_failure_delta: int | None
    duration_delta_seconds: float | None
    cost_delta: Decimal | None
    explanation: str


def compare_case_metrics(baseline: CaseMetrics, candidate: CaseMetrics) -> CaseComparison:
    if (
        baseline.scenario_id != candidate.scenario_id
        or baseline.scenario_revision != candidate.scenario_revision
        or baseline.scenario_sha256 != candidate.scenario_sha256
        or baseline.reliability_k != candidate.reliability_k
    ):
        return CaseComparison(
            candidate.scenario_id,
            ComparisonDisposition.INCOMPARABLE,
            None,
            None,
            None,
            None,
            "scenario identity or reliability policy differs",
        )
    if baseline.pass_at_1 is None or candidate.pass_at_1 is None:
        return CaseComparison(
            candidate.scenario_id,
            ComparisonDisposition.INCOMPARABLE,
            None,
            None,
            None,
            None,
            "one experiment has no completed trials",
        )
    pass_delta = candidate.pass_at_1 - baseline.pass_at_1
    gate_delta = candidate.hard_gate_failure_count - baseline.hard_gate_failure_count
    duration_delta = (
        None
        if baseline.mean_duration_seconds is None or candidate.mean_duration_seconds is None
        else candidate.mean_duration_seconds - baseline.mean_duration_seconds
    )
    cost_delta: Decimal | None = None
    if (
        baseline.known_mean_cost is not None
        and candidate.known_mean_cost is not None
        and baseline.cost_currency == candidate.cost_currency
    ):
        cost_delta = candidate.known_mean_cost - baseline.known_mean_cost

    quality_direction = (pass_delta > 0) - (pass_delta < 0)
    gate_direction = (gate_delta < 0) - (gate_delta > 0)
    if quality_direction == 0 and gate_direction == 0:
        disposition = ComparisonDisposition.UNCHANGED
    elif quality_direction >= 0 and gate_direction >= 0:
        disposition = ComparisonDisposition.IMPROVED
    elif quality_direction <= 0 and gate_direction <= 0:
        disposition = ComparisonDisposition.REGRESSED
    else:
        disposition = ComparisonDisposition.MIXED
    return CaseComparison(
        candidate.scenario_id,
        disposition,
        pass_delta,
        gate_delta,
        duration_delta,
        cost_delta,
        "quality and hard-gate deltas are reported independently",
    )
