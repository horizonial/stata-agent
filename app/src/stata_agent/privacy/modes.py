"""隐私三档（SPEC §4.10 / §4.9.2）。

- local_strict：PDF/数据/检索片段/模型调用均不出机（远端 LLM 也是数据出口 → 禁）。
- approved_remote：用户批准 provider/区域/保留政策/可发字段。
- mixed_sanitized：原始材料留本地，远端只收脱敏摘要/schema/证据 id。

规则：任何 fallback 不得"放松"隐私（本地 → 远端 = 新授权事件，不算普通降级）。
"""

from __future__ import annotations

import hashlib
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


_REMOTE_PROVIDERS = frozenset(
    {"deepseek", "qwen", "openai", "gemini", "anthropic", "claude", "remote", "cloud", "http", "https"}
)
_LOCAL_PROVIDERS = frozenset(
    {"local", "local-model", "mock", "fake", "ollama", "lmstudio", "llama.cpp"}
)


def provider_is_remote(provider: Any) -> bool | None:
    """Return provider locality, or ``None`` when it is not declared.

    A concrete provider may declare ``is_remote`` explicitly.  Named remote
    providers are retained for backwards compatibility.  Unknown *named*
    providers are deliberately indeterminate so a strict privacy gate can
    reject them; legacy test doubles without a provider name are treated as
    local and cannot perform a network request by themselves.
    """

    try:
        explicit = getattr(provider, "is_remote", None)
    except Exception:  # noqa: BLE001 - an uninspectable provider fails closed
        return None
    if isinstance(explicit, bool):
        return explicit
    try:
        name = getattr(provider, "provider", None) if not isinstance(provider, str) else provider
    except Exception:  # noqa: BLE001 - an uninspectable provider fails closed
        return None
    if name is None and not isinstance(provider, str):
        # Existing offline providers often implement only ``chat``.  Keep
        # those callers compatible; production live providers must declare a
        # provider name or locality.
        return False
    normalized = str(name or "").strip().lower()
    if normalized in _REMOTE_PROVIDERS:
        return True
    if normalized in _LOCAL_PROVIDERS:
        return False
    return None


def is_remote(provider: Any) -> bool:
    """Compatibility boolean for callers that only need a positive signal."""

    return provider_is_remote(provider) is True


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


def remote_llm_allowed(mode: str, provider: Any) -> bool:
    """Apply the privacy gate with unknown named providers failing closed."""
    try:
        mode = normalize_mode(mode)
    except PrivacyViolation:
        return False
    locality = provider_is_remote(provider)
    if locality is None:
        return False
    if not locality:
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


def _sanitized_marker(text: Any, *, label: str) -> str:
    """Return metadata only for a mixed-mode remote payload.

    ``sanitize_text`` is intentionally useful for approved local processing,
    but redacting only obvious secrets is not sufficient for
    ``mixed_sanitized``: research values and prose are sensitive even when no
    credential-looking token is present.  Mixed mode therefore sends a stable
    digest/length marker for untrusted text.  The local context assembler can
    still provide explicitly safe summaries through ``_privacy_safe``.
    """

    raw = str(text if text is not None else "")
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"[{label} sanitized sha256={digest} chars={len(raw)}]"


def _sanitize_remote_value(value: Any, *, label: str, key: str = "") -> Any:
    if isinstance(value, str):
        return _sanitized_marker(value, label=label)
    if isinstance(value, dict):
        return {
            str(k): _sanitize_remote_value(v, label=f"{label}.{k}", key=str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_remote_value(v, label=f"{label}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, tuple):
        return [_sanitize_remote_value(v, label=f"{label}[{i}]") for i, v in enumerate(value)]
    return value


def sanitize_tool_schemas(tools: list[dict], *, mode: str, provider: Any) -> list[dict]:
    """Sanitize untrusted text embedded in tool schemas for mixed mode.

    Tool names, JSON keys and protocol ``type`` fields remain available to the
    remote model; descriptions, defaults and enum values are metadata-only.
    """

    mode = normalize_mode(mode)
    if mode != MIXED_SANITIZED or provider_is_remote(provider) is not True:
        return tools
    structural_keys = {"type", "name", "role", "id", "tool_call_id", "properties"}

    def scrub(value: Any, *, label: str, key: str = "") -> Any:
        if isinstance(value, str):
            if key in structural_keys:
                return value
            return _sanitized_marker(value, label=label)
        if isinstance(value, dict):
            return {str(k): scrub(v, label=f"{label}.{k}", key=str(k)) for k, v in value.items()}
        if isinstance(value, list):
            if key == "required":
                # Required property names are schema structure, not research
                # values; preserving them keeps the remote tool contract
                # executable after sanitization.
                return [str(item) for item in value]
            return [scrub(v, label=f"{label}[{i}]") for i, v in enumerate(value)]
        if isinstance(value, tuple):
            return [scrub(v, label=f"{label}[{i}]") for i, v in enumerate(value)]
        return value

    return [scrub(tool, label=f"tool.{i}") for i, tool in enumerate(tools)]


def sanitize_messages(messages: list[dict], *, mode: str, provider: Any) -> list[dict]:
    """Return the prompt representation permitted for a provider.

    ``approved_remote`` preserves the existing API contract.  In
    ``mixed_sanitized`` system instructions remain available after secret/path
    redaction, while untrusted user/assistant/tool text is represented only by
    a stable digest/length marker; protocol names/roles remain so the remote
    model can still follow function-calling.  Local providers are returned
    unchanged in either mode.
    """

    mode = normalize_mode(mode)
    if mode != MIXED_SANITIZED or provider_is_remote(provider) is not True:
        return messages
    out: list[dict] = []
    for index, message in enumerate(messages):
        copied = dict(message)
        role = str(copied.get("role") or "").lower()
        safe_message = bool(copied.get("_privacy_safe")) or role == "system"
        if "content" in copied and copied.get("content") is not None:
            copied["content"] = (
                sanitize_text(copied["content"], label=f"message.{index}")
                if safe_message
                else _sanitized_marker(copied["content"], label=f"message.{index}")
            )
        if isinstance(copied.get("tool_calls"), list):
            calls: list[dict] = []
            for call_index, call in enumerate(copied["tool_calls"]):
                call_copy = dict(call)
                function = dict(call_copy.get("function") or {})
                if "arguments" in function:
                    raw_args = function["arguments"]
                    function["arguments"] = _sanitized_marker(
                        raw_args, label=f"message.{index}.tool.{call_index}"
                    )
                call_copy["function"] = function
                calls.append(call_copy)
            copied["tool_calls"] = calls
        # Scrub custom/nested message fields as a defence-in-depth measure;
        # standard protocol identity fields remain intact.
        protocol_fields = {"role", "tool_call_id", "name", "id", "type", "tool_calls", "content"}
        for key, value in list(copied.items()):
            if key not in protocol_fields and key != "_privacy_safe":
                copied[key] = _sanitize_remote_value(value, label=f"message.{index}.{key}")
        copied.pop("_privacy_safe", None)
        out.append(copied)
    return out
