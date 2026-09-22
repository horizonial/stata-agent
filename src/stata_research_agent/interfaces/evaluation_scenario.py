"""Strict loader for versioned Product Evaluation Scenario manifests."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from stata_research_agent.application.product_evaluation import (
    EvaluationFixture,
    EvaluationLevel,
    EvaluationReference,
    EvaluationScenario,
    EvaluationSplit,
    EvaluationSuiteKind,
    GraderKind,
    GraderRole,
    GraderSpec,
    ProductEvaluationError,
    ScenarioFault,
    ScenarioInteractionEvent,
    SubsystemEvaluationMode,
    TrialPolicy,
)

_SCHEMA = "stata-research-agent.evaluation-scenario/v1"
_TOP_LEVEL_KEYS = {
    "schema_version",
    "scenario_id",
    "revision",
    "title",
    "suite",
    "split",
    "level",
    "subsystem_mode",
    "subsystems",
    "tags",
    "interaction_mode",
    "initial_user_message",
    "fixtures",
    "interaction",
    "faults",
    "required_outcomes",
    "forbidden_outcomes",
    "permitted_freedom",
    "graders",
    "trial_policy",
    "reference",
}
_ENUM = TypeVar("_ENUM", bound=Enum)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class EvaluationScenarioLoader:
    def load(self, path: Path) -> EvaluationScenario:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProductEvaluationError("Evaluation Scenario is not readable JSON") from error
        if not isinstance(raw, dict) or set(raw) != _TOP_LEVEL_KEYS:
            raise ProductEvaluationError("Evaluation Scenario has an unknown or missing field")
        if raw["schema_version"] != _SCHEMA:
            raise ProductEvaluationError("unsupported evaluation scenario schema")
        revision = self._integer(raw, "revision", minimum=1)
        subsystem_mode_raw = raw["subsystem_mode"]
        if subsystem_mode_raw is not None and not isinstance(subsystem_mode_raw, str):
            raise ProductEvaluationError("subsystem_mode must be a string or null")
        scenario_hash = hashlib.sha256(_canonical(raw).encode("utf-8")).hexdigest()

        fixtures = tuple(
            EvaluationFixture(
                self._string(item, "fixture_id"),
                self._string(item, "role"),
                self._string(item, "locator"),
                self._string(item, "media_type"),
                self._string(item, "sha256"),
            )
            for item in self._object_list(
                raw,
                "fixtures",
                {"fixture_id", "role", "locator", "media_type", "sha256"},
            )
        )
        interaction = tuple(
            ScenarioInteractionEvent(
                self._string(item, "event_id"),
                self._string(item, "event_kind"),
                self._string(item, "content"),
            )
            for item in self._object_list(
                raw,
                "interaction",
                {"event_id", "event_kind", "content"},
            )
        )
        faults = tuple(
            ScenarioFault(
                self._string(item, "fault_id"),
                self._string(item, "fault_kind"),
                self._string(item, "trigger"),
                _canonical(self._dict(item, "parameters")),
            )
            for item in self._object_list(
                raw,
                "faults",
                {"fault_id", "fault_kind", "trigger", "parameters"},
            )
        )
        graders = tuple(
            GraderSpec(
                self._string(item, "grader_id"),
                self._enum(GraderKind, self._string(item, "kind"), "grader kind"),
                self._enum(GraderRole, self._string(item, "role"), "grader role"),
                self._string(item, "revision"),
                self._boolean(item, "required"),
                _canonical(self._dict(item, "config")),
            )
            for item in self._object_list(
                raw,
                "graders",
                {"grader_id", "kind", "role", "revision", "required", "config"},
            )
        )
        policy = self._object(
            raw,
            "trial_policy",
            {
                "trials",
                "reliability_k",
                "max_steps",
                "max_tool_calls",
                "max_provider_attempts",
                "max_wall_clock_seconds",
            },
        )
        reference = self._object(raw, "reference", {"status", "locator", "sha256"})
        return EvaluationScenario(
            _SCHEMA,
            self._string(raw, "scenario_id"),
            revision,
            self._string(raw, "title"),
            self._enum(EvaluationSuiteKind, self._string(raw, "suite"), "suite"),
            self._enum(EvaluationSplit, self._string(raw, "split"), "split"),
            self._enum(EvaluationLevel, self._string(raw, "level"), "level"),
            None
            if subsystem_mode_raw is None
            else self._enum(
                SubsystemEvaluationMode,
                subsystem_mode_raw,
                "subsystem_mode",
            ),
            self._string_list(raw, "subsystems", allow_empty=False),
            self._string_list(raw, "tags", allow_empty=True),
            self._string(raw, "interaction_mode"),
            self._string(raw, "initial_user_message"),
            fixtures,
            interaction,
            faults,
            self._string_list(raw, "required_outcomes", allow_empty=False),
            self._string_list(raw, "forbidden_outcomes", allow_empty=True),
            self._string_list(raw, "permitted_freedom", allow_empty=True),
            graders,
            TrialPolicy(
                self._integer(policy, "trials", minimum=1),
                self._integer(policy, "reliability_k", minimum=1),
                self._integer(policy, "max_steps", minimum=1),
                self._integer(policy, "max_tool_calls", minimum=1),
                self._integer(policy, "max_provider_attempts", minimum=1),
                self._number(policy, "max_wall_clock_seconds", minimum_exclusive=0),
            ),
            EvaluationReference(
                self._string(reference, "status"),
                self._string(reference, "locator"),
                self._string(reference, "sha256"),
            ),
            scenario_hash,
        )

    @staticmethod
    def _object(source: dict[str, Any], key: str, keys: set[str]) -> dict[str, Any]:
        value = source.get(key)
        if not isinstance(value, dict) or set(value) != keys:
            raise ProductEvaluationError(f"{key} has an unknown or missing field")
        return value

    @staticmethod
    def _dict(source: dict[str, Any], key: str) -> dict[str, Any]:
        value = source.get(key)
        if not isinstance(value, dict):
            raise ProductEvaluationError(f"{key} must be an object")
        return value

    @staticmethod
    def _object_list(
        source: dict[str, Any], key: str, keys: set[str]
    ) -> tuple[dict[str, Any], ...]:
        value = source.get(key)
        if not isinstance(value, list):
            raise ProductEvaluationError(f"{key} must be an array")
        results: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or set(item) != keys:
                raise ProductEvaluationError(f"{key} item has an unknown or missing field")
            results.append(item)
        return tuple(results)

    @staticmethod
    def _string(source: dict[str, Any], key: str) -> str:
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProductEvaluationError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _string_list(
        source: dict[str, Any], key: str, *, allow_empty: bool
    ) -> tuple[str, ...]:
        value = source.get(key)
        if (
            not isinstance(value, list)
            or (not allow_empty and not value)
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(value) != len(set(value))
        ):
            raise ProductEvaluationError(f"{key} must contain unique non-empty strings")
        return tuple(value)

    @staticmethod
    def _integer(source: dict[str, Any], key: str, *, minimum: int) -> int:
        value = source.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ProductEvaluationError(f"{key} must be an integer >= {minimum}")
        return value

    @staticmethod
    def _number(
        source: dict[str, Any], key: str, *, minimum_exclusive: float
    ) -> float:
        value = source.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value <= minimum_exclusive
        ):
            raise ProductEvaluationError(f"{key} must be greater than {minimum_exclusive}")
        return float(value)

    @staticmethod
    def _boolean(source: dict[str, Any], key: str) -> bool:
        value = source.get(key)
        if not isinstance(value, bool):
            raise ProductEvaluationError(f"{key} must be a boolean")
        return value

    @staticmethod
    def _enum(enum_type: type[_ENUM], value: str, field_name: str) -> _ENUM:
        try:
            return enum_type(value)
        except ValueError as error:
            raise ProductEvaluationError(f"invalid {field_name}: {value}") from error
