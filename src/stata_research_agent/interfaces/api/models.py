"""Public API DTOs; deliberately separate from domain objects and SQLite rows."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

WaitingReason = Literal["user_input", "user_confirmation", "external_resolution"]


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmptyPayload(PublicModel):
    pass


class SessionExchangeRequest(PublicModel):
    bootstrap_nonce: str = Field(min_length=32)


class LauncherBootstrapReceiptResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    purpose: Literal["browser_session_exchange"] = "browser_session_exchange"
    instance_id: str
    bootstrap_nonce: str
    expires_at: datetime


class SessionExchangeReceiptResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    instance_id: str
    browser_session_token: str


class ClientContext(PublicModel):
    client_version: str | None = None
    interaction_id: str | None = None


class NoPreconditions(PublicModel):
    pass


class WorkspaceCreateEnvelope(PublicModel):
    schema_version: Literal["1"]
    command_id: str = Field(pattern=r"^cmd_.+")
    workspace_id: str = Field(pattern=r"^ws_.+")
    command_type: Literal["workspace.create"]
    payload: EmptyPayload
    preconditions: NoPreconditions = Field(default_factory=NoPreconditions)
    client_context: ClientContext | None = None


class MessageSubmitPayload(PublicModel):
    content: str = Field(min_length=1)
    conversation_id: str | None = Field(default=None, pattern=r"^conv_.+")
    research_path_id: str | None = Field(default=None, pattern=r"^path_.+")
    execution_mode: Literal["read", "write"] = "write"
    goal_mode: Literal["research_loop", "deliver_word"] = "deliver_word"


class MessageSubmitEnvelope(PublicModel):
    schema_version: Literal["1"]
    command_id: str = Field(pattern=r"^cmd_.+")
    workspace_id: str = Field(pattern=r"^ws_.+")
    command_type: Literal["message.submit"]
    payload: MessageSubmitPayload
    preconditions: NoPreconditions = Field(default_factory=NoPreconditions)
    client_context: ClientContext | None = None


class WaitingAnswerPayload(PublicModel):
    waiting_request_id: str = Field(pattern=r"^waiting_.+")
    turn_id: str = Field(pattern=r"^turn_.+")
    waiting_revision: int = Field(ge=1)
    answer: str = Field(min_length=1)
    tool_decision: Literal["approve", "deny", "cancel"] | None = None


class WaitingAnswerEnvelope(PublicModel):
    schema_version: Literal["1"]
    command_id: str = Field(pattern=r"^cmd_.+")
    workspace_id: str = Field(pattern=r"^ws_.+")
    command_type: Literal["waiting.answer"]
    payload: WaitingAnswerPayload
    preconditions: NoPreconditions = Field(default_factory=NoPreconditions)
    client_context: ClientContext | None = None


class TurnPauseRequestPayload(PublicModel):
    turn_id: str = Field(pattern=r"^turn_.+")
    expected_turn_revision: int = Field(ge=1)
    reason: str = Field(min_length=1)


class TurnPauseRequestEnvelope(PublicModel):
    schema_version: Literal["1"]
    command_id: str = Field(pattern=r"^cmd_.+")
    workspace_id: str = Field(pattern=r"^ws_.+")
    command_type: Literal["turn.pause.request"]
    payload: TurnPauseRequestPayload
    preconditions: NoPreconditions = Field(default_factory=NoPreconditions)
    client_context: ClientContext | None = None


class ResearchPathBranchPayload(PublicModel):
    source_research_path_id: str = Field(pattern=r"^path_.+")
    conversation_id: str | None = Field(default=None, pattern=r"^conv_.+")
    canonical_key: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    branch_reason: str = Field(min_length=1, max_length=2_000)
    expected_workspace_revision: int = Field(ge=1)


class ResearchPathBranchEnvelope(PublicModel):
    schema_version: Literal["1"]
    command_id: str = Field(pattern=r"^cmd_.+")
    workspace_id: str = Field(pattern=r"^ws_.+")
    command_type: Literal["research_path.branch"]
    payload: ResearchPathBranchPayload
    preconditions: NoPreconditions = Field(default_factory=NoPreconditions)
    client_context: ClientContext | None = None


CommandEnvelope = Annotated[
    WorkspaceCreateEnvelope
    | MessageSubmitEnvelope
    | WaitingAnswerEnvelope
    | TurnPauseRequestEnvelope
    | ResearchPathBranchEnvelope,
    Field(discriminator="command_type"),
]


class ResourceRef(PublicModel):
    resource_type: Literal[
        "workspace",
        "conversation",
        "message",
        "turn",
        "waiting_request",
        "research_path",
        "plan_revision",
        "result",
        "analysis_output",
        "stata_run",
        "document_revision",
        "artifact",
        "journal_entry",
        "operation",
        "pause_intent",
    ]
    resource_id: str


class CommandReceiptResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    command_id: str
    command_status: Literal["committed"] = "committed"
    commit_revision: int = Field(ge=1)
    replayed: bool
    created_resource_refs: tuple[ResourceRef, ...]
    turn_id: str | None = None
    turn_revision: int | None = Field(default=None, ge=1)
    effect: str | None = None


class TurnResponse(PublicModel):
    turn_id: str
    conversation_id: str
    execution_mode: Literal["read", "write"]
    status: Literal["queued", "running", "waiting", "succeeded", "partial", "paused", "failed"]
    turn_revision: int = Field(ge=1)
    enqueue_ordinal: int = Field(ge=1)


class WorkspaceExecutionData(PublicModel):
    lane_revision: int = Field(ge=0)
    active_write_turn_id: str | None
    turns: tuple[TurnResponse, ...]


class WorkspaceExecutionResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    data: WorkspaceExecutionData
    resource_refs: tuple[ResourceRef, ...]


class OperationalMetricResponse(PublicModel):
    metric_id: str
    layer: Literal["L1", "L2", "L3"]
    subsystem: str
    status: Literal["pass", "warn", "fail", "observed", "not_applicable", "unknown"]
    value: float | None
    numerator: float | None
    denominator: float | None
    unit: str
    explanation: str
    source_tables: tuple[str, ...]


class OperationalLayerResponse(PublicModel):
    layer: Literal["L1", "L2", "L3"]
    status: Literal["pass", "warn", "fail", "observed", "not_applicable"]
    metrics: tuple[OperationalMetricResponse, ...]
    hard_failure_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)


class OperationalBreakdownItemResponse(PublicModel):
    key: str
    count: int = Field(ge=0)


class OperationalBreakdownResponse(PublicModel):
    breakdown_id: str
    layer: Literal["L1", "L2", "L3"]
    subsystem: str
    items: tuple[OperationalBreakdownItemResponse, ...]
    explanation: str
    source_tables: tuple[str, ...]


class OperationalConfigurationResponse(PublicModel):
    system_prompt_revision: str | None
    main_skill_name: str | None
    main_skill_revision: str | None
    tool_catalog_revision: str | None
    model_policy_revisions: tuple[str, ...]
    provider_kinds: tuple[str, ...]
    model_names: tuple[str, ...]


class TurnOperationalEvaluationData(PublicModel):
    policy_revision: str
    turn_id: str
    turn_status: str
    research_path_id: str
    created_at: datetime
    terminal_at: datetime | None
    configuration: OperationalConfigurationResponse
    layers: tuple[OperationalLayerResponse, ...]
    breakdowns: tuple[OperationalBreakdownResponse, ...]


class TurnOperationalEvaluationResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    data: TurnOperationalEvaluationData
    resource_refs: tuple[ResourceRef, ...]


class WorkspaceOperationalEvaluationData(PublicModel):
    policy_revision: str
    layers: tuple[OperationalLayerResponse, ...]
    breakdowns: tuple[OperationalBreakdownResponse, ...]
    turns: tuple[TurnOperationalEvaluationData, ...]


class WorkspaceOperationalEvaluationResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    data: WorkspaceOperationalEvaluationData
    resource_refs: tuple[ResourceRef, ...]


class OutcomeRatingRequest(PublicModel):
    dimension: str = Field(min_length=1, max_length=100)
    score: int = Field(ge=1, le=5)


class TurnOutcomeFeedbackRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    disposition: Literal["accepted", "needs_revision", "rejected"]
    ratings: tuple[OutcomeRatingRequest, ...] = ()
    issue_codes: tuple[str, ...] = ()
    comment: str = Field(default="", max_length=4000)
    policy_revision: str = Field(default="turn-outcome-feedback/v1", min_length=1)


class TurnOutcomeFeedbackResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    feedback_id: str
    turn_id: str
    disposition: Literal["accepted", "needs_revision", "rejected"]
    commit_revision: int = Field(ge=1)
    replayed: bool


class WorkspaceDataCandidateResponse(PublicModel):
    relative_path: str
    display_name: str
    size_bytes: int = Field(ge=0)
    modified_ns: int = Field(ge=0)


class WorkspaceDataCatalogResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    observed_at: datetime
    items: tuple[WorkspaceDataCandidateResponse, ...]


class ConversationSummaryResponse(PublicModel):
    conversation_id: str
    created_revision: int = Field(ge=1)
    latest_message_id: str | None
    latest_message_preview: str | None
    latest_message_ordinal: int | None = Field(default=None, ge=1)


class MessageSummaryResponse(PublicModel):
    message_id: str
    conversation_id: str
    role: Literal["user"]
    content: str
    ordinal: int = Field(ge=1)
    created_revision: int = Field(ge=1)


class WaitingRequestResponse(PublicModel):
    waiting_request_id: str
    turn_id: str
    wait_reason: WaitingReason
    prompt: str
    status: Literal["open"]
    created_turn_revision: int = Field(ge=1)


class PauseIntentResponse(PublicModel):
    pause_intent_id: str
    turn_id: str
    requested_turn_revision: int = Field(ge=1)
    reason: str
    status: Literal["requested", "converging"]


class ResearchPathSummaryResponse(PublicModel):
    research_path_id: str
    canonical_key: str
    created_revision: int = Field(ge=1)


class ProjectionWatermarkResponse(PublicModel):
    projection_name: Literal["evidence_current_state"]
    projection_revision: int = Field(ge=0)
    projection_lag: int = Field(ge=0)


class WorkspaceBootstrapData(PublicModel):
    active_conversation_id: str | None
    conversations: tuple[ConversationSummaryResponse, ...]
    recent_messages: tuple[MessageSummaryResponse, ...]
    execution: WorkspaceExecutionData
    open_waiting_request: WaitingRequestResponse | None
    active_pause_intent: PauseIntentResponse | None
    research_paths: tuple[ResearchPathSummaryResponse, ...]
    projection_watermarks: tuple[ProjectionWatermarkResponse, ...]


class WorkspaceBootstrapResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    durable_stream_cursor: str = Field(pattern=r"^wsc1_.+")
    data: WorkspaceBootstrapData
    resource_refs: tuple[ResourceRef, ...]


class ConversationTimelineItemResponse(PublicModel):
    item_kind: Literal["message", "assistant_output"]
    item_id: str
    turn_id: str | None
    role: Literal["user", "assistant"]
    content: str
    ordinal: int = Field(ge=1)
    created_revision: int = Field(ge=1)


class ConversationDetailData(PublicModel):
    conversation_id: str
    timeline: tuple[ConversationTimelineItemResponse, ...]
    turns: tuple[TurnResponse, ...]


class ConversationDetailResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    data: ConversationDetailData
    resource_refs: tuple[ResourceRef, ...]


class ProviderAttemptUsageResponse(PublicModel):
    provider_attempt_id: str
    model_invocation_id: str
    step_id: str
    step_ordinal: int = Field(ge=1)
    attempt_ordinal: int = Field(ge=1)
    provider_profile: str
    provider_kind: str
    model_name: str
    state: str
    usage_quality: Literal["exact", "estimated", "unknown"]
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    uncached_input_tokens: int | None = Field(default=None, ge=0)
    is_retry: bool
    is_fallback: bool
    observed_duration_seconds: float | None = Field(default=None, ge=0)
    error_code: str | None = None


class ToolOperationUsageResponse(PublicModel):
    operation_id: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    operation_kind: str
    status: str
    attempt_count: int = Field(ge=0)
    observed_duration_seconds: float | None = Field(default=None, ge=0)


class CountBudgetUsageResponse(PublicModel):
    policy_revision: str
    max_steps: int = Field(ge=1)
    used_steps: int = Field(ge=0)
    remaining_steps: int = Field(ge=0)
    max_tool_admissions: int = Field(ge=1)
    used_tool_admissions: int = Field(ge=0)
    remaining_tool_admissions: int = Field(ge=0)
    max_provider_attempts: int = Field(ge=1)
    used_provider_attempts: int = Field(ge=0)
    remaining_provider_attempts: int = Field(ge=0)


class RuntimeBudgetUsageResponse(PublicModel):
    policy_revision: str
    max_wall_clock_seconds: float = Field(gt=0)
    consumed_wall_clock_seconds: float = Field(ge=0)
    remaining_wall_clock_seconds: float = Field(ge=0)
    segment_count: int = Field(ge=0)


class MonetaryUsageEstimateResponse(PublicModel):
    pricing_revision: str
    currency: str
    quality: Literal["exact", "estimated", "unknown"]
    amount: str | None
    known_subtotal: str
    known_cached_savings: str
    priced_attempt_count: int = Field(ge=0)
    unpriced_attempt_count: int = Field(ge=0)
    explanation: str | None = None


class TurnUsageData(PublicModel):
    turn_id: str
    turn_status: str
    step_count: int = Field(ge=0)
    input_tokens_observed: int = Field(ge=0)
    output_tokens_observed: int = Field(ge=0)
    cached_input_tokens_observed: int = Field(ge=0)
    uncached_input_tokens_observed: int = Field(ge=0)
    unknown_usage_attempt_count: int = Field(ge=0)
    token_quality: Literal["exact", "estimated", "unknown"]
    retry_count: int = Field(ge=0)
    fallback_count: int = Field(ge=0)
    provider_duration_observed_seconds: float = Field(ge=0)
    tool_duration_observed_seconds: float = Field(ge=0)
    provider_attempts: tuple[ProviderAttemptUsageResponse, ...]
    tool_operations: tuple[ToolOperationUsageResponse, ...]
    count_budget: CountBudgetUsageResponse | None
    runtime_budget: RuntimeBudgetUsageResponse | None
    monetary_estimate: MonetaryUsageEstimateResponse


class TurnUsageResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    data: TurnUsageData
    resource_refs: tuple[ResourceRef, ...]


class ResultIndexItemResponse(PublicModel):
    result_slot_id: str
    slot_key: str
    slot_display_name: str
    result_id: str
    result_kind: Literal["statistical", "visual"]
    producing_stata_run_id: str
    created_by_turn_id: str
    created_revision: int = Field(ge=1)
    pointer_revision: int = Field(ge=1)
    adopted_revision: int = Field(ge=1)
    element_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    result_element_ids: tuple[str, ...]


class ResultIndexData(PublicModel):
    research_path_id: str
    items: tuple[ResultIndexItemResponse, ...]


class ResultIndexResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    data: ResultIndexData
    resource_refs: tuple[ResourceRef, ...]


class ResearchPlanNodeResponse(PublicModel):
    plan_node_id: str
    canonical_key: str
    node_kind: str
    specification: dict[str, object]
    ordinal: int = Field(ge=1)
    completed_run_count: int = Field(ge=0)


class ResearchPlanDependencyResponse(PublicModel):
    upstream_node_key: str
    downstream_node_key: str
    dependency_kind: Literal["data", "control", "evidence"]


class ResearchPlanData(PublicModel):
    research_path_id: str
    plan_id: str | None
    plan_revision_id: str | None
    revision_number: int | None = Field(default=None, ge=1)
    pointer_revision: int | None = Field(default=None, ge=1)
    summary: str | None
    change_kind: str | None
    trigger_references: tuple[object, ...]
    nodes: tuple[ResearchPlanNodeResponse, ...]
    dependencies: tuple[ResearchPlanDependencyResponse, ...]


class ResearchPlanResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    data: ResearchPlanData
    resource_refs: tuple[ResourceRef, ...]


class AnalysisOutputArtifactResponse(PublicModel):
    artifact_id: str
    role: Literal["primary", "preview", "supporting"]
    artifact_kind: Literal["dataset", "code", "log", "table", "document", "diagnostic"]
    media_type: str
    size_bytes: int = Field(ge=0)
    content_sha256: str = Field(min_length=64, max_length=64)
    verification_receipt_id: str | None


class AnalysisOutputItemResponse(PublicModel):
    analysis_output_id: str
    output_kind: Literal["visual", "scalar", "table", "test", "custom", "regression", "unknown"]
    output_fingerprint: str = Field(min_length=64, max_length=64)
    runtime_kind: Literal["python", "shell"]
    method_summary: str
    created_by_turn_id: str
    created_revision: int = Field(ge=1)
    document_eligible: bool
    adoption_id: str | None
    evidence_record_id: str | None
    artifacts: tuple[AnalysisOutputArtifactResponse, ...]


class AnalysisOutputIndexData(PublicModel):
    items: tuple[AnalysisOutputItemResponse, ...]


class AnalysisOutputIndexResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    data: AnalysisOutputIndexData
    resource_refs: tuple[ResourceRef, ...]


class DocumentSlotResponse(PublicModel):
    document_slot_id: str
    slot_key: Literal["manuscript.main.working", "manuscript.main.delivery"]
    pointer_revision: int | None = Field(default=None, ge=1)
    document_id: str | None
    document_revision_id: str | None
    origin_kind: Literal["agent_generated", "user_returned", "system_merged", "imported"] | None
    docx_artifact_id: str | None
    delivery_gate_verdict: Literal["pass", "fail", "unknown"] | None
    adopted_revision: int | None = Field(default=None, ge=1)


class DocumentIndexData(PublicModel):
    research_path_id: str
    items: tuple[DocumentSlotResponse, ...]


class DocumentIndexResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    generated_at: datetime
    data: DocumentIndexData
    resource_refs: tuple[ResourceRef, ...]


class JournalEntryResponse(PublicModel):
    journal_entry_id: str
    workspace_revision: int = Field(ge=1)
    ordinal: int = Field(ge=1)
    event_type: str
    object_type: str
    object_id: str
    summary: str
    payload_json: str


class JournalEntryPageResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    generated_at: datetime
    items: tuple[JournalEntryResponse, ...]
    next_cursor: str | None
    page_size: int = Field(ge=1, le=200)
    sort_direction: Literal["asc", "desc"]
    as_of_workspace_revision: int = Field(ge=0)
    authoritative_revision: int = Field(ge=0)
    workspace_advanced: bool
    newer_matching_entries_available: bool


class DurableNotificationResponse(PublicModel):
    event_class: Literal["durable"] = "durable"
    schema_version: Literal["1"] = "1"
    event_id: str
    stream_cursor: str = Field(pattern=r"^wsc1_.+")
    workspace_id: str
    workspace_revision: int = Field(ge=1)
    event_type: str
    resource_refs: tuple[ResourceRef, ...]
    summary_payload: dict[str, object]


class WorkspaceStreamHeadResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    requested_cursor: str = Field(pattern=r"^wsc1_.+")
    current_cursor: str = Field(pattern=r"^wsc1_.+")
    pending_notifications: bool


class AttentionRefResponse(PublicModel):
    attention_kind: str
    severity: Literal["INFO", "ACTION_REQUIRED", "CRITICAL"]
    resource_ref: ResourceRef


class WorkspaceAttentionResponse(PublicModel):
    workspace_id: str
    observed_authoritative_revision: int = Field(ge=0)
    active_write_turn_id: str | None
    active_write_turn_status: str | None
    queued_write_count: int = Field(ge=0)
    active_read_count: int = Field(ge=0)
    requires_action: bool
    highest_severity: Literal["INFO", "ACTION_REQUIRED", "CRITICAL"]
    attention_refs: tuple[AttentionRefResponse, ...]


class GlobalAttentionResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    generated_at: datetime
    workspaces: tuple[WorkspaceAttentionResponse, ...]


class LineageJournalResponse(PublicModel):
    journal_entry_id: str
    workspace_revision: int = Field(ge=1)
    ordinal: int = Field(ge=1)
    event_type: str
    object_type: str
    object_id: str


class DataStateStepResponse(PublicModel):
    operation_id: str
    execution_purpose: Literal[
        "data_load", "data_step", "formal_estimation", "formal_post_estimation"
    ]
    command_text: str
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_manifest_id: str
    data_state_token: str
    session_generation: int = Field(ge=1)


class EvidenceLineageData(PublicModel):
    source_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
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
    data_state_steps: tuple[DataStateStepResponse, ...]
    source_journal: LineageJournalResponse
    formal_result_block_id: str | None
    presentation_use_id: str | None
    rendered_text: str | None
    document_revision_id: str | None
    table_cell_evidence_use_id: str | None
    semantic_cell_slot: str | None


class EvidenceLineageResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    entry_kind: Literal["message_occurrence", "result_element", "document_cell", "evidence_record"]
    entry_id: str
    entry_subkey: str | None
    data: EvidenceLineageData


class MetaResponse(PublicModel):
    api_version: Literal["1"] = "1"
    server_version: str
    browser_session_required: bool


class ProviderCredentialCreateRequest(PublicModel):
    provider_kind: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=1, max_length=2048)
    account_label: str | None = Field(default=None, max_length=200)
    secret: SecretStr


class ProviderCredentialRotateRequest(PublicModel):
    secret: SecretStr


class ProviderCredentialReceiptResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    provider_profile_id: str
    credential_version_id: str
    status: Literal["adopted"]


class ProviderProfileResponse(PublicModel):
    provider_profile_id: str
    provider_kind: str
    endpoint: str
    account_label: str | None
    status: Literal["staging", "enabled", "disabled", "credential_unavailable"]
    credential_version_id: str | None


class ProviderProfileIndexResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    items: tuple[ProviderProfileResponse, ...]


class ProviderProfileDeleteReceiptResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    provider_profile_id: str
    status: Literal["deleted"]


class WorkspaceModelConfigurationRequest(PublicModel):
    provider_profile_id: str = Field(pattern=r"^provider_.+")
    model_name: str = Field(min_length=1, max_length=200)
    reasoning_effort: Literal["none", "low", "medium", "high"] = "medium"
    permission_mode: Literal["workspace_only", "full_access"] = "workspace_only"
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reserved_runtime_tokens: int = Field(default=4_096, ge=0)


class WorkspaceModelConfigurationResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    provider_profile_id: str
    provider_kind: str
    endpoint: str
    model_name: str
    reasoning_effort: Literal["none", "low", "medium", "high"]
    permission_mode: Literal["workspace_only", "full_access"]
    configuration_revision: int = Field(ge=1)
    context_window_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    reserved_runtime_tokens: int = Field(ge=0)


class DiagnosticBundleCreateRequest(PublicModel):
    mode: Literal["system_only", "scoped_workspace"]
    workspace_id: str | None = Field(default=None, pattern=r"^ws_.+")
    turn_id: str | None = Field(default=None, pattern=r"^turn_.+")
    operation_id: str | None = Field(default=None, pattern=r"^op_.+")


class DiagnosticBundlePreviewResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    bundle_request_id: str
    mode: Literal["system_only", "scoped_workspace"]
    diagnostic_snapshot_end: int = Field(ge=0)
    requested_workspace_revision: int | None = Field(default=None, ge=0)
    included_categories: tuple[str, ...]
    excluded_categories: tuple[str, ...]
    integrity_claim: str
    authenticity_claim: str


class DiagnosticBundleSaveRequest(PublicModel):
    output_path: str = Field(min_length=1, max_length=32767)


class DiagnosticBundleReceiptResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    bundle_id: str
    output_path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    member_count: int = Field(ge=1)
    diagnostic_snapshot_end: int = Field(ge=0)
    requested_workspace_revision: int | None = Field(default=None, ge=0)


class ErrorBody(PublicModel):
    code: str
    message: str
    next_action: str | None = None


class ErrorResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    error: ErrorBody


MemoryKindValue = Literal[
    "user_preference",
    "research_decision",
    "research_constraint",
    "feedback",
    "unresolved_question",
    "reference_pointer",
    "project_procedure",
]


class MemorySourceResponse(PublicModel):
    object_type: str
    object_id: str
    object_revision: str
    role: str


class MemoryItemResponse(PublicModel):
    memory_item_id: str
    memory_revision_id: str
    pointer_revision: int = Field(ge=1)
    lifecycle: Literal["proposed", "active", "retracted"]
    access_tier: Literal["hot", "warm", "cold", "archived"]
    pinned: bool
    retention_revision: int = Field(ge=1)
    superseded_by_memory_item_id: str | None = None
    scope_kind: Literal["workspace", "research_path"]
    scope_object_id: str
    kind: MemoryKindValue
    title: str
    content: str
    origin: Literal["explicit_user", "confirmed", "inferred", "imported"]
    created_revision: int = Field(ge=1)
    updated_revision: int = Field(ge=1)
    last_used_revision: int | None = Field(default=None, ge=1)
    recall_count: int = Field(ge=0)
    quality_flags: tuple[
        Literal[
            "awaiting_confirmation",
            "never_recalled",
            "potential_conflict",
            "source_revision_unavailable",
        ],
        ...,
    ]
    sources: tuple[MemorySourceResponse, ...]


class MemorySummaryResponse(PublicModel):
    scope_kind: Literal["workspace", "research_path"]
    scope_object_id: str
    summary_text: str
    source_revision: int = Field(ge=0)
    projection_revision: int = Field(ge=1)


class MemoryIndexResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    items: tuple[MemoryItemResponse, ...]
    summaries: tuple[MemorySummaryResponse, ...]


class MemoryCreateRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    kind: MemoryKindValue
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=4000)
    source_message_id: str = Field(pattern=r"^msg_.+")
    source_message_revision: int = Field(ge=1)
    research_path_id: str | None = Field(default=None, pattern=r"^path_.+")


class MemoryActivationRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    memory_revision_id: str = Field(pattern=r"^memoryrev_.+")
    expected_pointer_revision: int = Field(ge=1)


class MemoryRetractionRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    expected_pointer_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class MemoryRevisionRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    expected_pointer_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=4000)
    lifecycle: Literal["active", "proposed"] = "active"


class MemoryMutationResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    memory_item_id: str
    memory_revision_id: str
    lifecycle: Literal["proposed", "active", "retracted"]
    pointer_revision: int = Field(ge=1)
    commit_revision: int = Field(ge=1)
    replayed: bool


class MemoryAccessTierRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    expected_retention_revision: int = Field(ge=1)
    access_tier: Literal["hot", "warm", "cold", "archived"]
    reason: str = Field(min_length=1, max_length=1000)


class MemorySupersessionRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    expected_retention_revision: int = Field(ge=1)
    successor_memory_item_id: str = Field(pattern=r"^memoryitem_.+")
    reason: str = Field(min_length=1, max_length=1000)


class MemoryRetentionResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    memory_item_id: str
    access_tier: Literal["hot", "warm", "cold", "archived"]
    retention_revision: int = Field(ge=1)
    superseded_by_memory_item_id: str | None = None
    commit_revision: int = Field(ge=1)
    replayed: bool


class ConversationMemoryPolicyRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    use_memory: bool
    contribute_memory: bool
    expected_policy_revision: int | None = Field(default=None, ge=0)


class ConversationMemoryPolicyResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    conversation_id: str
    use_memory: bool
    contribute_memory: bool
    policy_revision: int = Field(ge=0)
    commit_revision: int | None = Field(default=None, ge=1)
    replayed: bool = False


class SkillEvolutionCandidateResponse(PublicModel):
    candidate_id: str
    skill_name: str
    proposed_version: str
    description: str
    instruction_body: str
    rationale: str
    policy_revision: str
    validation_status: Literal["passed", "blocked"]
    validation_findings: tuple[str, ...]
    lifecycle: Literal["proposed", "approved", "activated", "rejected", "retired"]
    pointer_revision: int = Field(ge=1)
    source_memory_item_ids: tuple[str, ...]
    created_revision: int = Field(ge=1)
    updated_revision: int = Field(ge=1)
    relative_skill_path: str | None = None


class SkillVersionResponse(PublicModel):
    skill_version_id: str
    version_label: str
    content_sha256: str
    source_candidate_id: str | None = None
    predecessor_skill_version_id: str | None = None
    created_revision: int = Field(ge=1)
    outcome_observation_count: int = Field(ge=0)


class SkillAdoptionResponse(PublicModel):
    skill_name: str
    lifecycle: Literal["active", "deactivated"]
    current_skill_version_id: str | None = None
    pointer_revision: int = Field(ge=1)
    updated_revision: int = Field(ge=1)
    versions: tuple[SkillVersionResponse, ...]


class SkillImprovementProposalResponse(PublicModel):
    proposal_id: str
    kind: Literal["revise", "merge", "retire", "keep_observing"]
    title: str
    rationale: str
    suggested_instruction_body: str | None = None
    merge_target_skill_name: str | None = None


class SkillEvaluationResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    run_id: str
    status: Literal["evaluating", "completed", "failed", "delivery_unknown"]
    skill_name: str
    candidate_skill_version_id: str
    baseline_skill_version_id: str | None = None
    evaluation_kind: Literal[
        "observational_single_version",
        "observational_version_comparison",
        "paired_replay",
    ]
    policy_revision: str
    source_start_revision: int = Field(ge=1)
    source_end_revision: int = Field(ge=1)
    candidate_use_turn_count: int = Field(ge=0)
    candidate_feedback_count: int = Field(ge=0)
    baseline_use_turn_count: int | None = Field(default=None, ge=0)
    baseline_feedback_count: int | None = Field(default=None, ge=0)
    verdict: (
        Literal[
            "insufficient_evidence",
            "candidate_preferred",
            "baseline_preferred",
            "mixed",
            "no_material_difference",
        ]
        | None
    ) = None
    rationale: str | None = None
    limitations: tuple[str, ...] = ()
    proposals: tuple[SkillImprovementProposalResponse, ...] = ()
    created_revision: int = Field(ge=1)
    updated_revision: int = Field(ge=1)
    evidence_relationship: Literal["co_occurrence_not_causation"] = "co_occurrence_not_causation"


class SkillChangeCandidateResponse(PublicModel):
    candidate_id: str
    source_proposal_id: str
    change_kind: Literal["revise", "merge"]
    skill_name: str
    base_skill_version_id: str
    merge_source_skill_version_id: str | None = None
    proposed_version: str
    description: str
    instruction_body: str
    rationale: str
    validation_status: Literal["passed", "blocked"]
    validation_findings: tuple[str, ...]
    lifecycle: Literal["proposed", "activated", "rejected"]
    pointer_revision: int = Field(ge=1)
    created_revision: int = Field(ge=1)
    updated_revision: int = Field(ge=1)
    activated_skill_version_id: str | None = None


class SkillEvolutionIndexResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    items: tuple[SkillEvolutionCandidateResponse, ...]
    adoptions: tuple[SkillAdoptionResponse, ...] = ()
    evaluations: tuple[SkillEvaluationResponse, ...] = ()
    change_candidates: tuple[SkillChangeCandidateResponse, ...] = ()


class SkillEvaluationRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    candidate_skill_version_id: str | None = Field(default=None, pattern=r"^skillversion_.+")
    baseline_skill_version_id: str | None = Field(default=None, pattern=r"^skillversion_.+")


class SkillChangeMaterializeRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")


class SkillChangeActivateRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    expected_pointer_revision: int = Field(ge=1)


class SkillChangeRejectRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    expected_pointer_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class SkillEvolutionApproveRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    expected_pointer_revision: int = Field(ge=1)


class SkillEvolutionRejectRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    expected_pointer_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class SkillRollbackRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    target_skill_version_id: str = Field(pattern=r"^skillversion_.+")
    expected_pointer_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class SkillDeactivateRequest(PublicModel):
    command_id: str = Field(pattern=r"^cmd_.+")
    expected_pointer_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class SkillEvolutionMutationResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    candidate_id: str
    lifecycle: Literal["proposed", "approved", "activated", "rejected", "retired"]
    pointer_revision: int = Field(ge=1)
    commit_revision: int = Field(ge=1)
    replayed: bool
    activation_manifest_id: str | None = None
    skill_version_id: str | None = None
    adoption_pointer_revision: int | None = Field(default=None, ge=1)


class SkillChangeMutationResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    candidate_id: str
    lifecycle: Literal["proposed", "activated", "rejected"]
    pointer_revision: int = Field(ge=1)
    skill_name: str
    base_skill_version_id: str
    merge_source_skill_version_id: str | None = None
    proposed_version: str
    validation_status: Literal["passed", "blocked"]
    validation_findings: tuple[str, ...]
    commit_revision: int = Field(ge=1)
    replayed: bool
    activated_skill_version_id: str | None = None


class SkillAdoptionMutationResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    skill_name: str
    lifecycle: Literal["active", "deactivated"]
    current_skill_version_id: str | None = None
    pointer_revision: int = Field(ge=1)
    commit_revision: int = Field(ge=1)
    replayed: bool
    publication_manifest_id: str


class KnowledgeDocumentResponse(PublicModel):
    relative_path: str
    availability: Literal["indexed", "missing", "extraction_failed"]
    content_sha256: str
    page_count: int | None = Field(default=None, ge=1)
    parser_profile: str | None = None
    canonical_ir_version: str | None = None
    ingestion_policy_revision: str | None = None
    structured_node_count: int = Field(default=0, ge=0)
    enrichment_finding_count: int = Field(default=0, ge=0)


class KnowledgeIndexResponse(PublicModel):
    schema_version: Literal["1"] = "1"
    workspace_id: str
    authoritative_revision: int = Field(ge=0)
    last_index_policy_revision: str | None = None
    documents: tuple[KnowledgeDocumentResponse, ...]
