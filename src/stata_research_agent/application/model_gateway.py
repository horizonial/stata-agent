"""Typed Step context and provider-gateway contracts.

These values deliberately contain references, policies, and de-secreted payloads only.  A
resolved provider credential is passed directly from the credential port to the transport and
must never enter one of these authoritative records.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from stata_research_agent.domain.identifiers import (
    AssistantOutputId,
    BudgetPolicySnapshotId,
    BudgetUsageId,
    CommandId,
    ContextBuildDecisionId,
    ContextItemId,
    ContextManifestId,
    ModelInputSnapshotId,
    ModelInvocationId,
    ModelPolicySnapshotId,
    OutboundMaterialRecordId,
    PermissionSnapshotId,
    ProviderAttemptId,
    ProviderRequestSnapshotId,
    ResearchPathId,
    StepId,
    TurnContextBaselineId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

JsonObject = Mapping[str, Any]


class ContextTrustClass(StrEnum):
    """Instruction authority carried by one compiled Context Item."""

    USER_INSTRUCTION = "user_instruction"
    AUTHORITATIVE_RECORD = "authoritative_record"
    RECALLED_CONTEXT = "recalled_context"
    RETRIEVED_UNTRUSTED = "retrieved_untrusted"
    TOOL_OUTPUT_UNTRUSTED = "tool_output_untrusted"


@dataclass(frozen=True, slots=True)
class ContextItemCandidate:
    item_kind: str
    source_object_type: str
    source_object_id: str
    source_revision: str
    remote_transmission_class: str
    content: str
    trust_class: str = ContextTrustClass.AUTHORITATIVE_RECORD

    def __post_init__(self) -> None:
        if self.remote_transmission_class not in {
            "remote_allowed",
            "local_only",
            "metadata_only",
        }:
            raise ValueError("invalid remote transmission class")
        if self.trust_class not in {item.value for item in ContextTrustClass}:
            raise ValueError("invalid context trust class")
        if not all(
            value.strip()
            for value in (
                self.item_kind,
                self.source_object_type,
                self.source_object_id,
                self.source_revision,
            )
        ):
            raise ValueError("context item references must be non-empty")


@dataclass(frozen=True, slots=True)
class ContextBuildDecisionCandidate:
    decision_kind: str
    source_object_type: str
    source_object_id: str
    reason_code: str
    detail: JsonObject

    def __post_init__(self) -> None:
        if self.decision_kind not in {"included", "excluded", "truncated", "summarized"}:
            raise ValueError("invalid context build decision")


@dataclass(frozen=True, slots=True)
class StartModelStepCommand:
    command_id: CommandId
    turn_id: TurnId
    expected_turn_revision: int
    system_prompt_revision: str
    system_prompt: str
    main_skill_name: str
    main_skill_revision: str
    main_skill_content: str
    tool_catalog_revision: str
    tool_schemas: tuple[JsonObject, ...]
    context_items: tuple[ContextItemCandidate, ...]
    build_decisions: tuple[ContextBuildDecisionCandidate, ...]
    permission_policy_revision: str
    permissions: JsonObject
    model_policy_revision: str
    provider_profile: str
    provider_kind: str
    model_name: str
    endpoint: str
    credential_ref: str
    provider_policy: JsonObject
    remote_provider: bool = True
    input_token_limit: int = 128_000
    remaining_step_budget: int = 64
    remaining_tool_budget: int = 128
    current_remaining_step_budget: int | None = None
    current_remaining_tool_budget: int | None = None
    budget_policy_revision: str = "budget-v0.1"
    max_provider_attempts_per_invocation: int = 3
    max_provider_attempts_per_turn: int = 64
    max_same_failure_fingerprint: int = 2

    def __post_init__(self) -> None:
        if self.expected_turn_revision < 1:
            raise ValueError("expected Turn revision must be positive")
        if (
            min(
                self.input_token_limit,
                self.remaining_step_budget,
                self.remaining_tool_budget,
                self.max_provider_attempts_per_invocation,
                self.max_provider_attempts_per_turn,
                self.max_same_failure_fingerprint,
            )
            < 1
        ):
            raise ValueError("invalid model context budget")
        current_steps = self.current_remaining_step_budget
        current_tools = self.current_remaining_tool_budget
        if current_steps is not None and not 1 <= current_steps <= self.remaining_step_budget:
            raise ValueError("invalid current remaining Step budget")
        if current_tools is not None and not 0 <= current_tools <= self.remaining_tool_budget:
            raise ValueError("invalid current remaining Tool budget")
        required = (
            self.system_prompt_revision,
            self.main_skill_name,
            self.main_skill_revision,
            self.tool_catalog_revision,
            self.model_policy_revision,
            self.provider_profile,
            self.provider_kind,
            self.model_name,
            self.endpoint,
            self.credential_ref,
        )
        if not all(value.strip() for value in required):
            raise ValueError("model gateway configuration fields must be non-empty")


@dataclass(frozen=True, slots=True)
class StepFreezeIdentity:
    baseline_id: TurnContextBaselineId
    permission_snapshot_id: PermissionSnapshotId
    model_policy_snapshot_id: ModelPolicySnapshotId
    step_id: StepId
    context_manifest_id: ContextManifestId
    context_item_ids: tuple[ContextItemId, ...]
    decision_ids: tuple[ContextBuildDecisionId, ...]
    input_snapshot_id: ModelInputSnapshotId
    invocation_id: ModelInvocationId
    budget_policy_snapshot_id: BudgetPolicySnapshotId
    step_budget_usage_id: BudgetUsageId


@dataclass(frozen=True, slots=True)
class FrozenModelInvocation:
    step_id: StepId
    invocation_id: ModelInvocationId
    input_snapshot_id: ModelInputSnapshotId
    research_path_id: ResearchPathId
    normalized_input: JsonObject
    normalized_input_sha256: str
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProviderAttemptIdentity:
    attempt_id: ProviderAttemptId
    request_snapshot_id: ProviderRequestSnapshotId
    outbound_record_id: OutboundMaterialRecordId
    budget_usage_id: BudgetUsageId


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    output: JsonObject
    usage_kind: str = "unknown"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    uncached_input_tokens: int | None = None
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if self.usage_kind not in {"exact", "estimated", "unknown"}:
            raise ValueError("invalid usage kind")
        if self.input_tokens is not None and self.input_tokens < 0:
            raise ValueError("input token usage cannot be negative")
        if self.output_tokens is not None and self.output_tokens < 0:
            raise ValueError("output token usage cannot be negative")
        if self.cached_input_tokens is not None and self.cached_input_tokens < 0:
            raise ValueError("cached input token usage cannot be negative")
        if self.uncached_input_tokens is not None and self.uncached_input_tokens < 0:
            raise ValueError("uncached input token usage cannot be negative")
        if self.finish_reason is not None and not self.finish_reason.strip():
            raise ValueError("finish reason cannot be blank")
        if (
            self.input_tokens is not None
            and self.cached_input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached input tokens cannot exceed total input tokens")


@dataclass(frozen=True, slots=True)
class ProviderResponseDelta:
    provider_attempt_id: ProviderAttemptId
    sequence: int
    channel: str
    content: str

    def __post_init__(self) -> None:
        if self.sequence < 1 or self.channel != "provider_content" or not self.content:
            raise ValueError("invalid ephemeral provider response delta")


class ProviderDispatchError(RuntimeError):
    """A classified transport failure.

    ``retry_safe`` means another bounded Provider Attempt cannot duplicate a real-world tool
    side effect.  It includes pre-dispatch transport failures and received model responses that
    cannot satisfy the output contract. ``delivery_unknown`` always dominates and forbids an
    automatic retry.
    """

    def __init__(
        self,
        code: str,
        *,
        retry_safe: bool,
        delivery_unknown: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")
        super().__init__(code)
        self.code = code
        self.retry_safe = retry_safe and not delivery_unknown
        self.delivery_unknown = delivery_unknown
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class ModelStepOutcome:
    step_id: StepId
    invocation_id: ModelInvocationId
    provider_attempt_ids: tuple[ProviderAttemptId, ...]
    assistant_output_id: AssistantOutputId | None
    output: JsonObject | None
    status: str
    commit_revision: WorkspaceRevision
    failure_code: str | None = None
