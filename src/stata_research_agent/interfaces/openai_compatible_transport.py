"""Credential-isolating OpenAI-compatible chat transport."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from stata_research_agent.application.model_gateway import (
    ProviderDispatchError,
    ProviderResponse,
)

_OUTPUT_CONTRACT = """
Return exactly one JSON object and no Markdown.
The object has:
- text: string
- tool_calls: array of {"name": string, "arguments": object}
- optional evaluation: {"verdict": "pass"|"warn"|"fail"|"unknown",
  "findings": array of registered finding-code strings}
- optional waiting: {"reason": "user_input"|"user_confirmation"|"external_resolution",
  "prompt": string}
- optional completion: {"disposition": "succeed"|"partial"|"pause"|"fail",
  "summary": string}
Use tool_calls when a tool is needed. Only propose completion when the supplied
context proves the requested obligation is complete. Never invent a tool result.
Use waiting, without tool_calls or completion, when a consequential research choice
requires the researcher's answer. Ask one concrete question and explain the options.
The three actions are mutually exclusive: when tool_calls is non-empty, omit waiting
and completion; when waiting is present, tool_calls must be empty and completion omitted;
completion is allowed only with an empty tool_calls array and no waiting object.
When proposing a formal Stata estimation, include a plan object before the tool call:
{"summary": string, "structured_plan": {"nodes": [{"canonical_key": string,
"node_kind": string,
"specification": object}], "dependencies": [{"upstream_node_key": string,
"downstream_node_key": string, "dependency_kind": "data"|"control"|"evidence"}]}}.
The plan expresses research intent, not an exact command whitelist. Give each formal
stata.execute call the matching semantic "plan_node_key". The runtime binds immutable
Plan and Node identities before admission and separately preserves the exact Stata code.
""".strip()

_PASSTHROUGH_POLICY_KEYS = {
    "frequency_penalty",
    "max_tokens",
    "max_completion_tokens",
    "presence_penalty",
    "reasoning_effort",
    "response_format",
    "stop",
    "temperature",
    "top_p",
}


class OpenAICompatibleChatTransport:
    """Translate the internal frozen request into an OpenAI-compatible chat call."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")
        self._client = client
        self._timeout = timeout_seconds

    async def send(
        self,
        *,
        endpoint: str,
        request_json: str,
        credential: str,
    ) -> ProviderResponse:
        internal = self._parse_object(request_json, "internal provider request")
        payload = self._chat_payload(internal)
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = await self._client.post(
                    endpoint, headers=headers, json=payload, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(endpoint, headers=headers, json=payload)
        except httpx.ConnectError as error:
            raise ProviderDispatchError("provider_connect_failed", retry_safe=True) from error
        except (httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError) as error:
            raise ProviderDispatchError(
                "provider_delivery_unknown",
                retry_safe=False,
                delivery_unknown=True,
            ) from error
        except httpx.TransportError as error:
            raise ProviderDispatchError("provider_transport_failed", retry_safe=False) from error

        if response.status_code >= 400:
            retry_safe = response.status_code in {408, 409, 429} or response.status_code >= 500
            raise ProviderDispatchError(
                f"provider_http_{response.status_code}",
                retry_safe=retry_safe,
                retry_after_seconds=self._retry_after_seconds(response.headers.get("Retry-After")),
            )
        try:
            envelope = response.json()
        except ValueError as error:
            raise ProviderDispatchError("provider_invalid_json", retry_safe=True) from error
        if not isinstance(envelope, dict):
            raise ProviderDispatchError("provider_invalid_envelope", retry_safe=True)
        finish_reason = self._finish_reason(envelope)
        if finish_reason == "length":
            # A syntactically valid prefix is still not a complete Assistant Output.  Growing
            # the output allowance changes the invocation policy, so the caller must schedule
            # a fresh logical invocation rather than replay this frozen request verbatim.
            raise ProviderDispatchError("provider_output_truncated", retry_safe=False)
        output = self._normalize_output(envelope)
        usage = envelope.get("usage")
        usage_mapping = usage if isinstance(usage, Mapping) else {}
        input_tokens = self._optional_nonnegative_int(usage_mapping.get("prompt_tokens"))
        output_tokens = self._optional_nonnegative_int(usage_mapping.get("completion_tokens"))
        details = usage_mapping.get("prompt_tokens_details")
        details_mapping = details if isinstance(details, Mapping) else {}
        cached_input_tokens = self._optional_nonnegative_int(details_mapping.get("cached_tokens"))
        if cached_input_tokens is None:
            cached_input_tokens = self._optional_nonnegative_int(
                usage_mapping.get("prompt_cache_hit_tokens")
            )
        uncached_input_tokens = self._optional_nonnegative_int(
            usage_mapping.get("prompt_cache_miss_tokens")
        )
        if (
            input_tokens is None
            and cached_input_tokens is not None
            and uncached_input_tokens is not None
        ):
            input_tokens = cached_input_tokens + uncached_input_tokens
        usage_kind = (
            "exact" if input_tokens is not None and output_tokens is not None else "unknown"
        )
        return ProviderResponse(
            output=output,
            usage_kind=usage_kind,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            uncached_input_tokens=uncached_input_tokens,
            finish_reason=finish_reason,
        )

    async def send_stream(
        self,
        *,
        endpoint: str,
        request_json: str,
        credential: str,
        on_text_delta: Callable[[str], Awaitable[None]],
    ) -> ProviderResponse:
        internal = self._parse_object(request_json, "internal provider request")
        payload = self._chat_payload(internal)
        payload["stream"] = True
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        content_parts: list[str] = []
        finish_reason: str | None = None
        try:
            async with client.stream(
                "POST", endpoint, headers=headers, json=payload, timeout=self._timeout
            ) as response:
                if response.status_code >= 400:
                    retry_safe = (
                        response.status_code in {408, 409, 429} or response.status_code >= 500
                    )
                    raise ProviderDispatchError(
                        f"provider_http_{response.status_code}",
                        retry_safe=retry_safe,
                        retry_after_seconds=self._retry_after_seconds(
                            response.headers.get("Retry-After")
                        ),
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except ValueError as error:
                        raise ProviderDispatchError(
                            "provider_stream_invalid_json",
                            retry_safe=False,
                            delivery_unknown=True,
                        ) from error
                    if not isinstance(event, Mapping):
                        continue
                    choices = event.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, Mapping):
                        continue
                    reason = choice.get("finish_reason")
                    if isinstance(reason, str):
                        finish_reason = reason
                    delta = choice.get("delta")
                    text = delta.get("content") if isinstance(delta, Mapping) else None
                    if isinstance(text, str) and text:
                        content_parts.append(text)
                        await on_text_delta(text)
        except httpx.ConnectError as error:
            raise ProviderDispatchError("provider_connect_failed", retry_safe=True) from error
        except ProviderDispatchError:
            raise
        except (httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError) as error:
            raise ProviderDispatchError(
                "provider_delivery_unknown",
                retry_safe=False,
                delivery_unknown=True,
            ) from error
        except httpx.TransportError as error:
            raise ProviderDispatchError("provider_transport_failed", retry_safe=False) from error
        finally:
            if owns_client:
                await client.aclose()
        if finish_reason == "length":
            raise ProviderDispatchError("provider_output_truncated", retry_safe=False)
        content = "".join(content_parts)
        output = self._normalize_output(
            {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}
        )
        return ProviderResponse(output=output, finish_reason=finish_reason)

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _chat_payload(internal: Mapping[str, Any]) -> dict[str, Any]:
        model = internal.get("model")
        normalized = internal.get("normalized_input")
        if not isinstance(model, str) or not model.strip():
            raise ProviderDispatchError("provider_model_missing", retry_safe=False)
        if not isinstance(normalized, Mapping):
            raise ProviderDispatchError("provider_input_missing", retry_safe=False)
        system = normalized.get("system")
        skill = normalized.get("main_skill")
        system_content = system.get("content") if isinstance(system, Mapping) else ""
        skill_content = skill.get("content") if isinstance(skill, Mapping) else ""
        user_payload = {
            "context": normalized.get("context", []),
            "tools": normalized.get("tools", {}),
            "runtime": normalized.get("runtime", {}),
        }
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "\n\n".join(
                        part
                        for part in (
                            str(system_content).strip(),
                            str(skill_content).strip(),
                            _OUTPUT_CONTRACT,
                        )
                        if part
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        user_payload,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }
        policy = internal.get("policy")
        if isinstance(policy, Mapping):
            for key in _PASSTHROUGH_POLICY_KEYS:
                if key in policy:
                    payload[key] = policy[key]
        return payload

    @classmethod
    def _normalize_output(cls, envelope: Mapping[str, Any]) -> dict[str, Any]:
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderDispatchError("provider_choices_missing", retry_safe=True)
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ProviderDispatchError("provider_choice_invalid", retry_safe=True)
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ProviderDispatchError("provider_message_missing", retry_safe=True)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderDispatchError("provider_content_missing", retry_safe=True)
        try:
            output = cls._parse_object(content, "assistant JSON output")
        except ValueError as error:
            raise ProviderDispatchError(
                "provider_output_contract_invalid", retry_safe=True
            ) from error
        text = output.get("text", "")
        tool_calls = output.get("tool_calls", [])
        if not isinstance(text, str) or not isinstance(tool_calls, list):
            raise ProviderDispatchError("provider_output_contract_invalid", retry_safe=True)
        normalized: dict[str, Any] = {"text": text, "tool_calls": tool_calls}
        for optional in ("plan", "evaluation", "waiting", "completion"):
            if optional in output:
                normalized[optional] = output[optional]
        # Some OpenAI-compatible models attach a premature completion proposal to
        # the final tool call even though the output contract makes the actions
        # mutually exclusive.  Completion is only a proposal and the Stop Guard
        # cannot accept it before the tool result exists, so retaining the tool
        # call while discarding that completion is a safe, deterministic repair.
        # A waiting request is different: it is an admission barrier and must not
        # be silently discarded when the same output also proposes side effects.
        if tool_calls and "completion" in normalized and "waiting" not in normalized:
            normalized.pop("completion")
        if tool_calls and "waiting" in normalized:
            raise ProviderDispatchError("provider_action_contract_conflict", retry_safe=True)
        if "waiting" in normalized and "completion" in normalized:
            raise ProviderDispatchError("provider_action_contract_conflict", retry_safe=True)
        return normalized

    @staticmethod
    def _finish_reason(envelope: Mapping[str, Any]) -> str | None:
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, Mapping):
            return None
        reason = first.get("finish_reason")
        return reason if isinstance(reason, str) and reason.strip() else None

    @staticmethod
    def _parse_object(raw: str, label: str) -> dict[str, Any]:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"{label} must be an object")
        return parsed

    @staticmethod
    def _optional_nonnegative_int(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None
