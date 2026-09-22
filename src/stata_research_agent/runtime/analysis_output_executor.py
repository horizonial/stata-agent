"""Production tools for classifying and explicitly adopting sandbox outputs."""

from __future__ import annotations

import json
from typing import Any

from stata_research_agent.application.analysis_output import (
    AdoptAnalysisOutputCommand,
    AnalysisArtifactBinding,
    AnalysisElement,
    ClassifyAnalysisOutputCommand,
)
from stata_research_agent.application.analysis_output_service import AnalysisOutputService
from stata_research_agent.application.artifact_data import VerifyArtifactCommand
from stata_research_agent.application.artifact_service import ArtifactDataService
from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import BrokerExecutionService
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.turn_driver import (
    ToolExecutionRequest,
    ToolExecutionResult,
)
from stata_research_agent.domain.analysis_output import AnalysisOutputKind
from stata_research_agent.domain.artifact_data import VerificationPurpose
from stata_research_agent.domain.identifiers import (
    AnalysisOutputId,
    ArtifactId,
    ArtifactVerificationReceiptId,
    CommandId,
    OperationAttemptId,
    OperationId,
)


class ClassifyAnalysisOutputExecutor:
    def __init__(
        self,
        connection: Any,
        bridge: BrokerExecutionService,
        analysis: AnalysisOutputService,
        identities: IdentityGenerator,
    ) -> None:
        self._connection = connection
        self._bridge = bridge
        self._analysis = analysis
        self._identities = identities

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        handle = self._bridge.begin(
            BeginBrokerExecutionCommand(
                self._identities.new(CommandId),
                request.turn_id,
                request.tool_call_id,
                request.operation_id,
            )
        )
        try:
            payload = self._classify(request)
        except Exception as error:
            return self._finish(
                handle,
                False,
                f"Analysis Output classification failed: {type(error).__name__}: {error}",
                {"error_type": type(error).__name__},
            )
        return self._finish(handle, True, "Analysis Output classified", payload)

    def _classify(self, request: ToolExecutionRequest) -> dict[str, object]:
        producer_id = OperationId(str(request.arguments["operation_id"]))
        producer = self._connection.execute(
            """
            SELECT operation.operation_kind, operation.status,
                   attempt.operation_attempt_id, result.structured_payload_json,
                   receipt.policy_sha256, receipt.network_mode,
                   receipt.receipt_sha256
            FROM operations AS operation
            JOIN operation_attempts AS attempt USING (operation_id)
            JOIN canonical_tool_results AS result
              ON result.tool_call_id = operation.tool_call_id
            JOIN sandbox_execution_receipts AS receipt
              ON receipt.operation_attempt_id = attempt.operation_attempt_id
            WHERE operation.operation_id = ?
            """,
            (producer_id.value,),
        ).fetchone()
        if producer is None or str(producer["status"]) != "completed":
            raise ValueError("producer sandbox Operation is not durably completed")
        if str(producer["operation_kind"]) not in {"python.execute", "shell.execute"}:
            raise ValueError("Analysis Output producer must be Python/Shell")
        attempt_id = OperationAttemptId(str(producer["operation_attempt_id"]))
        result_payload = json.loads(str(producer["structured_payload_json"]))
        input_ids = tuple(
            ArtifactId(str(value)) for value in result_payload.get("input_artifact_ids", [])
        )
        code_rows = self._connection.execute(
            """
            SELECT artifact_id FROM artifacts
            WHERE producer_attempt_id = ? AND artifact_kind = 'code'
            """,
            (attempt_id.value,),
        ).fetchall()
        if len(code_rows) != 1:
            raise ValueError("sandbox executable Artifact identity is ambiguous")
        executable_id = ArtifactId(str(code_rows[0][0]))

        raw_outputs = request.arguments["outputs"]
        if not isinstance(raw_outputs, list) or not raw_outputs:
            raise ValueError("classification requires at least one output binding")
        outputs = tuple(
            AnalysisArtifactBinding(ArtifactId(str(binding["artifact_id"])), str(binding["role"]))
            for binding in raw_outputs
            if isinstance(binding, dict)
        )
        if len(outputs) != len(raw_outputs):
            raise ValueError("classification output binding is malformed")
        for output in outputs:
            row = self._connection.execute(
                """
                SELECT artifact_kind FROM artifacts
                WHERE artifact_id = ? AND producer_attempt_id = ?
                """,
                (output.artifact_id.value, attempt_id.value),
            ).fetchone()
            if row is None or str(row["artifact_kind"]) == "code":
                raise ValueError("classification output is not owned by the producer Attempt")

        raw_elements = request.arguments.get("elements", [])
        if not isinstance(raw_elements, list):
            raise ValueError("classification elements must be an array")
        elements = tuple(
            AnalysisElement(
                str(element["stable_key"]),
                element.get("value"),
                str(element["rendered_text"]),
            )
            for element in raw_elements
            if isinstance(element, dict)
        )
        if len(elements) != len(raw_elements):
            raise ValueError("classification element is malformed")
        output_kind = AnalysisOutputKind(str(request.arguments["output_kind"]))
        classified = self._analysis.classify(
            ClassifyAnalysisOutputCommand(
                self._identities.new(CommandId),
                request.turn_id,
                producer_id,
                attempt_id,
                executable_id,
                input_ids,
                outputs,
                elements,
                output_kind,
                str(request.arguments["method_summary"]),
                {
                    "runtime_kind": (
                        "python" if str(producer["operation_kind"]) == "python.execute" else "shell"
                    ),
                    "policy_sha256": str(producer["policy_sha256"]),
                    "network_mode": str(producer["network_mode"]),
                    "execution_receipt_sha256": str(producer["receipt_sha256"]),
                },
                {
                    "declared_kind": output_kind.value,
                    "reviewable": True,
                    "source": "agent_explicit_classification",
                },
            )
        )
        return {
            "analysis_output_id": classified.analysis_output_id.value,
            "classification_id": classified.classification_id.value,
            "output_kind": classified.output_kind.value,
            "output_fingerprint": classified.output_fingerprint,
            "document_eligible_after_user_adoption": classified.document_eligible,
            "requires_later_user_turn": classified.document_eligible,
        }

    def _finish(
        self,
        handle: Any,
        success: bool,
        summary: str,
        payload: dict[str, object],
    ) -> ToolExecutionResult:
        outcome = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId),
                handle,
                success,
                summary,
                payload,
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            success,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )


