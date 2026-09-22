"""Commands for capturing exact artifacts produced by an admitted Tool Attempt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stata_research_agent.domain.artifact_data import ArtifactKind
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    ArtifactLocationId,
    ArtifactStateObservationId,
    ArtifactVerificationReceiptId,
    CommandId,
    OperationAttemptId,
    OperationId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class ProducedArtifactCandidate:
    source_path: Path
    artifact_kind: ArtifactKind
    media_type: str
    role: str
    relative_name: str

    def __post_init__(self) -> None:
        if not self.media_type.strip() or not self.role.strip():
            raise ValueError("produced Artifact media type and role are required")
        if not self.relative_name.strip():
            raise ValueError("produced Artifact relative name is required")
        raw = Path(self.relative_name)
        if raw.is_absolute() or ".." in raw.parts:
            raise ValueError("produced Artifact relative name is unsafe")


@dataclass(frozen=True, slots=True)
class CaptureProducedArtifactsCommand:
    command_id: CommandId
    operation_id: OperationId
    attempt_id: OperationAttemptId
    candidates: tuple[ProducedArtifactCandidate, ...]

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("at least one produced Artifact is required")
        names = [item.relative_name for item in self.candidates]
        if len(names) != len(set(names)):
            raise ValueError("produced Artifact relative names must be unique")


@dataclass(frozen=True, slots=True)
class ProducedArtifactIdentity:
    artifact_id: ArtifactId
    state_observation_id: ArtifactStateObservationId
    location_id: ArtifactLocationId
    verification_receipt_id: ArtifactVerificationReceiptId


@dataclass(frozen=True, slots=True)
class CapturedProducedArtifact:
    artifact_id: ArtifactId
    artifact_kind: ArtifactKind
    media_type: str
    role: str
    relative_name: str
    content_sha256: str
    size_bytes: int
    managed_handle: str
    verification_receipt_id: ArtifactVerificationReceiptId


@dataclass(frozen=True, slots=True)
class CapturedProducedArtifacts:
    operation_id: OperationId
    attempt_id: OperationAttemptId
    artifacts: tuple[CapturedProducedArtifact, ...]
    commit_revision: WorkspaceRevision
    replayed: bool
