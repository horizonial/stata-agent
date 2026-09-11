"""Closed provider/model catalog used by composition and the settings UI.

The catalog is deliberately data-driven rather than a list embedded in the
web page.  Every selectable model has a :class:`ModelCapabilityProfile`, so
adding a provider remains an explicit, reviewable change; the UI never accepts
an arbitrary model identifier.  The built-ins below use the common
OpenAI-compatible ``/chat/completions`` contract.  Provider-specific quirks
can be added to a profile or a dedicated adapter without changing the agent
loop.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .capabilities import ModelCapabilityProfile, deepseek_chat_profile, qwen_dashscope_profile


def _profile(
    provider: str,
    model: str,
    *,
    max_context: int,
    max_output: int = 8192,
    json_mode: bool = True,
    known_quirks: Iterable[str] = (),
) -> ModelCapabilityProfile:
    """Build a conservative OpenAI-compatible capability profile."""

    return ModelCapabilityProfile(
        provider=provider,
        model=model,
        supports_tools=True,
        strict_schema=False,
        thinking_with_tools=False,
        streaming=True,
        json_mode=json_mode,
        max_context=max_context,
        max_output=max_output,
        known_quirks=list(known_quirks),
        contract_test_version=1,
    )


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    """One registered remote provider and its approved model profiles."""

    provider_id: str
    label: str
    api_key_env: str
    credential_target: str
    default_base_url: str
    models: tuple[ModelCapabilityProfile, ...]
    protocol: str = "openai_chat_completions"

    @property
    def default_model(self) -> str:
        return self.models[0].model

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(profile.model for profile in self.models)

    def profile(self, model: str | None = None) -> ModelCapabilityProfile | None:
        requested = str(model or "auto").strip()
        if requested in {"", "auto"}:
            return self.models[0] if self.models else None
        return next((item for item in self.models if item.model == requested), None)

    def public_projection(self, *, secret: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return a UI-safe provider/model projection.

        Credential target names and environment variable names are intentionally
        omitted.  ``secret`` may contain only the masked status fields produced
        by :class:`SecretStatus`.
        """

        models = [
            {
                "id": profile.model,
                "provider": profile.provider,
                "supports_tools": profile.supports_tools,
                "streaming": profile.streaming,
                "json_mode": profile.json_mode,
                "max_context": profile.max_context,
                "max_output": profile.max_output,
            }
            for profile in self.models
        ]
        result: dict[str, Any] = {
            "id": self.provider_id,
            "label": self.label,
            "protocol": self.protocol,
            "default_base_url": self.default_base_url,
            "default_model": self.default_model,
            "models": models,
        }
        if secret is not None:
            result["secret"] = {
                key: secret[key]
                for key in ("provider", "configured", "source", "editable")
                if key in secret
            }
        return result


