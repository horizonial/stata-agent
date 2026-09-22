"""Versioned Founder journey contracts and deterministic audit outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FounderJourneyKind(StrEnum):
    AUTONOMOUS = "autonomous"
    CONTROLLED = "controlled"


class FounderAuditMode(StrEnum):
    IMPLEMENTATION = "implementation"
    FOUNDER_ACCEPTANCE = "founder_acceptance"


class FounderAuditStatus(StrEnum):
    IMPLEMENTATION_PASSED = "implementation_passed"
    ACCEPTANCE_PASSED = "acceptance_passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FounderFixtureIdentity:
    fixture_id: str
    media_type: str
    sha256: str


@dataclass(frozen=True, slots=True)
class FounderScenarioApplicability:
    decisions: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    support_profile: str
    release_gate: str


@dataclass(frozen=True, slots=True)
class FounderAcceptanceScenario:
    scenario_id: str
    version: int
    journey: FounderJourneyKind
    title: str
    idea: str
    fixture: FounderFixtureIdentity
    result_slot_key: str
    data_slot_key: str
    required_observations: tuple[str, ...]
    forbidden_observations: tuple[str, ...]
    applicability: FounderScenarioApplicability
    exact_candidate_required: bool
    scenario_sha256: str


@dataclass(frozen=True, slots=True)
class FounderObservation:
    code: str
    satisfied: bool
    detail: str


@dataclass(frozen=True, slots=True)
class FounderJourneyAuditRequest:
    scenario: FounderAcceptanceScenario
    mode: FounderAuditMode
    turn_id: str
    research_path_id: str
    observed_fixture_sha256: str
    exact_candidate_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ControlledJourneyAuditRequest:
    scenario: FounderAcceptanceScenario
    mode: FounderAuditMode
    predecessor_turn_id: str
    successor_turn_id: str
    parent_research_path_id: str
    child_research_path_id: str
    evidence_record_id: str
    observed_fixture_sha256: str
    exact_candidate_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class FounderJourneyAuditReport:
    status: FounderAuditStatus
    scenario_id: str
    scenario_version: int
    scenario_sha256: str
    exact_candidate_sha256: str | None
    acceptance_eligible: bool
    observations: tuple[FounderObservation, ...]
    findings: tuple[str, ...]


class FounderAcceptanceError(ValueError):
    """Raised when a scenario or its authoritative audit is invalid."""
