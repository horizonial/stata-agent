"""Framework-free execution facts returned by the trusted Stata runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class StataExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    COMMAND_FAILED = "command_failed"
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"
    START_FAILED = "start_failed"

    @property
    def is_uncertain(self) -> bool:
        return self in {
            StataExecutionStatus.TIMED_OUT,
            StataExecutionStatus.CRASHED,
            StataExecutionStatus.START_FAILED,
        }


@dataclass(frozen=True, slots=True)
class StataExecutionReceipt:
    schema_version: str
    executor_instance_id: str
    session_id: str
    session_generation: int
    exec_seq: int | None
    execution_status: StataExecutionStatus
    rc: int
    raw_output_status: str
    structured_result_status: str
    command_hash: str | None
    data_signature: str | None
    session_reset: bool
    runtime_environment: Mapping[str, Any]
    supervision_proof: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StataArtifactOutput:
    """One closed output file reported by the trusted runtime."""

    output_slot: str
    source_path: str
    relative_staging_path: str
    artifact_kind: str
    media_type: str
    producer_locator: str
    expected: bool = True


@dataclass(frozen=True, slots=True)
class StataArtifactOutputRequest:
    """One closed output path authorized for the current Stata call."""

    output_slot: str
    relative_staging_path: str
    artifact_kind: str
    media_type: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class StataRuntimeResult:
    envelope_schema_version: str
    text: str
    structured: Mapping[str, Any] | None
    receipt: StataExecutionReceipt
    is_error: bool
    artifacts: tuple[StataArtifactOutput, ...] = ()


@dataclass(frozen=True, slots=True)
class StataSessionCloseResult:
    schema_version: str
    executor_instance_id: str
    session_id: str
    closed: bool
    detail: Mapping[str, Any]
