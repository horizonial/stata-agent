"""Application orchestration for capture, verification, and explicit data adoption."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

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

from .artifact_data import (
    AdoptPathDataCommand,
    CaptureDataVersionCommand,
    CaptureDataVersionResult,
    PathDataAdoptionResult,
    VerifyArtifactCommand,
    VerifyArtifactResult,
)
from .ports.artifact_data import ArtifactDataRepository, ManagedArtifactStore
from .ports.identity import IdentityGenerator


class CaptureCrashPoint(StrEnum):
    AFTER_MANAGED_PUBLISH_BEFORE_FINALIZATION = "after_managed_publish_before_finalization"


CaptureCrashInjector = Callable[[CaptureCrashPoint], None]


class ArtifactDataService:
    def __init__(
        self,
        repository: ArtifactDataRepository,
        managed_store: ManagedArtifactStore,
        identities: IdentityGenerator,
    ) -> None:
        self._repository = repository
        self._managed_store = managed_store
        self._identities = identities

    def capture_data_version(
        self,
        command: CaptureDataVersionCommand,
        *,
        crash_injector: CaptureCrashInjector | None = None,
    ) -> CaptureDataVersionResult:
        source_locator = self._managed_store.source_locator(command.source_path)
        replay = self._repository.replay_capture(command, source_locator=source_locator)
        if replay is not None:
            return replay
        operation_id = self._identities.new(OperationId)
        attempt_id = self._identities.new(OperationAttemptId)
        artifact_id = self._identities.new(ArtifactId)
        payload = self._managed_store.capture(
            command.source_path,
            artifact_id=artifact_id,
            attempt_id=attempt_id,
        )
        if crash_injector is not None:
            crash_injector(CaptureCrashPoint.AFTER_MANAGED_PUBLISH_BEFORE_FINALIZATION)
        return self._repository.record_capture(
            command,
            payload=payload,
            operation_id=operation_id,
            attempt_id=attempt_id,
            file_observation_id=self._identities.new(FileObservationId),
            artifact_id=artifact_id,
            state_observation_id=self._identities.new(ArtifactStateObservationId),
            location_id=self._identities.new(ArtifactLocationId),
            verification_receipt_id=self._identities.new(ArtifactVerificationReceiptId),
            data_version_id=self._identities.new(DataVersionId),
        )

    def verify_artifact(self, command: VerifyArtifactCommand) -> VerifyArtifactResult:
        managed_handle = self._repository.artifact_managed_handle(command.artifact_id)
        observed_size, observed_sha256 = self._managed_store.verify(managed_handle)
        return self._repository.record_verification(
            command,
            observed_size=observed_size,
            observed_sha256=observed_sha256,
            state_observation_id=self._identities.new(ArtifactStateObservationId),
            verification_receipt_id=self._identities.new(ArtifactVerificationReceiptId),
        )

    def adopt_path_data(self, command: AdoptPathDataCommand) -> PathDataAdoptionResult:
        return self._repository.adopt_path_data(
            command,
            path_data_slot_id=self._identities.new(PathDataSlotId),
        )
