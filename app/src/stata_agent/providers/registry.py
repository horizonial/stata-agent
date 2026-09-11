"""按闭合 provider/model 目录选 LLM（SPEC §4.2）+ 隐私门（§4.9.2/§4.10）。

目录顺序只决定 ``auto`` 的候选顺序；新增 provider 必须同时登记能力画像、
凭据目标和 OpenAI-compatible（或专用）适配器，不能靠任意模型字符串绕过校验。
隐私：默认 local_strict——有远端 key 也不自动用，须显式 STATA_AGENT_PRIVACY
∈ {approved_remote, mixed_sanitized} 授权后才会把研究文本发给远端模型。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ..privacy import modes as _privacy_modes
from .catalog import PROVIDER_IDS, model_profile, provider_definition
from .protocol import ProviderError

APPROVED_REMOTE = _privacy_modes.APPROVED_REMOTE
LOCAL_STRICT = _privacy_modes.LOCAL_STRICT
MIXED_SANITIZED = _privacy_modes.MIXED_SANITIZED
configured_mode = _privacy_modes.configured_mode
normalize_mode = _privacy_modes.normalize_mode


class PrivacyBlock(ProviderError):
    """local_strict 下拒绝使用远端 LLM（内容不出机）；需显式授权。"""

    def __init__(self, message: str | None = None) -> None:
        super().__init__("policy_denied", provider="provider", detail=message)


class LiveProviderDisabled(ProviderError):
    """A live network provider needs an explicit process-level opt-in."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__("provider_disabled", provider="provider", detail=message)


def _setting_value(settings: Any, key: str, default: Any = None) -> Any:
    """Read a value from either an EffectiveSettings or a plain mapping.

    The registry intentionally depends on this tiny duck-typed projection,
    rather than importing the application settings service.  This keeps the
    provider layer usable by the legacy CLI/tests and avoids a new orchestral
    dependency edge.
    """

    if settings is None:
        return default
    values = getattr(settings, "values", settings)
    if not isinstance(values, Mapping):
        return default
    raw = values.get(key, default)
    return getattr(raw, "value", raw)


def _settings_environment(settings: Any, environ: Mapping[str, object] | None) -> Mapping[str, object]:
    if environ is not None:
        return environ
    candidate = getattr(settings, "environ", None)
    return candidate if isinstance(candidate, Mapping) else os.environ


def _settings_dotenv(settings: Any, dotenv: Mapping[str, object] | None) -> Mapping[str, object] | None:
    if dotenv is not None:
        return dotenv
    candidate = getattr(settings, "dotenv", None)
    return candidate if isinstance(candidate, Mapping) else None


def _selected_provider(settings: Any | None) -> str:
    configured = _setting_value(settings, "provider.primary", None)
    if configured is None and settings is None:
        configured = os.environ.get("STATA_AGENT_PROVIDER", "auto")
    selected = str(configured or "auto").strip().lower()
    return selected if selected in PROVIDER_IDS or selected == "auto" else "auto"


def _selected_model(settings: Any | None) -> str:
    configured = _setting_value(settings, "provider.model", None)
    if configured is None and settings is None:
        configured = os.environ.get("STATA_AGENT_MODEL", "auto")
    return str(configured or "auto").strip()


def _provider_order(settings: Any | None) -> tuple[str, ...]:
    selected = _selected_provider(settings)
    model = _selected_model(settings)
    if model not in {"", "auto"}:
        profile = model_profile(model)
        if profile is not None:
            if selected == "auto":
                return (profile.provider,)
            if selected == profile.provider:
                return (selected,)
            return ()
        return ()
    if selected != "auto":
        return (selected,)
    return PROVIDER_IDS


def _credential_values(
    settings: Any | None,
    *,
    secret_store: Any | None,
    dotenv: Mapping[str, object] | None,
    environ: Mapping[str, object] | None,
) -> dict[str, object]:
    env = _settings_environment(settings, environ)
    dotenv_values = _settings_dotenv(settings, dotenv)
    if secret_store is not None:
        from ..settings.secret_store import resolve_secret_value

        return {
            provider: resolve_secret_value(provider, secret_store, dotenv=dotenv_values, environ=env)
            for provider in PROVIDER_IDS
        }
    values: dict[str, object] = {}
    for provider in PROVIDER_IDS:
        definition = provider_definition(provider)
        if definition is None:
            continue
        values[provider] = env.get(definition.api_key_env) or (dotenv_values or {}).get(definition.api_key_env)
    return values


def privacy_mode(settings: Any | None = None) -> str:
    """Return the configured privacy mode, failing closed on typos."""

    configured = _setting_value(settings, "privacy.mode", None)
    if configured is None:
        return configured_mode()
    try:
        return normalize_mode(str(configured))
    except Exception:  # noqa: BLE001 - invalid settings fail closed
        return LOCAL_STRICT


