"""Workspace default model configuration without Provider secret material."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WorkspaceModelConfiguration:
    workspace_id: str
    provider_profile_id: str
    provider_kind: str
    endpoint: str
    credential_ref: str
    model_name: str
    reasoning_effort: str
    permission_mode: str
    configuration_revision: int
    context_window_tokens: int = 128_000
    max_output_tokens: int = 8_192
    reserved_runtime_tokens: int = 4_096


class WorkspaceModelConfigurationRepository(Protocol):
    def select_workspace_model(
        self,
        *,
        workspace_id: str,
        provider_profile_id: str,
        model_name: str,
        reasoning_effort: str,
        permission_mode: str,
        context_window_tokens: int,
        max_output_tokens: int,
        reserved_runtime_tokens: int,
    ) -> WorkspaceModelConfiguration: ...

    def resolve_workspace_model(self, workspace_id: str) -> WorkspaceModelConfiguration: ...


class WorkspaceModelConfigurationService:
    def __init__(self, repository: WorkspaceModelConfigurationRepository) -> None:
        self._repository = repository

    def select(
        self,
        *,
        workspace_id: str,
        provider_profile_id: str,
        model_name: str,
        reasoning_effort: str = "medium",
        permission_mode: str = "workspace_only",
        context_window_tokens: int | None = None,
        max_output_tokens: int | None = None,
        reserved_runtime_tokens: int = 4_096,
    ) -> WorkspaceModelConfiguration:
        if not workspace_id.startswith("ws_") or not model_name.strip():
            raise ValueError("Workspace and model identities are required")
        if reasoning_effort not in {"none", "low", "medium", "high"}:
            raise ValueError("unsupported reasoning effort")
        if permission_mode not in {"workspace_only", "full_access"}:
            raise ValueError("unsupported permission mode")
        known_context, known_output = self._known_capabilities(model_name.strip())
        context_window_tokens = context_window_tokens or known_context
        max_output_tokens = max_output_tokens or known_output
        if (
            context_window_tokens < 1
            or max_output_tokens < 1
            or reserved_runtime_tokens < 0
            or max_output_tokens + reserved_runtime_tokens >= context_window_tokens
        ):
            raise ValueError("invalid model token capacities")
        return self._repository.select_workspace_model(
            workspace_id=workspace_id,
            provider_profile_id=provider_profile_id,
            model_name=model_name.strip(),
            reasoning_effort=reasoning_effort,
            permission_mode=permission_mode,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            reserved_runtime_tokens=reserved_runtime_tokens,
        )

    def resolve(self, workspace_id: str) -> WorkspaceModelConfiguration:
        return self._repository.resolve_workspace_model(workspace_id)

    @staticmethod
    def _known_capabilities(model_name: str) -> tuple[int, int]:
        # Pinned model identities may carry reviewed defaults. Unknown/self-hosted models use
        # conservative compatibility defaults and can be configured explicitly through the API.
        known = {
            "gpt-5.3-codex": (400_000, 128_000),
            "deepseek-chat": (128_000, 8_192),
        }
        return known.get(model_name.casefold(), (128_000, 8_192))
