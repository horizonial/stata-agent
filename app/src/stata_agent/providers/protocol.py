"""LLM 聚合层契约（SPEC §4.2）。

The project has two provider entry points for compatibility: the old
``propose`` API and the live chat/tool API used by ``agent_loop``.  Keep the
protocols small, but make provider failures typed and safe at this boundary so
transport details never become user-facing ledger data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..domain.action import ActionProposal


SAFE_PROVIDER_CODES = frozenset(
    {
        "auth",
        "rate_limited",
        "timeout",
        "unavailable",
        "context_overflow",
        "protocol",
        "invalid_response",
        "policy_denied",
        "credentials_missing",
        "provider_disabled",
        "provider_unavailable",
        "provider_error",
    }
)


_SAFE_MESSAGES = {
    "auth": "远端模型鉴权失败。",
    "rate_limited": "远端模型暂时限流。",
    "timeout": "远端模型请求超时。",
    "unavailable": "远端模型暂时不可用。",
    "context_overflow": "远端模型拒绝了过大的上下文。",
    "protocol": "远端模型返回格式异常。",
    "invalid_response": "远端模型返回无效响应。",
    "policy_denied": "当前隐私策略不允许调用该模型。",
    "credentials_missing": "尚未配置模型凭据，请在应用设置中填写 API Key。",
    "provider_disabled": "已检测到模型凭据，但远端模型调用尚未启用。",
    "provider_unavailable": "没有可用的模型 provider。",
    "provider_error": "模型调用失败。",
}

_TRANSIENT_CODES = frozenset({"rate_limited", "timeout", "unavailable"})


@dataclass(frozen=True)
class ProviderErrorInfo:
    """Allow-listed provider error metadata suitable for an audit event."""

    code: str
    retryable: bool
    provider: str = ""
    status_code: int | None = None
    attempt: int = 0
    request_id: str | None = None


class ProviderError(RuntimeError):
    """A safe, typed provider failure.

    ``ProviderError`` deliberately never stores the source exception text in
    ``args``/``str``.  Callers may chain the original exception for local
    debugging, but replies, events and SSE consumers can safely use this
    object directly.  ``detail`` is accepted only for compatibility with
    callers that construct transport errors; it is not exposed.
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool | None = None,
        provider: str = "",
        status_code: int | None = None,
        attempt: int = 0,
        request_id: str | None = None,
        detail: Any = None,
    ) -> None:
        normalized = str(code or "provider_error").strip().lower()
        if normalized not in SAFE_PROVIDER_CODES:
            normalized = "provider_error"
        self.code = normalized
        self.retryable = normalized in _TRANSIENT_CODES if retryable is None else bool(retryable)
        self.provider = str(provider or "").strip().lower()
        self.status_code = int(status_code) if isinstance(status_code, int) else None
        self.attempt = max(0, int(attempt or 0))
        self.request_id = request_id if isinstance(request_id, str) else None
        # Keep a deliberately non-serializable/private debug hook out of the
        # exception message.  It is not consumed by application code.
        self._debug_detail = detail
        super().__init__(_SAFE_MESSAGES[self.code])

    @property
    def safe_message(self) -> str:
        return _SAFE_MESSAGES[self.code]

    def info(self) -> ProviderErrorInfo:
        return ProviderErrorInfo(
            code=self.code,
            retryable=self.retryable,
            provider=self.provider,
            status_code=self.status_code,
            attempt=self.attempt,
            request_id=self.request_id,
        )


class ChatProvider(Protocol):
    """Minimal live provider contract used by the canonical agent loop."""

    provider: str
    is_remote: bool

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None, **kwargs: Any) -> dict:
        ...


def provider_error_from_exception(
    error: BaseException,
    *,
    provider: str = "",
    attempt: int = 0,
    request_id: str | None = None,
) -> ProviderError:
    """Classify heterogeneous transport/protocol errors without exposing them.

    Classification intentionally uses only stable status/type markers.  The
    original text is never copied into the returned error.
    """

    if isinstance(error, ProviderError):
        error_provider = error.provider or str(provider or "").strip().lower()
        error_attempt = error.attempt or max(0, int(attempt or 0))
        error_request_id = error.request_id or request_id
        if (
            error_provider == error.provider
            and error_attempt == error.attempt
            and error_request_id == error.request_id
        ):
            return error
        return ProviderError(
            error.code,
            retryable=error.retryable,
            provider=error_provider,
            status_code=error.status_code,
            attempt=error_attempt,
            request_id=error_request_id,
        )
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        status = getattr(error, "code", None)
    if not isinstance(status, int):
        status = None
    name = type(error).__name__.lower()
    text = str(error).lower()
    if status in {401, 403} or "authentication" in name or "unauthorized" in text:
        code, retryable = "auth", False
    elif status == 429 or "rate" in text or "throttl" in text:
        code, retryable = "rate_limited", True
    elif status in {408, 504} or "timeout" in name or "timed out" in text:
        code, retryable = "timeout", True
    elif status is not None and 500 <= status <= 599:
        code, retryable = "unavailable", True
    elif "context_length" in text or "context length" in text or "too many tokens" in text:
        code, retryable = "context_overflow", False
    elif "json" in name or "protocol" in name or "protocol" in text:
        code, retryable = "protocol", False
    elif isinstance(error, (ValueError, KeyError, TypeError, IndexError, UnicodeError)):
        code, retryable = "invalid_response", False
    elif isinstance(error, (ConnectionError, TimeoutError)) or name in {
        "urlerror", "connectionerror", "connectionreseterror", "brokenpipeerror"
    }:
        code, retryable = "unavailable", True
    elif name == "missingapikey":
        code, retryable = "credentials_missing", False
    elif name == "liveproviderdisabled":
        code, retryable = "provider_disabled", False
    elif name == "privacyblock":
        code, retryable = "policy_denied", False
    else:
        code, retryable = "provider_error", False
    return ProviderError(
        code,
        retryable=retryable,
        provider=provider,
        status_code=status,
        attempt=attempt,
        request_id=request_id,
        detail=error,
    )


class ProposalProvider(Protocol):
    """给出一次研究动作提议（模型侧）。实现可 mock / deepseek / 其它。"""

    def propose(self, context: str) -> ActionProposal:
        ...
