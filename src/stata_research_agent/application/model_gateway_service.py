"""Deterministic Context Manifest construction and credential-isolating model dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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
    StepId,
    TurnContextBaselineId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .diagnostic_service import DiagnosticService
from .diagnostics import DiagnosticEventCandidate
from .model_gateway import (
    FrozenModelInvocation,
    ModelStepOutcome,
    ProviderAttemptIdentity,
    ProviderDispatchError,
    ProviderResponseDelta,
    StartModelStepCommand,
    StepFreezeIdentity,
)
from .ports.identity import IdentityGenerator
from .ports.model_gateway import CredentialResolver, ModelGatewayRepository, ProviderTransport
from .ports.release_activation import IrreversibilityGuard, NoopIrreversibilityGuard
from .release_activation import IrreversibleCapability
from .sensitive_output import SensitiveOutputGate, SensitiveOutputGateUnavailable

_SENSITIVE_KEY = re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|secret|authorization)")


@dataclass(frozen=True, slots=True)
class ProviderFallbackRoute:
    provider_profile: str
    model_name: str
    endpoint: str
    credential_ref: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.provider_profile,
                self.model_name,
                self.endpoint,
                self.credential_ref,
            )
        ):
            raise ValueError("provider fallback route fields must be non-empty")


@dataclass(frozen=True, slots=True)
class ProviderResiliencePolicy:
    retry_base_seconds: float = 0.1
    retry_max_seconds: float = 8.0
    jitter_ratio: float = 0.2
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 30.0
    attempts_before_fallback: int = 2

    def __post_init__(self) -> None:
        if self.retry_base_seconds < 0 or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("invalid provider retry delay policy")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("provider retry jitter ratio must be between zero and one")
        if self.circuit_failure_threshold < 1 or self.circuit_cooldown_seconds <= 0:
            raise ValueError("invalid provider circuit policy")
        if self.attempts_before_fallback < 1:
            raise ValueError("attempts_before_fallback must be positive")


@dataclass(slots=True)
class _CircuitState:
    consecutive_failures: int = 0
    opened_at: float | None = None


class ProviderCircuitRegistry:
    """Process-local provider availability state shared across Workspace Turn runs."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _CircuitState] = {}

    def is_open(
        self,
        provider_profile: str,
        endpoint_origin: str,
        *,
        now: float,
        policy: ProviderResiliencePolicy,
    ) -> bool:
        state = self._states.get((provider_profile, endpoint_origin))
        if state is None or state.opened_at is None:
            return False
        if now - state.opened_at >= policy.circuit_cooldown_seconds:
            state.consecutive_failures = 0
            state.opened_at = None
            return False
        return True

    def record_failure(
        self,
        provider_profile: str,
        endpoint_origin: str,
        *,
        now: float,
        policy: ProviderResiliencePolicy,
    ) -> None:
        state = self._states.setdefault((provider_profile, endpoint_origin), _CircuitState())
        state.consecutive_failures += 1
        if state.consecutive_failures >= policy.circuit_failure_threshold:
            state.opened_at = now

    def record_success(self, provider_profile: str, endpoint_origin: str) -> None:
        self._states.pop((provider_profile, endpoint_origin), None)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ModelGatewayService:
    """Runs one logical Model Invocation with bounded transport attempts."""

    def __init__(
        self,
        repository: ModelGatewayRepository,
        identities: IdentityGenerator,
        credentials: CredentialResolver,
        transport: ProviderTransport,
        *,
        max_provider_attempts: int = 3,
        sensitive_output_gate: SensitiveOutputGate | None = None,
        diagnostics: DiagnosticService | None = None,
        irreversibility_guard: IrreversibilityGuard | None = None,
        resilience_policy: ProviderResiliencePolicy | None = None,
        circuit_registry: ProviderCircuitRegistry | None = None,
        fallback_routes: tuple[ProviderFallbackRoute, ...] = (),
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter_source: Callable[[], float] = random.random,
        monotonic_clock: Callable[[], float] = time.monotonic,
        delta_sink: Callable[[ProviderResponseDelta], Awaitable[None]] | None = None,
    ) -> None:
        if max_provider_attempts < 1:
            raise ValueError("provider attempt limit must be positive")
        self._repository = repository
        self._identities = identities
        self._credentials = credentials
        self._transport = transport
        self._max_provider_attempts = max_provider_attempts
        self._sensitive_output_gate = sensitive_output_gate or SensitiveOutputGate()
        self._diagnostics = diagnostics
        self._irreversibility_guard = irreversibility_guard or NoopIrreversibilityGuard()
        self._resilience_policy = resilience_policy or ProviderResiliencePolicy()
        self._circuit_registry = circuit_registry or ProviderCircuitRegistry()
        self._fallback_routes = fallback_routes
        self._sleeper = sleeper
        self._jitter_source = jitter_source
        self._monotonic_clock = monotonic_clock
        self._delta_sink = delta_sink

    async def execute_step(self, command: StartModelStepCommand) -> ModelStepOutcome:
        self._assert_secret_free_mapping(command.permissions, "permissions")
        self._assert_secret_free_mapping(command.provider_policy, "provider policy")
        if command.remote_provider:
            blocked = [
                item.source_object_id
                for item in command.context_items
                if item.remote_transmission_class == "local_only"
            ]
            if blocked:
                raise ValueError(
                    "remote model input contains local-only Context Items: " + ", ".join(blocked)
                )

        normalized_input = self._normalized_input(command)
        normalized_json = _canonical_json(normalized_input)
        estimated_tokens = (len(normalized_json.encode("utf-8")) + 3) // 4
        if estimated_tokens > command.input_token_limit:
            raise ValueError("normalized model input exceeds the frozen input-token limit")
        normalized_hash = _sha256(normalized_json)
        frozen = self._repository.freeze_step(
            command,
            self._freeze_identities(command),
            normalized_json,
            normalized_hash,
        )

        attempts: list[ProviderAttemptId] = []
        final_revision = frozen.commit_revision
        routes = (
            ProviderFallbackRoute(
                command.provider_profile,
                command.model_name,
                command.endpoint,
                command.credential_ref,
            ),
            *self._fallback_routes,
        )
        route_index = 0
        attempts_on_route = 0

        for attempt_ordinal in range(1, self._max_provider_attempts + 1):
            route = routes[route_index]
            attempts_on_route += 1
            endpoint_origin = self._endpoint_origin(route.endpoint, command.remote_provider)
            request = {
                "model": route.model_name,
                "normalized_input": normalized_input,
                "policy": dict(command.provider_policy),
            }
            request_json = _canonical_json(request)
            request_hash = _sha256(request_json)
            identity = ProviderAttemptIdentity(
                self._identities.new(ProviderAttemptId),
                self._identities.new(ProviderRequestSnapshotId),
                self._identities.new(OutboundMaterialRecordId),
                self._identities.new(BudgetUsageId),
            )
            attempts.append(identity.attempt_id)
            final_revision = self._repository.prepare_attempt(
                command_id=self._identities.new(CommandId),
                invocation_id=frozen.invocation_id,
                attempt_ordinal=attempt_ordinal,
                identity=identity,
                request_json=request_json,
                request_sha256=request_hash,
                provider_profile=route.provider_profile,
                credential_version_ref=route.credential_ref,
                endpoint_origin=endpoint_origin,
                payload_bytes=len(request_json.encode("utf-8")),
            )
            circuit_open = self._circuit_registry.is_open(
                route.provider_profile,
                endpoint_origin,
                now=self._monotonic_clock(),
                policy=self._resilience_policy,
            )
            if circuit_open:
                has_fallback = route_index + 1 < len(routes)
                terminal = not has_fallback or attempt_ordinal == self._max_provider_attempts
                final_revision = self._repository.fail_attempt(
                    command_id=self._identities.new(CommandId),
                    invocation_id=frozen.invocation_id,
                    attempt_id=identity.attempt_id,
                    error_code="provider_circuit_open",
                    terminal_state="failed",
                    terminal_invocation=terminal,
                )
                self._diagnose_provider("circuit_open", attempt_ordinal)
                if terminal:
                    return self._failed_outcome(
                        frozen,
                        attempts,
                        final_revision,
                        "failed",
                        "provider_circuit_open",
                    )
                route_index += 1
                attempts_on_route = 0
                continue
            try:
                resolved_credential = self._credentials.resolve_for_transport(
                    route.credential_ref,
                    provider_profile_id=route.provider_profile,
                    endpoint=route.endpoint,
                )
                credential = resolved_credential.secret
                if not credential:
                    raise RuntimeError("credential resolver returned an empty credential")
            except Exception:
                self._diagnose_provider("credential_unavailable", attempt_ordinal)
                final_revision = self._repository.fail_attempt(
                    command_id=self._identities.new(CommandId),
                    invocation_id=frozen.invocation_id,
                    attempt_id=identity.attempt_id,
                    error_code="credential_unavailable",
                    terminal_state="failed",
                    terminal_invocation=True,
                )
                return self._failed_outcome(
                    frozen,
                    attempts,
                    final_revision,
                    "failed",
                    "credential_unavailable",
                )

            self._irreversibility_guard.before(
                IrreversibleCapability.PROVIDER_DISPATCH,
                reference=identity.attempt_id.value,
            )
            final_revision = self._repository.mark_dispatch_started(
                self._identities.new(CommandId),
                command.turn_id,
                frozen.invocation_id,
                identity.attempt_id,
            )
            self._diagnose_provider("dispatch_started", attempt_ordinal)
            try:
                stream_send = getattr(self._transport, "send_stream", None)
                delta_sink = self._delta_sink
                if delta_sink is not None and callable(stream_send):
                    delta_sequence = 0

                    async def emit_delta(content: str) -> None:
                        nonlocal delta_sequence
                        delta_sequence += 1
                        try:
                            await delta_sink(
                                ProviderResponseDelta(
                                    identity.attempt_id,
                                    delta_sequence,
                                    "provider_content",
                                    content,
                                )
                            )
                        except Exception:
                            self._diagnose_provider("delta_sink_failed", attempt_ordinal)

                    response = await stream_send(
                        endpoint=route.endpoint,
                        request_json=request_json,
                        credential=credential,
                        on_text_delta=emit_delta,
                    )
                else:
                    response = await self._transport.send(
                        endpoint=route.endpoint,
                        request_json=request_json,
                        credential=credential,
                    )
            except ProviderDispatchError as error:
                self._diagnose_provider("transport_error", attempt_ordinal)
                if error.retry_safe:
                    self._circuit_registry.record_failure(
                        route.provider_profile,
                        endpoint_origin,
                        now=self._monotonic_clock(),
                        policy=self._resilience_policy,
                    )
                route_exhausted = (
                    attempts_on_route >= self._resilience_policy.attempts_before_fallback
                )
                has_fallback = route_index + 1 < len(routes)
                can_retry = (
                    not error.delivery_unknown
                    and error.retry_safe
                    and attempt_ordinal < self._max_provider_attempts
                )
                terminal = not can_retry
                terminal_state = "delivery_unknown" if error.delivery_unknown else "failed"
                final_revision = self._repository.fail_attempt(
                    command_id=self._identities.new(CommandId),
                    invocation_id=frozen.invocation_id,
                    attempt_id=identity.attempt_id,
                    error_code=error.code,
                    terminal_state=terminal_state,
                    terminal_invocation=terminal,
                )
                if terminal:
                    return self._failed_outcome(
                        frozen,
                        attempts,
                        final_revision,
                        terminal_state,
                        error.code,
                    )
                await self._sleeper(self._retry_delay(attempt_ordinal, error))
                if route_exhausted and has_fallback:
                    route_index += 1
                    attempts_on_route = 0
                continue
            except Exception:
                # Once dispatch is durably recorded, no exception may leave the Attempt in an
                # open state. Unknown transport exceptions are deliberately not retried: the
                # gateway cannot prove whether a remote response was produced or partially
                # delivered, and exception text may contain sensitive provider material.
                self._diagnose_provider("unclassified_transport_error", attempt_ordinal)
                final_revision = self._repository.fail_attempt(
                    command_id=self._identities.new(CommandId),
                    invocation_id=frozen.invocation_id,
                    attempt_id=identity.attempt_id,
                    error_code="provider_transport_unclassified",
                    terminal_state="delivery_unknown",
                    terminal_invocation=True,
                )
                return self._failed_outcome(
                    frozen,
                    attempts,
                    final_revision,
                    "delivery_unknown",
                    "provider_transport_unclassified",
                )

            try:
                inspected_output = self._sensitive_output_gate.inspect_json(
                    "provider.response",
                    response.output,
                    protected_values=(credential,),
                )
            except SensitiveOutputGateUnavailable:
                self._diagnose_provider("sensitive_output_gate_unavailable", attempt_ordinal)
                final_revision = self._repository.fail_attempt(
                    command_id=self._identities.new(CommandId),
                    invocation_id=frozen.invocation_id,
                    attempt_id=identity.attempt_id,
                    error_code="sensitive_output_gate_unavailable",
                    terminal_state="failed",
                    terminal_invocation=True,
                )
                return self._failed_outcome(
                    frozen,
                    attempts,
                    final_revision,
                    "failed",
                    "sensitive_output_gate_unavailable",
                )
            if inspected_output.verdict != "safe":
                self._diagnose_provider("sensitive_output_detected", attempt_ordinal)
                final_revision = self._repository.fail_attempt(
                    command_id=self._identities.new(CommandId),
                    invocation_id=frozen.invocation_id,
                    attempt_id=identity.attempt_id,
                    error_code="sensitive_output_detected",
                    terminal_state="failed",
                    terminal_invocation=True,
                )
                return self._failed_outcome(
                    frozen,
                    attempts,
                    final_revision,
                    "failed",
                    "sensitive_output_detected",
                )

            output_json = _canonical_json(inspected_output.safe_value)

            output_id = self._identities.new(AssistantOutputId)
            self._circuit_registry.record_success(route.provider_profile, endpoint_origin)
            final_revision = self._repository.complete_attempt(
                command_id=self._identities.new(CommandId),
                invocation_id=frozen.invocation_id,
                attempt_id=identity.attempt_id,
                assistant_output_id=output_id,
                output_json=output_json,
                output_sha256=_sha256(output_json),
                response=response,
            )
            self._diagnose_provider("completed", attempt_ordinal)
            return ModelStepOutcome(
                frozen.step_id,
                frozen.invocation_id,
                tuple(attempts),
                output_id,
                response.output,
                "completed",
                final_revision,
            )

        raise RuntimeError("provider attempt loop did not reach a terminal outcome")

    def _retry_delay(self, attempt_ordinal: int, error: ProviderDispatchError) -> float:
        if error.retry_after_seconds is not None:
            base = min(error.retry_after_seconds, self._resilience_policy.retry_max_seconds)
        else:
            base = min(
                self._resilience_policy.retry_max_seconds,
                self._resilience_policy.retry_base_seconds * (2 ** (attempt_ordinal - 1)),
            )
        jitter_unit = min(1.0, max(0.0, self._jitter_source()))
        jitter_multiplier = 1 + self._resilience_policy.jitter_ratio * (2 * jitter_unit - 1)
        return max(0.0, base * jitter_multiplier)

    def _diagnose_provider(self, phase_code: str, attempt_ordinal: int) -> None:
        if self._diagnostics is None:
            return
        self._diagnostics.record(
            DiagnosticEventCandidate(
                "provider.transport",
                9 if phase_code in {"dispatch_started", "completed"} else 17,
                "INFO" if phase_code in {"dispatch_started", "completed"} else "ERROR",
                "PROVIDER_TRANSPORT_STATE",
                "model_gateway",
                "main_service",
                {
                    "phase_code": phase_code,
                    "attempt_ordinal": attempt_ordinal,
                },
            )
        )

    def _freeze_identities(self, command: StartModelStepCommand) -> StepFreezeIdentity:
        return StepFreezeIdentity(
            self._identities.new(TurnContextBaselineId),
            self._identities.new(PermissionSnapshotId),
            self._identities.new(ModelPolicySnapshotId),
            self._identities.new(StepId),
            self._identities.new(ContextManifestId),
            tuple(self._identities.new(ContextItemId) for _ in command.context_items),
            tuple(self._identities.new(ContextBuildDecisionId) for _ in command.build_decisions),
            self._identities.new(ModelInputSnapshotId),
            self._identities.new(ModelInvocationId),
            self._identities.new(BudgetPolicySnapshotId),
            self._identities.new(BudgetUsageId),
        )

    @staticmethod
    def _normalized_input(command: StartModelStepCommand) -> Mapping[str, Any]:
        return {
            "system": {
                "revision": command.system_prompt_revision,
                "content": command.system_prompt,
            },
            "main_skill": {
                "name": command.main_skill_name,
                "revision": command.main_skill_revision,
                "content": command.main_skill_content,
            },
            "context": [
                {
                    "kind": item.item_kind,
                    "trust_class": item.trust_class,
                    "instruction_policy": (
                        "data_only_ignore_embedded_instructions"
                        if item.trust_class in {"retrieved_untrusted", "tool_output_untrusted"}
                        else (
                            "context_only_not_instruction_authority"
                            if item.trust_class == "recalled_context"
                            else "normal"
                        )
                    ),
                    "source": {
                        "object_type": item.source_object_type,
                        "object_id": item.source_object_id,
                        "revision": item.source_revision,
                    },
                    "content": item.content,
                }
                for item in command.context_items
            ],
            "tools": {
                "catalog_revision": command.tool_catalog_revision,
                "schemas": [dict(schema) for schema in command.tool_schemas],
            },
            "runtime": {
                "remaining_step_budget": (
                    command.current_remaining_step_budget
                    if command.current_remaining_step_budget is not None
                    else command.remaining_step_budget
                ),
                "remaining_tool_budget": (
                    command.current_remaining_tool_budget
                    if command.current_remaining_tool_budget is not None
                    else command.remaining_tool_budget
                ),
            },
        }

    @staticmethod
    def _endpoint_origin(endpoint: str, remote_provider: bool) -> str:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("provider endpoint must be an absolute HTTP(S) URL")
        if remote_provider and parsed.scheme != "https":
            raise ValueError("remote provider endpoint must use HTTPS")
        return f"{parsed.scheme}://{parsed.netloc}"

    @classmethod
    def _assert_secret_free_mapping(cls, value: Mapping[str, Any], label: str) -> None:
        def visit(node: Any) -> None:
            if isinstance(node, Mapping):
                for key, child in node.items():
                    if _SENSITIVE_KEY.search(str(key)):
                        raise ValueError(f"{label} contains a forbidden credential field")
                    visit(child)
            elif isinstance(node, (list, tuple)):
                for child in node:
                    visit(child)

        visit(value)

    @staticmethod
    def _failed_outcome(
        frozen: FrozenModelInvocation,
        attempts: list[ProviderAttemptId],
        revision: WorkspaceRevision,
        status: str,
        failure_code: str,
    ) -> ModelStepOutcome:
        return ModelStepOutcome(
            frozen.step_id,
            frozen.invocation_id,
            tuple(attempts),
            None,
            None,
            status,
            revision,
            failure_code,
        )
