"""Ports used by the context builder and credential-isolating model gateway."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from stata_research_agent.application.model_gateway import (
    FrozenModelInvocation,
    ProviderAttemptIdentity,
    ProviderResponse,
    StartModelStepCommand,
    StepFreezeIdentity,
)
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.domain.identifiers import (
    AssistantOutputId,
    CommandId,
    ModelInvocationId,
    ProviderAttemptId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


class ModelGatewayRepository(Protocol):
    def freeze_step(
        self,
        command: StartModelStepCommand,
        identities: StepFreezeIdentity,
        normalized_input_json: str,
        normalized_input_sha256: str,
    ) -> FrozenModelInvocation: ...

    def prepare_attempt(
        self,
        *,
        command_id: CommandId,
        invocation_id: ModelInvocationId,
        attempt_ordinal: int,
        identity: ProviderAttemptIdentity,
        request_json: str,
        request_sha256: str,
        provider_profile: str,
        credential_version_ref: str,
        endpoint_origin: str,
        payload_bytes: int,
    ) -> WorkspaceRevision: ...

    def mark_dispatch_started(
        self,
        command_id: CommandId,
        turn_id: TurnId,
        invocation_id: ModelInvocationId,
        attempt_id: ProviderAttemptId,
    ) -> WorkspaceRevision: ...

    def complete_attempt(
        self,
        *,
        command_id: CommandId,
        invocation_id: ModelInvocationId,
        attempt_id: ProviderAttemptId,
        assistant_output_id: AssistantOutputId,
        output_json: str,
        output_sha256: str,
        response: ProviderResponse,
    ) -> WorkspaceRevision: ...

    def fail_attempt(
        self,
        *,
        command_id: CommandId,
        invocation_id: ModelInvocationId,
        attempt_id: ProviderAttemptId,
        error_code: str,
        terminal_state: str,
        terminal_invocation: bool,
    ) -> WorkspaceRevision: ...


class CredentialResolver(Protocol):
    def resolve_for_transport(
        self,
        credential_ref: str,
        *,
        provider_profile_id: str,
        endpoint: str,
    ) -> ResolvedProviderCredential: ...


class ProviderTransport(Protocol):
    async def send(
        self,
        *,
        endpoint: str,
        request_json: str,
        credential: str,
    ) -> ProviderResponse: ...


class StreamingProviderTransport(ProviderTransport, Protocol):
    async def send_stream(
        self,
        *,
        endpoint: str,
        request_json: str,
        credential: str,
        on_text_delta: Callable[[str], Awaitable[None]],
    ) -> ProviderResponse: ...
