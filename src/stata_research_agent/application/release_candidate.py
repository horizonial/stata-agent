"""Immutable Release Candidate, test evidence, and G0-G7 gate contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReleaseGate(StrEnum):
    G0 = "G0"
    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    G4 = "G4"
    G5 = "G5"
    G6 = "G6"
    G7 = "G7"


class ReleaseCandidateState(StrEnum):
    CREATED = "CREATED"
    TESTING = "TESTING"
    READY_FOR_FOUNDER = "READY_FOR_FOUNDER"
    APPROVED_FOR_SIGNING = "APPROVED_FOR_SIGNING"
    REJECTED = "REJECTED"


class ReleaseTestOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    OPEN_NOT_RUN = "OPEN_NOT_RUN"


class FounderDecision(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class ApplicableReleaseTest:
    test_case_id: str
    gate: ReleaseGate
    applicability_fingerprint: str

    def __post_init__(self) -> None:
        if not self.test_case_id or len(self.applicability_fingerprint) != 64:
            raise ValueError("release test requires identity and 64-char fingerprint")


@dataclass(frozen=True, slots=True)
class ReleaseCandidateDefinition:
    release_id: str
    unsigned_payload_sha256: str
    release_manifest_sha256: str
    runtime_sbom_sha256: str
    build_sbom_sha256: str
    verification_manifest_sha256: str
    source_revision: str
    applicable_tests: tuple[ApplicableReleaseTest, ...]

    def __post_init__(self) -> None:
        hashes = (
            self.unsigned_payload_sha256,
            self.release_manifest_sha256,
            self.runtime_sbom_sha256,
            self.build_sbom_sha256,
            self.verification_manifest_sha256,
        )
        if (
            not self.release_id
            or not self.source_revision
            or any(len(item) != 64 for item in hashes)
        ):
            raise ValueError("Release Candidate requires exact identities")
        ids = [item.test_case_id for item in self.applicable_tests]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Applicable Test Set must be non-empty and unique")


@dataclass(frozen=True, slots=True)
class ReleaseTestResultInput:
    test_result_id: str
    release_id: str
    test_case_id: str
    outcome: ReleaseTestOutcome
    applicability_fingerprint: str
    evidence_sha256: str
    environment_sha256: str

    def __post_init__(self) -> None:
        if not self.test_result_id:
            raise ValueError("test result identity is required")
        for value in (
            self.applicability_fingerprint,
            self.evidence_sha256,
            self.environment_sha256,
        ):
            if len(value) != 64:
                raise ValueError("test result hashes must be exact SHA-256 values")


@dataclass(frozen=True, slots=True)
class GateDecision:
    release_id: str
    gate: ReleaseGate
    passed: bool
    reason_codes: tuple[str, ...]
    decision_revision: int


@dataclass(frozen=True, slots=True)
class FounderAcceptanceInput:
    founder_acceptance_id: str
    release_id: str
    test_case_id: str
    scenario_id: str
    scenario_version: int
    scenario_sha256: str
    journey_audit_sha256: str
    decision: FounderDecision
    notes: str
    issue_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.founder_acceptance_id or self.scenario_version < 1:
            raise ValueError("Founder Acceptance identity/version is invalid")
        if len(self.scenario_sha256) != 64 or len(self.journey_audit_sha256) != 64:
            raise ValueError("Founder Acceptance must bind exact scenario/audit hashes")


class ReleaseCandidateError(RuntimeError):
    pass
