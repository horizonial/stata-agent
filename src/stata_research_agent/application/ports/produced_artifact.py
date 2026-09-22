"""Ports for publishing and recording artifacts produced by a Tool Attempt."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.ports.artifact_data import ManagedPayload
from stata_research_agent.application.produced_artifact import (
    CapturedProducedArtifacts,
    CaptureProducedArtifactsCommand,
    ProducedArtifactIdentity,
)


class ProducedArtifactRepository(Protocol):
    def record(
        self,
        command: CaptureProducedArtifactsCommand,
        *,
        payloads: tuple[ManagedPayload, ...],
        identities: tuple[ProducedArtifactIdentity, ...],
    ) -> CapturedProducedArtifacts: ...
