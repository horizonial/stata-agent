"""Strict loader for checked-in Founder Acceptance Scenario manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from stata_research_agent.application.founder_acceptance import (
    FounderAcceptanceError,
    FounderAcceptanceScenario,
    FounderFixtureIdentity,
    FounderJourneyKind,
    FounderScenarioApplicability,
)

_SCHEMA = "stata-research-agent.founder-acceptance/v1"
_TOP_LEVEL_KEYS = {
    "schema_version",
    "scenario_id",
    "version",
    "journey",
    "title",
    "idea",
    "fixture",
    "research_slots",
    "required_observations",
    "forbidden_observations",
    "applicability",
    "exact_candidate_required",
}


class FounderScenarioLoader:
    def load(self, path: Path) -> FounderAcceptanceScenario:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise FounderAcceptanceError("Founder Scenario is not readable JSON") from error
        if not isinstance(raw, dict) or set(raw) != _TOP_LEVEL_KEYS:
            raise FounderAcceptanceError("Founder Scenario has an unknown or missing field")
        if raw["schema_version"] != _SCHEMA:
            raise FounderAcceptanceError("unsupported Founder Scenario schema")
        canonical = json.dumps(
            raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        scenario_sha256 = hashlib.sha256(canonical).hexdigest()

        fixture = self._object(raw, "fixture", {"fixture_id", "media_type", "sha256"})
        slots = self._object(raw, "research_slots", {"data", "result"})
        applicability = self._object(
            raw,
            "applicability",
            {"decisions", "required_capabilities", "support_profile", "release_gate"},
        )
        scenario_id = self._string(raw, "scenario_id")
        version = raw["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise FounderAcceptanceError("Founder Scenario version must be a positive integer")
        fixture_sha = self._string(fixture, "sha256")
        if len(fixture_sha) != 64 or any(char not in "0123456789abcdef" for char in fixture_sha):
            raise FounderAcceptanceError("Founder fixture sha256 must be lowercase hex")
        exact_candidate_required = raw["exact_candidate_required"]
        if exact_candidate_required is not True:
            raise FounderAcceptanceError("Founder Acceptance must bind an exact candidate")

        required = self._string_list(raw, "required_observations")
        forbidden = self._string_list(raw, "forbidden_observations")
        if not required or not forbidden or set(required) & set(forbidden):
            raise FounderAcceptanceError("scenario observations must be non-empty and disjoint")
        decisions = self._string_list(applicability, "decisions")
        capabilities = self._string_list(applicability, "required_capabilities")
        if not decisions or not capabilities:
            raise FounderAcceptanceError("scenario applicability cannot be empty")
        return FounderAcceptanceScenario(
            scenario_id,
            version,
            FounderJourneyKind(self._string(raw, "journey")),
            self._string(raw, "title"),
            self._string(raw, "idea"),
            FounderFixtureIdentity(
                self._string(fixture, "fixture_id"),
                self._string(fixture, "media_type"),
                fixture_sha,
            ),
            self._string(slots, "result"),
            self._string(slots, "data"),
            required,
            forbidden,
            FounderScenarioApplicability(
                decisions,
                capabilities,
                self._string(applicability, "support_profile"),
                self._string(applicability, "release_gate"),
            ),
            exact_candidate_required,
            scenario_sha256,
        )

    @staticmethod
    def _object(source: dict[str, Any], key: str, keys: set[str]) -> dict[str, Any]:
        value = source.get(key)
        if not isinstance(value, dict) or set(value) != keys:
            raise FounderAcceptanceError(f"{key} has an unknown or missing field")
        return value

    @staticmethod
    def _string(source: dict[str, Any], key: str) -> str:
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            raise FounderAcceptanceError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _string_list(source: dict[str, Any], key: str) -> tuple[str, ...]:
        value = source.get(key)
        if (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)
            or len(value) != len(set(value))
        ):
            raise FounderAcceptanceError(f"{key} must contain unique non-empty strings")
        return tuple(value)