class AdoptAnalysisOutputExecutor:
    def __init__(
        self,
        connection: Any,
        bridge: BrokerExecutionService,
        analysis: AnalysisOutputService,
        artifacts: ArtifactDataService,
        identities: IdentityGenerator,
    ) -> None:
        self._connection = connection
        self._bridge = bridge
        self._analysis = analysis
        self._artifacts = artifacts
        self._identities = identities

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        handle = self._bridge.begin(
            BeginBrokerExecutionCommand(
                self._identities.new(CommandId),
                request.turn_id,
                request.tool_call_id,
                request.operation_id,
            )
        )
        try:
            output_id = AnalysisOutputId(str(request.arguments["analysis_output_id"]))
            preview_id = ArtifactId(str(request.arguments["preview_artifact_id"]))
            artifact_rows = self._connection.execute(
                """
                SELECT binding.artifact_id
                FROM analysis_output_artifacts AS binding
                WHERE binding.analysis_output_id = ?
                ORDER BY binding.ordinal
                """,
                (output_id.value,),
            ).fetchall()
            if not artifact_rows:
                raise ValueError("Analysis Output Artifacts are unavailable")
            verifications = tuple(
                self._artifacts.verify_artifact(
                    VerifyArtifactCommand(
                        self._identities.new(CommandId),
                        ArtifactId(str(row[0])),
                        VerificationPurpose.DOCUMENT_DELIVERY,
                    )
                )
                for row in artifact_rows
            )
            adopted = self._analysis.adopt(
                AdoptAnalysisOutputCommand(
                    self._identities.new(CommandId),
                    output_id,
                    request.turn_id,
                    str(request.arguments["expected_output_fingerprint"]),
                    preview_id,
                    tuple(
                        ArtifactVerificationReceiptId(item.receipt_id.value)
                        for item in verifications
                    ),
                    str(request.arguments["confirmation_summary"]),
                )
            )
            payload: dict[str, object] = {
                "analysis_output_id": adopted.analysis_output_id.value,
                "adoption_id": adopted.adoption_id.value,
                "evidence_record_id": adopted.evidence_record_id.value,
                "eligibility_id": adopted.eligibility_id.value,
                "output_fingerprint": adopted.output_fingerprint,
            }
            success = True
            summary = "Analysis Output explicitly adopted"
        except Exception as error:
            payload = {"error_type": type(error).__name__}
            success = False
            summary = f"Analysis Output adoption failed: {type(error).__name__}: {error}"
        outcome = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId),
                handle,
                success,
                summary,
                payload,
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            success,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
