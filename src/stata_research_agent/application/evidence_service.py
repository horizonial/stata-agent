"""Application orchestration for Evidence issuance, rendering, and coverage."""

from __future__ import annotations

from stata_research_agent.domain.evidence import render_covered_formal_text
from stata_research_agent.domain.identifiers import (
    EvidenceIssuanceReceiptId,
    EvidencePresentationUseId,
    EvidenceRecordId,
    EvidenceRenderBindingId,
    EvidenceRenderReceiptId,
    FormalResultBlockId,
    FormatRuleSnapshotId,
    NumericCoverageManifestId,
    NumericOccurrenceId,
    ResultSlotId,
)

from .evidence import (
    AdoptPathResultCommand,
    EvidenceLineage,
    EvidenceOccurrenceIdentity,
    FormalBlockIdentity,
    FormalBlockOutcome,
    PathResultAdoptionOutcome,
    PreparedFormalBlock,
    RenderFormalResultBlockCommand,
)
from .ports.evidence import EvidenceRepository
from .ports.identity import IdentityGenerator


class EvidenceService:
    def __init__(self, repository: EvidenceRepository, identities: IdentityGenerator) -> None:
        self._repository = repository
        self._identities = identities

    def adopt_path_result(self, command: AdoptPathResultCommand) -> PathResultAdoptionOutcome:
        return self._repository.adopt_path_result(command, self._identities.new(ResultSlotId))

    def render_formal_block(self, command: RenderFormalResultBlockCommand) -> FormalBlockOutcome:
        elements = self._repository.load_elements_for_render(command)
        by_id = {element.result_element_id.value: element for element in elements}
        slot_values: dict[str, tuple[str, str, int]] = {}
        for slot in command.slots:
            try:
                element = by_id[slot.result_element_id.value]
            except KeyError as error:
                raise ValueError(
                    f"Result Element is not eligible for this formal block: "
                    f"{slot.result_element_id.value}"
                ) from error
            slot_values[slot.name] = (
                element.result_element_id.value,
                element.binary64_bits,
                slot.decimal_places,
            )
        rendered = render_covered_formal_text(command.template, slot_values)
        identities = FormalBlockIdentity(
            self._identities.new(ResultSlotId),
            self._identities.new(FormalResultBlockId),
            self._identities.new(NumericCoverageManifestId),
            tuple(
                EvidenceOccurrenceIdentity(
                    self._identities.new(EvidenceIssuanceReceiptId),
                    self._identities.new(EvidenceRecordId),
                    self._identities.new(FormatRuleSnapshotId),
                    self._identities.new(EvidenceRenderReceiptId),
                    self._identities.new(EvidencePresentationUseId),
                    self._identities.new(EvidenceRenderBindingId),
                    self._identities.new(NumericOccurrenceId),
                )
                for _ in rendered.occurrences
            ),
        )
        return self._repository.commit_formal_block(
            command,
            PreparedFormalBlock(rendered, by_id),
            identities,
        )

    def lineage(
        self, formal_result_block_id: FormalResultBlockId, byte_start: int
    ) -> EvidenceLineage:
        if byte_start < 0:
            raise ValueError("byte_start cannot be negative")
        return self._repository.load_lineage(formal_result_block_id.value, byte_start)
