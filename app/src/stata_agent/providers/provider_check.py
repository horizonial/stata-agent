"""Live provider policy, bounded retry and fallback helpers.

This module is deliberately a transport adapter, not a second agent
orchestrator.  The canonical loop still decides when to call a provider and
when to execute a tool.  ``ProviderRouter`` only makes a single logical
provider step safe: it freezes one privacy projection, allows at most two
physical attempts, and falls back only for typed transient failures.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
import inspect
import json
import time
from typing import Any

from ..privacy.modes import (
    MIXED_SANITIZED,
    LOCAL_STRICT,
    normalize_mode,
    provider_is_remote,
    remote_llm_allowed,
    sanitize_messages,
    sanitize_tool_schemas,
)
from .protocol import ProviderError, provider_error_from_exception


FallbackCallback = Callable[[dict[str, Any]], bool | None]


def _name(provider: Any) -> str:
    return str(
        getattr(provider, "provider", None)
        or getattr(provider, "provider_name", None)
        or provider.__class__.__name__
    ).strip().lower()


class ProviderRouter:
    """A bounded, privacy-aware route over one primary and optional fallback.

    ``chat`` and ``stream_chat`` retain the existing provider surface.  Call
    :meth:`begin_step` before each new logical model step; an overflow retry
    from ``agent_loop`` intentionally does *not* call it again, so the two
    physical-attempt budget is shared with the outer loop.
    """

    provider = "provider"
    is_remote = False

    def __init__(
        self,
        providers: Iterable[Any],
        *,
        privacy_mode: str = LOCAL_STRICT,
        request_id: str | None = None,
        max_attempts: int = 2,
    ) -> None:
        candidates = tuple(provider for provider in providers if provider is not None)
        if not candidates:
            raise ProviderError("provider_unavailable", provider="", request_id=request_id)
        self._providers = candidates
        self.privacy_mode = normalize_mode(privacy_mode)
        self.request_id = request_id
        self.max_attempts = max(1, min(2, int(max_attempts)))
        self._attempts_used = 0
        self._index = 0
        self._pending_index: int | None = None
        self._fallback_callback: FallbackCallback | None = None
        self._last_fallback: dict[str, Any] | None = None
        self._set_identity(candidates[0])

    def _set_identity(self, provider: Any) -> None:
        self.provider = _name(provider)
        self.provider_name = self.provider
        self.is_remote = provider_is_remote(provider) is True
        self.profile = getattr(provider, "profile", None)

    @property
    def attempts_used(self) -> int:
        return self._attempts_used

    @property
    def last_fallback(self) -> dict[str, Any] | None:
        return dict(self._last_fallback) if self._last_fallback else None

    def begin_step(self) -> None:
        """Start a fresh logical provider step and reset its attempt budget."""

        self._attempts_used = 0
        self._index = 0
        self._pending_index = None
        self._last_fallback = None
        self._set_identity(self._providers[0])

    def set_fallback_callback(self, callback: FallbackCallback | None) -> None:
        self._fallback_callback = callback

    def set_privacy_mode(self, mode: str) -> None:
        """Update the request-scoped privacy policy before the next step."""

        self.privacy_mode = normalize_mode(mode)

    def set_request_id(self, request_id: str | None) -> None:
        """Attach the application turn id to fallback/error metadata."""

        self.request_id = request_id if isinstance(request_id, str) else None

    def _allowed(self, provider: Any) -> bool:
        locality = provider_is_remote(provider)
        if locality is None:
            # A named but undeclared provider is not safe to use in any route.
            return False
        return remote_llm_allowed(self.privacy_mode, provider)

    def _prepared(
        self,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> tuple[list[dict], list[dict] | None]:
        """Freeze one payload before trying primary/fallback providers."""

        remote_route = any(provider_is_remote(item) is True for item in self._providers)
        if self.privacy_mode == MIXED_SANITIZED and remote_route:
            # Use one provider-independent remote projection for every
            # candidate.  Re-sanitizing per candidate would create different
            # digests and make fallback observably change the prompt.
            prepared_messages = sanitize_messages(messages, mode=self.privacy_mode, provider="deepseek")
            prepared_tools = sanitize_tool_schemas(
                list(tools or []), mode=self.privacy_mode, provider="deepseek"
            ) if tools is not None else None
            return prepared_messages, prepared_tools
        return messages, tools

    def _error(self, error: BaseException, provider: Any) -> ProviderError:
        return provider_error_from_exception(
            error,
            provider=_name(provider),
            attempt=self._attempts_used,
            request_id=self.request_id,
        )

    def _record_fallback(self, from_provider: Any, to_provider: Any, error: ProviderError) -> None:
        event = {
            "from_provider": _name(from_provider),
            "to_provider": _name(to_provider),
            "reason_code": error.code,
            "attempt": self._attempts_used,
            "privacy_mode": self.privacy_mode,
            "request_id": self.request_id,
        }
        self._last_fallback = event
        callback = self._fallback_callback
        if callback is not None:
            try:
                accepted = callback(dict(event))
            except Exception as error:  # noqa: BLE001 - audit failure fails closed
                raise ProviderError(
                    "policy_denied",
                    provider=_name(to_provider),
                    attempt=self._attempts_used,
                    request_id=self.request_id,
                    detail=error,
                ) from None
            if accepted is False:
                raise ProviderError(
                    "policy_denied",
                    provider=_name(to_provider),
                    attempt=self._attempts_used,
                    request_id=self.request_id,
                )

    def _next_index(self, index: int) -> int:
        # Prefer the next explicitly configured provider once; otherwise retry
        # the same provider.  Either branch is still bounded by max_attempts.
        return index + 1 if index + 1 < len(self._providers) else index

    def _call_candidate(
        self,
        provider: Any,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        json_mode: bool = False,
    ) -> dict:
        if not self._allowed(provider):
            raise ProviderError(
                "policy_denied",
                provider=_name(provider),
                attempt=self._attempts_used,
                request_id=self.request_id,
            )
        chat = getattr(provider, "chat", None)
        if not callable(chat):
            raise ProviderError(
                "provider_unavailable",
                provider=_name(provider),
                attempt=self._attempts_used,
                request_id=self.request_id,
            )
        self._attempts_used += 1
        try:
            # Inspect the callable instead of probing with a second call:
            # compatibility adaptation must not consume another physical
            # attempt or duplicate a remote request.
            accepts_kwargs = False
            accepts_json_mode = False
            try:
                parameters = inspect.signature(chat).parameters.values()
                accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters)
                accepts_json_mode = "json_mode" in inspect.signature(chat).parameters
            except (TypeError, ValueError):
                accepts_kwargs = True
            call_kwargs: dict[str, Any] = {"tools": tools}
            if json_mode and (accepts_kwargs or accepts_json_mode):
                call_kwargs["json_mode"] = True
            response = chat(messages, **call_kwargs)
        except BaseException as error:
            raise self._error(error, provider) from None
        if not isinstance(response, dict):
            raise ProviderError(
                "invalid_response",
                provider=_name(provider),
                attempt=self._attempts_used,
                request_id=self.request_id,
            )
        self._set_identity(provider)
        return response

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None, **kwargs: Any) -> dict:
        prepared_messages, prepared_tools = self._prepared(messages, tools)
        json_mode = bool(kwargs.get("json_mode", False))
        index = self._pending_index if self._pending_index is not None else self._index
        while self._attempts_used < self.max_attempts:
            provider = self._providers[index]
            try:
                result = self._call_candidate(
                    provider,
                    prepared_messages,
                    prepared_tools,
                    json_mode=json_mode,
                )
                self._index = index
                self._pending_index = None
                return result
            except ProviderError as error:
                if not error.retryable or self._attempts_used >= self.max_attempts:
                    self._pending_index = index
                    raise
                next_index = self._next_index(index)
                if next_index != index:
                    self._record_fallback(provider, self._providers[next_index], error)
                self._pending_index = next_index
                index = next_index
        raise ProviderError(
            "provider_unavailable",
            provider=self.provider,
            attempt=self._attempts_used,
            request_id=self.request_id,
        )

    def stream_chat(self, messages: list[dict], *, tools: list[dict] | None = None, **kwargs: Any):
        """Stream one route; never retry/fallback after the first text delta."""

        prepared_messages, prepared_tools = self._prepared(messages, tools)
        del kwargs

        def generate():
            index = self._pending_index if self._pending_index is not None else self._index
            while self._attempts_used < self.max_attempts:
                provider = self._providers[index]
                if not self._allowed(provider):
                    raise ProviderError(
                        "policy_denied",
                        provider=_name(provider),
                        attempt=self._attempts_used,
                        request_id=self.request_id,
                    )
                stream = getattr(provider, "stream_chat", None)
                if not callable(stream):
                    raise ProviderError(
                        "provider_unavailable",
                        provider=_name(provider),
                        attempt=self._attempts_used,
                        request_id=self.request_id,
                    )
                self._attempts_used += 1
                saw_delta = False
                try:
                    terminal = False
                    for event in stream(prepared_messages, tools=prepared_tools):
                        if not isinstance(event, dict):
                            raise ProviderError(
                                "protocol",
                                provider=_name(provider),
                                attempt=self._attempts_used,
                                request_id=self.request_id,
                            )
                        if event.get("type") == "text_delta":
                            saw_delta = True
                        if event.get("type") == "done":
                            terminal = True
                        yield event
                    if not terminal:
                        raise ProviderError(
                            "protocol",
                            provider=_name(provider),
                            attempt=self._attempts_used,
                            request_id=self.request_id,
                        )
                    self._set_identity(provider)
                    self._pending_index = None
                    return
                except ProviderError as error:
                    safe_error = self._error(error, provider)
                    if saw_delta or not safe_error.retryable or self._attempts_used >= self.max_attempts:
                        raise safe_error from None
                    next_index = self._next_index(index)
                    if next_index != index:
                        self._record_fallback(provider, self._providers[next_index], safe_error)
                    self._pending_index = next_index
                    index = next_index
                except BaseException as error:
                    safe_error = self._error(error, provider)
                    if saw_delta or not safe_error.retryable or self._attempts_used >= self.max_attempts:
                        raise safe_error from None
                    next_index = self._next_index(index)
                    if next_index != index:
                        self._record_fallback(provider, self._providers[next_index], safe_error)
                    self._pending_index = next_index
                    index = next_index
            raise ProviderError(
                "provider_unavailable",
                provider=self.provider,
                attempt=self._attempts_used,
                request_id=self.request_id,
            )

        return generate()


class _OfflineProvider:
    is_remote = True

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.provider = name
        self.fail = fail
        self.calls = 0

    def chat(self, messages, *, tools=None):
        del messages, tools
        self.calls += 1
        if self.fail:
            raise TimeoutError("offline synthetic timeout")
        return {"content": "ok"}


def run_offline_acceptance() -> dict[str, Any]:
    """Run deterministic transport-policy checks without network access."""

    denied = _OfflineProvider("deepseek")
    denied_code = ""
    try:
        ProviderRouter([denied], privacy_mode=LOCAL_STRICT).chat([{"role": "user", "content": "canary"}])
    except ProviderError as error:
        denied_code = error.code

    primary = _OfflineProvider("deepseek", fail=True)
    fallback = _OfflineProvider("qwen")
    router = ProviderRouter([primary, fallback], privacy_mode=MIXED_SANITIZED)
    response = router.chat([{"role": "user", "content": "offline canary"}])
    checks = {
        "local_strict_zero_io": denied.calls == 0 and denied_code == "policy_denied",
        "bounded_attempts": router.attempts_used == 2 and primary.calls == 1 and fallback.calls == 1,
        "fallback_success": response.get("content") == "ok" and router.last_fallback is not None,
    }
    return {"schema": "provider.acceptance.v1", "offline": True, "checks": checks, "ok": all(checks.values())}


def run_live_acceptance() -> dict[str, Any]:
    """Send only a fixed non-sensitive canary after all explicit live gates."""

    from .registry import default_provider

    started = time.monotonic()
    provider = default_provider()
    try:
        response = provider.chat([
            {"role": "system", "content": "Return exactly OK."},
            {"role": "user", "content": "STATA_AGENT_PROVIDER_CANARY_V1"},
        ])
        ok = isinstance(response, dict)
        code = "ok" if ok else "invalid_response"
    except ProviderError as error:
        ok = False
        code = error.code
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    return {
        "schema": "provider.acceptance.v1",
        "offline": False,
        "ok": ok,
        "code": code,
        "provider": str(getattr(provider, "provider", "provider")),
        "attempts": int(getattr(provider, "attempts_used", 1)),
        "latency_bucket_ms": min(60000, (elapsed_ms // 100) * 100),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bounded provider acceptance gate.")
    parser.add_argument("--live", action="store_true", help="explicitly run the configured live canary")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    result = run_live_acceptance() if args.live else run_offline_acceptance()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())


__all__ = ["ProviderRouter", "main", "run_live_acceptance", "run_offline_acceptance"]
