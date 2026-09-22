"""Typed commands for the Artifact/Data capture vertical."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stata_research_agent.domain.artifact_data import (
    ArtifactVerification,
    CapturedDataVersion,
    DataVersionKind,
    VerificationPurpose,
)
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    CommandId,
    DataVersionId,
    PathDataSlotId,
    ResearchPathId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class CaptureDataVersionCommand:
    command_id: CommandId
    source_path: Path
    data_version_kind: DataVersionKind
    created_by_turn_id: TurnId
    media_type: str = "application/x-stata-dta"

    def __post_init__(self) -> None:
        if not self.media_type.strip():
            raise ValueError("media_type is required")


@dataclass(frozen=True, slots=True)
class VerifyArtifactCommand:
    command_id: CommandId
    artifact_id: ArtifactId
    purpose: VerificationPurpose


@dataclass(frozen=True, slots=True)
class AdoptPathDataCommand:
    command_id: CommandId
    research_path_id: ResearchPathId
    canonical_slot_key: str
    data_version_id: DataVersionId
    expected_pointer_revision: int
    display_name: str = "Primary analysis data"

    def __post_init__(self) -> None:
        if not self.canonical_slot_key.strip():
            raise ValueError("canonical_slot_key is required")
        if self.expected_pointer_revision < 0:
            raise ValueError("expected_pointer_revision cannot be negative")


@dataclass(frozen=True, slots=True)
class PathDataAdoptionResult:
    path_data_slot_id: PathDataSlotId
    data_version_id: DataVersionId
    pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool


CaptureDataVersionResult = CapturedDataVersion
VerifyArtifactResult = ArtifactVerification