_PROVIDERS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        provider_id="deepseek",
        label="DeepSeek",
        api_key_env="DEEPSEEK_API_KEY",
        credential_target="StataAgent/provider/deepseek/default",
        default_base_url="https://api.deepseek.com",
        models=(deepseek_chat_profile(),),
    ),
    ProviderDefinition(
        provider_id="qwen",
        label="通义千问 / DashScope",
        api_key_env="DASHSCOPE_API_KEY",
        credential_target="StataAgent/provider/qwen/default",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=(
            qwen_dashscope_profile(),
            _profile("qwen", "qwen-turbo", max_context=128_000, known_quirks=("OpenAI 兼容 v1",)),
        ),
    ),
    ProviderDefinition(
        provider_id="openai",
        label="OpenAI",
        api_key_env="OPENAI_API_KEY",
        credential_target="StataAgent/provider/openai/default",
        default_base_url="https://api.openai.com/v1",
        models=(
            _profile("openai", "gpt-4o-mini", max_context=128_000),
            _profile("openai", "gpt-4.1-mini", max_context=1_000_000),
        ),
    ),
    ProviderDefinition(
        provider_id="moonshot",
        label="Moonshot / Kimi",
        api_key_env="MOONSHOT_API_KEY",
        credential_target="StataAgent/provider/moonshot/default",
        default_base_url="https://api.moonshot.cn/v1",
        models=(
            _profile("moonshot", "moonshot-v1-8k", max_context=8_192),
            _profile("moonshot", "moonshot-v1-32k", max_context=32_768),
        ),
    ),
    ProviderDefinition(
        provider_id="zhipu",
        label="智谱 GLM",
        api_key_env="ZHIPUAI_API_KEY",
        credential_target="StataAgent/provider/zhipu/default",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        models=(
            _profile("zhipu", "glm-4-flash", max_context=128_000),
            _profile("zhipu", "glm-4-air", max_context=128_000),
        ),
    ),
    ProviderDefinition(
        provider_id="groq",
        label="Groq",
        api_key_env="GROQ_API_KEY",
        credential_target="StataAgent/provider/groq/default",
        default_base_url="https://api.groq.com/openai/v1",
        models=(
            _profile("groq", "llama-3.3-70b-versatile", max_context=131_072),
            _profile("groq", "mixtral-8x7b-32768", max_context=32_768),
        ),
    ),
    ProviderDefinition(
        provider_id="mistral",
        label="Mistral",
        api_key_env="MISTRAL_API_KEY",
        credential_target="StataAgent/provider/mistral/default",
        default_base_url="https://api.mistral.ai/v1",
        models=(
            _profile("mistral", "mistral-small-latest", max_context=32_768),
            _profile("mistral", "mistral-large-latest", max_context=128_000),
        ),
    ),
    ProviderDefinition(
        provider_id="openrouter",
        label="OpenRouter",
        api_key_env="OPENROUTER_API_KEY",
        credential_target="StataAgent/provider/openrouter/default",
        default_base_url="https://openrouter.ai/api/v1",
        models=(
            _profile("openrouter", "openai/gpt-4o-mini", max_context=128_000),
            _profile("openrouter", "anthropic/claude-3.5-sonnet", max_context=200_000),
        ),
    ),
)

PROVIDER_DEFINITIONS: tuple[ProviderDefinition, ...] = _PROVIDERS
PROVIDER_BY_ID: Mapping[str, ProviderDefinition] = MappingProxyType(
    {item.provider_id: item for item in _PROVIDERS}
)
PROVIDER_IDS = tuple(item.provider_id for item in _PROVIDERS)
MODEL_IDS = tuple(profile.model for item in _PROVIDERS for profile in item.models)
MODEL_TO_PROVIDER: Mapping[str, str] = MappingProxyType(
    {profile.model: item.provider_id for item in _PROVIDERS for profile in item.models}
)


def provider_definitions() -> tuple[ProviderDefinition, ...]:
    return PROVIDER_DEFINITIONS


def provider_ids() -> tuple[str, ...]:
    return PROVIDER_IDS


def provider_definition(provider: str) -> ProviderDefinition | None:
    return PROVIDER_BY_ID.get(str(provider or "").strip().lower())


def model_ids() -> tuple[str, ...]:
    return MODEL_IDS


def model_profile(model: str, *, provider: str | None = None) -> ModelCapabilityProfile | None:
    profile_provider = MODEL_TO_PROVIDER.get(str(model or "").strip())
    if profile_provider is None:
        return None
    if provider and profile_provider != str(provider).strip().lower():
        return None
    definition = PROVIDER_BY_ID[profile_provider]
    return definition.profile(model)


def default_profile(provider: str) -> ModelCapabilityProfile | None:
    definition = provider_definition(provider)
    return definition.profile() if definition else None


def provider_public_catalog(secret_statuses: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    statuses = secret_statuses or {}
    return [item.public_projection(secret=statuses.get(item.provider_id)) for item in PROVIDER_DEFINITIONS]


__all__ = [
    "MODEL_IDS",
    "MODEL_TO_PROVIDER",
    "PROVIDER_BY_ID",
    "PROVIDER_DEFINITIONS",
    "PROVIDER_IDS",
    "ProviderDefinition",
    "default_profile",
    "model_ids",
    "model_profile",
    "provider_definition",
    "provider_definitions",
    "provider_ids",
    "provider_public_catalog",
]
