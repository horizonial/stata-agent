"""Ports for managed bytes and authoritative Artifact/Data facts."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from stata_research_agent.application.artifact_data import (
    AdoptPathDataCommand,
    CaptureDataVersionCommand,
    PathDataAdoptionResult,
    VerifyArtifactCommand,
)
from stata_research_agent.domain.artifact_data import ArtifactVerification, CapturedDataVersion
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    ArtifactLocationId,
    ArtifactStateObservationId,
    ArtifactVerificationReceiptId,
    DataVersionId,
    FileObservationId,
    OperationAttemptId,
    OperationId,
    PathDataSlotId,
)


class ManagedPayload(Protocol):
    @property
    def managed_handle(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    @property
    def source_locator(self) -> str: ...

    @property
    def source_size_bytes(self) -> int: ...

    @property
    def source_modified_ns(self) -> int: ...

    @property
    def source_file_identity(self) -> str: ...


class ManagedArtifactStore(Protocol):
    def prepare_attempt_staging(self, attempt_id: OperationAttemptId) -> Path: ...

    def write_staging_bytes(
        self, attempt_id: OperationAttemptId, relative_path: str, payload: bytes
    ) -> Path: ...

    def read_small_payload(self, managed_handle: str, *, max_bytes: int) -> bytes: ...

    def source_locator(self, source_path: Path) -> str: ...

    def capture(
        self,
        source_path: Path,
        *,
        artifact_id: ArtifactId,
        attempt_id: OperationAttemptId,
    ) -> ManagedPayload: ...

    def verify(self, managed_handle: str) -> tuple[int | None, str | None]: ...

    def publish_candidate(
        self,
        source_path: Path,
        *,
        artifact_id: ArtifactId,
        attempt_id: OperationAttemptId,
    ) -> ManagedPayload: ...


class ArtifactDataRepository(Protocol):
    def replay_capture(
        self, command: CaptureDataVersionCommand, *, source_locator: str
    ) -> CapturedDataVersion | None: ...

    def record_capture(
        self,
        command: CaptureDataVersionCommand,
        *,
        payload: ManagedPayload,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
        file_observation_id: FileObservationId,
        artifact_id: ArtifactId,
        state_observation_id: ArtifactStateObservationId,
        location_id: ArtifactLocationId,
        verification_receipt_id: ArtifactVerificationReceiptId,
        data_version_id: DataVersionId,
    ) -> CapturedDataVersion: ...

    def artifact_managed_handle(self, artifact_id: ArtifactId) -> str: ...

    def record_verification(
        self,
        command: VerifyArtifactCommand,
        *,
        observed_size: int | None,
        observed_sha256: str | None,
        state_observation_id: ArtifactStateObservationId,
        verification_receipt_id: ArtifactVerificationReceiptId,
    ) -> ArtifactVerification: ...

    def adopt_path_data(
        self,
        command: AdoptPathDataCommand,
        *,
        path_data_slot_id: PathDataSlotId,
    ) -> PathDataAdoptionResult: ...
