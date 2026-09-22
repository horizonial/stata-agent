"""Page-independent authoritative query DTOs for the M0 control vertical."""

from dataclasses import dataclass

from stata_research_agent.domain.identifiers import (
    ConversationId,
    MessageId,
    ResearchPathId,
    TurnId,
)
from stata_research_agent.domain.revisions import (
    ControlRevision,
    EntityRevision,
    Ordinal,
    WorkspaceRevision,
)
from stata_research_agent.domain.status import ExecutionMode, TurnStatus


@dataclass(frozen=True, slots=True)
class TurnSummary:
    turn_id: TurnId
    conversation_id: ConversationId
    execution_mode: ExecutionMode
    status: TurnStatus
    turn_revision: EntityRevision
    enqueue_ordinal: Ordinal


@dataclass(frozen=True, slots=True)
class WorkspaceExecutionSnapshot:
    authoritative_revision: WorkspaceRevision
    lane_revision: ControlRevision
    active_write_turn_id: TurnId | None
    turns: tuple[TurnSummary, ...]


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    conversation_id: ConversationId
    created_revision: WorkspaceRevision
    latest_message_id: MessageId | None
    latest_message_preview: str | None
    latest_message_ordinal: Ordinal | None


@dataclass(frozen=True, slots=True)
class MessageSummary:
    message_id: MessageId
    conversation_id: ConversationId
    role: str
    content: str
    ordinal: Ordinal
    created_revision: WorkspaceRevision


@dataclass(frozen=True, slots=True)
class WaitingRequestSummary:
    waiting_request_id: str
    turn_id: TurnId
    wait_reason: str
    prompt: str
    status: str
    created_turn_revision: EntityRevision


@dataclass(frozen=True, slots=True)
class PauseIntentSummary:
    pause_intent_id: str
    turn_id: TurnId
    requested_turn_revision: EntityRevision
    reason: str
    status: str


@dataclass(frozen=True, slots=True)
class ResearchPathSummary:
    research_path_id: ResearchPathId
    canonical_key: str
    created_revision: WorkspaceRevision


@dataclass(frozen=True, slots=True)
class ProjectionWatermark:
    projection_name: str
    projection_revision: WorkspaceRevision


@dataclass(frozen=True, slots=True)
class WorkspaceBootstrapSnapshot:
    workspace_id: str
    authoritative_revision: WorkspaceRevision
    durable_stream_cursor: str
    lane_revision: ControlRevision
    active_write_turn_id: TurnId | None
    conversations: tuple[ConversationSummary, ...]
    active_conversation_id: ConversationId | None
    recent_messages: tuple[MessageSummary, ...]
    turns: tuple[TurnSummary, ...]
    open_waiting_request: WaitingRequestSummary | None
    active_pause_intent: PauseIntentSummary | None
    research_paths: tuple[ResearchPathSummary, ...]
    projection_watermarks: tuple[ProjectionWatermark, ...]


@dataclass(frozen=True, slots=True)
class ConversationTimelineItem:
    item_kind: str
    item_id: str
    turn_id: TurnId | None
    role: str
    content: str
    ordinal: int
    created_revision: WorkspaceRevision


@dataclass(frozen=True, slots=True)
class ConversationDetailSnapshot:
    authoritative_revision: WorkspaceRevision
    conversation_id: ConversationId
    timeline: tuple[ConversationTimelineItem, ...]
    turns: tuple[TurnSummary, ...]


@dataclass(frozen=True, slots=True)
class ResultIndexItem:
    result_slot_id: str
    slot_key: str
    slot_display_name: str
    result_id: str
    result_kind: str
    producing_stata_run_id: str
    created_by_turn_id: TurnId
    created_revision: WorkspaceRevision
    pointer_revision: int
    adopted_revision: WorkspaceRevision
    element_count: int
    evidence_count: int
    result_element_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResultIndexSnapshot:
    authoritative_revision: WorkspaceRevision
    research_path_id: ResearchPathId
    items: tuple[ResultIndexItem, ...]


@dataclass(frozen=True, slots=True)
class AnalysisOutputArtifactItem:
    artifact_id: str
    role: str
    artifact_kind: str
    media_type: str
    size_bytes: int
    content_sha256: str
    verification_receipt_id: str | None


@dataclass(frozen=True, slots=True)
class AnalysisOutputIndexItem:
    analysis_output_id: str
    output_kind: str
    output_fingerprint: str
    runtime_kind: str
    method_summary: str
    created_by_turn_id: TurnId
    created_revision: WorkspaceRevision
    document_eligible: bool
    adoption_id: str | None
    evidence_record_id: str | None
    artifacts: tuple[AnalysisOutputArtifactItem, ...]


@dataclass(frozen=True, slots=True)
class AnalysisOutputIndexSnapshot:
    authoritative_revision: WorkspaceRevision
    items: tuple[AnalysisOutputIndexItem, ...]


@dataclass(frozen=True, slots=True)
class DocumentSlotItem:
    document_slot_id: str
    slot_key: str
    pointer_revision: int | None
    document_id: str | None
    document_revision_id: str | None
    origin_kind: str | None
    docx_artifact_id: str | None
    delivery_gate_verdict: str | None
    adopted_revision: WorkspaceRevision | None


@dataclass(frozen=True, slots=True)
class DocumentIndexSnapshot:
    authoritative_revision: WorkspaceRevision
    research_path_id: ResearchPathId
    items: tuple[DocumentSlotItem, ...]


@dataclass(frozen=True, slots=True)
class JournalEntryItem:
    journal_entry_id: str
    workspace_revision: WorkspaceRevision
    ordinal: int
    event_type: str
    object_type: str
    object_id: str
    summary: str
    payload_json: str


@dataclass(frozen=True, slots=True)
class JournalEntryPage:
    authoritative_revision: WorkspaceRevision
    as_of_workspace_revision: WorkspaceRevision
    items: tuple[JournalEntryItem, ...]
    next_cursor: str | None
    page_size: int
    sort_direction: str
    workspace_advanced: bool
    newer_matching_entries_available: bool


@dataclass(frozen=True, slots=True)
class AttentionRef:
    attention_kind: str
    severity: str
    resource_type: str
    resource_id: str


@dataclass(frozen=True, slots=True)
class WorkspaceAttentionSnapshot:
    workspace_id: str
    authoritative_revision: WorkspaceRevision
    active_write_turn_id: TurnId | None
    active_write_turn_status: str | None
    queued_write_count: int
    active_read_count: int
    requires_action: bool
    highest_severity: str
    attention_refs: tuple[AttentionRef, ...]
