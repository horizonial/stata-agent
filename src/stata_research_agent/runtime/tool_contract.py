"""Versioned Tool/Operation/Attempt/Manifest/Result boundary schemas."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def canonical_arguments_hash(arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reject_absolute_paths(value: Any) -> Any:
    if isinstance(value, str):
        if PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute():
            raise ValueError("absolute OS paths are forbidden at the Tool boundary")
    elif isinstance(value, dict):
        for nested in value.values():
            _reject_absolute_paths(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_absolute_paths(nested)
    return value


class ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CanonicalToolCallEnvelope(ToolModel):
    schema_version: Literal["1"]
    tool_call_id: str = Field(pattern=r"^toolcall_.+")
    assistant_output_id: str
    call_ordinal: int = Field(ge=1)
    provider_tool_call_id: str | None = None
    requested_tool_name: str = Field(pattern=r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
    resolved_tool_contract_id: str | None = None
    raw_arguments_snapshot_id: str
    canonical_arguments_snapshot_id: str
    canonical_arguments: dict[str, Any]
    arguments_hash: str = Field(min_length=64, max_length=64)
    proposal_status: Literal[
        "proposed",
        "awaiting_confirmation",
        "rejected",
        "scheduled",
        "admitted",
        "resolved",
        "blocked",
    ]
    created_revision: int = Field(ge=1)

    @field_validator("canonical_arguments")
    @classmethod
    def no_absolute_paths(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_absolute_paths(value)
        return value

    @model_validator(mode="after")
    def hash_matches(self) -> CanonicalToolCallEnvelope:
        if canonical_arguments_hash(self.canonical_arguments) != self.arguments_hash:
            raise ValueError("arguments_hash does not match canonical_arguments")
        return self


class OperationEnvelope(ToolModel):
    schema_version: Literal["1"]
    operation_id: str = Field(pattern=r"^op_.+")
    tool_call_id: str = Field(pattern=r"^toolcall_.+")
    tool_contract_id: str
    status: Literal[
        "proposed",
        "authorized",
        "admitted",
        "handoff_committed",
        "completed",
        "failed",
        "interrupted",
    ]
    idempotency_key: str | None = None


class OperationAttemptEnvelope(ToolModel):
    schema_version: Literal["1"]
    attempt_id: str = Field(pattern=r"^attempt_.+")
    operation_id: str = Field(pattern=r"^op_.+")
    attempt_ordinal: int = Field(ge=1)
    session_generation: int = Field(ge=1)
    status: Literal["created", "handoff_committed", "completed", "failed", "interrupted"]


class ManifestArtifact(ToolModel):
    candidate_id: str
    managed_locator: str
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("managed_locator")
    @classmethod
    def logical_locator_only(cls, value: str) -> str:
        _reject_absolute_paths(value)
        if ".." in PurePosixPath(value).parts:
            raise ValueError("managed locator cannot traverse parents")
        return value


class CompletionManifestEnvelope(ToolModel):
    schema_version: Literal["1"]
    manifest_id: str = Field(pattern=r"^manifest_.+")
    operation_id: str = Field(pattern=r"^op_.+")
    attempt_id: str = Field(pattern=r"^attempt_.+")
    session_generation: int = Field(ge=1)
    execution_status: Literal["completed", "failed"]
    artifacts: tuple[ManifestArtifact, ...]


class CanonicalToolResultEnvelope(ToolModel):
    schema_version: Literal["1"]
    tool_result_id: str
    tool_call_id: str = Field(pattern=r"^toolcall_.+")
    result_kind: Literal["success", "error", "rejected", "denied", "cancelled", "blocked"]
    result_schema_version: str
    summary: str
    artifact_references: tuple[str, ...] = ()
    operation_references: tuple[str, ...] = ()
    is_truncated: bool = False
    full_payload_artifact_id: str | None = None
    content_hash: str = Field(min_length=64, max_length=64)
    created_revision: int = Field(ge=1)
