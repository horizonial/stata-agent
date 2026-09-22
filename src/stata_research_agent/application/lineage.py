"""Typed selectors and response for the path-neutral Evidence lineage query."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from stata_research_agent.domain.revisions import WorkspaceRevision


class LineageEntryKind(StrEnum):
    MESSAGE_OCCURRENCE = "message_occurrence"
    RESULT_ELEMENT = "result_element"
    DOCUMENT_CELL = "document_cell"
    EVIDENCE_RECORD = "evidence_record"


@dataclass(frozen=True, slots=True)
class LineageSelector:
    entry_kind: LineageEntryKind
    entry_id: str
    entry_subkey: str | None = None

    def __post_init__(self) -> None:
        if not self.entry_id:
            raise ValueError("lineage entry_id is required")
        if (
            self.entry_kind
            in {
                LineageEntryKind.MESSAGE_OCCURRENCE,
                LineageEntryKind.DOCUMENT_CELL,
            }
            and not self.entry_subkey
        ):
            raise ValueError(f"{self.entry_kind.value} requires entry_subkey")


@dataclass(frozen=True, slots=True)
class TypedJournalLocator:
    journal_entry_id: str
    workspace_revision: WorkspaceRevision
    ordinal: int
    event_type: str
    object_type: str
    object_id: str


@dataclass(frozen=True, slots=True)
class DataStateStep:
    operation_id: str
    execution_purpose: str
    command_text: str
    command_sha256: str
    completion_manifest_id: str
    data_state_token: str
    session_generation: int


@dataclass(frozen=True, slots=True)
class UnifiedEvidenceLineage:
    authoritative_revision: WorkspaceRevision
    selector: LineageSelector
    source_chain_sha256: str
    evidence_record_id: str
    result_element_id: str
    semantic_key: str
    canonical_binary64_bits: str
    canonical_decimal_text: str
    result_source_locator_id: str
    locator_type: str
    locator_json: str
    result_id: str
    result_candidate_id: str
    result_capture_snapshot_id: str
    stata_run_id: str
    research_command_instance_id: str
    command_text: str
    command_sha256: str
    executable_source_id: str
    operation_id: str
    operation_attempt_id: str
    completion_manifest_id: str
    data_version_id: str
    data_artifact_id: str
    input_data_slot_key: str
    environment_snapshot_id: str
    data_state_steps: tuple[DataStateStep, ...]
    source_journal: TypedJournalLocator
    formal_result_block_id: str | None = None
    presentation_use_id: str | None = None
    rendered_text: str | None = None
    document_revision_id: str | None = None
    table_cell_evidence_use_id: str | None = None
    semantic_cell_slot: str | None = None
