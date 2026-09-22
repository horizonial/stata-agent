from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

from stata_research_agent.application.artifact_data import (
    AdoptPathDataCommand,
    CaptureDataVersionCommand,
    VerifyArtifactCommand,
)
from stata_research_agent.application.artifact_service import ArtifactDataService
from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.document_delivery import (
    DeliverEsttabDocumentCommand,
)
from stata_research_agent.application.document_delivery_service import (
    DocumentDeliveryService,
)
from stata_research_agent.application.evidence import (
    AdoptPathResultCommand,
    EvidenceSlot,
    RenderFormalResultBlockCommand,
)
from stata_research_agent.application.evidence_service import EvidenceService
from stata_research_agent.application.lineage import LineageEntryKind, LineageSelector
from stata_research_agent.application.lineage_service import EvidenceLineageService
from stata_research_agent.application.result_profile import (
    PromoteStataResultCommand,
    RegisterGenericStataResultProfileCommand,
)
from stata_research_agent.application.result_profile_service import (
    RegisteredResultProfileService,
)
from stata_research_agent.application.stata_operation import ExecuteStataCommand
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.table_export import (
    ExportEsttabTableCommand,
    RegisterEsttabProfileCommand,
)
from stata_research_agent.application.table_export_service import EsttabTableExportService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.completion_manifest_store import (
    FilesystemCompletionManifestStore,
)
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.documents.word_renderer import MicrosoftWordRtfRenderer
from stata_research_agent.domain.artifact_data import (
    DataVersionKind,
    VerificationPurpose,
)
from stata_research_agent.domain.identifiers import CommandId, ResultElementId, WorkspaceId
from stata_research_agent.domain.result_profile import (
    GenericStataResultProfile,
    QualificationVerdict,
)
from stata_research_agent.domain.stata_execution import (
    StataArtifactOutputRequest,
    StataExecutionStatus,
)
from stata_research_agent.domain.status import RecoveryClassification
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.persistence.artifact_data_store import (
    SqliteArtifactDataRepository,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.document_delivery_store import (
    SqliteDocumentDeliveryRepository,
)
from stata_research_agent.persistence.evidence_store import SqliteEvidenceRepository
from stata_research_agent.persistence.lineage_query import SqliteEvidenceLineageQuery
from stata_research_agent.persistence.result_profile_store import (
    SqliteResultProfileRepository,
)
from stata_research_agent.persistence.stata_operation_store import (
    SqliteStataOperationRepository,
)
from stata_research_agent.persistence.stata_recovery_scanner import (
    SqliteStataRecoveryScanner,
)
from stata_research_agent.persistence.table_export_store import (
    SqliteTableExportRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

MCP_ROOT = Path.home() / "stata-mcp"
MCP_PYTHON = MCP_ROOT / ".venv-uv" / "Scripts" / "python.exe"
STATA_HOME = Path(r"C:\Program Files\Stata18")


def test_real_esttab_nested_artifact_contract_accepts_stored_estimate(tmp_path: Path) -> None:
    if not (MCP_PYTHON.is_file() and AUTO_DATA.is_file()):
        pytest.skip("certified local Stata MCP environment is not installed")
    shutil.copy2(AUTO_DATA, tmp_path / "auto.dta")

    async def scenario() -> None:
        runtime = StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=tmp_path,
            stata_home=STATA_HOME,
        )
        async with runtime:
            fitted = await runtime.execute(
                session_id="esttab-nested",
                code='use "auto.dta", clear\nregress price mpg weight',
                timeout_seconds=20,
            )
            assert fitted.receipt.execution_status is StataExecutionStatus.SUCCEEDED
            exported = await runtime.execute(
                session_id="esttab-nested",
                code=(
                    "estimates store __sra_352751ad9793ddd0\n"
                    'esttab __sra_352751ad9793ddd0 using ".stata-agent/staging/attempt_nested/'
                    'tables/baseline-table.rtf", replace rtf b(%18.3f) se(%18.3f) '
                    "keep(mpg weight _cons) stats(N r2, fmt(0 3) "
                    'labels("Observations" "R-squared")) '
                    'star(* 0.05 ** 0.01 *** 0.001) title("Baseline regression")\n'
                    "estimates drop __sra_352751ad9793ddd0"
                ),
                timeout_seconds=20,
                operation_attempt_id="attempt_nested",
                artifact_outputs=(
                    StataArtifactOutputRequest(
                        "table.baseline",
                        "tables/baseline-table.rtf",
                        "table",
                        "application/rtf",
                    ),
                ),
            )
            assert exported.receipt.execution_status is StataExecutionStatus.SUCCEEDED
            assert len(exported.artifacts) == 1

    asyncio.run(scenario())
AUTO_DATA = STATA_HOME / "auto.dta"


def test_real_stdio_runtime_executes_and_closes_one_stata_session(tmp_path: Path) -> None:
    python_executable = MCP_PYTHON
    if not (python_executable.is_file() and AUTO_DATA.is_file()):
        pytest.skip("certified local Stata MCP environment is not installed")
    shutil.copy2(AUTO_DATA, tmp_path / "auto.dta")
    workspace_root = tmp_path / "ws_real_stata_mcp"
    database = WorkspaceDatabase(workspace_root, WorkspaceId("ws_real_stata_mcp"))
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(
            CommandId("cmd_real_stata_workspace"), WorkspaceId("ws_real_stata_mcp")
        )
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_real_stata_turn"), "Run auto regression")
    )
    managed_store = FilesystemManagedArtifactStore(workspace_root)
    artifact_service = ArtifactDataService(
        SqliteArtifactDataRepository(connection),
        managed_store,
        identities,
    )
    captured = artifact_service.capture_data_version(
        CaptureDataVersionCommand(
            CommandId("cmd_real_stata_data_capture"),
            tmp_path / "auto.dta",
            DataVersionKind.EXTERNAL_IMPORT,
            turn.turn_id,
        )
    )
    verified = artifact_service.verify_artifact(
        VerifyArtifactCommand(
            CommandId("cmd_real_stata_data_verify"),
            captured.artifact_id,
            VerificationPurpose.FORMAL_RUN_INPUT,
        )
    )

    async def scenario() -> None:
        runtime = StdioStataRuntime(
            python_executable=python_executable,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=workspace_root,
            stata_home=STATA_HOME,
        )
        async with runtime:
            loaded = await runtime.execute(
                session_id="agent-next-integration",
                code=f'use "{(tmp_path / "auto.dta").as_posix()}", clear',
                timeout_seconds=20,
            )
            assert loaded.receipt.execution_status is StataExecutionStatus.SUCCEEDED
            result = await runtime.execute(
                session_id="agent-next-integration",
                code="regress price mpg weight",
                timeout_seconds=20,
            )
            assert result.receipt.execution_status is StataExecutionStatus.SUCCEEDED
            assert result.receipt.data_signature
            assert result.structured is not None
            assert result.structured["N"] == 74.0
            assert result.structured["r2"] == pytest.approx(0.29338912319475285)
            summary = await runtime.execute(
                session_id="agent-next-integration",
                code="summarize price, detail",
                timeout_seconds=20,
            )
            assert summary.receipt.execution_status is StataExecutionStatus.SUCCEEDED
            assert summary.receipt.structured_result_status == "complete"
            assert summary.structured is not None
            summary_keys = {
                item["source_key"] for item in summary.structured["result_catalog"]["elements"]
            }
            assert {"scalar.N", "scalar.mean", "scalar.p50"} <= summary_keys
            table = await runtime.execute(
                session_id="agent-next-integration",
                code=(
                    'esttab using ".stata-agent/staging/attempt_direct_esttab/table.rtf", '
                    "replace rtf b(%9.3f) se(%9.3f) "
                    'stats(N r2, fmt(0 3) labels("Observations" "R-squared")) '
                    'star(* 0.05 ** 0.01 *** 0.001) title("Baseline regression")'
                ),
                timeout_seconds=20,
                operation_attempt_id="attempt_direct_esttab",
                artifact_outputs=(
                    StataArtifactOutputRequest(
                        "table.baseline",
                        "table.rtf",
                        "table",
                        "application/rtf",
                    ),
                ),
            )
            assert table.receipt.execution_status is StataExecutionStatus.SUCCEEDED
            assert len(table.artifacts) == 1
            assert Path(table.artifacts[0].source_path).read_bytes().startswith(b"{\\rtf")
            closed = await runtime.close_session(
                session_id="agent-next-integration", reason="integration test cleanup"
            )
            assert closed.closed is True
            assert closed.detail["before"]["worker_alive_observed"] is True
            assert closed.detail["after"]["worker_alive_observed"] is False

            completion_store = FilesystemCompletionManifestStore(workspace_root)
            service = StataOperationService(
                SqliteStataOperationRepository(connection),
                runtime,
                identities,
                completion_store,
                managed_store,
            )
            formal_loaded = await service.execute(
                ExecuteStataCommand(
                    CommandId("cmd_real_stata_data_load"),
                    turn.turn_id,
                    "agent-next-operation",
                    f'use "{captured.managed_handle}", clear',
                    20,
                    input_data_version_id=captured.data_version_id,
                    input_data_slot_key="analysis.primary",
                    input_verification_receipt_id=verified.receipt_id,
                    execution_purpose="data_load",
                )
            )
            assert formal_loaded.status == "completed"
            session_binding = service.session_data_binding(formal_loaded.operation_id)
            outcome = await service.execute(
                ExecuteStataCommand(
                    CommandId("cmd_real_stata_operation"),
                    turn.turn_id,
                    "agent-next-operation",
                    "regress price mpg weight",
                    20,
                    input_data_version_id=captured.data_version_id,
                    input_data_slot_key="analysis.primary",
                    input_verification_receipt_id=verified.receipt_id,
                    execution_purpose="formal_estimation",
                    source_data_state_operation_id=(session_binding.source_data_state_operation_id),
                    expected_data_state_token=session_binding.data_state_token,
                    expected_session_generation=session_binding.session_generation,
                )
            )
            assert outcome.status == "completed"
            manifest = connection.execute(
                """
                SELECT execution_status, session_generation, structured_result_json
                FROM completion_manifests WHERE completion_manifest_id = ?
                """,
                (outcome.manifest_id.value,),
            ).fetchone()
            assert manifest["execution_status"] == "succeeded"
            assert manifest["session_generation"] >= 1
            assert '"N":74.0' in manifest["structured_result_json"]
            profile_service = RegisteredResultProfileService(
                SqliteResultProfileRepository(connection), identities
            )
            profile_service.register_generic_profile(
                RegisterGenericStataResultProfileCommand(
                    CommandId("cmd_real_regress_profile_register"), turn.turn_id
                )
            )
            selected_source_keys = (
                "term.mpg.coefficient",
                "term.mpg.se",
                "term.weight.coefficient",
                "term.weight.se",
                "term._cons.coefficient",
                "term._cons.se",
                "scalar.N",
                "scalar.r2",
            )
            qualified = profile_service.promote(
                PromoteStataResultCommand(
                    CommandId("cmd_real_regress_qualify"),
                    outcome.operation_id,
                    turn.turn_id,
                    initialized.main_path_id,
                    selected_source_keys,
                    "Baseline relation between price, fuel economy, and weight.",
                )
            )
            assert qualified.verdict is QualificationVerdict.QUALIFIED
            assert qualified.result_id is not None
            assert qualified.element_count == 8
            assert (
                connection.execute(
                    """
                SELECT canonical_decimal_text FROM result_elements
                WHERE semantic_key = 'term.weight.coefficient'
                """
                ).fetchone()[0]
                == "1.7465591583457587"
            )
            artifact_service.adopt_path_data(
                AdoptPathDataCommand(
                    CommandId("cmd_real_table_adopt_data"),
                    initialized.main_path_id,
                    "analysis.primary",
                    captured.data_version_id,
                    0,
                )
            )
            evidence_service = EvidenceService(SqliteEvidenceRepository(connection), identities)
            evidence_service.adopt_path_result(
                AdoptPathResultCommand(
                    CommandId("cmd_real_table_adopt_result"),
                    initialized.main_path_id,
                    "baseline.primary",
                    qualified.result_id,
                    turn.turn_id,
                    0,
                )
            )
            weight_element_id = ResultElementId(
                str(
                    connection.execute(
                        """
                        SELECT result_element_id FROM result_elements
                        WHERE result_id = ? AND semantic_key = 'term.weight.coefficient'
                        """,
                        (qualified.result_id.value,),
                    ).fetchone()[0]
                )
            )
            message_block = evidence_service.render_formal_block(
                RenderFormalResultBlockCommand(
                    CommandId("cmd_real_lineage_message"),
                    turn.turn_id,
                    initialized.main_path_id,
                    "baseline.primary",
                    "weight coefficient: {coef}",
                    (EvidenceSlot("coef", weight_element_id, 3),),
                )
            )
            table_service = EsttabTableExportService(
                SqliteTableExportRepository(connection),
                service,
                managed_store,
                identities,
            )
            table_service.register_builtin_profile(
                RegisterEsttabProfileCommand(
                    CommandId("cmd_real_esttab_profile_register"), turn.turn_id
                )
            )
            # A later command may legitimately advance the live Stata session before the
            # user asks for a table.  Export must materialize the immutable qualified Result
            # rather than requiring the old in-memory e() state and exact exec sequence.
            intervening = await service.execute(
                ExecuteStataCommand(
                    CommandId("cmd_real_intervening_summary"),
                    turn.turn_id,
                    "agent-next-operation",
                    "summarize price",
                    20,
                )
            )
            assert intervening.status == "completed"
            exported = await table_service.export(
                ExportEsttabTableCommand(
                    CommandId("cmd_real_esttab_export"),
                    turn.turn_id,
                    initialized.main_path_id,
                    "baseline.primary",
                )
            )
            assert exported.estimation_state_gate == "pass"
            assert exported.cell_evidence_gate == "pass"
            assert exported.cell_count == 8
            state_verification = json.loads(
                str(
                    connection.execute(
                        """
                        SELECT state_verification_json FROM table_export_manifests
                        WHERE table_export_manifest_id = ?
                        """,
                        (exported.export_manifest_id.value,),
                    ).fetchone()[0]
                )
            )
            assert state_verification["render_source"] == "immutable_result_elements"
            assert state_verification["live_estimation_state_required"] is False
            assert (
                connection.execute("SELECT count(*) FROM table_cell_evidence_uses").fetchone()[0]
                == 8
            )
            document_service = DocumentDeliveryService(
                SqliteDocumentDeliveryRepository(connection),
                managed_store,
                identities,
                MicrosoftWordRtfRenderer(),
            )
            delivered = document_service.deliver(
                DeliverEsttabDocumentCommand(
                    CommandId("cmd_real_docx_delivery"),
                    turn.turn_id,
                    initialized.main_path_id,
                    exported.render_receipt_id,
                )
            )
            assert delivered.verdict == "pass"
            assert delivered.working_pointer_revision == 1
            assert delivered.delivery_pointer_revision == 1
            docx_handle = connection.execute(
                "SELECT managed_handle FROM artifact_locations WHERE artifact_id = ?",
                (delivered.docx_artifact_id.value,),
            ).fetchone()[0]
            docx = managed_store.read_small_payload(str(docx_handle), max_bytes=50 * 1024 * 1024)
            assert docx.startswith(b"PK")
            assert (
                connection.execute("SELECT verdict FROM delivery_gate_reports").fetchone()[0]
                == "pass"
            )

            occurrence = connection.execute(
                """
                SELECT byte_start FROM numeric_occurrences
                WHERE formal_result_block_id = ?
                """,
                (message_block.formal_result_block_id.value,),
            ).fetchone()
            evidence_record_id = connection.execute(
                """
                SELECT evidence_record_id FROM evidence_statistical_sources
                WHERE result_element_id = ?
                """,
                (weight_element_id.value,),
            ).fetchone()[0]
            lineage = EvidenceLineageService(SqliteEvidenceLineageQuery(connection))
            entries = (
                lineage.load(
                    LineageSelector(
                        LineageEntryKind.MESSAGE_OCCURRENCE,
                        message_block.formal_result_block_id.value,
                        str(occurrence["byte_start"]),
                    )
                ),
                lineage.load(
                    LineageSelector(LineageEntryKind.RESULT_ELEMENT, weight_element_id.value)
                ),
                lineage.load(
                    LineageSelector(
                        LineageEntryKind.DOCUMENT_CELL,
                        delivered.document_revision_id.value,
                        "row.2.coefficient",
                    )
                ),
                lineage.load(
                    LineageSelector(LineageEntryKind.EVIDENCE_RECORD, str(evidence_record_id))
                ),
            )
            assert len({entry.source_chain_sha256 for entry in entries}) == 1
            assert len({entry.authoritative_revision for entry in entries}) == 1
            assert {entry.result_element_id for entry in entries} == {weight_element_id.value}
            assert {entry.command_text for entry in entries} == {"regress price mpg weight"}
            assert {entry.data_version_id for entry in entries} == {captured.data_version_id.value}
            assert entries[0].formal_result_block_id == message_block.formal_result_block_id.value
            assert entries[2].document_revision_id == delivered.document_revision_id.value
            assert entries[2].semantic_cell_slot == "row.2.coefficient"
            assert entries[3].source_journal.event_type == "result.qualification_recorded"

            api = create_app(WorkspaceHost(tmp_path))
            transport = httpx.ASGITransport(app=api)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/api/v1/workspaces/ws_real_stata_mcp/lineage",
                    params={
                        "entry_kind": "evidence_record",
                        "entry_id": str(evidence_record_id),
                    },
                )
            assert response.status_code == 200
            assert response.json()["data"]["source_chain_sha256"] == entries[3].source_chain_sha256
            assert (
                response.json()["authoritative_revision"] == entries[3].authoritative_revision.value
            )

            cross_layer_assertions = {
                "data_version_owns_captured_artifact": connection.execute(
                    "SELECT canonical_artifact_id FROM data_versions WHERE data_version_id = ?",
                    (captured.data_version_id.value,),
                ).fetchone()[0]
                == captured.artifact_id.value,
                "formal_input_uses_exact_verification_receipt": connection.execute(
                    """
                    SELECT input_verification_receipt_id
                    FROM stata_operation_input_bindings WHERE operation_attempt_id = ?
                    """,
                    (outcome.attempt_id.value,),
                ).fetchone()[0]
                == verified.receipt_id.value,
                "run_preserves_operation_identity": entries[0].operation_id
                == outcome.operation_id.value,
                "run_preserves_attempt_identity": entries[0].operation_attempt_id
                == outcome.attempt_id.value,
                "run_preserves_completion_manifest_identity": entries[0].completion_manifest_id
                == outcome.manifest_id.value,
                "research_command_preserves_exact_text": entries[0].command_text
                == "regress price mpg weight",
                "qualified_result_preserves_run_identity": entries[0].stata_run_id
                == qualified.run_id.value,
                "element_locator_uses_candidate_snapshot": connection.execute(
                    """
                    SELECT locator.result_capture_snapshot_id = candidate.result_capture_snapshot_id
                    FROM result_elements AS element
                    JOIN result_source_locators AS locator
                      ON locator.result_source_locator_id = element.result_source_locator_id
                    JOIN results AS result ON result.result_id = element.result_id
                    JOIN result_candidates AS candidate
                      ON candidate.result_candidate_id = result.result_candidate_id
                    WHERE element.result_element_id = ?
                    """,
                    (weight_element_id.value,),
                ).fetchone()[0]
                == 1,
                "evidence_preserves_result_element_identity": entries[0].result_element_id
                == weight_element_id.value,
                "table_input_preserves_result_and_run": connection.execute(
                    """
                    SELECT source_result_id = ? AND source_stata_run_id = ?
                    FROM table_export_input_manifests
                    WHERE table_export_input_manifest_id = ?
                    """,
                    (
                        qualified.result_id.value,
                        qualified.run_id.value,
                        exported.input_manifest_id.value,
                    ),
                ).fetchone()[0]
                == 1,
                "table_estimation_state_gate_passed": exported.estimation_state_gate == "pass",
                "table_cell_evidence_and_coverage_passed": (
                    exported.cell_evidence_gate == "pass" and exported.cell_count == 8
                ),
                "document_manifest_and_dual_adoption_preserve_table": connection.execute(
                    """
                    SELECT count(*) = 2
                    FROM document_revisions AS revision
                    JOIN document_manifests AS manifest
                      ON manifest.document_manifest_id = revision.document_manifest_id
                    JOIN path_document_adoptions AS adoption
                      ON adoption.target_document_revision_id = revision.document_revision_id
                    WHERE revision.document_revision_id = ?
                      AND manifest.table_render_receipt_id = ?
                    """,
                    (
                        delivered.document_revision_id.value,
                        exported.render_receipt_id.value,
                    ),
                ).fetchone()[0]
                == 1,
            }
            assert len(cross_layer_assertions) == 13
            assert all(cross_layer_assertions.values()), {
                key: passed for key, passed in cross_layer_assertions.items() if not passed
            }

            # A wider Agent-selected specification must not be forced through the legacy
            # eight-cell table shape.  Five coefficient rows plus N and fit statistic form
            # twelve independently traceable numeric cells.  Repeating the exact export in
            # the same Turn reuses that verified Artifact rather than touching Stata again.
            expanded = await service.execute(
                ExecuteStataCommand(
                    CommandId("cmd_real_expanded_operation"),
                    turn.turn_id,
                    "agent-next-operation",
                    "regress price mpg weight length turn",
                    20,
                    input_data_version_id=captured.data_version_id,
                    input_data_slot_key="analysis.primary",
                    input_verification_receipt_id=verified.receipt_id,
                    execution_purpose="formal_estimation",
                    source_data_state_operation_id=(
                        session_binding.source_data_state_operation_id
                    ),
                    expected_data_state_token=session_binding.data_state_token,
                    expected_session_generation=session_binding.session_generation,
                )
            )
            expanded_keys = tuple(
                key
                for term in ("mpg", "weight", "length", "turn", "_cons")
                for key in (f"term.{term}.coefficient", f"term.{term}.se")
            ) + ("scalar.N", "scalar.r2")
            expanded_result = profile_service.promote(
                PromoteStataResultCommand(
                    CommandId("cmd_real_expanded_promote"),
                    expanded.operation_id,
                    turn.turn_id,
                    initialized.main_path_id,
                    expanded_keys,
                    "Expanded price regression selected by the Agent.",
                )
            )
            assert expanded_result.verdict is QualificationVerdict.QUALIFIED
            assert expanded_result.result_id is not None
            evidence_service.adopt_path_result(
                AdoptPathResultCommand(
                    CommandId("cmd_real_expanded_adopt"),
                    initialized.main_path_id,
                    "robustness.expanded",
                    expanded_result.result_id,
                    turn.turn_id,
                    0,
                )
            )
            expanded_export_command = ExportEsttabTableCommand(
                CommandId("cmd_real_expanded_export"),
                turn.turn_id,
                initialized.main_path_id,
                "robustness.expanded",
                "Expanded regression",
                ("mpg", "weight", "length", "turn", "_cons"),
            )
            expanded_table = await table_service.export(expanded_export_command)
            assert expanded_table.cell_count == 12
            assert expanded_table.replayed is False
            assert (
                connection.execute(
                    """
                    SELECT controlled_cell_count
                    FROM table_render_shape_manifests
                    WHERE table_render_receipt_id = ?
                    """,
                    (expanded_table.render_receipt_id.value,),
                ).fetchone()[0]
                == 12
            )
            run_count_after_export = connection.execute(
                "SELECT count(*) FROM stata_runs"
            ).fetchone()[0]
            replayed_table = await table_service.export(
                ExportEsttabTableCommand(
                    CommandId("cmd_real_expanded_export_retry"),
                    turn.turn_id,
                    initialized.main_path_id,
                    "robustness.expanded",
                    "Expanded regression",
                    ("mpg", "weight", "length", "turn", "_cons"),
                )
            )
            assert replayed_table.replayed is True
            assert replayed_table.render_receipt_id == expanded_table.render_receipt_id
            assert (
                connection.execute("SELECT count(*) FROM stata_runs").fetchone()[0]
                == run_count_after_export
            )
            expanded_document = document_service.deliver(
                DeliverEsttabDocumentCommand(
                    CommandId("cmd_real_expanded_docx_delivery"),
                    turn.turn_id,
                    initialized.main_path_id,
                    replayed_table.render_receipt_id,
                    expected_working_pointer_revision=1,
                    expected_delivery_pointer_revision=1,
                )
            )
            assert expanded_document.verdict == "pass"
            assert expanded_document.working_pointer_revision == 2
            assert expanded_document.delivery_pointer_revision == 2
            assert (
                connection.execute(
                    """
                    SELECT count(*)
                    FROM statistical_evidence_use_validation_receipts AS receipt
                    JOIN document_revisions AS revision
                      ON revision.created_revision = receipt.created_revision
                    WHERE revision.document_revision_id = ?
                      AND receipt.purpose = 'document_delivery'
                    """,
                    (expanded_document.document_revision_id.value,),
                ).fetchone()[0]
                == 12
            )

            # Probit was never registered as a method profile.  It must still be promotable
            # because the authoritative boundary is the generic Stata source catalog.
            probit = await service.execute(
                ExecuteStataCommand(
                    CommandId("cmd_real_probit_operation"),
                    turn.turn_id,
                    "agent-next-operation",
                    "probit foreign mpg weight, vce(robust)",
                    20,
                    input_data_version_id=captured.data_version_id,
                    input_data_slot_key="analysis.primary",
                    input_verification_receipt_id=verified.receipt_id,
                    execution_purpose="formal_estimation",
                    source_data_state_operation_id=(session_binding.source_data_state_operation_id),
                    expected_data_state_token=session_binding.data_state_token,
                    expected_session_generation=session_binding.session_generation,
                )
            )
            probit_result = profile_service.promote(
                PromoteStataResultCommand(
                    CommandId("cmd_real_probit_promote"),
                    probit.operation_id,
                    turn.turn_id,
                    initialized.main_path_id,
                    (
                        "term.mpg.coefficient",
                        "term.mpg.se",
                        "scalar.N",
                        "scalar.r2_p",
                    ),
                    "Exploratory binary-response result selected by the Agent.",
                )
            )
            assert probit_result.verdict is QualificationVerdict.QUALIFIED
            assert probit_result.result_id is not None
            summary_operation = await service.execute(
                ExecuteStataCommand(
                    CommandId("cmd_real_summary_operation"),
                    turn.turn_id,
                    "agent-next-operation",
                    "summarize price, detail",
                    20,
                    input_data_version_id=captured.data_version_id,
                    input_data_slot_key="analysis.primary",
                    input_verification_receipt_id=verified.receipt_id,
                    execution_purpose="formal_estimation",
                    source_data_state_operation_id=(session_binding.source_data_state_operation_id),
                    expected_data_state_token=session_binding.data_state_token,
                    expected_session_generation=session_binding.session_generation,
                )
            )
            summary_result = profile_service.promote(
                PromoteStataResultCommand(
                    CommandId("cmd_real_summary_promote"),
                    summary_operation.operation_id,
                    turn.turn_id,
                    initialized.main_path_id,
                    ("scalar.N", "scalar.mean", "scalar.p50"),
                    "Selected descriptive statistics produced by Stata.",
                )
            )
            assert summary_result.verdict is QualificationVerdict.QUALIFIED
            assert summary_result.result_id is not None
            assert connection.execute("SELECT count(*) FROM result_profiles").fetchone()[0] == 1

            interrupted_task = asyncio.create_task(
                service.execute(
                    ExecuteStataCommand(
                        CommandId("cmd_real_stata_mcp_process_loss"),
                        turn.turn_id,
                        "agent-next-process-loss",
                        "sleep 10000",
                        20,
                    )
                )
            )
            await asyncio.sleep(0.5)
            await runtime.close()
            interrupted = await asyncio.wait_for(interrupted_task, timeout=10)
            assert interrupted.status == "outcome_unknown"
            assessment = SqliteStataRecoveryScanner(connection, completion_store).assess(
                interrupted.operation_id.value
            )
            assert assessment.classification is RecoveryClassification.OUTCOME_UNKNOWN

    try:
        asyncio.run(scenario())
    finally:
        connection.close()


