"""按环境选 LLM provider（SPEC §4.2）+ 隐私门（§4.9.2/§4.10）。

优先级：DEEPSEEK_API_KEY → deepseek；否则 DASHSCOPE_API_KEY → qwen(百炼)。
隐私：默认 local_strict——有远端 key 也不自动用，须显式 STATA_AGENT_PRIVACY
∈ {approved_remote, mixed_sanitized} 授权后才会把研究文本发给远端模型。
"""

from __future__ import annotations

import os

from ..privacy import modes as _privacy_modes

APPROVED_REMOTE = _privacy_modes.APPROVED_REMOTE
LOCAL_STRICT = _privacy_modes.LOCAL_STRICT
MIXED_SANITIZED = _privacy_modes.MIXED_SANITIZED
configured_mode = _privacy_modes.configured_mode
normalize_mode = _privacy_modes.normalize_mode


class PrivacyBlock(RuntimeError):
    """local_strict 下拒绝使用远端 LLM（内容不出机）；需显式授权。"""


def privacy_mode() -> str:
    """Return the configured privacy mode, failing closed on typos."""

    return configured_mode()


def _remote_available() -> tuple[str, bool]:
    """返回 (provider, has_key)；远端 provider 用到的 key 是否存在。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "deepseek", True
    if os.environ.get("DASHSCOPE_API_KEY"):
        return "qwen", True
    return "", False


def live_available() -> bool:
    """UI/测试判据：有远端 key 且当前隐私模式允许发远端。"""
    _, has = _remote_available()
    try:
        mode = normalize_mode(privacy_mode())
    except Exception:  # noqa: BLE001 - an invalid mode is never live-enabled
        return False
    return has and mode != LOCAL_STRICT


def default_provider():
    from .deepseek import DeepSeekProvider, MissingApiKey, QwenProvider

    provider, has_key = _remote_available()
    if not has_key:
        raise MissingApiKey("既无 DEEPSEEK_API_KEY 也无 DASHSCOPE_API_KEY（设置其一）")
    if privacy_mode() == LOCAL_STRICT:
        raise PrivacyBlock(
            f"local_strict 下不自动使用远端 LLM（{provider}）。"
            "如需把研究文本发给远端模型，请显式设 STATA_AGENT_PRIVACY=approved_remote（或 mixed_sanitized）。"
        )
    if provider == "deepseek":
        return DeepSeekProvider()
    return QwenProvider()
