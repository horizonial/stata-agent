"""Frozen request and result contracts for local Diagnostic Bundles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class DiagnosticBundleMode(StrEnum):
    SYSTEM_ONLY = "system_only"
    SCOPED_WORKSPACE = "scoped_workspace"


@dataclass(frozen=True, slots=True)
class DiagnosticBundleRequest:
    bundle_request_id: str
    mode: DiagnosticBundleMode
    requested_at: str
    diagnostic_snapshot_end: int
    requested_workspace_revision: int | None
    turn_id: str | None
    operation_id: str | None
    redaction_policy_version: str
    health_snapshot_json: str
    crash_capsules_snapshot_json: str
    bundle_salt: bytes
    requesting_user_action: bool


@dataclass(frozen=True, slots=True)
class DiagnosticBundleOutcome:
    bundle_id: str
    output_path: Path
    size_bytes: int
    sha256: str
    member_count: int
    diagnostic_snapshot_end: int
    requested_workspace_revision: int | None