def _remote_available(
    settings: Any | None = None,
    *,
    secret_store: Any | None = None,
    dotenv: Mapping[str, object] | None = None,
    environ: Mapping[str, object] | None = None,
) -> tuple[str, bool]:
    """Return the selected catalog provider and whether its key is present."""

    credentials = _credential_values(
        settings,
        secret_store=secret_store,
        dotenv=dotenv,
        environ=environ,
    )
    for provider in _provider_order(settings):
        if str(credentials.get(provider) or "").strip():
            return provider, True
    return "", False


def _remote_candidates(
    settings: Any | None = None,
    *,
    secret_store: Any | None = None,
    dotenv: Mapping[str, object] | None = None,
    environ: Mapping[str, object] | None = None,
):
    """Construct configured catalog-backed candidates without network I/O."""

    from .deepseek import DeepSeekProvider, QwenProvider
    from .openai_compatible import OpenAICompatibleProvider

    keys = _credential_values(
        settings,
        secret_store=secret_store,
        dotenv=dotenv,
        environ=environ,
    )
    requested_model = _selected_model(settings)
    candidates = []
    for provider in _provider_order(settings):
        key = keys.get(provider)
        if not isinstance(key, str) or not key.strip():
            continue
        definition = provider_definition(provider)
        if definition is None:
            continue
        profile = definition.profile(requested_model)
        if profile is None:
            # ``auto`` is represented by the provider default profile.  A
            # model belonging to another provider is rejected rather than
            # silently changing the user's selection.
            continue
        base_url = _setting_value(settings, "provider.base_url", "") or ""
        if not base_url and settings is None:
            base_url = os.environ.get("STATA_AGENT_BASE_URL", "")
        if not base_url and provider == "deepseek":
            base_url = _setting_value(settings, "provider.deepseek.base_url", "") or ""
        if not base_url and provider == "qwen":
            base_url = _setting_value(settings, "provider.qwen.base_url", "") or ""
        base_url = str(base_url or definition.default_base_url)
        if provider == "deepseek":
            candidates.append(DeepSeekProvider(api_key=key, base_url=base_url, profile=profile))
        elif provider == "qwen":
            candidates.append(QwenProvider(api_key=key, base_url=base_url, profile=profile))
        else:
            candidates.append(
                OpenAICompatibleProvider(
                    provider_id=provider,
                    api_key=key,
                    base_url=base_url,
                    profile=profile,
                )
            )
    return candidates


def live_available(settings: Any | None = None, *, secret_store: Any | None = None) -> bool:
    """Return configuration readiness without probing the network."""
    from ..config import live_provider_enabled

    try:
        _, has = _remote_available(settings, secret_store=secret_store)
    except Exception:  # noqa: BLE001 - credential/backend failures fail closed
        has = False
    try:
        mode = normalize_mode(privacy_mode(settings))
    except Exception:  # noqa: BLE001 - an invalid mode is never live-enabled
        return False
    live = _setting_value(settings, "provider.live_enabled", None)
    enabled = live_provider_enabled() if live is None else bool(live)
    return has and mode != LOCAL_STRICT and enabled


def default_provider(settings: Any | None = None, *, secret_store: Any | None = None):
    from .deepseek import MissingApiKey

    try:
        provider, has_key = _remote_available(settings, secret_store=secret_store)
    except Exception as error:  # noqa: BLE001 - no backend details escape
        del error
        provider, has_key = "", False
    if not has_key:
        raise MissingApiKey("没有已配置的目录 provider 凭据")
    selected_mode = privacy_mode(settings)
    if selected_mode == LOCAL_STRICT:
        raise PrivacyBlock(
            f"local_strict 下不自动使用远端 LLM（{provider}）。"
            "如需把研究文本发给远端模型，请显式设 STATA_AGENT_PRIVACY=approved_remote（或 mixed_sanitized）。"
        )
    from ..config import live_provider_enabled

    configured_live = _setting_value(settings, "provider.live_enabled", None)
    if not (live_provider_enabled() if configured_live is None else bool(configured_live)):
        raise LiveProviderDisabled(
            "live provider 已配置但未显式启用；请设置 STATA_AGENT_LIVE=1。"
        )
    from .provider_check import ProviderRouter

    candidates = _remote_candidates(settings, secret_store=secret_store)
    if not candidates:
        raise MissingApiKey("没有可构造的 live provider")
    # ProviderRouter owns only transport retry/fallback.  AgentLoop remains
    # the sole model/tool orchestrator.
    return ProviderRouter(candidates, privacy_mode=selected_mode)
