from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from stata_research_agent.application.broker_execution import BrokerExecutionHandle
from stata_research_agent.application.turn_driver import ToolExecutionRequest
from stata_research_agent.domain.identifiers import (
    OperationAttemptId,
    OperationId,
    ResearchPathId,
    ToolCallId,
    TurnId,
)
from stata_research_agent.runtime.open_research_executor import (
    PromoteStataResultExecutor,
    _artifact_output_expectations,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _Bridge:
    def __init__(self) -> None:
        self.completions: list[Any] = []

    def begin(self, command: Any) -> BrokerExecutionHandle:
        return BrokerExecutionHandle(
            command.operation_id,
            OperationAttemptId("attempt_promote_test"),
            command.tool_call_id,
            False,
        )

    def complete(self, command: Any) -> Any:
        self.completions.append(command)
        return SimpleNamespace(status="failed")


class _FailingPromoter(PromoteStataResultExecutor):
    def _promote(self, request: ToolExecutionRequest) -> dict[str, Any]:
        del request
        raise ValueError("operation is not a completed formal Result candidate")


def test_promotion_failure_is_one_canonical_result_not_a_second_executor_exception() -> None:
    bridge = _Bridge()
    executor = _FailingPromoter(
        None,
        bridge,  # type: ignore[arg-type]
        None,
        None,
        None,
        None,
        UuidIdentityGenerator(),
        ResearchPathId("path_main"),
    )
    request = ToolExecutionRequest(
        TurnId("turn_promote"),
        ToolCallId("toolcall_promote"),
        OperationId("op_promote"),
        "research.promote_stata_result",
        {},
    )

    result = asyncio.run(executor.execute(request))

    assert result.success is False
    assert len(bridge.completions) == 1
    payload = json.loads(result.context_text)
    assert payload == {
        "error_kind": "ValueError",
        "error_detail": "operation is not a completed formal Result candidate",
    }
    assert bridge.completions[0].payload == payload


def test_open_stata_artifact_outputs_become_typed_expectations() -> None:
    outputs = _artifact_output_expectations(
        [
            {
                "output_slot": "figure.main",
                "relative_staging_path": "figures/main.png",
                "artifact_kind": "document",
                "media_type": "image/png",
            }
        ]
    )

    assert len(outputs) == 1
    assert outputs[0].output_slot == "figure.main"
    assert outputs[0].relative_staging_path == "figures/main.png"
    assert outputs[0].required is True


def test_open_stata_artifact_outputs_reject_duplicate_paths() -> None:
    with pytest.raises(ValueError, match="paths must be unique"):
        _artifact_output_expectations(
            [
                {
                    "output_slot": "figure.one",
                    "relative_staging_path": "figures/main.png",
                    "artifact_kind": "document",
                    "media_type": "image/png",
                },
                {
                    "output_slot": "figure.two",
                    "relative_staging_path": "figures/main.png",
                    "artifact_kind": "document",
                    "media_type": "image/png",
                },
            ]
        )
