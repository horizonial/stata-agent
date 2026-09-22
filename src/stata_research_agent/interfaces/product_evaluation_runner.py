"""Filesystem-backed isolated Trial runner for offline Product Evaluation."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from stata_research_agent.application.product_evaluation import (
    CaseMetrics,
    EvaluationScenario,
    ExperimentManifest,
    ExperimentMetrics,
    GraderRole,
    ProductEvaluationError,
    ScoreVerdict,
    TaskOutcome,
    TrialBundle,
    TrialGraderRegistry,
    TrialResult,
    TrialStatus,
    aggregate_case_trials,
    aggregate_experiment_cases,
)


class EvaluationScenarioAdapter(Protocol):
    scenario_id: str

    def execute(
        self,
        scenario: EvaluationScenario,
        *,
        trial_id: str,
        trial_root: Path,
    ) -> TrialBundle: ...


class FilesystemEvaluationRunStore:
    """Own immutable experiment identities and per-Trial result envelopes."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def create_experiment(self, manifest: ExperimentManifest) -> Path:
        self._validate_segment(manifest.experiment_id, "experiment_id")
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / manifest.experiment_id
        if target.exists():
            raise ProductEvaluationError("evaluation experiment already exists")
        staging = self._root / f".{manifest.experiment_id}.staging.{uuid4().hex}"
        staging.mkdir()
        try:
            self._write_new(staging / "experiment-manifest.json", manifest.to_json())
            (staging / ".staging").mkdir()
            (staging / "trials").mkdir()
            (staging / "case-metrics").mkdir()
            os.replace(staging, target)
        except BaseException:
            for child in (
                staging / "experiment-manifest.json",
                staging / ".staging",
                staging / "trials",
                staging / "case-metrics",
            ):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            if staging.is_dir():
                staging.rmdir()
            raise
        return target

    def reserve_trial(
        self,
        manifest: ExperimentManifest,
        scenario: EvaluationScenario,
        ordinal: int,
    ) -> tuple[str, Path, str]:
        self._assert_manifest(manifest)
        self._validate_segment(scenario.scenario_id, "scenario_id")
        if ordinal < 1 or ordinal > scenario.trial_policy.trials:
            raise ProductEvaluationError("trial ordinal is outside scenario policy")
        trial_id = f"trial-{ordinal:03d}-{scenario.scenario_sha256[:12]}"
        relative = f"trials/{trial_id}"
        experiment_root = self._root / manifest.experiment_id
        trial_root = experiment_root / ".staging" / trial_id
        final_root = experiment_root / relative
        if final_root.exists():
            raise ProductEvaluationError("evaluation trial identity already exists")
        try:
            trial_root.mkdir()
        except FileExistsError as error:
            raise ProductEvaluationError("evaluation trial identity already exists") from error
        self._write_new(
            trial_root / "trial-manifest.json",
            self._json(
                {
                    "schema_version": "stata-research-agent.evaluation-trial-manifest/v1",
                    "trial_id": trial_id,
                    "scenario_id": scenario.scenario_id,
                    "scenario_revision": scenario.revision,
                    "scenario_sha256": scenario.scenario_sha256,
                    "trial_ordinal": ordinal,
                }
            ),
        )
        return trial_id, trial_root, relative

    def finalize_trial(
        self,
        manifest: ExperimentManifest,
        result: TrialResult,
        bundle: TrialBundle | None,
    ) -> Path:
        self._assert_manifest(manifest)
        relative = Path(result.bundle_locator)
        if relative.is_absolute() or ".." in relative.parts:
            raise ProductEvaluationError("Trial Result bundle locator is unsafe")
        experiment_root = self._root / manifest.experiment_id
        final_root = experiment_root / relative.parent
        staging_root = experiment_root / ".staging" / result.trial_id
        if final_root.name != result.trial_id or not staging_root.is_dir():
            raise ProductEvaluationError("Trial Result does not target a reserved Trial")
        if final_root.exists():
            raise ProductEvaluationError("evaluation trial identity already exists")
        if bundle is not None:
            self._write_artifact_integrity(staging_root, bundle)
        self._write_new(
            staging_root / "scores.json",
            self._json(
                {
                    "schema_version": "stata-research-agent.evaluation-scores/v1",
                    "trial_id": result.trial_id,
                    "scores": self._scores_payload(result),
                }
            ),
        )
        self._write_new(
            staging_root / "trial-result.json",
            self._json(self._result_payload(result)),
        )
        os.replace(staging_root, final_root)
        return final_root / "trial-result.json"

    def write_case_metrics(
        self,
        manifest: ExperimentManifest,
        metrics: CaseMetrics,
    ) -> Path:
        self._assert_manifest(manifest)
        self._validate_segment(metrics.scenario_id, "scenario_id")
        destination = (
            self._root
            / manifest.experiment_id
            / "case-metrics"
            / f"{metrics.scenario_id}.json"
        )
        payload = asdict(metrics)
        payload["known_mean_cost"] = (
            None if metrics.known_mean_cost is None else str(metrics.known_mean_cost)
        )
        self._write_new(
            destination,
            self._json(
                {
                    "schema_version": "stata-research-agent.evaluation-case-metrics/v1",
                    **payload,
                }
            ),
        )
        return destination

    def write_experiment_metrics(
        self,
        manifest: ExperimentManifest,
        metrics: ExperimentMetrics,
    ) -> Path:
        self._assert_manifest(manifest)
        if metrics.experiment_id != manifest.experiment_id:
            raise ProductEvaluationError("Experiment Metrics target another Experiment")
        payload = asdict(metrics)
        for case in payload["cases"]:
            if case["known_mean_cost"] is not None:
                case["known_mean_cost"] = str(case["known_mean_cost"])
        destination = self._root / manifest.experiment_id / "experiment-result.json"
        self._write_new(
            destination,
            self._json(
                {
                    "schema_version": "stata-research-agent.evaluation-experiment-result/v1",
                    **payload,
                }
            ),
        )
        return destination

    def _write_artifact_integrity(
        self,
        staging_root: Path,
        bundle: TrialBundle,
    ) -> None:
        members = (
            ("workspace_database", bundle.workspace_database),
            ("trace", bundle.trace_export),
            ("adapter_artifact_manifest", bundle.artifact_manifest),
        ) + tuple(
            (f"evaluation_payload:{payload.payload_id}", payload.path)
            for payload in bundle.evaluation_payloads
        )
        payloads: list[dict[str, object]] = []
        resolved_staging = staging_root.resolve()
        for role, path in members:
            resolved = path.resolve()
            if (
                not resolved.is_relative_to(resolved_staging)
                or not resolved.is_file()
                or resolved.is_symlink()
            ):
                raise ProductEvaluationError("Trial Artifact is missing or outside staging")
            content = resolved.read_bytes()
            declared = next(
                (
                    payload
                    for payload in bundle.evaluation_payloads
                    if path == payload.path
                ),
                None,
            )
            if declared is not None and sha256(content).hexdigest() != declared.sha256:
                raise ProductEvaluationError("Evaluation payload digest mismatch")
            payloads.append(
                {
                    "role": role,
                    "locator": resolved.relative_to(resolved_staging).as_posix(),
                    "size_bytes": len(content),
                    "sha256": sha256(content).hexdigest(),
                }
            )
        self._write_new(
            staging_root / "artifact-integrity.json",
            self._json(
                {
                    "schema_version": "stata-research-agent.evaluation-artifacts/v1",
                    "trial_id": bundle.trial_id,
                    "artifacts": payloads,
                }
            ),
        )

    def _assert_manifest(self, manifest: ExperimentManifest) -> None:
        manifest_path = self._root / manifest.experiment_id / "experiment-manifest.json"
        try:
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProductEvaluationError("evaluation experiment manifest is unavailable") from error
        if persisted != manifest.to_dict():
            raise ProductEvaluationError("evaluation experiment manifest identity mismatch")

    @staticmethod
    def _validate_segment(value: str, field_name: str) -> None:
        if (
            not value
            or value in {".", ".."}
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in value
            )
        ):
            raise ProductEvaluationError(f"{field_name} must be a safe path segment")

    @staticmethod
    def _write_new(path: Path, payload: str) -> None:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ) + "\n"

    @staticmethod
    def _result_payload(result: TrialResult) -> dict[str, object]:
        return {
            "schema_version": "stata-research-agent.evaluation-trial-result/v1",
            "trial_id": result.trial_id,
            "experiment_id": result.experiment_id,
            "scenario_id": result.scenario_id,
            "scenario_revision": result.scenario_revision,
            "scenario_sha256": result.scenario_sha256,
            "trial_ordinal": result.trial_ordinal,
            "status": result.status.value,
            "task_outcome": result.task_outcome.value,
            "scores": FilesystemEvaluationRunStore._scores_payload(result),
            "failure_codes": list(result.failure_codes),
            "duration_seconds": result.duration_seconds,
            "cost_amount": (
                None if result.cost_amount is None else str(result.cost_amount)
            ),
            "cost_currency": result.cost_currency,
            "bundle_locator": result.bundle_locator,
        }

    @staticmethod
    def _scores_payload(result: TrialResult) -> list[dict[str, object]]:
        return [
            {
                **asdict(score),
                "grader_kind": score.grader_kind.value,
                "role": score.role.value,
                "verdict": score.verdict.value,
            }
            for score in result.scores
        ]


