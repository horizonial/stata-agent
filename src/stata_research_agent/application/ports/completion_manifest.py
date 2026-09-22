"""Port for the durable filesystem side of isolated tool completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from stata_research_agent.application.stata_operation import StataOperationHandle
from stata_research_agent.domain.identifiers import CompletionManifestId
from stata_research_agent.domain.stata_execution import StataRuntimeResult


@dataclass(frozen=True, slots=True)
class ObservedCompletionArtifact:
    output_slot: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class IsolatedCompletion:
    manifest_id: CompletionManifestId
    manifest_locator: str
    artifacts: tuple[ObservedCompletionArtifact, ...]


@dataclass(frozen=True, slots=True)
class RecoveredIsolatedCompletion:
    manifest_id: CompletionManifestId
    operation_id: str
    attempt_id: str
    result: StataRuntimeResult


class CompletionManifestStore(Protocol):
    def publish(
        self,
        *,
        manifest_id: CompletionManifestId,
        handle: StataOperationHandle,
        result: StataRuntimeResult,
    ) -> IsolatedCompletion: ...

    def verify_attempt(self, attempt_id: str) -> dict[str, object] | None: ...

    def load_attempt(self, attempt_id: str) -> RecoveredIsolatedCompletion: ...
