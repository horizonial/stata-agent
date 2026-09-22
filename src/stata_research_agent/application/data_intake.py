"""Typed contracts for discovering and freezing Workspace Stata datasets."""

from __future__ import annotations

from dataclasses import dataclass

from stata_research_agent.domain.artifact_data import (
    ArtifactVerification,
    CapturedDataVersion,
)
from stata_research_agent.domain.identifiers import CommandId, TurnId


@dataclass(frozen=True, slots=True)
class WorkspaceDataCandidate:
    relative_path: str
    display_name: str
    size_bytes: int
    modified_ns: int

    def __post_init__(self) -> None:
        if not self.relative_path or not self.display_name:
            raise ValueError("Workspace Data candidate path and name are required")
        if self.size_bytes < 0 or self.modified_ns < 0:
            raise ValueError("Workspace Data candidate metadata cannot be negative")


@dataclass(frozen=True, slots=True)
class PrepareWorkspaceDataCommand:
    command_id: CommandId
    created_by_turn_id: TurnId
    relative_path: str

    def __post_init__(self) -> None:
        if not self.relative_path.strip():
            raise ValueError("Workspace Data relative path is required")


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceData:
    candidate: WorkspaceDataCandidate
    captured: CapturedDataVersion
    formal_input_verification: ArtifactVerification
