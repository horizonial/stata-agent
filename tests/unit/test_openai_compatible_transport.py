"""OpenAI-compatible transport contract without external network access."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from stata_research_agent.application.model_gateway import ProviderDispatchError
from stata_research_agent.interfaces.openai_compatible_transport import (
    OpenAICompatibleChatTransport,
)


def internal_request() -> str:
    return json.dumps(
        {
            "model": "deepseek-chat",
            "normalized_input": {
                "system": {"content": "You are a research agent."},
                "main_skill": {"content": "Use registered tools."},
                "context": [{"content": "Regress price on mpg."}],
                "tools": {
                    "schemas": [
                        {
                            "name": "research.run_to_word",
                            "input_schema": {"type": "object"},
                        }
                    ]
                },
                "runtime": {"remaining_step_budget": 4},
            },
            "policy": {"temperature": 0, "untrusted_extra": "do-not-forward"},
        }
    )


def test_transport_keeps_credential_out_of_body_and_normalizes_json_output() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "text": "Running the registered workflow.",
                                    "tool_calls": [
                                        {
                                            "name": "research.run_to_word",
                                            "arguments": {},
                                        }
                                    ],
                                }
                            )
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 7,
                    "prompt_cache_hit_tokens": 8,
                    "prompt_cache_miss_tokens": 2,
                },
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await OpenAICompatibleChatTransport(client=client).send(
                endpoint="https://api.deepseek.com/chat/completions",
                request_json=internal_request(),
                credential="transient-secret",
            )
        assert result.output["tool_calls"] == [{"name": "research.run_to_word", "arguments": {}}]
        assert result.usage_kind == "exact"
        assert result.cached_input_tokens == 8
        assert result.uncached_input_tokens == 2
        assert result.finish_reason == "stop"

    asyncio.run(scenario())
    assert seen["authorization"] == "Bearer transient-secret"
    assert b"transient-secret" not in seen["body"]
    assert b"untrusted_extra" not in seen["body"]


def test_transport_preserves_mixed_language_research_context_and_output() -> None:
    seen: dict[str, object] = {}
    request_payload = json.loads(internal_request())
    request_payload["normalized_input"]["system"]["content"] = (
        "你是实证研究 Agent；保留 Stata 变量名，不要翻译标识符。"
    )
    request_payload["normalized_input"]["context"] = [
        {"content": "请使用 auto.dta 回归 price 对 mpg 和 weight，并用中文解释。"}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "text": "将使用 Stata 执行 price、mpg 与 weight 的回归。",
                                    "tool_calls": [
                                        {"name": "research.run_to_word", "arguments": {}}
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                ]
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await OpenAICompatibleChatTransport(client=client).send(
                endpoint="https://api.deepseek.com/chat/completions",
                request_json=json.dumps(request_payload, ensure_ascii=False),
                credential="transient-secret",
            )
        assert result.output["text"] == "将使用 Stata 执行 price、mpg 与 weight 的回归。"

    asyncio.run(scenario())
    rendered = json.dumps(seen["body"], ensure_ascii=False)
    assert "请使用 auto.dta 回归 price 对 mpg 和 weight" in rendered
    assert "不要翻译标识符" in rendered


def test_length_finish_reason_is_never_admitted_as_complete_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": json.dumps({"text": "truncated prefix", "tool_calls": []})
                        },
                    }
                ]
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(
                ProviderDispatchError, match="provider_output_truncated"
            ) as captured:
                await OpenAICompatibleChatTransport(client=client).send(
                    endpoint="https://api.deepseek.com/chat/completions",
                    request_json=internal_request(),
                    credential="transient-secret",
                )
            assert captured.value.retry_safe is False

    asyncio.run(scenario())


def test_invalid_assistant_contract_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not JSON"}}]},
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(
                ProviderDispatchError, match="provider_output_contract_invalid"
            ) as captured:
                await OpenAICompatibleChatTransport(client=client).send(
                    endpoint="https://api.deepseek.com/chat/completions",
                    request_json=internal_request(),
                    credential="transient-secret",
                )
            assert captured.value.retry_safe is True

    asyncio.run(scenario())


def test_streaming_transport_emits_ephemeral_deltas_and_returns_final_output() -> None:
    content = json.dumps({"text": "done", "tool_calls": []})
    midpoint = len(content) // 2

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        events = [
            {"choices": [{"delta": {"content": content[:midpoint]}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": content[midpoint:]}, "finish_reason": "stop"}]},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def scenario() -> None:
        deltas: list[str] = []

        async def receive(value: str) -> None:
            deltas.append(value)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await OpenAICompatibleChatTransport(client=client).send_stream(
                endpoint="https://api.deepseek.com/chat/completions",
                request_json=internal_request(),
                credential="transient-secret",
                on_text_delta=receive,
            )
        assert deltas == [content[:midpoint], content[midpoint:]]
        assert result.output == {"text": "done", "tool_calls": []}
        assert result.finish_reason == "stop"

    asyncio.run(scenario())


def test_tool_and_completion_conflict_keeps_tool_and_drops_premature_completion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "text": "premature",
                                    "tool_calls": [
                                        {"name": "research.run_to_word", "arguments": {}}
                                    ],
                                    "completion": {
                                        "disposition": "succeed",
                                        "summary": "not yet proven",
                                    },
                                }
                            )
                        }
                    }
                ]
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await OpenAICompatibleChatTransport(client=client).send(
                endpoint="https://api.deepseek.com/chat/completions",
                request_json=internal_request(),
                credential="transient-secret",
            )
        assert result.output == {
            "text": "premature",
            "tool_calls": [{"name": "research.run_to_word", "arguments": {}}],
        }

    asyncio.run(scenario())


def test_tool_and_waiting_conflict_still_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "text": "conflicted",
                                    "tool_calls": [
                                        {"name": "research.run_to_word", "arguments": {}}
                                    ],
                                    "waiting": {
                                        "reason": "user_confirmation",
                                        "prompt": "Proceed?",
                                    },
                                }
                            )
                        }
                    }
                ]
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(
                ProviderDispatchError, match="provider_action_contract_conflict"
            ):
                await OpenAICompatibleChatTransport(client=client).send(
                    endpoint="https://api.deepseek.com/chat/completions",
                    request_json=internal_request(),
                    credential="transient-secret",
                )

    asyncio.run(scenario())
