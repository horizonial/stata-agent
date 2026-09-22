"""Independent model checkpoint evaluation for candidate Turn completion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from stata_research_agent.application.model_gateway import StartModelStepCommand
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.domain.identifiers import StepId

from .ipc_contract import WorkerEvaluation


@dataclass(frozen=True, slots=True)
class ModelEvaluationOutcome:
    step_id: StepId
    status: str
    evaluation: WorkerEvaluation | None


class ModelEvaluationCoordinator:
    """Run a separate logical invocation and accept only an evaluation proposal."""

    def __init__(self, gateway: ModelGatewayService) -> None:
        self._gateway = gateway

    async def evaluate(self, command: StartModelStepCommand) -> ModelEvaluationOutcome:
        outcome = await self._gateway.execute_step(command)
        if outcome.status != "completed" or outcome.output is None:
            return ModelEvaluationOutcome(outcome.step_id, outcome.status, None)
        output = outcome.output
        if self._contains_agent_action(output):
            return ModelEvaluationOutcome(outcome.step_id, "invalid_evaluation_output", None)
        raw = output.get("evaluation")
        if not isinstance(raw, Mapping):
            return ModelEvaluationOutcome(outcome.step_id, "evaluation_missing", None)
        verdict = str(raw.get("verdict", "unknown"))
        if verdict not in {"pass", "warn", "fail", "unknown"}:
            return ModelEvaluationOutcome(outcome.step_id, "evaluation_invalid", None)
        findings = raw.get("findings", [])
        if not isinstance(findings, list) or not all(isinstance(item, str) for item in findings):
            return ModelEvaluationOutcome(outcome.step_id, "evaluation_invalid", None)
        return ModelEvaluationOutcome(
            outcome.step_id,
            "completed",
            WorkerEvaluation(
                verdict=cast(Literal["pass", "warn", "fail", "unknown"], verdict),
                findings=tuple(findings),
            ),
        )

    @staticmethod
    def _contains_agent_action(output: Mapping[str, object]) -> bool:
        return bool(
            output.get("plan")
            or output.get("tool_calls")
            or output.get("waiting")
            or output.get("completion")
        )
