"""Commands and immutable records for Result adoption and formal Evidence rendering."""

from __future__ import annotations

from dataclasses import dataclass

from stata_research_agent.domain.evidence import CoveredFormalText
from stata_research_agent.domain.identifiers import (
    CommandId,
    EvidenceIssuanceReceiptId,
    EvidencePresentationUseId,
    EvidenceRecordId,
    EvidenceRenderBindingId,
    EvidenceRenderReceiptId,
    FormalResultBlockId,
    FormatRuleSnapshotId,
    NumericCoverageManifestId,
    NumericOccurrenceId,
    ResearchPathId,
    ResultElementId,
    ResultId,
    ResultSlotId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class AdoptPathResultCommand:
    command_id: CommandId
    research_path_id: ResearchPathId
    canonical_slot_key: str
    result_id: ResultId
    updated_by_turn_id: TurnId
    expected_pointer_revision: int
    display_name: str = "Primary result"

    def __post_init__(self) -> None:
        if not self.canonical_slot_key.strip():
            raise ValueError("canonical_slot_key is required")
        if self.expected_pointer_revision < 0:
            raise ValueError("expected_pointer_revision cannot be negative")


@dataclass(frozen=True, slots=True)
class PathResultAdoptionOutcome:
    result_slot_id: ResultSlotId
    result_id: ResultId
    pointer_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class EvidenceSlot:
    name: str
    result_element_id: ResultElementId
    decimal_places: int

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "a").isalnum():
            raise ValueError("Evidence slot name must be alphanumeric/underscore")
        if not 0 <= self.decimal_places <= 12:
            raise ValueError("decimal_places must be between 0 and 12")


@dataclass(frozen=True, slots=True)
class RenderFormalResultBlockCommand:
    command_id: CommandId
    created_by_turn_id: TurnId
    research_path_id: ResearchPathId
    result_slot_key: str
    template: str
    slots: tuple[EvidenceSlot, ...]

    def __post_init__(self) -> None:
        if not self.result_slot_key.strip() or not self.template:
            raise ValueError("result slot key and template are required")
        names = tuple(slot.name for slot in self.slots)
        if not names or len(names) != len(set(names)):
            raise ValueError("Evidence slot names must be non-empty and unique")


@dataclass(frozen=True, slots=True)
class ResultElementForEvidence:
    result_element_id: ResultElementId
    result_id: ResultId
    semantic_key: str
    statistic_kind: str
    binary64_bits: str
    decimal_text: str


@dataclass(frozen=True, slots=True)
class EvidenceOccurrenceIdentity:
    issuance_receipt_id: EvidenceIssuanceReceiptId
    evidence_record_candidate_id: EvidenceRecordId
    format_rule_snapshot_id: FormatRuleSnapshotId
    render_receipt_id: EvidenceRenderReceiptId
    presentation_use_id: EvidencePresentationUseId
    render_binding_id: EvidenceRenderBindingId
    numeric_occurrence_id: NumericOccurrenceId


@dataclass(frozen=True, slots=True)
class FormalBlockIdentity:
    result_slot_candidate_id: ResultSlotId
    formal_result_block_id: FormalResultBlockId
    coverage_manifest_id: NumericCoverageManifestId
    occurrences: tuple[EvidenceOccurrenceIdentity, ...]


@dataclass(frozen=True, slots=True)
class FormalBlockOutcome:
    formal_result_block_id: FormalResultBlockId
    coverage_manifest_id: NumericCoverageManifestId
    content: str
    evidence_record_ids: tuple[EvidenceRecordId, ...]
    presentation_use_ids: tuple[EvidencePresentationUseId, ...]
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class EvidenceLineage:
    formal_result_block_id: FormalResultBlockId
    numeric_occurrence_id: NumericOccurrenceId
    numeric_lexeme: str
    evidence_record_id: EvidenceRecordId
    result_element_id: ResultElementId
    semantic_key: str
    result_id: ResultId
    stata_run_id: str
    research_command_instance_id: str
    command_text: str
    data_version_id: str
    result_source_locator_id: str
    locator_json: str


@dataclass(frozen=True, slots=True)
class PreparedFormalBlock:
    rendered: CoveredFormalText
    elements_by_id: dict[str, ResultElementForEvidence]
