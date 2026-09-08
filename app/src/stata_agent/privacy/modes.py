"""隐私三档（SPEC §4.10 / §4.9.2）。

- local_strict：PDF/数据/检索片段/模型调用均不出机（远端 LLM 也是数据出口 → 禁）。
- approved_remote：用户批准 provider/区域/保留政策/可发字段。
- mixed_sanitized：原始材料留本地，远端只收脱敏摘要/schema/证据 id。

规则：任何 fallback 不得"放松"隐私（本地 → 远端 = 新授权事件，不算普通降级）。
"""

from __future__ import annotations

LOCAL_STRICT = "local_strict"
APPROVED_REMOTE = "approved_remote"
MIXED_SANITIZED = "mixed_sanitized"
MODES = (LOCAL_STRICT, APPROVED_REMOTE, MIXED_SANITIZED)

# 出境排序：index 越大越"松"
_STRICTNESS = {LOCAL_STRICT: 0, MIXED_SANITIZED: 1, APPROVED_REMOTE: 2}


class PrivacyViolation(RuntimeError):
    pass


def is_remote(provider: str) -> bool:
    return provider in {"deepseek", "qwen", "openai", "gemini"}  # 远端 LLM = 数据出口


def fallback_allowed(from_mode: str, to_mode: str) -> bool:
    """降级不得放松隐私；同档/更严才允许。"""
    return _STRICTNESS[to_mode] <= _STRICTNESS[from_mode]


def assert_fallback_allowed(from_mode: str, to_mode: str) -> None:
    if not fallback_allowed(from_mode, to_mode):
        raise PrivacyViolation(f"禁止跨隐私边界的 fallback：{from_mode} → {to_mode}（需用户授权事件）")


def remote_llm_allowed(mode: str, provider: str) -> bool:
    """local_strict 下不许用远端模型处理研究内容（本地模型才可）。"""
    if not is_remote(provider):
        return True
    return mode in {APPROVED_REMOTE, MIXED_SANITIZED}