class ProductEvaluationTrialRunner:
    """Run one frozen Scenario without borrowing state from another Trial."""

    def __init__(
        self,
        store: FilesystemEvaluationRunStore,
        grader_registry: TrialGraderRegistry,
        adapters: tuple[EvaluationScenarioAdapter, ...],
    ) -> None:
        identities = [adapter.scenario_id for adapter in adapters]
        if len(identities) != len(set(identities)):
            raise ProductEvaluationError("Scenario adapter identities must be unique")
        self._store = store
        self._graders = grader_registry
        self._adapters = {adapter.scenario_id: adapter for adapter in adapters}

    def run_trial(
        self,
        manifest: ExperimentManifest,
        scenario: EvaluationScenario,
        ordinal: int,
    ) -> TrialResult:
        self._assert_scenario_bound(manifest, scenario)
        trial_id, trial_root, relative = self._store.reserve_trial(
            manifest,
            scenario,
            ordinal,
        )
        started = time.monotonic()
        adapter = self._adapters.get(scenario.scenario_id)
        bundle: TrialBundle | None = None
        if adapter is None:
            result = self._infrastructure_result(
                manifest,
                scenario,
                trial_id,
                ordinal,
                relative,
                started,
                "SCENARIO_ADAPTER_UNAVAILABLE",
            )
        else:
            try:
                bundle = adapter.execute(
                    scenario,
                    trial_id=trial_id,
                    trial_root=trial_root / "bundle",
                )
                scores = self._graders.grade(scenario, bundle)
                required_keys = {
                    (spec.grader_id, spec.revision)
                    for spec in scenario.graders
                    if spec.required
                }
                passed_keys = {
                    (score.grader_id, score.grader_revision)
                    for score in scores
                    if score.verdict is ScoreVerdict.PASS
                }
                hard_failure = any(
                    score.role is GraderRole.HARD_GATE
                    and score.verdict is not ScoreVerdict.PASS
                    for score in scores
                )
                passed = not hard_failure and required_keys <= passed_keys
                result = TrialResult(
                    trial_id,
                    manifest.experiment_id,
                    scenario.scenario_id,
                    scenario.revision,
                    scenario.scenario_sha256,
                    ordinal,
                    TrialStatus.COMPLETED,
                    TaskOutcome.PASS if passed else TaskOutcome.FAIL,
                    scores,
                    () if passed else ("REQUIRED_EVALUATION_FAILED",),
                    time.monotonic() - started,
                    None,
                    None,
                    f"{relative}/bundle",
                )
            except Exception as error:
                result = self._infrastructure_result(
                    manifest,
                    scenario,
                    trial_id,
                    ordinal,
                    relative,
                    started,
                    f"ADAPTER_{type(error).__name__.upper()}",
                )
        self._store.finalize_trial(manifest, result, bundle)
        return result

    def run_case(
        self,
        manifest: ExperimentManifest,
        scenario: EvaluationScenario,
    ) -> tuple[tuple[TrialResult, ...], CaseMetrics]:
        trials = tuple(
            self.run_trial(manifest, scenario, ordinal)
            for ordinal in range(1, scenario.trial_policy.trials + 1)
        )
        metrics = aggregate_case_trials(scenario, trials)
        self._store.write_case_metrics(manifest, metrics)
        return trials, metrics

    def run_experiment(
        self,
        manifest: ExperimentManifest,
        scenarios: tuple[EvaluationScenario, ...],
    ) -> ExperimentMetrics:
        cases = tuple(self.run_case(manifest, scenario)[1] for scenario in scenarios)
        metrics = aggregate_experiment_cases(manifest, cases)
        self._store.write_experiment_metrics(manifest, metrics)
        return metrics

    @staticmethod
    def _assert_scenario_bound(
        manifest: ExperimentManifest,
        scenario: EvaluationScenario,
    ) -> None:
        if not any(
            binding.scenario_id == scenario.scenario_id
            and binding.revision == scenario.revision
            and binding.scenario_sha256 == scenario.scenario_sha256
            for binding in manifest.scenarios
        ):
            raise ProductEvaluationError("Scenario is not frozen in Experiment Manifest")

    @staticmethod
    def _infrastructure_result(
        manifest: ExperimentManifest,
        scenario: EvaluationScenario,
        trial_id: str,
        ordinal: int,
        relative: str,
        started: float,
        failure_code: str,
    ) -> TrialResult:
        return TrialResult(
            trial_id,
            manifest.experiment_id,
            scenario.scenario_id,
            scenario.revision,
            scenario.scenario_sha256,
            ordinal,
            TrialStatus.INFRASTRUCTURE_ERROR,
            TaskOutcome.UNSCORED,
            (),
            (failure_code,),
            time.monotonic() - started,
            None,
            None,
            f"{relative}/bundle",
        )
