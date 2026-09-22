"""Typed contracts for arbitrary staged Python/Shell candidate execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SandboxExecutionError(RuntimeError):
    pass


class SandboxOutcomeUnknownError(SandboxExecutionError):
    """The isolated process crossed Handoff but did not return a reliable completion."""


class SandboxIntegrityViolationError(SandboxExecutionError):
    """The process completed but the staged tree violated the alias contract."""


@dataclass(frozen=True, slots=True)
class SandboxInput:
    source_path: Path
    relative_target: str


@dataclass(frozen=True, slots=True)
class SandboxOutputCandidate:
    relative_path: str
    source_path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SandboxExecutionRequest:
    attempt_id: str
    language: str
    code: str
    inputs: tuple[SandboxInput, ...] = ()
    network_mode: str = "block"
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if self.language not in {"python", "powershell"}:
            raise ValueError("unsupported sandbox language")
        if self.network_mode not in {"block", "allow"}:
            raise ValueError("invalid sandbox network mode")
        if not self.attempt_id or not self.code or self.timeout_seconds < 1:
            raise ValueError("invalid sandbox execution request")


@dataclass(frozen=True, slots=True)
class SandboxExecutionReceipt:
    schema_version: str
    attempt_id: str
    isolation_tier: str
    policy_sha256: str
    network_mode: str
    readonly_grants: tuple[str, ...]
    readwrite_grants: tuple[str, ...]
    dacl_fallback_allowed: bool
    staging_preflight: str
    staging_postflight: str
    exit_code: int
    safe_stdout: str
    safe_stderr: str
    executable_source_path: Path
    output_candidates: tuple[SandboxOutputCandidate, ...]

    def authoritative_payload(self) -> dict[str, object]:
        """Return the path-free receipt persisted at the Operation boundary."""

        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "isolation_tier": self.isolation_tier,
            "policy_sha256": self.policy_sha256,
            "network_mode": self.network_mode,
            "readonly_grants": list(self.readonly_grants),
            "readwrite_grants": list(self.readwrite_grants),
            "dacl_fallback_allowed": self.dacl_fallback_allowed,
            "staging_preflight": self.staging_preflight,
            "staging_postflight": self.staging_postflight,
            "exit_code": self.exit_code,
            "output_candidates": [
                {
                    "relative_path": item.relative_path,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in self.output_candidates
            ],
        }
