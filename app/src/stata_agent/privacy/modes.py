"""隐私三档（SPEC §4.10 / §4.9.2）。

- local_strict：PDF/数据/检索片段/模型调用均不出机（远端 LLM 也是数据出口 → 禁）。
- approved_remote：用户批准 provider/区域/保留政策/可发字段。
- mixed_sanitized：原始材料留本地，远端只收脱敏摘要/schema/证据 id。

规则：任何 fallback 不得"放松"隐私（本地 → 远端 = 新授权事件，不算普通降级）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from enum import Enum
from typing import Any


class PrivacyMode(str, Enum):
    """Closed set of supported privacy modes.

    The string constants below are retained as the public API used by the
    existing CLI and tests.  ``PrivacyMode`` gives callers an explicit enum
    while ``normalize_mode`` makes configuration parsing fail closed.
    """

    LOCAL_STRICT = "local_strict"
    APPROVED_REMOTE = "approved_remote"
    MIXED_SANITIZED = "mixed_sanitized"


LOCAL_STRICT = PrivacyMode.LOCAL_STRICT.value
APPROVED_REMOTE = PrivacyMode.APPROVED_REMOTE.value
MIXED_SANITIZED = PrivacyMode.MIXED_SANITIZED.value
MODES = tuple(mode.value for mode in PrivacyMode)

# 出境排序：index 越大越"松"
_STRICTNESS = {LOCAL_STRICT: 0, MIXED_SANITIZED: 1, APPROVED_REMOTE: 2}


class PrivacyViolation(RuntimeError):
    pass


def normalize_mode(mode: str | PrivacyMode | None) -> str:
    """Return a valid mode or raise instead of silently relaxing privacy."""

    value = mode.value if isinstance(mode, PrivacyMode) else str(mode or "").strip()
    if value not in MODES:
        raise PrivacyViolation(
            f"未知隐私模式 {value!r}；仅允许 {', '.join(MODES)}"
        )
    return value


def configured_mode(*, environ: dict[str, str] | None = None) -> str:
    """Read ``STATA_AGENT_PRIVACY`` with a local-strict fail-closed default.

    Unknown values deliberately map to ``local_strict``.  This function is
    used at configuration boundaries where raising would make a health check
    or UI unavailable; callers that need diagnostics can call
    :func:`normalize_mode` directly first.
    """

    env = environ if environ is not None else os.environ
    raw = env.get("STATA_AGENT_PRIVACY", LOCAL_STRICT)
    try:
        return normalize_mode(raw)
    except PrivacyViolation:
        return LOCAL_STRICT


def is_remote(provider: str) -> bool:
    return str(provider or "").strip().lower() in {"deepseek", "qwen", "openai", "gemini"}  # 远端 LLM = 数据出口


def fallback_allowed(from_mode: str, to_mode: str) -> bool:
    """降级不得放松隐私；同档/更严才允许。"""
    try:
        source = normalize_mode(from_mode)
        target = normalize_mode(to_mode)
    except PrivacyViolation:
        return False
    return _STRICTNESS[target] <= _STRICTNESS[source]


def assert_fallback_allowed(from_mode: str, to_mode: str) -> None:
    if not fallback_allowed(from_mode, to_mode):
        raise PrivacyViolation(f"禁止跨隐私边界的 fallback：{from_mode} → {to_mode}（需用户授权事件）")


def remote_llm_allowed(mode: str, provider: str) -> bool:
    """local_strict 下不许用远端模型处理研究内容（本地模型才可）。"""
    try:
        mode = normalize_mode(mode)
    except PrivacyViolation:
        return False
    if not is_remote(provider):
        return True
    return mode in {APPROVED_REMOTE, MIXED_SANITIZED}


# These patterns intentionally trade recall for safety.  A false positive only
# removes a small piece of text from a remote prompt; a false negative could
# disclose credentials or a local data location.
_SECRET_LINE = re.compile(
    r"(?is)([^\n]{0,80}\b(?:api[ _-]?key|secret|password|passwd|token|authorization|bearer)\b"
    r"[^\n]{0,240})"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY = re.compile(r"(?i)\b(?:sk|rk|ak|key)[-_][A-Za-z0-9._~+/=-]{12,}\b")
_ABS_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\|/(?:Users|home|tmp|var|mnt|data|private)/)[^\s\"'<>`]+"
)
_URL_CREDENTIAL = re.compile(r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@")


def sanitize_text(text: Any, *, label: str = "text", limit: int = 4000) -> str:
    """Conservatively sanitize text before it crosses a mixed-mode boundary.

    The original text is never returned by reference.  Redaction covers common
    credentials, bearer keys, local absolute paths and URL credentials, and a
    short digest lets the model correlate repeated snippets without exposing
    the source value itself.  The function is deterministic and has no network
    side effects.
    """

    raw = str(text if text is not None else "")
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    safe = _SECRET_LINE.sub("[REDACTED_SECRET]", raw)
    safe = _BEARER.sub("Bearer [REDACTED]", safe)
    safe = _KEY.sub("[REDACTED_KEY]", safe)
    safe = _URL_CREDENTIAL.sub(r"\1[REDACTED]@", safe)
    safe = _ABS_PATH.sub("[LOCAL_PATH]", safe)
    safe = safe[: max(0, int(limit))]
    return f"[{label} sanitized sha256={digest}]\n{safe}"


def _sanitize_value(value: Any, *, label: str, limit: int = 4000) -> Any:
    if isinstance(value, str):
        return sanitize_text(value, label=label, limit=limit)
    if isinstance(value, dict):
        return {
            str(k): _sanitize_value(v, label=f"{label}.{k}", limit=limit)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(v, label=f"{label}[{i}]", limit=limit) for i, v in enumerate(value)]
    if isinstance(value, tuple):
        return [_sanitize_value(v, label=f"{label}[{i}]", limit=limit) for i, v in enumerate(value)]
    return value


def sanitize_messages(messages: list[dict], *, mode: str, provider: str) -> list[dict]:
    """Return the prompt representation permitted for a provider.

    ``approved_remote`` preserves the existing API contract.  In
    ``mixed_sanitized`` every textual field and tool argument is replaced by a
    redacted, labelled representation; tool names/roles remain so the remote
    model can still follow the function-calling protocol.  Local providers are
    returned unchanged in either mode.
    """

    mode = normalize_mode(mode)
    if mode != MIXED_SANITIZED or not is_remote(provider):
        return messages
    out: list[dict] = []
    for index, message in enumerate(messages):
        copied = dict(message)
        if "content" in copied and copied.get("content") is not None:
            copied["content"] = sanitize_text(copied["content"], label=f"message.{index}")
        if isinstance(copied.get("tool_calls"), list):
            calls: list[dict] = []
            for call_index, call in enumerate(copied["tool_calls"]):
                call_copy = dict(call)
                function = dict(call_copy.get("function") or {})
                if "arguments" in function:
                    raw_args = function["arguments"]
                    try:
                        parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        function["arguments"] = json.dumps(
                            _sanitize_value(
                                parsed,
                                label=f"message.{index}.tool.{call_index}",
                            ),
                            ensure_ascii=False,
                        )
                    except (TypeError, json.JSONDecodeError):
                        function["arguments"] = sanitize_text(
                            raw_args, label=f"message.{index}.tool.{call_index}"
                        )
                call_copy["function"] = function
                calls.append(call_copy)
            copied["tool_calls"] = calls
        out.append(copied)
    return out
