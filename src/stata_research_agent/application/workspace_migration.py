"""Typed contracts for staged, identity-preserving Workspace migration."""

from dataclasses import dataclass
from enum import StrEnum


class WorkspaceMigrationState(StrEnum):
    PREPARING = "PREPARING"
    PREPARED = "PREPARED"
    ADOPTING = "ADOPTING"
    ADOPTED = "ADOPTED"
    CLEANUP_COMPLETE = "CLEANUP_COMPLETE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass(frozen=True, slots=True)
class WorkspaceMigrationAttempt:
    migration_attempt_id: str
    source_schema_version: int
    target_schema_version: int
    target_release_id: str
    state: WorkspaceMigrationState
    backup_manifest_json: str | None
    candidate_manifest_json: str | None
    failure_code: str | None
    attempt_revision: int


@dataclass(frozen=True, slots=True)
class WorkspaceMigrationOutcome:
    attempt: WorkspaceMigrationAttempt
    migration_receipt_id: str | None
    backup_retained: bool


class WorkspaceMigrationError(RuntimeError):
    pass
