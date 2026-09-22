"""Open production research tools: Stata execution, Result qualification, and Word export.

The runtime enforces authority and provenance.  Research sequencing remains a model/Skill
decision instead of being embedded in a fixed application workflow.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from stata_research_agent.application.artifact_data import AdoptPathDataCommand
from stata_research_agent.application.artifact_service import ArtifactDataService
from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import BrokerExecutionService
from stata_research_agent.application.data_intake import (
    PreparedWorkspaceData,
    PrepareWorkspaceDataCommand,
)
from stata_research_agent.application.data_intake_service import WorkspaceDataIntakeService
from stata_research_agent.application.document_delivery import (
    DeliverEsttabDocumentCommand,
    ManuscriptSections,
)
from stata_research_agent.application.document_delivery_service import DocumentDeliveryService
from stata_research_agent.application.evidence import AdoptPathResultCommand
from stata_research_agent.application.evidence_service import EvidenceService
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.result_profile import (
    PromoteStataResultCommand,
    RegisterGenericStataResultProfileCommand,
)
from stata_research_agent.application.result_profile_service import (
    RegisteredResultProfileService,
)
from stata_research_agent.application.stata_operation import (
    ArtifactOutputExpectation,
    ExecuteStataCommand,
    FormalSessionDataBinding,
)
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.table_export import (
    ExportEsttabTableCommand,
    RegisterEsttabProfileCommand,
)
from stata_research_agent.application.table_export_service import EsttabTableExportService
from stata_research_agent.application.turn_driver import (
    ToolExecutionRequest,
    ToolExecutionResult,
)
from stata_research_agent.domain.identifiers import (
    CommandId,
    DataVersionId,
    OperationId,
    PlanNodeId,
    PlanRevisionId,
    ResearchPathId,
    TurnId,
)

from .plan_coordinator import ResearchPlanCoordinator


class FormalResultQualificationError(RuntimeError):
    """A safe, model-actionable formal Result qualification failure."""

    def __init__(self, findings: tuple[str, ...]) -> None:
        self.findings = findings
        super().__init__("formal Result promotion failed: " + ", ".join(findings))


def _validate_formal_plan_arguments(
    connection: Any,
    research_path_id: ResearchPathId,
    arguments: Mapping[str, Any],
) -> tuple[PlanRevisionId, PlanNodeId]:
    revision_id = str(arguments.get("plan_revision_id", "")).strip()
    node_id = str(arguments.get("plan_node_id", "")).strip()
    node_key = str(arguments.get("plan_node_key", "")).strip()
    if not revision_id or not node_id or not node_key:
        raise ValueError("formal Stata execution requires a frozen semantic Plan binding")
    row = connection.execute(
        """
        SELECT 1
        FROM path_plan_adoptions AS adoption
        JOIN plan_revision_nodes AS member
          ON member.plan_revision_id = adoption.target_plan_revision_id
        JOIN plan_nodes AS node ON node.plan_node_id = member.plan_node_id
        WHERE adoption.research_path_id = ?
          AND adoption.target_plan_revision_id = ?
          AND member.plan_node_id = ?
          AND node.canonical_key = ?
        """,
        (research_path_id.value, revision_id, node_id, node_key),
    ).fetchone()
    if row is None:
        raise ValueError("formal Stata execution Plan binding is stale or inconsistent")
    return PlanRevisionId(revision_id), PlanNodeId(node_id)


def _plan_binding_for_operation(
    connection: Any, operation_id: OperationId
) -> tuple[PlanRevisionId, PlanNodeId]:
    row = connection.execute(
        """
        SELECT json_extract(arguments.arguments_json, '$.plan_revision_id') AS plan_revision_id,
               json_extract(arguments.arguments_json, '$.plan_node_id') AS plan_node_id,
               json_extract(arguments.arguments_json, '$.plan_node_key') AS plan_node_key
        FROM operations AS operation
        JOIN tool_calls AS call ON call.tool_call_id = operation.tool_call_id
        JOIN canonical_tool_argument_snapshots AS arguments
          ON arguments.canonical_arguments_snapshot_id =
             call.canonical_arguments_snapshot_id
        WHERE operation.operation_id = ?
        """,
        (operation_id.value,),
    ).fetchone()
    if row is None or any(
        row[key] is None for key in ("plan_revision_id", "plan_node_id", "plan_node_key")
    ):
        raise ValueError("formal Result operation lacks a frozen semantic Plan binding")
    revision_id = str(row["plan_revision_id"])
    node_id = str(row["plan_node_id"])
    member = connection.execute(
        """
        SELECT 1 FROM plan_revision_nodes AS revision_node
        JOIN plan_nodes AS node ON node.plan_node_id = revision_node.plan_node_id
        WHERE revision_node.plan_revision_id = ? AND revision_node.plan_node_id = ?
          AND node.canonical_key = ?
        """,
        (revision_id, node_id, str(row["plan_node_key"])),
    ).fetchone()
    if member is None:
        raise ValueError("formal Result operation references an invalid semantic Plan binding")
    return PlanRevisionId(revision_id), PlanNodeId(node_id)


class OpenStataExecutor:
    """Execute arbitrary Stata code while maintaining a verifiable data-state chain."""

    def __init__(
        self,
        connection: Any,
        intake: WorkspaceDataIntakeService,
        stata: StataOperationService,
        identities: IdentityGenerator,
        session_id: str,
        bind_managed_input: Callable[[str, str], None],
        bind_workspace_inputs: Callable[
            [tuple[str, ...]], tuple[dict[str, object], ...]
        ],
        research_path_id: ResearchPathId,
        plan_coordinator: ResearchPlanCoordinator,
    ) -> None:
        self._connection = connection
        self._intake = intake
        self._stata = stata
        self._identities = identities
        self._session_id = session_id
        self._bind_managed_input = bind_managed_input
        self._bind_workspace_inputs = bind_workspace_inputs
        self._research_path_id = research_path_id
        self._plan_coordinator = plan_coordinator
        self._relative_path: str | None = None
        self._prepared: PreparedWorkspaceData | None = None
        self._binding: FormalSessionDataBinding | None = None

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        relative_path = str(request.arguments["dataset_relative_path"]).strip()
        code = str(request.arguments["code"])
        reset_data = bool(request.arguments.get("reset_data", False))
        role = str(request.arguments.get("execution_role", "data_step"))
        raw_workspace_inputs = request.arguments.get("workspace_file_inputs", [])
        if not isinstance(raw_workspace_inputs, list) or not all(
            isinstance(item, str) for item in raw_workspace_inputs
        ):
            raise ValueError("workspace_file_inputs must be an array of relative paths")
        workspace_inputs = tuple(str(item) for item in raw_workspace_inputs)
        artifact_outputs = _artifact_output_expectations(
            request.arguments.get("artifact_outputs", [])
        )
        timeout_value = request.arguments.get("timeout_seconds", 300)
        if (
            not isinstance(timeout_value, (int, float))
            or isinstance(timeout_value, bool)
            or not 0.1 <= float(timeout_value) <= 3600
        ):
            raise ValueError("timeout_seconds must be numeric and between 0.1 and 3600")
        if request.remaining_time_seconds is not None:
            timeout_value = min(float(timeout_value), max(0.1, request.remaining_time_seconds))
        if role not in {
            "data_step",
            "formal_result_candidate",
            "formal_post_estimation",
        }:
            raise ValueError(
                "execution_role must be data_step, formal_result_candidate, "
                "or formal_post_estimation"
            )
        if not code.strip():
            raise ValueError("Stata code is required")
        if role in {"formal_result_candidate", "formal_post_estimation"}:
            _validate_formal_plan_arguments(
                self._connection, self._research_path_id, request.arguments
            )
        if role == "formal_post_estimation" and reset_data:
            raise ValueError("formal_post_estimation must continue the current estimation state")
        if self._binding is None or self._relative_path != relative_path or reset_data:
            await self._load_dataset(request.turn_id, relative_path)
        assert self._prepared is not None and self._binding is not None
        supporting_input_receipts = self._bind_workspace_inputs(workspace_inputs)
        purpose = {
            "data_step": "data_step",
            "formal_result_candidate": "formal_estimation",
            "formal_post_estimation": "formal_post_estimation",
        }[role]
        outcome = await self._stata.execute(
            ExecuteStataCommand(
                self._identities.new(CommandId),
                request.turn_id,
                self._session_id,
                code,
                timeout_seconds=float(timeout_value),
                tool_call_id=request.tool_call_id,
                admitted_operation_id=request.operation_id,
                input_data_version_id=self._prepared.captured.data_version_id,
                input_data_slot_key="analysis.primary",
                input_verification_receipt_id=(self._prepared.formal_input_verification.receipt_id),
                execution_purpose=purpose,
                source_data_state_operation_id=(self._binding.source_data_state_operation_id),
                expected_data_state_token=self._binding.data_state_token,
                expected_session_generation=self._binding.session_generation,
                expected_outputs=artifact_outputs,
            )
        )
        if outcome.status == "completed":
            self._binding = self._stata.session_data_binding(outcome.operation_id)
        else:
            # A failed/uncertain Stata operation may have reset the supervised session.  The
            # previous binding is then only historical evidence, never proof that the new
            # generation still has data loaded.  Force the next Call through the verified
            # immutable Data Version load boundary before any further research command.
            self._binding = None
        manifest = self._connection.execute(
            """
            SELECT raw_text, structured_result_status, structured_result_json,
                   receipt_json
            FROM completion_manifests WHERE operation_id = ?
            """,
            (outcome.operation_id.value,),
        ).fetchone()
        payload: dict[str, Any] = {
            "operation_id": outcome.operation_id.value,
            "status": outcome.status,
            "execution_role": role,
            "dataset_relative_path": relative_path,
            "data_artifact_id": self._prepared.captured.artifact_id.value,
            "data_version_id": self._prepared.captured.data_version_id.value,
            "supporting_workspace_files": list(supporting_input_receipts),
        }
        if manifest is not None:
            payload.update(
                {
                    "raw_output": str(manifest["raw_text"])[:40_000],
                    "structured_result_status": str(manifest["structured_result_status"]),
                    "structured_result": (
                        json.loads(str(manifest["structured_result_json"]))
                        if manifest["structured_result_json"] is not None
                        else None
                    ),
                    "execution_receipt": json.loads(str(manifest["receipt_json"])),
                }
            )
        return ToolExecutionResult(
            outcome.status == "completed",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    async def _load_dataset(self, turn_id: TurnId, relative_path: str) -> None:
        prepared = self._intake.prepare_for_formal_run(
            PrepareWorkspaceDataCommand(self._identities.new(CommandId), turn_id, relative_path)
        )
        self._bind_managed_input(prepared.captured.managed_handle, relative_path)
        loaded = await self._stata.execute(
            ExecuteStataCommand(
                self._identities.new(CommandId),
                turn_id,
                self._session_id,
                f'use "{prepared.captured.managed_handle}", clear',
                input_data_version_id=prepared.captured.data_version_id,
                input_data_slot_key="analysis.primary",
                input_verification_receipt_id=(prepared.formal_input_verification.receipt_id),
                execution_purpose="data_load",
            )
        )
        if loaded.status != "completed":
            raise RuntimeError(f"Stata data load did not complete: {loaded.status}")
        self._relative_path = relative_path
        self._prepared = prepared
        self._binding = self._stata.session_data_binding(loaded.operation_id)


def _artifact_output_expectations(raw: object) -> tuple[ArtifactOutputExpectation, ...]:
    if not isinstance(raw, list):
        raise ValueError("artifact_outputs must be an array")
    if len(raw) > 32:
        raise ValueError("artifact_outputs must contain at most 32 items")
    expectations: list[ArtifactOutputExpectation] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("each artifact output must be an object")
        required_keys = (
            "output_slot",
            "relative_staging_path",
            "artifact_kind",
            "media_type",
        )
        if not all(isinstance(item.get(key), str) for key in required_keys):
            raise ValueError("artifact output identity fields must be strings")
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise ValueError("artifact output required must be boolean")
        artifact_kind = str(item["artifact_kind"])
        if artifact_kind not in {
            "dataset",
            "code",
            "log",
            "table",
            "document",
            "diagnostic",
        }:
            raise ValueError("artifact output kind is unsupported")
        expectations.append(
            ArtifactOutputExpectation(
                str(item["output_slot"]),
                str(item["relative_staging_path"]),
                artifact_kind,
                str(item["media_type"]),
                required,
            )
        )
    if len({item.output_slot for item in expectations}) != len(expectations):
        raise ValueError("artifact output slots must be unique")
    if len({item.relative_staging_path for item in expectations}) != len(expectations):
        raise ValueError("artifact output paths must be unique")
    return tuple(expectations)


class PromoteStataResultExecutor:
    """Promote Agent-selected values from any traceable Stata result."""

    def __init__(
        self,
        connection: Any,
        bridge: BrokerExecutionService,
        profiles: RegisteredResultProfileService,
        plan_coordinator: ResearchPlanCoordinator,
        artifacts: ArtifactDataService,
        evidence: EvidenceService,
        identities: IdentityGenerator,
        research_path_id: ResearchPathId,
    ) -> None:
        self._connection = connection
        self._bridge = bridge
        self._profiles = profiles
        self._plan_coordinator = plan_coordinator
        self._artifacts = artifacts
        self._evidence = evidence
        self._identities = identities
        self._path_id = research_path_id

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
            payload = self._promote(request)
        except FormalResultQualificationError as error:
            payload = {
                "error_code": "FORMAL_RESULT_NOT_QUALIFIED",
                "findings": list(error.findings),
            }
            completed = self._bridge.complete(
                CompleteBrokerExecutionCommand(
                    self._identities.new(CommandId),
                    handle,
                    False,
                    "Formal Stata Result did not pass qualification",
                    payload,
                )
            )
            return ToolExecutionResult(
                completed.status == "completed",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception as error:
            error_detail = (
                str(error)[:500]
                if isinstance(error, (ValueError, RuntimeError))
                else type(error).__name__
            )
            completed = self._bridge.complete(
                CompleteBrokerExecutionCommand(
                    self._identities.new(CommandId),
                    handle,
                    False,
                    f"Result promotion failed: {type(error).__name__}",
                    {
                        "error_kind": type(error).__name__,
                        "error_detail": error_detail,
                    },
                )
            )
            return ToolExecutionResult(
                completed.status == "completed",
                json.dumps(
                    {
                        "error_kind": type(error).__name__,
                        "error_detail": error_detail,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        completed = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId),
                handle,
                True,
                "Stata result promoted and adopted",
                payload,
            )
        )
        return ToolExecutionResult(
            completed.status == "completed",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def _promote(self, request: ToolExecutionRequest) -> dict[str, Any]:
        arguments: Mapping[str, Any] = request.arguments
        operation_id = OperationId(str(arguments["operation_id"]))
        selected_source_keys = _string_array(arguments["selected_source_keys"])
        result_slot = str(arguments.get("result_slot_key", "baseline.primary")).strip()
        summary = str(arguments["plan_summary"]).strip()
        if not result_slot or not summary:
            raise ValueError("result_slot_key and plan_summary are required")
        facts = self._connection.execute(
            """
            SELECT source.command_text, binding.input_data_version_id,
                   binding.input_data_slot_key
            FROM operations AS operation
            JOIN operation_attempts AS attempt USING (operation_id)
            JOIN stata_operation_input_bindings AS binding
              ON binding.operation_attempt_id = attempt.operation_attempt_id
            JOIN executable_sources AS source
              ON source.executable_source_id = binding.executable_source_id
            WHERE operation.operation_id = ? AND operation.status = 'completed'
              AND binding.execution_purpose IN (
                  'formal_estimation', 'formal_post_estimation'
              )
            """,
            (operation_id.value,),
        ).fetchone()
        if facts is None:
            raise ValueError("operation is not a completed formal Result candidate")
        plan_binding = _plan_binding_for_operation(self._connection, operation_id)
        plan_revision_id, plan_node_id = plan_binding
        self._profiles.register_generic_profile(
            RegisterGenericStataResultProfileCommand(
                self._identities.new(CommandId), request.turn_id
            )
        )
        qualified = self._profiles.promote(
            PromoteStataResultCommand(
                self._identities.new(CommandId),
                operation_id,
                request.turn_id,
                self._path_id,
                selected_source_keys,
                summary,
                plan_revision_id,
                plan_node_id,
            )
        )
        if qualified.result_id is None:
            raise FormalResultQualificationError(qualified.findings)
        data_slot = str(facts["input_data_slot_key"])
        data_version_id = DataVersionId(str(facts["input_data_version_id"]))
        self._artifacts.adopt_path_data(
            AdoptPathDataCommand(
                self._identities.new(CommandId),
                self._path_id,
                data_slot,
                data_version_id,
                _slot_pointer(
                    self._connection,
                    "path_data_slots",
                    "path_data_adoptions",
                    "path_data_slot_id",
                    self._path_id.value,
                    data_slot,
                ),
            )
        )
        self._evidence.adopt_path_result(
            AdoptPathResultCommand(
                self._identities.new(CommandId),
                self._path_id,
                result_slot,
                qualified.result_id,
                request.turn_id,
                _slot_pointer(
                    self._connection,
                    "result_slots",
                    "path_result_adoptions",
                    "result_slot_id",
                    self._path_id.value,
                    result_slot,
                ),
            )
        )
        return {
            "result_id": qualified.result_id.value,
            "result_slot_key": result_slot,
            "plan_revision_id": plan_revision_id.value,
            "source_operation_id": operation_id.value,
            "qualification_findings": list(qualified.findings),
        }


class ExportWordExecutor:
    """Render an adopted coefficient-bearing Result through esttab and the Word gate."""

    def __init__(
        self,
        connection: Any,
        bridge: BrokerExecutionService,
        tables: EsttabTableExportService,
        documents: DocumentDeliveryService,
        identities: IdentityGenerator,
        research_path_id: ResearchPathId,
    ) -> None:
        self._connection = connection
        self._bridge = bridge
        self._tables = tables
        self._documents = documents
        self._identities = identities
        self._path_id = research_path_id

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
            result_slot = str(request.arguments["result_slot_key"]).strip()
            title = str(request.arguments["document_title"]).strip()
            raw_terms = request.arguments.get("coefficient_terms", ["mpg", "weight", "_cons"])
            selected_terms = _string_array(raw_terms)
            coefficient_terms = selected_terms
            fit_statistic = str(request.arguments.get("fit_statistic", "r2")).strip()
            fit_label = str(request.arguments.get("fit_label", "R-squared")).strip()
            raw_manuscript = request.arguments.get("manuscript_sections")
            manuscript = None
            if raw_manuscript is not None:
                if not isinstance(raw_manuscript, Mapping):
                    raise ValueError("manuscript_sections must be an object")
                required_sections = (
                    "title",
                    "abstract",
                    "research_question",
                    "data_and_methods",
                    "results",
                    "limitations",
                    "conclusion",
                )
                if set(raw_manuscript) != set(required_sections):
                    raise ValueError("manuscript_sections has missing or unknown fields")
                manuscript = ManuscriptSections(
                    **{name: str(raw_manuscript[name]) for name in required_sections}
                )
            self._tables.register_builtin_profile(
                RegisterEsttabProfileCommand(self._identities.new(CommandId), request.turn_id)
            )
            table = await self._tables.export(
                ExportEsttabTableCommand(
                    self._identities.new(CommandId),
                    request.turn_id,
                    self._path_id,
                    result_slot,
                    title,
                    coefficient_terms,
                    fit_statistic,
                    fit_label,
                )
            )
            if table.estimation_state_gate != "pass" or table.cell_evidence_gate != "pass":
                raise RuntimeError("esttab table did not pass formal delivery gates")
            delivered = self._documents.deliver(
                DeliverEsttabDocumentCommand(
                    self._identities.new(CommandId),
                    request.turn_id,
                    self._path_id,
                    table.render_receipt_id,
                    _slot_pointer(
                        self._connection,
                        "document_slots",
                        "path_document_adoptions",
                        "document_slot_id",
                        self._path_id.value,
                        "manuscript.main.working",
                    ),
                    _slot_pointer(
                        self._connection,
                        "document_slots",
                        "path_document_adoptions",
                        "document_slot_id",
                        self._path_id.value,
                        "manuscript.main.delivery",
                    ),
                    manuscript=manuscript,
                )
            )
            if delivered.verdict != "pass":
                raise RuntimeError("Word delivery gate failed")
            payload = {
                "result_slot_key": result_slot,
                "table_render_receipt_id": table.render_receipt_id.value,
                "document_revision_id": delivered.document_revision_id.value,
                "docx_artifact_id": delivered.docx_artifact_id.value,
                "delivery_verdict": delivered.verdict,
                "manuscript_composed": manuscript is not None,
            }
        except Exception as error:
            error_detail = (
                str(error)[:500]
                if isinstance(error, (ValueError, RuntimeError))
                else type(error).__name__
            )
            completed = self._bridge.complete(
                CompleteBrokerExecutionCommand(
                    self._identities.new(CommandId),
                    handle,
                    False,
                    f"Word export failed: {type(error).__name__}",
                    {
                        "error_kind": type(error).__name__,
                        "error_detail": error_detail,
                    },
                )
            )
            return ToolExecutionResult(
                completed.status == "completed",
                json.dumps(
                    {
                        "error_kind": type(error).__name__,
                        "error_detail": error_detail,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        completed = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId),
                handle,
                True,
                "Qualified Stata Result exported to Word",
                payload,
                (str(payload["docx_artifact_id"]),),
            )
        )
        return ToolExecutionResult(
            completed.status == "completed",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )


def _string_array(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("expected an array of strings")
    normalized = tuple(str(item).strip() for item in value)
    if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
        raise ValueError("array values must be non-empty and unique")
    return normalized


def _slot_pointer(
    connection: Any,
    slot_table: str,
    adoption_table: str,
    slot_id_column: str,
    path_id: str,
    canonical_key: str,
) -> int:
    row = connection.execute(
        f"""
        SELECT adoption.pointer_revision
        FROM {slot_table} AS slot
        LEFT JOIN {adoption_table} AS adoption USING ({slot_id_column})
        WHERE slot.research_path_id = ? AND slot.canonical_key = ?
        """,
        (path_id, canonical_key),
    ).fetchone()
    return 0 if row is None or row[0] is None else int(row[0])
