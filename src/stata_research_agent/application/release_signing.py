"""Exact candidate approval and non-blind signing-attempt contracts."""

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class ApprovedReleaseCandidate:
    release_id: str
    unsigned_payload_sha256: str
    runtime_sbom_sha256: str
    build_sbom_sha256: str
    gate_evidence_sha256: str

    def __post_init__(self) -> None:
        if not self.release_id:
            raise ValueError("release candidate identity is required")
        hashes = (
            self.unsigned_payload_sha256,
            self.runtime_sbom_sha256,
            self.build_sbom_sha256,
            self.gate_evidence_sha256,
        )
        if any(len(value) != 64 for value in hashes):
            raise ValueError("release candidate evidence hash is invalid")


class SigningAttemptState(StrEnum):
    REQUESTED = "REQUESTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True, slots=True)
class SigningAttempt:
    signing_attempt_id: str
    request_id: str
    release_id: str
    unsigned_payload_sha256: str
    state: SigningAttemptState
    signed_payload_sha256: str | None
    failure_code: str | None


class ReleaseSigningError(RuntimeError):
    pass
