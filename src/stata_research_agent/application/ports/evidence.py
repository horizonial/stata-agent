"""Persistence port for formal Evidence issuance and numeric coverage."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.evidence import (
    AdoptPathResultCommand,
    EvidenceLineage,
    FormalBlockIdentity,
    FormalBlockOutcome,
    PathResultAdoptionOutcome,
    PreparedFormalBlock,
    RenderFormalResultBlockCommand,
    ResultElementForEvidence,
)
from stata_research_agent.domain.identifiers import ResultSlotId


class EvidenceRepository(Protocol):
    def adopt_path_result(
        self, command: AdoptPathResultCommand, result_slot_id: ResultSlotId
    ) -> PathResultAdoptionOutcome: ...

    def load_elements_for_render(
        self, command: RenderFormalResultBlockCommand
    ) -> tuple[ResultElementForEvidence, ...]: ...

    def commit_formal_block(
        self,
        command: RenderFormalResultBlockCommand,
        prepared: PreparedFormalBlock,
        identities: FormalBlockIdentity,
    ) -> FormalBlockOutcome: ...

    def load_lineage(self, formal_result_block_id: str, byte_start: int) -> EvidenceLineage: ...
