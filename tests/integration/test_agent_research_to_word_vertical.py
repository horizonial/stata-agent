"""M2-06 real autonomous idea + data → Stata → Evidence table → Word vertical."""

from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest

from stata_research_agent.application.artifact_data import (
    CaptureDataVersionCommand,
    VerifyArtifactCommand,
)
from stata_research_agent.application.artifact_service import ArtifactDataService
from stata_research_agent.application.broker_execution_service import BrokerExecutionService
from stata_research_agent.application.control import (
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.document_delivery_service import (
    DocumentDeliveryService,
)
from stata_research_agent.application.document_roundtrip import (
    ImportReturnedDocumentCommand,
    MergeDocumentRevisionsCommand,
    RejectedDocumentReturn,
)
from stata_research_agent.application.document_roundtrip_service import (
    DocumentRoundtripService,
)
from stata_research_agent.application.evaluation import (
    ContractObligationCandidate,
    NormalizeCompletionContractCommand,
    ObligationProvenance,
    RequirementLevel,
)
from stata_research_agent.application.evaluation_service import RuntimeEvaluationService
from stata_research_agent.application.evidence_eligibility import (
    EligibilityVerdict,
    EvidenceEligibilityQuery,
    EvidenceUseContext,
    EvidenceUsePurpose,
    ValidateEvidenceUseCommand,
)
from stata_research_agent.application.evidence_eligibility_service import (
    EvidenceEligibilityService,
)
from stata_research_agent.application.evidence_service import EvidenceService
from stata_research_agent.application.founder_acceptance import (
    ControlledJourneyAuditRequest,
    FounderAuditMode,
    FounderAuditStatus,
    FounderJourneyAuditRequest,
)
from stata_research_agent.application.model_gateway import (
    ContextItemCandidate,
    ProviderResponse,
)
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.research_state import (
    AdoptPlanRevisionCommand,
    AdoptResearchBundleCommand,
    CreatePlanRevisionCommand,
    CreateResearchPathBranchCommand,
    DataSlotAdoptionTarget,
    PlanDependencyCandidate,
    PlanNodeCandidate,
    ResultSlotAdoptionTarget,
)
from stata_research_agent.application.research_state_service import ResearchStateService
from stata_research_agent.application.research_workflow import RunResearchToWordCommand
from stata_research_agent.application.research_workflow_service import ResearchToWordService
from stata_research_agent.application.result_profile_service import (
    RegisteredResultProfileService,
)
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.table_export_service import EsttabTableExportService
from stata_research_agent.application.tool_broker import (
    RegisterToolContractCommand,
    ResourceClaimTemplate,
    ToolContractDefinition,
)
from stata_research_agent.application.tool_broker_service import ToolBrokerService
from stata_research_agent.application.turn_driver import (
    TurnDriverConfig,
    TurnDriverModelConfig,
)
from stata_research_agent.application.turn_interaction import (
    AnswerWaitingCommand,
    ContinuePausedTurnCommand,
    ConvergePauseCommand,
    OpenWaitingCommand,
    RequestPauseCommand,
)
from stata_research_agent.application.turn_interaction_service import TurnInteractionService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.completion_manifest_store import (
    FilesystemCompletionManifestStore,
)
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.documents.roundtrip import OoxmlDocumentRoundtripEngine
from stata_research_agent.documents.word_renderer import MicrosoftWordRtfRenderer
from stata_research_agent.domain.artifact_data import (
    DataVersionKind,
    VerificationPurpose,
)
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    ArtifactLocationId,
    ArtifactStateObservationId,
    CommandId,
    DataVersionId,
    DocumentManifestId,
    DocumentRevisionId,
    EvidenceRecordId,
    OperationAttemptId,
    OperationId,
    PathDataSlotId,
    PlanNodeId,
    ResultId,
    ResultSlotId,
    TurnId,
    WorkspaceId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.status import TurnStatus, WaitReason
from stata_research_agent.interfaces.founder_scenario import FounderScenarioLoader
from stata_research_agent.persistence.artifact_data_store import (
    SqliteArtifactDataRepository,
)
from stata_research_agent.persistence.atomic_commit import (
    AtomicCommitService,
    CommitCrashPoint,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)
from stata_research_agent.persistence.broker_execution_store import (
    SqliteBrokerExecutionRepository,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.document_delivery_store import (
    SqliteDocumentDeliveryRepository,
)
from stata_research_agent.persistence.document_roundtrip_store import (
    SqliteDocumentRoundtripRepository,
)
from stata_research_agent.persistence.evaluation_store import SqliteEvaluationRepository
from stata_research_agent.persistence.evidence_eligibility_store import (
    SqliteEvidenceEligibilityRepository,
)
from stata_research_agent.persistence.evidence_store import SqliteEvidenceRepository
from stata_research_agent.persistence.founder_journey_audit import (
    SqliteFounderJourneyAuditor,
)
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.persistence.research_state_store import (
    SqliteResearchStateRepository,
)
from stata_research_agent.persistence.result_profile_store import (
    SqliteResultProfileRepository,
)
from stata_research_agent.persistence.stata_operation_store import (
    SqliteStataOperationRepository,
)
from stata_research_agent.persistence.table_export_store import (
    SqliteTableExportRepository,
)
from stata_research_agent.persistence.tool_broker_store import SqliteToolBrokerRepository
from stata_research_agent.persistence.turn_interaction_store import (
    SqliteTurnInteractionRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.agent_turn_driver import AgentTurnDriver
from stata_research_agent.runtime.research_workflow_executor import (
    ResearchWorkflowExecutor,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

PROJECT_ROOT = Path(__file__).parents[2]
WORKER_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
MCP_ROOT = Path.home() / "stata-mcp"
STATA_HOME = Path(r"C:\Program Files\Stata18")
AUTO_DATA = STATA_HOME / "auto.dta"
FOUNDER_SCENARIO = PROJECT_ROOT / "verification" / "scenarios" / "founder-autonomous-auto-v1.json"
FOUNDER_CONTROLLED_SCENARIO = (
    PROJECT_ROOT / "verification" / "scenarios" / "founder-controlled-auto-v1.json"
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"


def _edit_docx(
    payload: bytes,
    *,
    prose: str | None = None,
    managed_value: str | None = None,
) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload)) as package:
        parts = {item.filename: package.read(item.filename) for item in package.infolist()}
    root = ElementTree.fromstring(parts["word/document.xml"])
    if prose is not None:
        parents = {child: parent for parent in root.iter() for child in parent}

        def in_table(node: ElementTree.Element) -> bool:
            parent = parents.get(node)
            while parent is not None:
                if parent.tag == f"{W}tbl":
                    return True
                parent = parents.get(parent)
            return False

        paragraph = next(
            item
            for item in root.iter(f"{W}p")
            if not in_table(item) and item.find(f".//{W}t") is not None
        )
        paragraph.find(f".//{W}t").text = prose
    if managed_value is not None:
        node = root.find(f".//{W}sdtContent/{W}r/{W}t")
        assert node is not None
        node.text = managed_value
    parts["word/document.xml"] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in sorted(parts.items()):
            package.writestr(name, content)
    return target.getvalue()


def _seed_agent_document_branch(
    connection: sqlite3.Connection,
    managed_store: FilesystemManagedArtifactStore,
    identities: UuidIdentityGenerator,
    *,
    base_revision_id: DocumentRevisionId,
    requested_by_turn_id: TurnId,
    agent_payload: bytes,
) -> DocumentRevisionId:
    operation_id = identities.new(OperationId)
    attempt_id = identities.new(OperationAttemptId)
    revision_id = identities.new(DocumentRevisionId)
    manifest_id = identities.new(DocumentManifestId)
    docx_artifact_id = identities.new(ArtifactId)
    docx_state_id = identities.new(ArtifactStateObservationId)
    docx_location_id = identities.new(ArtifactLocationId)
    manifest_artifact_id = identities.new(ArtifactId)
    manifest_state_id = identities.new(ArtifactStateObservationId)
    manifest_location_id = identities.new(ArtifactLocationId)
    docx_path = managed_store.write_staging_bytes(attempt_id, "agent-branch.docx", agent_payload)
    docx = managed_store.publish_candidate(
        docx_path, artifact_id=docx_artifact_id, attempt_id=attempt_id
    )
    base = connection.execute(
        """
        SELECT revision.document_id, manifest.table_render_receipt_id,
               manifest.table_coverage_manifest_id
        FROM document_revisions AS revision
        JOIN document_manifests AS manifest
          ON manifest.document_manifest_id = revision.document_manifest_id
        WHERE revision.document_revision_id = ?
        """,
        (base_revision_id.value,),
    ).fetchone()
    manifest_json = canonical_json(
        {
            "schema_version": "stata-agent.document-manifest/v2",
            "origin_kind": "agent_generated",
            "document_revision_id": revision_id.value,
            "primary_parent_revision_id": base_revision_id.value,
            "docx_sha256": docx.sha256,
        }
    )
    manifest_path = managed_store.write_staging_bytes(
        attempt_id, "agent-branch-manifest.json", manifest_json.encode()
    )
    manifest = managed_store.publish_candidate(
        manifest_path,
        artifact_id=manifest_artifact_id,
        attempt_id=attempt_id,
    )

    def mutate(db: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
        db.execute(
            "INSERT INTO operations VALUES (?, 'document.render', ?, NULL, 'completed', ?, ?, ?)",
            (
                operation_id.value,
                requested_by_turn_id.value,
                "agent-branch-test",
                revision.value,
                revision.value,
            ),
        )
        db.execute(
            "INSERT INTO operation_attempts VALUES (?, ?, 1, NULL, 'completed', ?, ?)",
            (attempt_id.value, operation_id.value, revision.value, revision.value),
        )
        observed_at = datetime.now(UTC).isoformat()
        for artifact_id, state_id, location_id, payload, media_type in (
            (
                docx_artifact_id,
                docx_state_id,
                docx_location_id,
                docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (
                manifest_artifact_id,
                manifest_state_id,
                manifest_location_id,
                manifest,
                "application/json",
            ),
        ):
            db.execute(
                """
                INSERT INTO artifacts VALUES (
                    ?, 'document', ?, ?, 'sha256', ?, ?, 'internal_artifact', ?
                )
                """,
                (
                    artifact_id.value,
                    media_type,
                    payload.size_bytes,
                    payload.sha256,
                    attempt_id.value,
                    revision.value,
                ),
            )
            db.execute(
                """
                INSERT INTO artifact_state_history VALUES (
                    ?, ?, 'available', 'test_agent_branch', ?, ?, ?, ?
                )
                """,
                (
                    state_id.value,
                    artifact_id.value,
                    payload.size_bytes,
                    payload.sha256,
                    observed_at,
                    revision.value,
                ),
            )
            db.execute(
                "INSERT INTO artifact_states VALUES (?, ?, 'available', ?, ?)",
                (artifact_id.value, state_id.value, observed_at, revision.value),
            )
            db.execute(
                "INSERT INTO artifact_location_history VALUES (?, ?, 1, 'installed', ?, ?)",
                (
                    location_id.value,
                    artifact_id.value,
                    payload.managed_handle,
                    revision.value,
                ),
            )
            db.execute(
                "INSERT INTO artifact_locations VALUES (?, ?, 1, ?, ?)",
                (
                    artifact_id.value,
                    location_id.value,
                    payload.managed_handle,
                    revision.value,
                ),
            )
        db.execute(
            "INSERT INTO document_manifests VALUES (?, ?, ?, ?, ?, 'word.rtf-to-docx.v1', ?)",
            (
                manifest_id.value,
                str(base["table_render_receipt_id"]),
                str(base["table_coverage_manifest_id"]),
                manifest_json,
                manifest.sha256,
                revision.value,
            ),
        )
        db.execute(
            "INSERT INTO document_revisions VALUES (?, ?, 'agent_generated', ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id.value,
                str(base["document_id"]),
                base_revision_id.value,
                docx_artifact_id.value,
                manifest_artifact_id.value,
                manifest_id.value,
                requested_by_turn_id.value,
                operation_id.value,
                revision.value,
            ),
        )
        return MutationPayload(
            {"document_revision_id": revision_id.value},
            (
                JournalDraft(
                    "test.agent_document_branch",
                    "document_revision",
                    revision_id.value,
                    {"base_document_revision_id": base_revision_id.value},
                ),
            ),
            (
                OutboxDraft(
                    "test.agent_document_branch",
                    {"document_revision_id": revision_id.value},
                ),
            ),
        )

    AtomicCommitService(connection).commit_mutation(
        command_id=CommandId("cmd_agent_word_seed_agent_branch"),
        command_type="test.agent_document_branch",
        request={"base_document_revision_id": base_revision_id.value},
        mutation=mutate,
    )
    return revision_id


class Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id,
            "credentialversion_test",
            endpoint,
            "agent-word-test-secret",
        )


class ResearchToWordModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        self.calls += 1
        if self.calls == 1:
            assert "price" in request_json and "mpg" in request_json
            return ProviderResponse(
                {
                    "text": "I will run the registered traceable research workflow.",
                    "tool_calls": [{"name": "research.run_to_word", "arguments": {}}],
                }
            )
        assert "document_revision_id" in request_json
        return ProviderResponse(
            {
                "text": "The source-linked Word draft is available.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "The required Word draft passed its delivery gate.",
                },
            }
        )


def workflow_contract() -> ToolContractDefinition:
    return ToolContractDefinition(
        "research.run_to_word",
        "1.0.0",
        "Run traceable research workflow to Word",
        "document.render",
        {"type": "object", "properties": {}, "additionalProperties": False},
        {"type": "object"},
        "local_runtime",
        "workspace_write",
        "exclusive_scope",
        "non_replayable",
        "never",
        "not_interruptible",
        600,
        3600,
        5_000_000,
        (ResourceClaimTemplate("research-workflow:main", "exclusive"),),
    )


def test_real_agent_turn_delivers_traceable_word_without_user_intervention(
    tmp_path: Path,
) -> None:
    mcp_python = MCP_ROOT / ".venv" / "Scripts" / "python.exe"
    if not (mcp_python.is_file() and AUTO_DATA.is_file()):
        pytest.skip("certified local Stata MCP environment is not installed")
    source = tmp_path / "auto.dta"
    shutil.copy2(AUTO_DATA, source)
    workspace_root = tmp_path / "workspace"
    workspace_id = WorkspaceId("ws_agent_word")
    database = WorkspaceDatabase(workspace_root, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    initialized = control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_agent_word_workspace"), workspace_id)
    )
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_agent_word_turn"),
            "Use auto data to regress price on mpg and weight and deliver Word.",
        )
    )
    managed_store = FilesystemManagedArtifactStore(workspace_root)
    artifacts = ArtifactDataService(
        SqliteArtifactDataRepository(connection), managed_store, identities
    )
    captured = artifacts.capture_data_version(
        CaptureDataVersionCommand(
            CommandId("cmd_agent_word_capture"),
            source,
            DataVersionKind.EXTERNAL_IMPORT,
            turn.turn_id,
        )
    )
    verified = artifacts.verify_artifact(
        VerifyArtifactCommand(
            CommandId("cmd_agent_word_verify"),
            captured.artifact_id,
            VerificationPurpose.FORMAL_RUN_INPUT,
        )
    )
    evaluator = RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities)
    normalized = evaluator.normalize_contract(
        NormalizeCompletionContractCommand(
            CommandId("cmd_agent_word_contract"),
            turn.turn_id,
            1,
            "Estimate price on mpg and weight and deliver a source-linked Word draft.",
            (
                ContractObligationCandidate(
                    "deliver.word",
                    "Deliver source-linked Word",
                    ObligationProvenance.USER_EXPLICIT,
                    "message",
                    turn.message_id.value,
                    RequirementLevel.REQUIRED,
                    "A Word revision passes the delivery and evidence gates.",
                ),
            ),
        )
    )
    research_state = ResearchStateService(SqliteResearchStateRepository(connection), identities)
    plan = research_state.create_plan_revision(
        CreatePlanRevisionCommand(
            CommandId("cmd_agent_word_plan"),
            turn.turn_id,
            "study.agent-word",
            "Regress price on mpg and weight, then deliver Word.",
            {"outcome": "price", "terms": ["mpg", "weight"]},
            (
                PlanNodeCandidate(
                    "variables.confirmed",
                    "variable_construction",
                    {"variables": ["price", "mpg", "weight"]},
                ),
                PlanNodeCandidate(
                    "estimate.baseline",
                    "estimation",
                    {"command": "regress price mpg weight"},
                ),
            ),
            (PlanDependencyCandidate("variables.confirmed", "estimate.baseline", "data"),),
        )
    )
    research_state.adopt_plan_revision(
        AdoptPlanRevisionCommand(
            CommandId("cmd_agent_word_adopt_plan"),
            initialized.main_path_id,
            plan.plan_revision_id,
            turn.turn_id,
            0,
        )
    )
    estimation_node_id = PlanNodeId(
        str(
            connection.execute(
                """
                SELECT plan_node_id FROM plan_nodes
                WHERE plan_id = ? AND canonical_key = 'estimate.baseline'
                """,
                (plan.plan_id.value,),
            ).fetchone()[0]
        )
    )
    broker = ToolBrokerService(SqliteToolBrokerRepository(connection), identities)
    broker.register_contract(
        RegisterToolContractCommand(
            CommandId("cmd_agent_word_register_tool"),
            turn.turn_id,
            workflow_contract(),
        )
    )

    async def scenario() -> None:
        runtime = StdioStataRuntime(
            python_executable=mcp_python,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=workspace_root,
            stata_home=STATA_HOME,
        )
        async with runtime:
            stata = StataOperationService(
                SqliteStataOperationRepository(connection),
                runtime,
                identities,
                FilesystemCompletionManifestStore(workspace_root),
                managed_store,
            )
            workflow = ResearchToWordService(
                stata,
                RegisteredResultProfileService(
                    SqliteResultProfileRepository(connection), identities
                ),
                artifacts,
                EvidenceService(SqliteEvidenceRepository(connection), identities),
                EsttabTableExportService(
                    SqliteTableExportRepository(connection),
                    stata,
                    managed_store,
                    identities,
                ),
                DocumentDeliveryService(
                    SqliteDocumentDeliveryRepository(connection),
                    managed_store,
                    identities,
                    MicrosoftWordRtfRenderer(),
                ),
                identities,
            )
            executor = ResearchWorkflowExecutor(
                workflow,
                BrokerExecutionService(SqliteBrokerExecutionRepository(connection), identities),
                identities,
                RunResearchToWordCommand(
                    turn.turn_id,
                    initialized.main_path_id,
                    captured.data_version_id,
                    verified.receipt_id,
                    captured.managed_handle,
                    "agent-word",
                    "price",
                    ("mpg", "weight"),
                    plan.plan_revision_id,
                    estimation_node_id,
                ),
            )
            driver = AgentTurnDriver(
                ModelGatewayService(
                    SqliteModelGatewayRepository(connection),
                    identities,
                    Credential(),
                    ResearchToWordModel(),
                ),
                broker,
                evaluator,
                executor,
                identities,
                WORKER_PYTHON,
            )
            outcome = await driver.run(
                TurnDriverConfig(
                    turn.turn_id,
                    2,
                    workspace_id.value,
                    initialized.main_scope_id.value,
                    initialized.main_path_id.value,
                    TurnDriverModelConfig(
                        "system-v1",
                        "You are a traceable Stata research agent.",
                        "research-main",
                        "skill-v1",
                        "Use registered tools and only finish after Word delivery.",
                        "catalog-v1",
                        (
                            {
                                "name": "research.run_to_word",
                                "input_schema": workflow_contract().input_schema,
                            },
                        ),
                        "permission-v1",
                        {"workspace_write": True, "stata_execute": True},
                        "model-policy-v1",
                        "test-provider",
                        "test",
                        "test-model",
                        "https://provider.invalid/responses",
                        "credential://test",
                        {},
                    ),
                    (
                        ContextItemCandidate(
                            "user_message",
                            "message",
                            turn.message_id.value,
                            "1",
                            "remote_allowed",
                            "Regress price on mpg and weight, then deliver Word.",
                        ),
                        ContextItemCandidate(
                            "data_version",
                            "data_version",
                            captured.data_version_id.value,
                            "1",
                            "metadata_only",
                            "Verified auto.dta with 74 observations is available.",
                        ),
                    ),
                    {"resource_identities": {}},
                    ("workspace_write",),
                    {"research.run_to_word": normalized.obligation_ids},
                    4,
                )
            )
            assert outcome.status == "succeeded"
            assert outcome.tool_executions == 1

    try:
        asyncio.run(scenario())
        binding = connection.execute(
            """
            SELECT plan_revision_id, plan_node_id, adherence_verdict
            FROM stata_run_plan_bindings
            """
        ).fetchone()
        assert tuple(binding) == (
            plan.plan_revision_id.value,
            estimation_node_id.value,
            "matches",
        )
        founder_scenario = FounderScenarioLoader().load(FOUNDER_SCENARIO)
        founder_audit = SqliteFounderJourneyAuditor(connection).audit(
            FounderJourneyAuditRequest(
                founder_scenario,
                FounderAuditMode.IMPLEMENTATION,
                turn.turn_id.value,
                initialized.main_path_id.value,
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
        )
        assert founder_audit.status is FounderAuditStatus.IMPLEMENTATION_PASSED
        assert not founder_audit.acceptance_eligible
        assert founder_audit.findings == ()
        assert all(item.satisfied for item in founder_audit.observations)
        unbound_acceptance = SqliteFounderJourneyAuditor(connection).audit(
            FounderJourneyAuditRequest(
                founder_scenario,
                FounderAuditMode.FOUNDER_ACCEPTANCE,
                turn.turn_id.value,
                initialized.main_path_id.value,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                "a" * 64,
            )
        )
        assert unbound_acceptance.status is FounderAuditStatus.FAILED
        assert unbound_acceptance.findings == ("VERIFIED_ACTIVE_INSTALLED_CANDIDATE_REQUIRED",)
        controlled_turn = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_founder_controlled_turn"),
                "Discuss a decision, pause, branch the research, and trace a number.",
                research_path_id=initialized.main_path_id,
            )
        )
        interaction = TurnInteractionService(
            SqliteTurnInteractionRepository(connection), identities
        )
        waiting = interaction.open_waiting(
            OpenWaitingCommand(
                CommandId("cmd_founder_controlled_waiting"),
                controlled_turn.turn_id,
                1,
                WaitReason.USER_CONFIRMATION,
                "Keep the current baseline before opening an alternative path?",
            )
        )
        answered = interaction.answer_waiting(
            AnswerWaitingCommand(
                CommandId("cmd_founder_controlled_answer"),
                waiting.waiting_request_id,
                "Yes. Preserve the baseline and create an alternative path.",
                expected_turn_revision=waiting.turn_revision,
                expected_turn_id=controlled_turn.turn_id,
            )
        )
        interaction.request_pause(
            RequestPauseCommand(
                CommandId("cmd_founder_controlled_pause"),
                controlled_turn.turn_id,
                answered.turn_revision,
                "Inspect the completed baseline and its trace before branching.",
            )
        )
        converged = interaction.converge_pause(
            ConvergePauseCommand(
                CommandId("cmd_founder_controlled_converge"),
                controlled_turn.turn_id,
            )
        )
        assert converged.converged
        continuation = interaction.continue_paused_turn(
            ContinuePausedTurnCommand(
                CommandId("cmd_founder_controlled_continue"),
                controlled_turn.turn_id,
                "Continue from the reviewed state and open the alternative path.",
            )
        )
        branch_basis = int(
            connection.execute("SELECT MAX(workspace_revision) FROM workspace_commits").fetchone()[
                0
            ]
        )
        controlled_branch = research_state.create_path_branch(
            CreateResearchPathBranchCommand(
                CommandId("cmd_founder_controlled_branch"),
                initialized.main_path_id,
                continuation.successor_turn_id,
                "founder-controlled-alternative",
                "Founder controlled alternative",
                "Preserve the baseline while exploring a separate direction.",
                branch_basis,
                branch_basis,
            )
        )
        controlled_evidence_id = str(
            connection.execute(
                "SELECT evidence_record_id FROM evidence_records "
                "ORDER BY evidence_record_id LIMIT 1"
            ).fetchone()[0]
        )
        controlled_scenario = FounderScenarioLoader().load(FOUNDER_CONTROLLED_SCENARIO)
        controlled_audit = SqliteFounderJourneyAuditor(connection).audit_controlled(
            ControlledJourneyAuditRequest(
                controlled_scenario,
                FounderAuditMode.IMPLEMENTATION,
                controlled_turn.turn_id.value,
                continuation.successor_turn_id.value,
                initialized.main_path_id.value,
                controlled_branch.research_path_id.value,
                controlled_evidence_id,
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
        )
        assert controlled_audit.status is FounderAuditStatus.IMPLEMENTATION_PASSED
        assert controlled_audit.findings == ()
        assert all(item.satisfied for item in controlled_audit.observations)
        control.complete_turn(
            CompleteTurnCommand(
                CommandId("cmd_founder_controlled_complete_successor"),
                continuation.successor_turn_id,
                TurnStatus.SUCCEEDED,
            )
        )
        adoption_turn = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_agent_word_bundle_turn"),
                "Explicitly retain this Data and Result as the current research bundle.",
            )
        )
        adopted = connection.execute(
            """
            SELECT data_slot.path_data_slot_id,
                   data_adoption.target_data_version_id,
                   data_adoption.pointer_revision AS data_pointer_revision,
                   result_slot.result_slot_id,
                   result_adoption.target_result_id,
                   result_adoption.pointer_revision AS result_pointer_revision
            FROM path_data_slots AS data_slot
            JOIN path_data_adoptions AS data_adoption
              ON data_adoption.path_data_slot_id = data_slot.path_data_slot_id
            JOIN result_slots AS result_slot
              ON result_slot.research_path_id = data_slot.research_path_id
            JOIN path_result_adoptions AS result_adoption
              ON result_adoption.result_slot_id = result_slot.result_slot_id
            WHERE data_slot.research_path_id = ?
              AND data_slot.canonical_key = 'analysis.primary'
              AND result_slot.canonical_key = 'baseline.primary'
            """,
            (initialized.main_path_id.value,),
        ).fetchone()
        assert adopted is not None
        bundle = research_state.adopt_research_bundle(
            AdoptResearchBundleCommand(
                CommandId("cmd_agent_word_bundle_adopt"),
                initialized.main_path_id,
                adoption_turn.turn_id,
                data_targets=(
                    DataSlotAdoptionTarget(
                        PathDataSlotId(str(adopted["path_data_slot_id"])),
                        DataVersionId(str(adopted["target_data_version_id"])),
                        verified.receipt_id,
                        int(adopted["data_pointer_revision"]),
                    ),
                ),
                result_targets=(
                    ResultSlotAdoptionTarget(
                        ResultSlotId(str(adopted["result_slot_id"])),
                        ResultId(str(adopted["target_result_id"])),
                        int(adopted["result_pointer_revision"]),
                    ),
                ),
            )
        )
        assert set(bundle.data_pointer_revisions.values()) == {2}
        assert set(bundle.result_pointer_revisions.values()) == {2}
        alternate_data = artifacts.capture_data_version(
            CaptureDataVersionCommand(
                CommandId("cmd_agent_word_capture_alternate_identity"),
                source,
                DataVersionKind.WORKING_CAPTURE,
                adoption_turn.turn_id,
            )
        )
        revision_before_rejected_bundle = connection.execute(
            "SELECT MAX(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        with pytest.raises(
            ValueError, match="Result required Data dependency is not atomically adopted"
        ):
            research_state.adopt_research_bundle(
                AdoptResearchBundleCommand(
                    CommandId("cmd_agent_word_reject_incompatible_bundle"),
                    initialized.main_path_id,
                    adoption_turn.turn_id,
                    data_targets=(
                        DataSlotAdoptionTarget(
                            PathDataSlotId(str(adopted["path_data_slot_id"])),
                            alternate_data.data_version_id,
                            alternate_data.verification_receipt_id,
                            2,
                        ),
                    ),
                    result_targets=(
                        ResultSlotAdoptionTarget(
                            ResultSlotId(str(adopted["result_slot_id"])),
                            ResultId(str(adopted["target_result_id"])),
                            2,
                        ),
                    ),
                )
            )
        assert (
            connection.execute("SELECT MAX(workspace_revision) FROM workspace_commits").fetchone()[
                0
            ]
            == revision_before_rejected_bundle
        )
        document = connection.execute(
            """
            SELECT revision.document_revision_id, gate.verdict,
                   location.managed_handle
            FROM document_revisions AS revision
            JOIN delivery_gate_reports AS gate
              ON gate.document_revision_id = revision.document_revision_id
            JOIN artifact_locations AS location
              ON location.artifact_id = revision.docx_artifact_id
            """
        ).fetchone()
        assert document is not None
        assert document["verdict"] == "pass"
        assert managed_store.read_small_payload(
            str(document["managed_handle"]), max_bytes=50 * 1024 * 1024
        ).startswith(b"PK")
        assert (
            connection.execute("SELECT coverage_status FROM goal_coverages").fetchone()[0]
            == "satisfied"
        )
        assert (
            connection.execute("SELECT count(*) FROM table_cell_evidence_uses").fetchone()[0] == 8
        )

        roundtrip = DocumentRoundtripService(
            SqliteDocumentRoundtripRepository(connection),
            managed_store,
            identities,
            OoxmlDocumentRoundtripEngine(),
        )
        base_payload = managed_store.read_small_payload(
            str(document["managed_handle"]), max_bytes=50 * 1024 * 1024
        )
        human_path = workspace_root / "human-returned.docx"
        human_path.write_bytes(
            _edit_docx(base_payload, prose="Human revised interpretation paragraph.")
        )
        human = roundtrip.import_returned(
            ImportReturnedDocumentCommand(
                CommandId("cmd_agent_word_human_return"),
                adoption_turn.turn_id,
                initialized.main_path_id,
                DocumentRevisionId(str(document["document_revision_id"])),
                human_path,
                1,
                1,
            )
        )
        assert human.classification == "prose_only"
        assert human.delivery_advanced
        assert (human.working_pointer_revision, human.delivery_pointer_revision) == (2, 2)

        agent_branch = _seed_agent_document_branch(
            connection,
            managed_store,
            identities,
            base_revision_id=DocumentRevisionId(str(document["document_revision_id"])),
            requested_by_turn_id=adoption_turn.turn_id,
            agent_payload=_edit_docx(base_payload, managed_value="2.500"),
        )
        merged = roundtrip.merge(
            MergeDocumentRevisionsCommand(
                CommandId("cmd_agent_word_merge_human_agent"),
                adoption_turn.turn_id,
                initialized.main_path_id,
                DocumentRevisionId(str(document["document_revision_id"])),
                human.returned_revision_id,
                agent_branch,
                2,
                2,
            )
        )
        assert (merged.working_pointer_revision, merged.delivery_pointer_revision) == (
            3,
            3,
        )
        assert (
            connection.execute("SELECT merge_verdict FROM document_merge_receipts").fetchone()[0]
            == "merged"
        )

        merged_handle = connection.execute(
            """
            SELECT location.managed_handle
            FROM document_revisions AS revision
            JOIN artifact_locations AS location
              ON location.artifact_id = revision.docx_artifact_id
            WHERE revision.document_revision_id = ?
            """,
            (merged.merged_revision_id.value,),
        ).fetchone()[0]
        conflict_path = workspace_root / "human-conflict.docx"
        conflict_path.write_bytes(
            _edit_docx(
                managed_store.read_small_payload(str(merged_handle), max_bytes=50 * 1024 * 1024),
                managed_value="999.999",
            )
        )
        conflict = roundtrip.import_returned(
            ImportReturnedDocumentCommand(
                CommandId("cmd_agent_word_human_conflict"),
                adoption_turn.turn_id,
                initialized.main_path_id,
                merged.merged_revision_id,
                conflict_path,
                3,
                3,
            )
        )
        assert conflict.classification == "managed_conflict"
        assert not conflict.delivery_advanced
        assert (conflict.working_pointer_revision, conflict.delivery_pointer_revision) == (
            4,
            3,
        )
        slots = {
            str(row["canonical_key"]): str(row["target_document_revision_id"])
            for row in connection.execute(
                """
                SELECT slot.canonical_key, adoption.target_document_revision_id
                FROM document_slots AS slot
                JOIN path_document_adoptions AS adoption
                  ON adoption.document_slot_id = slot.document_slot_id
                WHERE slot.research_path_id = ?
                """,
                (initialized.main_path_id.value,),
            )
        }
        assert slots["manuscript.main.working"] == conflict.returned_revision_id.value
        assert slots["manuscript.main.delivery"] == merged.merged_revision_id.value
        validation_counts = {
            str(row["document_revision_id"]): int(row["validation_count"])
            for row in connection.execute(
                """
                SELECT revision.document_revision_id, count(receipt.evidence_record_id)
                       AS validation_count
                FROM document_revisions AS revision
                LEFT JOIN statistical_evidence_use_validation_receipts AS receipt
                  ON receipt.created_revision = revision.created_revision
                WHERE revision.document_revision_id IN (?, ?, ?, ?)
                GROUP BY revision.document_revision_id
                """,
                (
                    str(document["document_revision_id"]),
                    human.returned_revision_id.value,
                    merged.merged_revision_id.value,
                    conflict.returned_revision_id.value,
                ),
            )
        }
        assert validation_counts == {
            str(document["document_revision_id"]): 8,
            human.returned_revision_id.value: 8,
            merged.merged_revision_id.value: 8,
            conflict.returned_revision_id.value: 0,
        }

        conflict_handle = connection.execute(
            """
            SELECT location.managed_handle
            FROM document_revisions AS revision
            JOIN artifact_locations AS location
              ON location.artifact_id = revision.docx_artifact_id
            WHERE revision.document_revision_id = ?
            """,
            (conflict.returned_revision_id.value,),
        ).fetchone()[0]
        conflict_prose_path = workspace_root / "conflict-with-more-prose.docx"
        conflict_prose_path.write_bytes(
            _edit_docx(
                managed_store.read_small_payload(str(conflict_handle), max_bytes=50 * 1024 * 1024),
                prose="More human prose on a still-conflicted working draft.",
            )
        )
        inherited_conflict = roundtrip.import_returned(
            ImportReturnedDocumentCommand(
                CommandId("cmd_agent_word_conflict_cannot_self_heal"),
                adoption_turn.turn_id,
                initialized.main_path_id,
                conflict.returned_revision_id,
                conflict_prose_path,
                4,
                3,
            )
        )
        assert not isinstance(inherited_conflict, RejectedDocumentReturn)
        assert not inherited_conflict.delivery_advanced
        assert "BASE_REVISION_NOT_DELIVERY_ELIGIBLE" in inherited_conflict.findings
        assert (
            inherited_conflict.working_pointer_revision,
            inherited_conflict.delivery_pointer_revision,
        ) == (5, 3)

        revision_count = connection.execute("SELECT count(*) FROM document_revisions").fetchone()[0]
        invalid_path = workspace_root / "unreadable-return.docx"
        invalid_path.write_bytes(b"not-a-docx-package")
        invalid_command = ImportReturnedDocumentCommand(
            CommandId("cmd_agent_word_invalid_return"),
            adoption_turn.turn_id,
            initialized.main_path_id,
            inherited_conflict.returned_revision_id,
            invalid_path,
            5,
            3,
        )

        def crash_rejected_return_finalization(point: CommitCrashPoint) -> None:
            if point == CommitCrashPoint.BEFORE_COMMIT:
                raise RuntimeError("injected document rejection finalization crash")

        crashing_roundtrip = DocumentRoundtripService(
            SqliteDocumentRoundtripRepository(
                connection,
                finalization_crash_injector=crash_rejected_return_finalization,
            ),
            managed_store,
            identities,
            OoxmlDocumentRoundtripEngine(),
        )
        with pytest.raises(RuntimeError, match="injected document rejection finalization crash"):
            crashing_roundtrip.import_returned(invalid_command)
        assert (
            connection.execute("SELECT count(*) FROM document_raw_return_reports").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM document_revisions").fetchone()[0]
            == revision_count
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        # Retry uses the same prepare receipt and Attempt. Exact staging bytes are
        # accepted idempotently, but the rejected return is finalized only once.
        rejected = roundtrip.import_returned(invalid_command)
        assert isinstance(rejected, RejectedDocumentReturn)
        assert rejected.finding_code == "DOCX_PACKAGE_INVALID"
        assert (rejected.working_pointer_revision, rejected.delivery_pointer_revision) == (
            5,
            3,
        )
        assert (
            connection.execute("SELECT count(*) FROM document_revisions").fetchone()[0]
            == revision_count
        )
        assert (
            connection.execute("SELECT count(*) FROM document_raw_return_reports").fetchone()[0]
            == 1
        )

        eligibility = EvidenceEligibilityService(
            SqliteEvidenceEligibilityRepository(connection), identities
        )
        rebuilt = eligibility.rebuild_projection()
        assert rebuilt.statistical_count == 8
        evidence_id = EvidenceRecordId(
            str(
                connection.execute(
                    "SELECT evidence_record_id FROM evidence_records ORDER BY evidence_record_id"
                ).fetchone()[0]
            )
        )
        current_query = EvidenceEligibilityQuery(
            evidence_id,
            initialized.main_path_id,
            EvidenceUseContext.CURRENT_ADOPTED_RESULT,
            EvidenceUsePurpose.DOCUMENT_DELIVERY,
        )
        current = eligibility.query(current_query)
        assert current.verdict is EligibilityVerdict.ELIGIBLE
        assert current.source_state == "available"
        assert current.projection_lag == 0
        validated = eligibility.validate_for_use(
            ValidateEvidenceUseCommand(
                CommandId("cmd_agent_word_validate_current_evidence"),
                adoption_turn.turn_id,
                current_query,
            )
        )
        assert validated.eligibility.verdict is EligibilityVerdict.ELIGIBLE

        branch_source_revision = int(
            connection.execute("SELECT MAX(workspace_revision) FROM workspace_commits").fetchone()[
                0
            ]
        )
        comparison_path = research_state.create_path_branch(
            CreateResearchPathBranchCommand(
                CommandId("cmd_agent_word_create_evidence_comparison_path"),
                initialized.main_path_id,
                adoption_turn.turn_id,
                "evidence-comparison",
                "Evidence comparison",
                "Keep the original Data/Result/Plan bundle for comparison.",
                branch_source_revision,
                branch_source_revision,
            )
        )

        research_state.adopt_research_bundle(
            AdoptResearchBundleCommand(
                CommandId("cmd_agent_word_move_data_after_evidence_use"),
                initialized.main_path_id,
                adoption_turn.turn_id,
                data_targets=(
                    DataSlotAdoptionTarget(
                        PathDataSlotId(str(adopted["path_data_slot_id"])),
                        alternate_data.data_version_id,
                        alternate_data.verification_receipt_id,
                        2,
                    ),
                ),
            )
        )
        stale = eligibility.query(current_query)
        assert stale.verdict is EligibilityVerdict.INELIGIBLE
        assert "REQUIRED_DATA_NOT_CURRENT" in stale.reason_codes
        assert stale.source_state == "available"
        assert stale.projection_lag >= 2
        branch_current = eligibility.query(
            EvidenceEligibilityQuery(
                evidence_id,
                comparison_path.research_path_id,
                EvidenceUseContext.CURRENT_ADOPTED_RESULT,
                EvidenceUsePurpose.DOCUMENT_DELIVERY,
            )
        )
        assert branch_current.verdict is EligibilityVerdict.ELIGIBLE
        historical = eligibility.query(
            EvidenceEligibilityQuery(
                evidence_id,
                initialized.main_path_id,
                EvidenceUseContext.EXPLICIT_HISTORICAL_COMPARISON,
                EvidenceUsePurpose.LINEAGE_PREVIEW,
            )
        )
        assert historical.verdict is EligibilityVerdict.ELIGIBLE
        stale_validation = eligibility.validate_for_use(
            ValidateEvidenceUseCommand(
                CommandId("cmd_agent_word_validate_stale_evidence"),
                adoption_turn.turn_id,
                current_query,
            )
        )
        assert stale_validation.eligibility.verdict is EligibilityVerdict.INELIGIBLE
        assert [
            str(row[0])
            for row in connection.execute(
                """
                SELECT verdict FROM statistical_evidence_use_validation_receipts
                WHERE evidence_validation_receipt_id IN (?, ?)
                ORDER BY created_revision
                """,
                (
                    validated.receipt_id.value,
                    stale_validation.receipt_id.value,
                ),
            )
        ] == ["eligible", "ineligible"]
        refreshed_projection = eligibility.rebuild_projection()
        refreshed_stale = eligibility.query(current_query)
        assert refreshed_projection.projection_revision == (refreshed_stale.authoritative_revision)
        assert refreshed_stale.projection_lag == 0
        assert refreshed_stale.source_state == "available"
        assert refreshed_stale.verdict is EligibilityVerdict.INELIGIBLE
    finally:
        connection.close()
