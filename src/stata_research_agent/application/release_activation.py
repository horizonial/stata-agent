"""Application contracts for side-by-side release activation and rollback."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReleaseState(StrEnum):
    PENDING_ACTIVATION = "PENDING_ACTIVATION"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    INVALID = "INVALID"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"


class ActivationState(StrEnum):
    PROBING = "PROBING"
    STARTING_SAFE = "STARTING_SAFE"
    ACTIVE_REVERSIBLE = "ACTIVE_REVERSIBLE"
    IRREVERSIBLE = "IRREVERSIBLE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RollbackEligibility(StrEnum):
    VERIFIED_REVERSIBLE = "VERIFIED_REVERSIBLE"
    FORBIDDEN_IRREVERSIBLE = "FORBIDDEN_IRREVERSIBLE"
    UNKNOWN = "UNKNOWN"


class IrreversibleCapability(StrEnum):
    AUTHORITATIVE_MUTATION = "authoritative_mutation"
    MIGRATION_ADOPTION = "migration_adoption"
    TOOL_HANDOFF = "tool_handoff"
    PROVIDER_DISPATCH = "provider_dispatch"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    release_id: str
    semantic_version: str
    build_id: str
    publisher_key_id: str
    manifest_sha256: str
    version_directory: str
    entry_point: str
    global_control_schema_min: int
    global_control_schema_max: int
    workspace_schema_read_min: int
    workspace_schema_read_max: int
    workspace_schema_write_min: int
    workspace_schema_write_max: int
    minimum_launcher_version: str


@dataclass(frozen=True, slots=True)
class ActivationAttempt:
    activation_attempt_id: str
    candidate_release_id: str
    previous_release_id: str | None
    state: ActivationState
    rollback_eligibility: RollbackEligibility
    irreversible_reasons: tuple[str, ...]
    failure_reason: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class ActivationReport:
    activation_attempt_id: str
    outcome: str
    active_release_id: str | None
    candidate_release_id: str
    previous_release_id: str | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class ActivationStartOutcome:
    attempt: ActivationAttempt
    probe_codes: tuple[str, ...]


class ReleaseActivationError(RuntimeError):
    pass