def test_real_reghdfe_snapshot_exposes_generic_result_catalog(tmp_path: Path) -> None:
    if not (MCP_PYTHON.is_file() and AUTO_DATA.is_file()):
        pytest.skip("certified local Stata MCP environment is not installed")
    shutil.copy2(AUTO_DATA, tmp_path / "auto.dta")

    async def scenario() -> None:
        runtime = StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=tmp_path,
            stata_home=STATA_HOME,
        )
        async with runtime:
            loaded = await runtime.execute(
                session_id="agent-next-reghdfe-profile",
                code=f'use "{(tmp_path / "auto.dta").as_posix()}", clear',
                timeout_seconds=20,
            )
            assert loaded.receipt.execution_status is StataExecutionStatus.SUCCEEDED
            result = await runtime.execute(
                session_id="agent-next-reghdfe-profile",
                code="reghdfe price mpg, absorb(foreign) vce(cluster rep78)",
                timeout_seconds=30,
            )
            assert result.receipt.execution_status is StataExecutionStatus.SUCCEEDED
            evaluation = GenericStataResultProfile().evaluate(
                result.structured,
                selected_source_keys=(
                    "scalar.N",
                    "scalar.r2",
                    "scalar.r2_within",
                    "term.mpg.coefficient",
                ),
            )
            assert evaluation.verdict is QualificationVerdict.QUALIFIED
            assert {item.semantic_key for item in evaluation.elements} >= {
                "scalar.N",
                "scalar.r2",
                "scalar.r2_within",
                "term.mpg.coefficient",
            }

    asyncio.run(scenario())
