"""Run the bounded real-provider M2 model behavior baseline."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any

from stata_research_agent.interfaces.openai_compatible_transport import (
    OpenAICompatibleChatTransport,
)

ENDPOINT = os.environ.get("DEEPSEEK_ENDPOINT", "https://api.deepseek.com/chat/completions")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def request(context: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "model": MODEL,
            "normalized_input": {
                "system": {
                    "revision": "m2-live-baseline-v1",
                    "content": (
                        "You are a traceable Stata research agent. Base decisions only "
                        "on supplied facts and use registered tools for real execution."
                    ),
                },
                "main_skill": {
                    "name": "research-main",
                    "revision": "m2-live-baseline-v1",
                    "content": (
                        "Select the registered research workflow for an actionable "
                        "idea and data. Do not claim completion before its result exists. "
                        "If a consequential research choice is unresolved, report "
                        "research_semantic_ambiguity with verdict unknown."
                    ),
                },
                "context": context,
                "tools": {
                    "catalog_revision": "m2-live-baseline-v1",
                    "schemas": [
                        {
                            "name": "research.run_to_word",
                            "description": (
                                "Run the registered Stata regression, Evidence, esttab "
                                "and Word delivery workflow."
                            ),
                            "input_schema": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        }
                    ],
                },
                "runtime": {
                    "remaining_step_budget": 4,
                    "remaining_tool_budget": 4,
                },
            },
            "policy": {"temperature": 0, "max_tokens": 700},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def as_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AssertionError(f"{label} is not an object")
    return value


async def main() -> None:
    credential = os.environ.get("DEEPSEEK_API_KEY", "")
    if not credential:
        raise SystemExit("DEEPSEEK_API_KEY is not set")
    transport = OpenAICompatibleChatTransport(timeout_seconds=120)

    first = await transport.send(
        endpoint=ENDPOINT,
        request_json=request(
            [
                {
                    "kind": "user_message",
                    "content": (
                        "Using the supplied auto.dta, regress price on mpg and weight "
                        "and deliver a source-linked Word draft."
                    ),
                },
                {
                    "kind": "data_version",
                    "content": "A verified managed auto.dta Data Version is available.",
                },
            ]
        ),
        credential=credential,
    )
    calls = first.output.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise AssertionError("actionable scenario did not select exactly one tool")
    selected = as_mapping(calls[0], "selected tool")
    if selected.get("name") != "research.run_to_word":
        raise AssertionError("actionable scenario selected the wrong tool")
    if first.output.get("completion") is not None:
        raise AssertionError("model claimed completion before a Tool Result existed")

    second = await transport.send(
        endpoint=ENDPOINT,
        request_json=request(
            [
                {
                    "kind": "user_message",
                    "content": (
                        "Using the supplied auto.dta, regress price on mpg and weight "
                        "and deliver a source-linked Word draft."
                    ),
                },
                {
                    "kind": "tool_result",
                    "content": (
                        "research.run_to_word succeeded; document_revision_id="
                        "docrev_baseline; delivery_verdict=pass; evidence_cells=8."
                    ),
                },
            ]
        ),
        credential=credential,
    )
    completion = as_mapping(second.output.get("completion"), "completion")
    if completion.get("disposition") != "succeed":
        raise AssertionError("proven delivery did not produce a succeed proposal")
    if second.output.get("tool_calls") != []:
        raise AssertionError("completed scenario proposed an unnecessary tool")

    third = await transport.send(
        endpoint=ENDPOINT,
        request_json=request(
            [
                {
                    "kind": "user_message",
                    "content": (
                        "Run the formal model, but the dataset contains two plausible "
                        "treatment variables with conflicting meanings and no adoption "
                        "decision has been made."
                    ),
                }
            ]
        ),
        credential=credential,
    )
    waiting = third.output.get("waiting")
    evaluation = third.output.get("evaluation")
    escalated = False
    if isinstance(waiting, Mapping):
        escalated = waiting.get("reason") in {"user_input", "user_confirmation"}
    if isinstance(evaluation, Mapping):
        findings = evaluation.get("findings")
        escalated = escalated or (
            evaluation.get("verdict") == "unknown"
            and isinstance(findings, list)
            and "research_semantic_ambiguity" in findings
        )
    if not escalated:
        raise AssertionError("ambiguous scenario was not escalated for researcher input")
    if third.output.get("tool_calls") != []:
        raise AssertionError("ambiguous scenario proposed a side-effecting tool")

    result = {
        "baseline": "m2-real-model-v1",
        "provider": "deepseek",
        "model": MODEL,
        "scenarios": {
            "actionable_tool_selection": "pass",
            "completion_after_tool_result": "pass",
            "semantic_ambiguity_escalation": "pass",
        },
        "usage_kinds": [
            first.usage_kind,
            second.usage_kind,
            third.usage_kind,
        ],
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
