"""Publish exact Tool outputs before recording their authoritative Artifact facts."""

from __future__ import annotations

from stata_research_agent.domain.identifiers import (
    ArtifactId,
    ArtifactLocationId,
    ArtifactStateObservationId,
    ArtifactVerificationReceiptId,
)

from .ports.artifact_data import ManagedArtifactStore
from .ports.identity import IdentityGenerator
from .ports.produced_artifact import ProducedArtifactRepository
from .produced_artifact import (
    CapturedProducedArtifacts,
    CaptureProducedArtifactsCommand,
    ProducedArtifactIdentity,
)


class ProducedArtifactService:
    def __init__(
        self,
        repository: ProducedArtifactRepository,
        managed_store: ManagedArtifactStore,
        identities: IdentityGenerator,
    ) -> None:
        self._repository = repository
        self._managed_store = managed_store
        self._identities = identities

    def capture(self, command: CaptureProducedArtifactsCommand) -> CapturedProducedArtifacts:
        identities = tuple(
            ProducedArtifactIdentity(
                self._identities.new(ArtifactId),
                self._identities.new(ArtifactStateObservationId),
                self._identities.new(ArtifactLocationId),
                self._identities.new(ArtifactVerificationReceiptId),
            )
            for _candidate in command.candidates
        )
        payloads = tuple(
            self._managed_store.publish_candidate(
                candidate.source_path,
                artifact_id=identity.artifact_id,
                attempt_id=command.attempt_id,
            )
            for candidate, identity in zip(command.candidates, identities, strict=True)
        )
        return self._repository.record(command, payloads=payloads, identities=identities)
