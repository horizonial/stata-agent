"""FastAPI composition root for the M0 public command/query vertical."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from stata_research_agent import __version__
from stata_research_agent.application.control import (
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.diagnostic_bundle import (
    DiagnosticBundleMode,
    DiagnosticBundleRequest,
)
from stata_research_agent.application.lineage import LineageEntryKind, LineageSelector
from stata_research_agent.application.lineage_service import EvidenceLineageService
from stata_research_agent.application.local_session import (
    LocalSessionAuthority,
    LocalSessionAuthorizationError,
)
from stata_research_agent.application.memory import (
    ActivateMemoryCommand,
    CreateMemoryCommand,
    MemoryAccessTier,
    MemoryKind,
    MemoryLifecycle,
    MemoryOriginKind,
    MemoryScopeKind,
    MemorySource,
    MemorySourceRole,
    RetractMemoryCommand,
    ReviseMemoryCommand,
    SetConversationMemoryPolicyCommand,
    SetMemoryAccessTierCommand,
    SupersedeMemoryCommand,
)
from stata_research_agent.application.memory_service import MemoryService
from stata_research_agent.application.model_configuration import (
    WorkspaceModelConfiguration,
    WorkspaceModelConfigurationService,
)
from stata_research_agent.application.model_gateway import ProviderDispatchError
from stata_research_agent.application.outcome_feedback import (
    OutcomeDisposition,
    OutcomeRating,
    RecordTurnOutcomeFeedbackCommand,
)
from stata_research_agent.application.outcome_feedback_service import (
    TurnOutcomeFeedbackService,
)
from stata_research_agent.application.ports.turn_dispatch import TurnDispatcher
from stata_research_agent.application.provider_credentials import (
    CredentialLifecycleError,
    ProviderCredentialService,
)
from stata_research_agent.application.research_state import CreateResearchPathBranchCommand
from stata_research_agent.application.research_state_service import ResearchStateService
from stata_research_agent.application.runtime_usage import ProviderPricingCatalog
from stata_research_agent.application.skill_change import (
    ActivateSkillChangeCommand,
    MaterializeSkillChangeCommand,
    RejectSkillChangeCommand,
)
from stata_research_agent.application.skill_change_service import SkillChangeService
from stata_research_agent.application.skill_evaluation import EvaluateSkillCommand
from stata_research_agent.application.skill_evolution import (
    ApproveSkillEvolutionCommand,
    DeactivateSkillCommand,
    RejectSkillEvolutionCommand,
    RollbackSkillCommand,
)
from stata_research_agent.application.skill_evolution_service import SkillEvolutionService
from stata_research_agent.application.streaming import (
    DurableNotification,
    DurableReplayBatch,
    EphemeralModelDeltaHub,
)
from stata_research_agent.application.turn_interaction import (
    AnswerWaitingCommand,
    RequestPauseCommand,
)
from stata_research_agent.application.turn_interaction_service import TurnInteractionService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    CommandId,
    ConversationId,
    MemoryItemId,
    MemoryRevisionId,
    ResearchPathId,
    SkillChangeCandidateId,
    SkillEvolutionCandidateId,
    SkillImprovementProposalId,
    SkillVersionId,
    TurnId,
    WaitingRequestId,
    WorkspaceId,
)
from stata_research_agent.domain.status import ExecutionMode, TurnStatus
from stata_research_agent.interfaces.diagnostic_bundle_builder import (
    DiagnosticBundleBuilder,
)
from stata_research_agent.interfaces.filesystem_data_catalog import (
    FilesystemWorkspaceDataCatalog,
)
from stata_research_agent.interfaces.skill_evaluation_runner import (
    ProductionSkillEvaluationRunner,
)
from stata_research_agent.interfaces.workspace_skill_publisher import WorkspaceSkillPublisher
from stata_research_agent.persistence.control_query import SqliteWorkspaceQuery
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.errors import (
    CommandConflictError,
    PersistenceBootstrapError,
)
from stata_research_agent.persistence.filesystem_memory import FilesystemMemoryStore
from stata_research_agent.persistence.investigation_query import SqliteInvestigationQuery
from stata_research_agent.persistence.lineage_query import SqliteEvidenceLineageQuery
from stata_research_agent.persistence.memory_query import SqliteMemoryQuery
from stata_research_agent.persistence.memory_store import SqliteMemoryRepository
from stata_research_agent.persistence.operational_evaluation_query import (
    SqliteOperationalEvaluationQuery,
)
from stata_research_agent.persistence.outbox_stream import (
    SqliteOutboxStreamQuery,
    StreamResyncRequiredError,
)
from stata_research_agent.persistence.outcome_feedback_store import (
    SqliteTurnOutcomeFeedbackRepository,
)
from stata_research_agent.persistence.research_state_store import (
    SqliteResearchStateRepository,
)
from stata_research_agent.persistence.research_view_query import (
    JournalCursorCodec,
    JournalCursorInvalidError,
    JournalCursorQueryMismatchError,
    JournalFilters,
    SqliteResearchViewQuery,
)
from stata_research_agent.persistence.runtime_usage_query import SqliteRuntimeUsageQuery
from stata_research_agent.persistence.skill_change_query import SqliteSkillChangeQuery
from stata_research_agent.persistence.skill_change_store import SqliteSkillChangeRepository
from stata_research_agent.persistence.skill_evaluation_query import (
    SkillEvaluationSnapshot,
    SqliteSkillEvaluationQuery,
)
from stata_research_agent.persistence.skill_evolution_query import SqliteSkillEvolutionQuery
from stata_research_agent.persistence.skill_evolution_store import (
    SqliteSkillEvolutionRepository,
)
from stata_research_agent.persistence.turn_interaction_store import (
    SqliteTurnInteractionRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.persistence.workspace_scheduler import (
    SqliteWorkspaceTurnAuthority,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator

from .models import (
    AnalysisOutputArtifactResponse,
    AnalysisOutputIndexData,
    AnalysisOutputIndexResponse,
    AnalysisOutputItemResponse,
    AttentionRefResponse,
    CommandEnvelope,
    CommandReceiptResponse,
    ConversationDetailData,
    ConversationDetailResponse,
    ConversationMemoryPolicyRequest,
    ConversationMemoryPolicyResponse,
    ConversationSummaryResponse,
    ConversationTimelineItemResponse,
    CountBudgetUsageResponse,
    DataStateStepResponse,
    DiagnosticBundleCreateRequest,
    DiagnosticBundlePreviewResponse,
    DiagnosticBundleReceiptResponse,
    DiagnosticBundleSaveRequest,
    DocumentIndexData,
    DocumentIndexResponse,
    DocumentSlotResponse,
    DurableNotificationResponse,
    ErrorBody,
    ErrorResponse,
    EvidenceLineageData,
    EvidenceLineageResponse,
    GlobalAttentionResponse,
    InvestigationFindingResponse,
    JournalEntryPageResponse,
    JournalEntryResponse,
    KnowledgeDocumentResponse,
    KnowledgeIndexResponse,
    LauncherBootstrapReceiptResponse,
    LineageJournalResponse,
    MemoryAccessTierRequest,
    MemoryActivationRequest,
    MemoryCreateRequest,
    MemoryIndexResponse,
    MemoryItemResponse,
    MemoryMutationResponse,
    MemoryRetentionResponse,
    MemoryRetractionRequest,
    MemoryRevisionRequest,
    MemorySourceResponse,
    MemorySummaryResponse,
    MemorySupersessionRequest,
    MessageSubmitEnvelope,
    MessageSummaryResponse,
    MetaResponse,
    MonetaryUsageEstimateResponse,
    OperationalBreakdownResponse,
    OperationalConfigurationResponse,
    OperationalLayerResponse,
    PauseIntentResponse,
    ProjectionWatermarkResponse,
    ProviderAttemptUsageResponse,
    ProviderCredentialCreateRequest,
    ProviderCredentialReceiptResponse,
    ProviderCredentialRotateRequest,
    ProviderProfileDeleteReceiptResponse,
    ProviderProfileIndexResponse,
    ProviderProfileResponse,
    ResearchPathBranchEnvelope,
    ResearchPathSummaryResponse,
    ResearchPlanData,
    ResearchPlanDependencyResponse,
    ResearchPlanNodeResponse,
    ResearchPlanResponse,
    ResourceRef,
    ResultIndexData,
    ResultIndexItemResponse,
    ResultIndexResponse,
    RuntimeBudgetUsageResponse,
    SessionExchangeReceiptResponse,
    SessionExchangeRequest,
    SkillAdoptionMutationResponse,
    SkillAdoptionResponse,
    SkillChangeActivateRequest,
    SkillChangeCandidateResponse,
    SkillChangeMaterializeRequest,
    SkillChangeMutationResponse,
    SkillChangeRejectRequest,
    SkillDeactivateRequest,
    SkillEvaluationRequest,
    SkillEvaluationResponse,
    SkillEvolutionApproveRequest,
    SkillEvolutionCandidateResponse,
    SkillEvolutionIndexResponse,
    SkillEvolutionMutationResponse,
    SkillEvolutionRejectRequest,
    SkillImprovementProposalResponse,
    SkillRollbackRequest,
    SkillVersionResponse,
    ToolDecisionTraceData,
    ToolDecisionTraceResponse,
    ToolOperationTraceResponse,
    ToolOperationUsageResponse,
    ToolStatusObservationResponse,
    TurnInvestigationData,
    TurnInvestigationResponse,
    TurnOperationalEvaluationData,
    TurnOperationalEvaluationResponse,
    TurnOutcomeFeedbackRequest,
    TurnOutcomeFeedbackResponse,
    TurnPauseRequestEnvelope,
    TurnResponse,
    TurnUsageData,
    TurnUsageResponse,
    WaitingAnswerEnvelope,
    WaitingReason,
    WaitingRequestResponse,
    WorkspaceAttentionResponse,
    WorkspaceBootstrapData,
    WorkspaceBootstrapResponse,
    WorkspaceCreateEnvelope,
    WorkspaceDataCandidateResponse,
    WorkspaceDataCatalogResponse,
    WorkspaceExecutionData,
    WorkspaceExecutionResponse,
    WorkspaceModelConfigurationRequest,
    WorkspaceModelConfigurationResponse,
    WorkspaceOperationalEvaluationData,
    WorkspaceOperationalEvaluationResponse,
    WorkspaceStreamHeadResponse,
)


class WorkspaceHost:
    """Maps validated Workspace identities to isolated local database roots."""

    def __init__(self, base_directory: Path) -> None:
        self._base_directory = base_directory.resolve()
        self._base_directory.mkdir(parents=True, exist_ok=True)
        self._journal_cursor_secret = self._load_or_create_cursor_secret()

    @property
    def journal_cursor_secret(self) -> bytes:
        return self._journal_cursor_secret

    def _load_or_create_cursor_secret(self) -> bytes:
        key_path = self._base_directory / ".journal-cursor-key"
        try:
            key = key_path.read_bytes()
        except FileNotFoundError:
            candidate = secrets.token_bytes(32)
            try:
                with key_path.open("xb") as stream:
                    stream.write(candidate)
                key = candidate
            except FileExistsError:
                key = key_path.read_bytes()
        if len(key) != 32:
            raise ValueError("Workspace host Journal cursor key is invalid")
        return key

    def database(self, workspace_id: WorkspaceId) -> WorkspaceDatabase:
        root = (self._base_directory / workspace_id.value).resolve()
        if not root.is_relative_to(self._base_directory):
            raise ValueError("Workspace root escaped the configured host directory")
        return WorkspaceDatabase(root, workspace_id)

    def scheduling_authority(self, workspace_id: WorkspaceId) -> SqliteWorkspaceTurnAuthority:
        return SqliteWorkspaceTurnAuthority(self.database(workspace_id), UuidIdentityGenerator())

    def workspace_ids(self) -> tuple[WorkspaceId, ...]:
        candidates: list[WorkspaceId] = []
        for path in self._base_directory.iterdir():
            if not path.is_dir() or not path.name.startswith("ws_"):
                continue
            workspace_id = WorkspaceId(path.name)
            if WorkspaceDatabase(path, workspace_id).database_path.is_file():
                candidates.append(workspace_id)
        return tuple(sorted(candidates, key=lambda item: item.value))


def create_app(
    host: WorkspaceHost,
    *,
    static_directory: Path | None = None,
    turn_dispatcher: TurnDispatcher | None = None,
    local_session_authority: LocalSessionAuthority | None = None,
    provider_credentials: ProviderCredentialService | None = None,
    model_configuration: WorkspaceModelConfigurationService | None = None,
    diagnostic_bundle_factory: Callable[[WorkspaceId | None], DiagnosticBundleBuilder]
    | None = None,
    diagnostic_protected_values: Callable[[], tuple[str, ...]] | None = None,
    model_delta_hub: EphemeralModelDeltaHub | None = None,
    provider_pricing: ProviderPricingCatalog | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Stata Research Agent API",
        version=__version__,
        openapi_version="3.1.0",
    )
    journal_cursor_codec = JournalCursorCodec(host.journal_cursor_secret)
    diagnostic_requests: dict[str, tuple[DiagnosticBundleRequest, WorkspaceId | None]] = {}
    pricing_catalog = provider_pricing or ProviderPricingCatalog.unconfigured()

    @app.middleware("http")
    async def local_trust_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        protected_api = request.url.path.startswith("/api/v1/") and request.url.path not in {
            "/api/v1/meta",
            "/api/v1/launcher/browser-bootstrap",
            "/api/v1/session/exchange",
        }
        response: Response
        if local_session_authority is not None:
            try:
                local_session_authority.validate_host(request.headers.get("host", ""))
                if protected_api:
                    session_token = request.headers.get("x-stata-browser-session", "")
                    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                        local_session_authority.authorize_mutation(
                            session_token,
                            host=request.headers.get("host", ""),
                            origin=request.headers.get("origin", ""),
                            fetch_site=request.headers.get("sec-fetch-site", ""),
                        )
                    else:
                        local_session_authority.authorize(
                            session_token,
                            host=request.headers.get("host", ""),
                            origin=request.headers.get("origin"),
                        )
            except LocalSessionAuthorizationError:
                body = ErrorResponse(
                    error=ErrorBody(
                        code=(
                            "BROWSER_SESSION_REQUIRED"
                            if protected_api
                            else "LOOPBACK_HOST_REJECTED"
                        ),
                        message="The local request capability was rejected.",
                    )
                )
                response = JSONResponse(status_code=401, content=body.model_dump(mode="json"))
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorBody(
                code="INVALID_REQUEST",
                message="Request does not match the published API contract.",
                next_action="Correct the request using the OpenAPI schema.",
            )
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(HTTPException)
    async def typed_http_error(_request: Request, error: HTTPException) -> JSONResponse:
        detail = str(error.detail)
        code = detail if detail.isupper() and " " not in detail else "REQUEST_REJECTED"
        body = ErrorResponse(
            error=ErrorBody(code=code, message="The request was rejected by the server.")
        )
        return JSONResponse(status_code=error.status_code, content=body.model_dump(mode="json"))

    @app.get("/api/v1/meta", response_model=MetaResponse)
    def meta() -> MetaResponse:
        return MetaResponse(
            server_version=__version__,
            browser_session_required=local_session_authority is not None,
        )

    @app.post(
        "/api/v1/launcher/browser-bootstrap",
        response_model=LauncherBootstrapReceiptResponse,
        responses={401: {"model": ErrorResponse}},
    )
    def launcher_browser_bootstrap(
        request: Request, response: Response
    ) -> LauncherBootstrapReceiptResponse:
        if local_session_authority is None:
            raise HTTPException(status_code=404, detail="LOCAL_SESSION_DISABLED")
        try:
            receipt = local_session_authority.issue_bootstrap_nonce(
                request.headers.get("x-stata-launcher-capability", "")
            )
        except LocalSessionAuthorizationError as error:
            raise HTTPException(status_code=401, detail="LAUNCHER_CAPABILITY_REJECTED") from error
        response.headers["Cache-Control"] = "no-store"
        return LauncherBootstrapReceiptResponse(
            instance_id=receipt.instance_id,
            bootstrap_nonce=receipt.nonce,
            expires_at=receipt.expires_at,
        )

    @app.post(
        "/api/v1/session/exchange",
        response_model=SessionExchangeReceiptResponse,
        responses={401: {"model": ErrorResponse}},
    )
    def session_exchange(payload: SessionExchangeRequest, request: Request) -> JSONResponse:
        if local_session_authority is None:
            raise HTTPException(status_code=404, detail="LOCAL_SESSION_DISABLED")
        try:
            receipt = local_session_authority.exchange(
                payload.bootstrap_nonce,
                host=request.headers.get("host", ""),
                origin=request.headers.get("origin", ""),
                fetch_site=request.headers.get("sec-fetch-site", ""),
            )
        except LocalSessionAuthorizationError as error:
            raise HTTPException(status_code=401, detail="BOOTSTRAP_NONCE_REJECTED") from error
        body = SessionExchangeReceiptResponse(
            instance_id=receipt.instance_id,
            browser_session_token=receipt.session_token,
        )
        return JSONResponse(
            content=body.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/api/v1/provider-credentials",
        response_model=ProviderCredentialReceiptResponse,
        responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def create_provider_credential(
        payload: ProviderCredentialCreateRequest,
        response: Response,
    ) -> ProviderCredentialReceiptResponse:
        if provider_credentials is None:
            raise HTTPException(status_code=503, detail="CREDENTIAL_STORE_UNAVAILABLE")
        try:
            receipt = provider_credentials.create_profile(
                provider_kind=payload.provider_kind,
                endpoint=payload.endpoint,
                account_label=payload.account_label,
                secret=payload.secret.get_secret_value(),
            )
        except CredentialLifecycleError as error:
            raise HTTPException(status_code=503, detail="CREDENTIAL_STORE_UNAVAILABLE") from error
        response.headers["Cache-Control"] = "no-store"
        return ProviderCredentialReceiptResponse(
            provider_profile_id=receipt.provider_profile_id,
            credential_version_id=receipt.credential_version_id,
            status="adopted",
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/research-paths/{research_path_id}/plan",
        response_model=ResearchPlanResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def research_plan(workspace_id: str, research_path_id: str) -> ResearchPlanResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            typed_path_id = ResearchPathId(research_path_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                if (
                    connection.execute(
                        "SELECT 1 FROM research_paths WHERE research_path_id = ?",
                        (typed_path_id.value,),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("unknown Research Path")
                authoritative_revision = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                    ).fetchone()[0]
                )
                plan = connection.execute(
                    """
                    SELECT revision.plan_id, revision.plan_revision_id,
                           revision.revision_number, revision.summary,
                           revision.specification_json, adoption.pointer_revision
                    FROM path_plan_adoptions AS adoption
                    JOIN plan_revisions AS revision
                      ON revision.plan_revision_id = adoption.target_plan_revision_id
                    WHERE adoption.research_path_id = ?
                    """,
                    (typed_path_id.value,),
                ).fetchone()
                node_rows: list[Any] = []
                dependency_rows: list[Any] = []
                if plan is not None:
                    node_rows = connection.execute(
                        """
                        SELECT member.plan_node_id, node.canonical_key, member.node_kind,
                               member.specification_json, member.ordinal,
                               count(binding.stata_run_id) AS completed_run_count
                        FROM plan_revision_nodes AS member
                        JOIN plan_nodes AS node ON node.plan_node_id = member.plan_node_id
                        LEFT JOIN stata_run_plan_bindings AS binding
                          ON binding.plan_revision_id = member.plan_revision_id
                         AND binding.plan_node_id = member.plan_node_id
                        WHERE member.plan_revision_id = ?
                        GROUP BY member.plan_node_id, node.canonical_key, member.node_kind,
                                 member.specification_json, member.ordinal
                        ORDER BY member.ordinal
                        """,
                        (str(plan["plan_revision_id"]),),
                    ).fetchall()
                    dependency_rows = connection.execute(
                        """
                        SELECT upstream.canonical_key AS upstream_key,
                               downstream.canonical_key AS downstream_key,
                               dependency.dependency_kind
                        FROM plan_node_dependencies AS dependency
                        JOIN plan_nodes AS upstream
                          ON upstream.plan_node_id = dependency.upstream_plan_node_id
                        JOIN plan_nodes AS downstream
                          ON downstream.plan_node_id = dependency.downstream_plan_node_id
                        WHERE dependency.plan_revision_id = ?
                        ORDER BY upstream.canonical_key, downstream.canonical_key
                        """,
                        (str(plan["plan_revision_id"]),),
                    ).fetchall()
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="RESEARCH_PATH_NOT_FOUND") from error
        metadata: dict[str, Any] = {}
        if plan is not None:
            parsed_specification = json.loads(str(plan["specification_json"]))
            candidate_metadata = parsed_specification.get("_adaptive_plan", {})
            if isinstance(candidate_metadata, dict):
                metadata = candidate_metadata
        raw_trigger_references = metadata.get("trigger_references", ())
        trigger_references = (
            tuple(raw_trigger_references)
            if isinstance(raw_trigger_references, list)
            else (() if raw_trigger_references is None else (raw_trigger_references,))
        )
        return ResearchPlanResponse(
            workspace_id=workspace_id,
            authoritative_revision=authoritative_revision,
            generated_at=datetime.now(UTC),
            data=ResearchPlanData(
                research_path_id=research_path_id,
                plan_id=None if plan is None else str(plan["plan_id"]),
                plan_revision_id=None if plan is None else str(plan["plan_revision_id"]),
                revision_number=None if plan is None else int(plan["revision_number"]),
                pointer_revision=None if plan is None else int(plan["pointer_revision"]),
                summary=None if plan is None else str(plan["summary"]),
                change_kind=(
                    None if metadata.get("change_kind") is None else str(metadata["change_kind"])
                ),
                trigger_references=trigger_references,
                nodes=tuple(
                    ResearchPlanNodeResponse(
                        plan_node_id=str(row["plan_node_id"]),
                        canonical_key=str(row["canonical_key"]),
                        node_kind=str(
                            json.loads(str(row["specification_json"])).get(
                                "semantic_node_kind", row["node_kind"]
                            )
                        ),
                        specification=json.loads(str(row["specification_json"])),
                        ordinal=int(row["ordinal"]),
                        completed_run_count=int(row["completed_run_count"]),
                    )
                    for row in node_rows
                ),
                dependencies=tuple(
                    ResearchPlanDependencyResponse(
                        upstream_node_key=str(row["upstream_key"]),
                        downstream_node_key=str(row["downstream_key"]),
                        dependency_kind=cast(Any, row["dependency_kind"]),
                    )
                    for row in dependency_rows
                ),
            ),
            resource_refs=(
                ()
                if plan is None
                else (
                    ResourceRef(
                        resource_type="plan_revision",
                        resource_id=str(plan["plan_revision_id"]),
                    ),
                )
            ),
        )

    @app.get(
        "/api/v1/provider-credentials",
        response_model=ProviderProfileIndexResponse,
        responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def list_provider_credentials(response: Response) -> ProviderProfileIndexResponse:
        if provider_credentials is None:
            raise HTTPException(status_code=503, detail="CREDENTIAL_STORE_UNAVAILABLE")
        response.headers["Cache-Control"] = "no-store"
        return ProviderProfileIndexResponse(
            items=tuple(
                ProviderProfileResponse(
                    provider_profile_id=item.provider_profile_id,
                    provider_kind=item.provider_kind,
                    endpoint=item.endpoint,
                    account_label=item.account_label,
                    status=cast(Any, item.status),
                    credential_version_id=item.credential_version_id,
                )
                for item in provider_credentials.list_profiles()
            )
        )

    @app.post(
        "/api/v1/provider-credentials/{provider_profile_id}/rotate",
        response_model=ProviderCredentialReceiptResponse,
        responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def rotate_provider_credential(
        provider_profile_id: str,
        payload: ProviderCredentialRotateRequest,
        response: Response,
    ) -> ProviderCredentialReceiptResponse:
        if provider_credentials is None:
            raise HTTPException(status_code=503, detail="CREDENTIAL_STORE_UNAVAILABLE")
        try:
            receipt = provider_credentials.rotate(
                provider_profile_id,
                secret=payload.secret.get_secret_value(),
            )
        except CredentialLifecycleError as error:
            raise HTTPException(status_code=503, detail="CREDENTIAL_STORE_UNAVAILABLE") from error
        response.headers["Cache-Control"] = "no-store"
        return ProviderCredentialReceiptResponse(
            provider_profile_id=receipt.provider_profile_id,
            credential_version_id=receipt.credential_version_id,
            status="adopted",
        )

    @app.delete(
        "/api/v1/provider-credentials/{provider_profile_id}",
        response_model=ProviderProfileDeleteReceiptResponse,
        responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def delete_provider_credential(
        provider_profile_id: str,
        response: Response,
    ) -> ProviderProfileDeleteReceiptResponse:
        if provider_credentials is None:
            raise HTTPException(status_code=503, detail="CREDENTIAL_STORE_UNAVAILABLE")
        try:
            receipt = provider_credentials.delete_profile(provider_profile_id)
        except CredentialLifecycleError as error:
            raise HTTPException(status_code=503, detail="CREDENTIAL_STORE_UNAVAILABLE") from error
        response.headers["Cache-Control"] = "no-store"
        return ProviderProfileDeleteReceiptResponse(
            provider_profile_id=receipt.provider_profile_id,
            status="deleted",
        )

    def model_configuration_response(
        configuration: WorkspaceModelConfiguration,
    ) -> WorkspaceModelConfigurationResponse:
        return WorkspaceModelConfigurationResponse(
            workspace_id=configuration.workspace_id,
            provider_profile_id=configuration.provider_profile_id,
            provider_kind=configuration.provider_kind,
            endpoint=configuration.endpoint,
            model_name=configuration.model_name,
            reasoning_effort=cast(
                Literal["none", "low", "medium", "high"],
                configuration.reasoning_effort,
            ),
            permission_mode=cast(
                Literal["workspace_only", "full_access"],
                configuration.permission_mode,
            ),
            configuration_revision=configuration.configuration_revision,
            context_window_tokens=configuration.context_window_tokens,
            max_output_tokens=configuration.max_output_tokens,
            reserved_runtime_tokens=configuration.reserved_runtime_tokens,
        )

    @app.put(
        "/api/v1/workspaces/{workspace_id}/model-configuration",
        response_model=WorkspaceModelConfigurationResponse,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def select_workspace_model(
        workspace_id: str, payload: WorkspaceModelConfigurationRequest
    ) -> WorkspaceModelConfigurationResponse:
        if model_configuration is None:
            raise HTTPException(status_code=503, detail="MODEL_CONFIGURATION_UNAVAILABLE")
        if not host.database(WorkspaceId(workspace_id)).database_path.is_file():
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND")
        try:
            selected = model_configuration.select(
                workspace_id=workspace_id,
                provider_profile_id=payload.provider_profile_id,
                model_name=payload.model_name,
                reasoning_effort=payload.reasoning_effort,
                permission_mode=payload.permission_mode,
                context_window_tokens=payload.context_window_tokens,
                max_output_tokens=payload.max_output_tokens,
                reserved_runtime_tokens=payload.reserved_runtime_tokens,
            )
        except (CredentialLifecycleError, ValueError) as error:
            raise HTTPException(status_code=409, detail="MODEL_CONFIGURATION_REJECTED") from error
        return model_configuration_response(selected)

    @app.get(
        "/api/v1/workspaces/{workspace_id}/model-configuration",
        response_model=WorkspaceModelConfigurationResponse,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def get_workspace_model(
        workspace_id: str,
    ) -> WorkspaceModelConfigurationResponse:
        if model_configuration is None:
            raise HTTPException(status_code=503, detail="MODEL_CONFIGURATION_UNAVAILABLE")
        try:
            return model_configuration_response(model_configuration.resolve(workspace_id))
        except CredentialLifecycleError as error:
            raise HTTPException(status_code=404, detail="MODEL_CONFIGURATION_NOT_FOUND") from error

    @app.post(
        "/api/v1/diagnostic-bundles/requests",
        response_model=DiagnosticBundlePreviewResponse,
        responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    def create_diagnostic_bundle_request(
        payload: DiagnosticBundleCreateRequest,
        response: Response,
    ) -> DiagnosticBundlePreviewResponse:
        if diagnostic_bundle_factory is None:
            raise HTTPException(status_code=503, detail="DIAGNOSTICS_UNAVAILABLE")
        mode = DiagnosticBundleMode(payload.mode)
        workspace_id = None if payload.workspace_id is None else WorkspaceId(payload.workspace_id)
        if mode is DiagnosticBundleMode.SCOPED_WORKSPACE and workspace_id is None:
            raise HTTPException(status_code=422, detail="WORKSPACE_SCOPE_REQUIRED")
        if mode is DiagnosticBundleMode.SYSTEM_ONLY and workspace_id is not None:
            raise HTTPException(status_code=422, detail="SYSTEM_ONLY_HAS_NO_WORKSPACE")
        builder = diagnostic_bundle_factory(workspace_id)
        request_record = builder.create_request(
            mode,
            turn_id=payload.turn_id,
            operation_id=payload.operation_id,
            requesting_user_action=True,
        )
        diagnostic_requests[request_record.bundle_request_id] = (
            request_record,
            workspace_id,
        )
        preview = builder.preview(request_record)
        response.headers["Cache-Control"] = "no-store"
        return DiagnosticBundlePreviewResponse(
            bundle_request_id=request_record.bundle_request_id,
            mode=payload.mode,
            diagnostic_snapshot_end=request_record.diagnostic_snapshot_end,
            requested_workspace_revision=request_record.requested_workspace_revision,
            included_categories=tuple(preview["included_categories"]),
            excluded_categories=tuple(preview["excluded_categories"]),
            integrity_claim=str(preview["integrity_claim"]),
            authenticity_claim=str(preview["authenticity_claim"]),
        )

    @app.post(
        "/api/v1/diagnostic-bundles/{bundle_request_id}/save",
        response_model=DiagnosticBundleReceiptResponse,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    def save_diagnostic_bundle(
        bundle_request_id: str,
        payload: DiagnosticBundleSaveRequest,
        response: Response,
    ) -> DiagnosticBundleReceiptResponse:
        pending = diagnostic_requests.get(bundle_request_id)
        if pending is None or diagnostic_bundle_factory is None:
            raise HTTPException(status_code=404, detail="BUNDLE_REQUEST_NOT_FOUND")
        request_record, workspace_id = pending
        builder = diagnostic_bundle_factory(workspace_id)
        protected = () if diagnostic_protected_values is None else diagnostic_protected_values()
        try:
            outcome = builder.build(
                request_record,
                Path(payload.output_path),
                protected_values=protected,
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="BUNDLE_BUILD_REJECTED") from error
        diagnostic_requests.pop(bundle_request_id, None)
        response.headers["Cache-Control"] = "no-store"
        return DiagnosticBundleReceiptResponse(
            bundle_id=outcome.bundle_id,
            output_path=str(outcome.output_path),
            size_bytes=outcome.size_bytes,
            sha256=outcome.sha256,
            member_count=outcome.member_count,
            diagnostic_snapshot_end=outcome.diagnostic_snapshot_end,
            requested_workspace_revision=outcome.requested_workspace_revision,
        )

    @app.get("/api/v1/workspace-attention", response_model=GlobalAttentionResponse)
    def workspace_attention() -> GlobalAttentionResponse:
        summaries = []
        for workspace_id in host.workspace_ids():
            connection = host.database(workspace_id).open(writable=False)
            try:
                summaries.append(SqliteWorkspaceQuery(connection).attention_snapshot())
            finally:
                connection.close()
        order = {"CRITICAL": 0, "ACTION_REQUIRED": 1, "INFO": 2}
        summaries.sort(key=lambda item: (order[item.highest_severity], item.workspace_id))
        return GlobalAttentionResponse(
            generated_at=datetime.now(UTC),
            workspaces=tuple(
                WorkspaceAttentionResponse(
                    workspace_id=item.workspace_id,
                    observed_authoritative_revision=item.authoritative_revision.value,
                    active_write_turn_id=None
                    if item.active_write_turn_id is None
                    else item.active_write_turn_id.value,
                    active_write_turn_status=item.active_write_turn_status,
                    queued_write_count=item.queued_write_count,
                    active_read_count=item.active_read_count,
                    requires_action=item.requires_action,
                    highest_severity=cast(
                        Literal["INFO", "ACTION_REQUIRED", "CRITICAL"],
                        item.highest_severity,
                    ),
                    attention_refs=tuple(
                        AttentionRefResponse(
                            attention_kind=ref.attention_kind,
                            severity=cast(
                                Literal["INFO", "ACTION_REQUIRED", "CRITICAL"],
                                ref.severity,
                            ),
                            resource_ref=ResourceRef(
                                resource_type=cast(
                                    Literal[
                                        "workspace",
                                        "conversation",
                                        "message",
                                        "turn",
                                        "waiting_request",
                                        "research_path",
                                        "result",
                                        "stata_run",
                                        "document_revision",
                                        "artifact",
                                        "journal_entry",
                                        "operation",
                                        "pause_intent",
                                    ],
                                    ref.resource_type,
                                ),
                                resource_id=ref.resource_id,
                            ),
                        )
                        for ref in item.attention_refs
                    ),
                )
                for item in summaries
            ),
        )

    def attention_fingerprint() -> tuple[tuple[str, int], ...]:
        fingerprint: list[tuple[str, int]] = []
        for workspace_id in host.workspace_ids():
            connection = host.database(workspace_id).open(writable=False)
            try:
                revision = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            fingerprint.append((workspace_id.value, revision))
        return tuple(fingerprint)

    @app.get(
        "/api/v1/workspace-attention/events",
        responses={
            200: {
                "content": {"text/event-stream": {}},
                "description": "Ephemeral invalidation stream for the global Attention query",
            }
        },
    )
    async def workspace_attention_events(request: Request) -> StreamingResponse:
        async def event_stream() -> AsyncIterator[str]:
            previous = attention_fingerprint()
            yield "retry: 1000\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(0.5)
                current = attention_fingerprint()
                if current != previous:
                    previous = current
                    yield 'event: attention_invalidated\ndata: {"schema_version":"1"}\n\n'
                else:
                    yield ": keepalive\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/api/v1/commands",
        response_model=CommandReceiptResponse,
        responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    )
    async def submit_command(command: CommandEnvelope) -> CommandReceiptResponse:
        workspace_id = WorkspaceId(command.workspace_id)
        database = host.database(workspace_id)
        try:
            if isinstance(command, WorkspaceCreateEnvelope):
                if not database.database_path.exists():
                    database.create()
                connection = database.open(writable=True)
                try:
                    create_result = WorkspaceControlService(
                        SqliteControlStore(connection), UuidIdentityGenerator()
                    ).create_workspace(
                        CreateWorkspaceCommand(CommandId(command.command_id), workspace_id)
                    )
                finally:
                    connection.close()
                return CommandReceiptResponse(
                    command_id=command.command_id,
                    commit_revision=create_result.commit_revision.value,
                    replayed=create_result.replayed,
                    created_resource_refs=(
                        ResourceRef(resource_type="workspace", resource_id=workspace_id.value),
                    ),
                )

            if isinstance(command, MessageSubmitEnvelope):
                connection = database.open(writable=True)
                try:
                    message_result = WorkspaceControlService(
                        SqliteControlStore(connection), UuidIdentityGenerator()
                    ).submit_message(
                        SubmitMessageCommand(
                            command_id=CommandId(command.command_id),
                            content=command.payload.content,
                            execution_mode=ExecutionMode(command.payload.execution_mode),
                            conversation_id=None
                            if command.payload.conversation_id is None
                            else ConversationId(command.payload.conversation_id),
                            research_path_id=None
                            if command.payload.research_path_id is None
                            else ResearchPathId(command.payload.research_path_id),
                            goal_mode=command.payload.goal_mode,
                        )
                    )
                finally:
                    connection.close()
                if turn_dispatcher is not None and message_result.turn_status.value == "running":
                    turn_dispatcher.dispatch(workspace_id, message_result.turn_id)
                return CommandReceiptResponse(
                    command_id=command.command_id,
                    commit_revision=message_result.commit_revision.value,
                    replayed=message_result.replayed,
                    created_resource_refs=(
                        ResourceRef(
                            resource_type="conversation",
                            resource_id=message_result.conversation_id.value,
                        ),
                        ResourceRef(
                            resource_type="message", resource_id=message_result.message_id.value
                        ),
                        ResourceRef(resource_type="turn", resource_id=message_result.turn_id.value),
                    ),
                    turn_id=message_result.turn_id.value,
                )
            if isinstance(command, ResearchPathBranchEnvelope):
                connection = database.open(writable=True)
                identities = UuidIdentityGenerator()
                try:
                    intent_command_id = CommandId(
                        "cmd_branch_intent_" + command.command_id.removeprefix("cmd_")
                    )
                    complete_command_id = CommandId(
                        "cmd_branch_complete_" + command.command_id.removeprefix("cmd_")
                    )
                    replaying = (
                        connection.execute(
                            "SELECT 1 FROM command_receipts WHERE command_id = ?",
                            (intent_command_id.value,),
                        ).fetchone()
                        is not None
                    )
                    if not replaying:
                        current_revision = int(
                            connection.execute(
                                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                            ).fetchone()[0]
                        )
                        if current_revision != command.payload.expected_workspace_revision:
                            raise ValueError("stale Workspace revision for branch request")
                    control = WorkspaceControlService(SqliteControlStore(connection), identities)
                    intent = control.submit_message(
                        SubmitMessageCommand(
                            intent_command_id,
                            f"创建研究分支：{command.payload.display_name}。"
                            f"原因：{command.payload.branch_reason}",
                            ExecutionMode.WRITE,
                            None
                            if command.payload.conversation_id is None
                            else ConversationId(command.payload.conversation_id),
                            ResearchPathId(command.payload.source_research_path_id),
                            "research_loop",
                        )
                    )
                    if intent.turn_status is not TurnStatus.RUNNING:
                        raise ValueError(
                            "Workspace is busy; branch creation requires the write lane"
                        )
                    branch = ResearchStateService(
                        SqliteResearchStateRepository(connection), identities
                    ).create_path_branch(
                        CreateResearchPathBranchCommand(
                            CommandId(command.command_id),
                            ResearchPathId(command.payload.source_research_path_id),
                            intent.turn_id,
                            command.payload.canonical_key,
                            command.payload.display_name,
                            command.payload.branch_reason,
                            intent.commit_revision.value,
                            intent.commit_revision.value,
                        )
                    )
                    completed = control.complete_turn(
                        CompleteTurnCommand(
                            complete_command_id,
                            intent.turn_id,
                            TurnStatus.SUCCEEDED,
                        )
                    )
                finally:
                    connection.close()
                return CommandReceiptResponse(
                    command_id=command.command_id,
                    commit_revision=completed.commit_revision.value,
                    replayed=branch.replayed,
                    created_resource_refs=(
                        ResourceRef(
                            resource_type="research_path",
                            resource_id=branch.research_path_id.value,
                        ),
                        ResourceRef(
                            resource_type="turn",
                            resource_id=intent.turn_id.value,
                        ),
                    ),
                    turn_id=intent.turn_id.value,
                )
            if isinstance(command, WaitingAnswerEnvelope):
                connection = database.open(writable=True)
                try:
                    waiting_result = TurnInteractionService(
                        SqliteTurnInteractionRepository(connection), UuidIdentityGenerator()
                    ).answer_waiting(
                        AnswerWaitingCommand(
                            command_id=CommandId(command.command_id),
                            waiting_request_id=WaitingRequestId(command.payload.waiting_request_id),
                            answer=command.payload.answer,
                            tool_decision=command.payload.tool_decision,
                            expected_turn_revision=command.payload.waiting_revision,
                            expected_turn_id=TurnId(command.payload.turn_id),
                        )
                    )
                finally:
                    connection.close()
                if turn_dispatcher is not None:
                    turn_dispatcher.dispatch(workspace_id, waiting_result.turn_id)
                return CommandReceiptResponse(
                    command_id=command.command_id,
                    commit_revision=waiting_result.commit_revision.value,
                    replayed=waiting_result.replayed,
                    created_resource_refs=(
                        ResourceRef(
                            resource_type="waiting_request",
                            resource_id=waiting_result.waiting_request_id.value,
                        ),
                        ResourceRef(resource_type="turn", resource_id=waiting_result.turn_id.value),
                    ),
                    turn_id=waiting_result.turn_id.value,
                    turn_revision=waiting_result.turn_revision,
                    effect="waiting_answered",
                )
            if isinstance(command, TurnPauseRequestEnvelope):
                connection = database.open(writable=True)
                try:
                    pause_result = TurnInteractionService(
                        SqliteTurnInteractionRepository(connection), UuidIdentityGenerator()
                    ).request_pause(
                        RequestPauseCommand(
                            command_id=CommandId(command.command_id),
                            turn_id=TurnId(command.payload.turn_id),
                            expected_turn_revision=command.payload.expected_turn_revision,
                            reason=command.payload.reason,
                        )
                    )
                finally:
                    connection.close()
                return CommandReceiptResponse(
                    command_id=command.command_id,
                    commit_revision=pause_result.commit_revision.value,
                    replayed=pause_result.replayed,
                    created_resource_refs=(
                        ResourceRef(
                            resource_type="pause_intent",
                            resource_id=pause_result.pause_intent_id.value,
                        ),
                        ResourceRef(resource_type="turn", resource_id=pause_result.turn_id.value),
                    ),
                    turn_id=pause_result.turn_id.value,
                    turn_revision=pause_result.turn_revision,
                    effect="pause_requested",
                )
            raise HTTPException(status_code=422, detail="unregistered command type")
        except CommandConflictError as error:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_REUSED") from error
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="COMMAND_REJECTED") from error

    @app.get(
        "/api/v1/workspaces/{workspace_id}/bootstrap",
        response_model=WorkspaceBootstrapResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def bootstrap(workspace_id: str) -> WorkspaceBootstrapResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            database = host.database(typed_workspace_id)
            connection = database.open(writable=False)
            try:
                snapshot = SqliteWorkspaceQuery(connection).bootstrap_snapshot()
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND") from error

        turns = tuple(
            TurnResponse(
                turn_id=turn.turn_id.value,
                conversation_id=turn.conversation_id.value,
                execution_mode=turn.execution_mode.value,
                status=turn.status.value,
                turn_revision=turn.turn_revision.value,
                enqueue_ordinal=turn.enqueue_ordinal.value,
            )
            for turn in snapshot.turns
        )
        waiting = snapshot.open_waiting_request
        pause_intent = snapshot.active_pause_intent
        resource_refs = (
            ResourceRef(resource_type="workspace", resource_id=workspace_id),
            *(
                ResourceRef(
                    resource_type="conversation",
                    resource_id=conversation.conversation_id.value,
                )
                for conversation in snapshot.conversations
            ),
            *(ResourceRef(resource_type="turn", resource_id=turn.turn_id) for turn in turns),
            *(
                ResourceRef(
                    resource_type="research_path",
                    resource_id=path.research_path_id.value,
                )
                for path in snapshot.research_paths
            ),
            *(
                ()
                if pause_intent is None
                else (
                    ResourceRef(
                        resource_type="pause_intent",
                        resource_id=pause_intent.pause_intent_id,
                    ),
                )
            ),
        )
        return WorkspaceBootstrapResponse(
            workspace_id=snapshot.workspace_id,
            authoritative_revision=snapshot.authoritative_revision.value,
            generated_at=datetime.now(UTC),
            durable_stream_cursor=snapshot.durable_stream_cursor,
            data=WorkspaceBootstrapData(
                active_conversation_id=None
                if snapshot.active_conversation_id is None
                else snapshot.active_conversation_id.value,
                conversations=tuple(
                    ConversationSummaryResponse(
                        conversation_id=conversation.conversation_id.value,
                        created_revision=conversation.created_revision.value,
                        latest_message_id=None
                        if conversation.latest_message_id is None
                        else conversation.latest_message_id.value,
                        latest_message_preview=conversation.latest_message_preview,
                        latest_message_ordinal=None
                        if conversation.latest_message_ordinal is None
                        else conversation.latest_message_ordinal.value,
                    )
                    for conversation in snapshot.conversations
                ),
                recent_messages=tuple(
                    MessageSummaryResponse(
                        message_id=message.message_id.value,
                        conversation_id=message.conversation_id.value,
                        role="user",
                        content=message.content,
                        ordinal=message.ordinal.value,
                        created_revision=message.created_revision.value,
                    )
                    for message in snapshot.recent_messages
                ),
                execution=WorkspaceExecutionData(
                    lane_revision=snapshot.lane_revision.value,
                    active_write_turn_id=None
                    if snapshot.active_write_turn_id is None
                    else snapshot.active_write_turn_id.value,
                    turns=turns,
                ),
                open_waiting_request=None
                if waiting is None
                else WaitingRequestResponse(
                    waiting_request_id=waiting.waiting_request_id,
                    turn_id=waiting.turn_id.value,
                    wait_reason=cast(WaitingReason, waiting.wait_reason),
                    prompt=waiting.prompt,
                    status="open",
                    created_turn_revision=waiting.created_turn_revision.value,
                ),
                active_pause_intent=None
                if pause_intent is None
                else PauseIntentResponse(
                    pause_intent_id=pause_intent.pause_intent_id,
                    turn_id=pause_intent.turn_id.value,
                    requested_turn_revision=pause_intent.requested_turn_revision.value,
                    reason=pause_intent.reason,
                    status=cast(Literal["requested", "converging"], pause_intent.status),
                ),
                research_paths=tuple(
                    ResearchPathSummaryResponse(
                        research_path_id=path.research_path_id.value,
                        canonical_key=path.canonical_key,
                        created_revision=path.created_revision.value,
                    )
                    for path in snapshot.research_paths
                ),
                projection_watermarks=tuple(
                    ProjectionWatermarkResponse(
                        projection_name="evidence_current_state",
                        projection_revision=watermark.projection_revision.value,
                        projection_lag=max(
                            0,
                            snapshot.authoritative_revision.value
                            - watermark.projection_revision.value,
                        ),
                    )
                    for watermark in snapshot.projection_watermarks
                ),
            ),
            resource_refs=resource_refs,
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/data-files",
        response_model=WorkspaceDataCatalogResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def workspace_data_files(workspace_id: str) -> WorkspaceDataCatalogResponse:
        typed_workspace_id = WorkspaceId(workspace_id)
        database = host.database(typed_workspace_id)
        if not database.database_path.is_file():
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND")
        connection = database.open(writable=False)
        try:
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        candidates = FilesystemWorkspaceDataCatalog(database.root).discover()
        return WorkspaceDataCatalogResponse(
            workspace_id=workspace_id,
            authoritative_revision=revision,
            observed_at=datetime.now(UTC),
            items=tuple(
                WorkspaceDataCandidateResponse(
                    relative_path=item.relative_path,
                    display_name=item.display_name,
                    size_bytes=item.size_bytes,
                    modified_ns=item.modified_ns,
                )
                for item in candidates
            ),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/execution",
        response_model=WorkspaceExecutionResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def execution(workspace_id: str) -> WorkspaceExecutionResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            database = host.database(typed_workspace_id)
            connection = database.open(writable=False)
            try:
                snapshot = SqliteWorkspaceQuery(connection).execution_snapshot()
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND") from error
        turns = tuple(
            TurnResponse(
                turn_id=turn.turn_id.value,
                conversation_id=turn.conversation_id.value,
                execution_mode=turn.execution_mode.value,
                status=turn.status.value,
                turn_revision=turn.turn_revision.value,
                enqueue_ordinal=turn.enqueue_ordinal.value,
            )
            for turn in snapshot.turns
        )
        return WorkspaceExecutionResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision.value,
            generated_at=datetime.now(UTC),
            data=WorkspaceExecutionData(
                lane_revision=snapshot.lane_revision.value,
                active_write_turn_id=None
                if snapshot.active_write_turn_id is None
                else snapshot.active_write_turn_id.value,
                turns=turns,
            ),
            resource_refs=tuple(
                ResourceRef(resource_type="turn", resource_id=turn.turn_id) for turn in turns
            ),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}",
        response_model=ConversationDetailResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def conversation_detail(workspace_id: str, conversation_id: str) -> ConversationDetailResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            typed_conversation_id = ConversationId(conversation_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                snapshot = SqliteResearchViewQuery(connection, journal_cursor_codec).conversation(
                    typed_conversation_id
                )
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
        turns = tuple(
            TurnResponse(
                turn_id=turn.turn_id.value,
                conversation_id=turn.conversation_id.value,
                execution_mode=turn.execution_mode.value,
                status=turn.status.value,
                turn_revision=turn.turn_revision.value,
                enqueue_ordinal=turn.enqueue_ordinal.value,
            )
            for turn in snapshot.turns
        )

        timeline = tuple(
            ConversationTimelineItemResponse(
                item_kind=cast(Literal["message", "assistant_output"], item.item_kind),
                item_id=item.item_id,
                turn_id=None if item.turn_id is None else item.turn_id.value,
                role=cast(Literal["user", "assistant"], item.role),
                content=item.content,
                ordinal=item.ordinal,
                created_revision=item.created_revision.value,
            )
            for item in snapshot.timeline
        )
        return ConversationDetailResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision.value,
            generated_at=datetime.now(UTC),
            data=ConversationDetailData(
                conversation_id=conversation_id, timeline=timeline, turns=turns
            ),
            resource_refs=(
                ResourceRef(resource_type="conversation", resource_id=conversation_id),
                *(ResourceRef(resource_type="turn", resource_id=turn.turn_id) for turn in turns),
            ),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/turns/{turn_id}/usage",
        response_model=TurnUsageResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def turn_usage(workspace_id: str, turn_id: str) -> TurnUsageResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            typed_turn_id = TurnId(turn_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                snapshot = SqliteRuntimeUsageQuery(connection, pricing_catalog).turn(typed_turn_id)
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="TURN_NOT_FOUND") from error
        count_budget = None
        if snapshot.count_budget is not None:
            count_budget = CountBudgetUsageResponse(**asdict(snapshot.count_budget))
        runtime_budget = None
        if snapshot.runtime_budget is not None:
            runtime_budget = RuntimeBudgetUsageResponse(**asdict(snapshot.runtime_budget))
        money = snapshot.monetary_estimate
        return TurnUsageResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision,
            generated_at=datetime.now(UTC),
            data=TurnUsageData(
                turn_id=snapshot.turn_id,
                turn_status=snapshot.turn_status,
                step_count=snapshot.step_count,
                input_tokens_observed=snapshot.input_tokens_observed,
                output_tokens_observed=snapshot.output_tokens_observed,
                cached_input_tokens_observed=snapshot.cached_input_tokens_observed,
                uncached_input_tokens_observed=snapshot.uncached_input_tokens_observed,
                unknown_usage_attempt_count=snapshot.unknown_usage_attempt_count,
                token_quality=snapshot.token_quality,
                retry_count=snapshot.retry_count,
                fallback_count=snapshot.fallback_count,
                provider_duration_observed_seconds=(snapshot.provider_duration_observed_seconds),
                tool_duration_observed_seconds=snapshot.tool_duration_observed_seconds,
                provider_attempts=tuple(
                    ProviderAttemptUsageResponse(**asdict(attempt))
                    for attempt in snapshot.provider_attempts
                ),
                tool_operations=tuple(
                    ToolOperationUsageResponse(**asdict(operation))
                    for operation in snapshot.tool_operations
                ),
                count_budget=count_budget,
                runtime_budget=runtime_budget,
                monetary_estimate=MonetaryUsageEstimateResponse(
                    pricing_revision=money.pricing_revision,
                    currency=money.currency,
                    quality=money.quality,
                    amount=None if money.amount is None else format(money.amount, "f"),
                    known_subtotal=format(money.known_subtotal, "f"),
                    known_cached_savings=format(money.known_cached_savings, "f"),
                    priced_attempt_count=money.priced_attempt_count,
                    unpriced_attempt_count=money.unpriced_attempt_count,
                    explanation=money.explanation,
                ),
            ),
            resource_refs=(ResourceRef(resource_type="turn", resource_id=turn_id),),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/turns/{turn_id}/evaluation",
        response_model=TurnOperationalEvaluationResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def turn_operational_evaluation(
        workspace_id: str, turn_id: str
    ) -> TurnOperationalEvaluationResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            typed_turn_id = TurnId(turn_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                snapshot = SqliteOperationalEvaluationQuery(connection).turn(typed_turn_id)
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="TURN_NOT_FOUND") from error
        return TurnOperationalEvaluationResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision,
            generated_at=datetime.now(UTC),
            data=TurnOperationalEvaluationData(
                policy_revision=snapshot.policy_revision,
                turn_id=snapshot.turn_id,
                turn_status=snapshot.turn_status,
                research_path_id=snapshot.research_path_id,
                created_at=datetime.fromisoformat(snapshot.created_at),
                terminal_at=(
                    None
                    if snapshot.terminal_at is None
                    else datetime.fromisoformat(snapshot.terminal_at)
                ),
                configuration=OperationalConfigurationResponse(**asdict(snapshot.configuration)),
                layers=tuple(
                    OperationalLayerResponse(**asdict(layer)) for layer in snapshot.layers
                ),
                breakdowns=tuple(
                    OperationalBreakdownResponse(**asdict(item)) for item in snapshot.breakdowns
                ),
            ),
            resource_refs=(ResourceRef(resource_type="turn", resource_id=turn_id),),
        )

    @app.post(
        "/api/v1/workspaces/{workspace_id}/turns/{turn_id}/outcome-feedback",
        response_model=TurnOutcomeFeedbackResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    def record_turn_outcome_feedback(
        workspace_id: str,
        turn_id: str,
        request: TurnOutcomeFeedbackRequest,
    ) -> TurnOutcomeFeedbackResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            typed_turn_id = TurnId(turn_id)
            connection = host.database(typed_workspace_id).open(writable=True)
            try:
                outcome = TurnOutcomeFeedbackService(
                    SqliteTurnOutcomeFeedbackRepository(connection),
                    UuidIdentityGenerator(),
                ).record(
                    RecordTurnOutcomeFeedbackCommand(
                        command_id=CommandId(request.command_id),
                        turn_id=typed_turn_id,
                        disposition=OutcomeDisposition(request.disposition),
                        ratings=tuple(
                            OutcomeRating(item.dimension, item.score) for item in request.ratings
                        ),
                        issue_codes=request.issue_codes,
                        comment=request.comment,
                        policy_revision=request.policy_revision,
                    )
                )
            finally:
                connection.close()
        except PersistenceBootstrapError as error:
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND") from error
        except CommandConflictError as error:
            raise HTTPException(status_code=409, detail="COMMAND_CONFLICT") from error
        except ValueError as error:
            detail = str(error)
            status_code = 404 if detail == "Turn does not exist" else 409
            raise HTTPException(status_code=status_code, detail=detail) from error
        return TurnOutcomeFeedbackResponse(
            feedback_id=outcome.feedback_id.value,
            turn_id=outcome.turn_id.value,
            disposition=outcome.disposition.value,
            commit_revision=outcome.commit_revision.value,
            replayed=outcome.replayed,
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/evaluation",
        response_model=WorkspaceOperationalEvaluationResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def workspace_operational_evaluation(
        workspace_id: str,
    ) -> WorkspaceOperationalEvaluationResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                snapshot = SqliteOperationalEvaluationQuery(connection).workspace()
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND") from error
        return WorkspaceOperationalEvaluationResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision,
            generated_at=datetime.now(UTC),
            data=WorkspaceOperationalEvaluationData(
                policy_revision=snapshot.policy_revision,
                layers=tuple(
                    OperationalLayerResponse(**asdict(layer)) for layer in snapshot.layers
                ),
                breakdowns=tuple(
                    OperationalBreakdownResponse(**asdict(item)) for item in snapshot.breakdowns
                ),
                turns=tuple(
                    TurnOperationalEvaluationData(
                        policy_revision=turn.policy_revision,
                        turn_id=turn.turn_id,
                        turn_status=turn.turn_status,
                        research_path_id=turn.research_path_id,
                        created_at=datetime.fromisoformat(turn.created_at),
                        terminal_at=(
                            None
                            if turn.terminal_at is None
                            else datetime.fromisoformat(turn.terminal_at)
                        ),
                        configuration=OperationalConfigurationResponse(
                            **asdict(turn.configuration)
                        ),
                        layers=tuple(
                            OperationalLayerResponse(**asdict(layer)) for layer in turn.layers
                        ),
                        breakdowns=tuple(
                            OperationalBreakdownResponse(**asdict(item)) for item in turn.breakdowns
                        ),
                    )
                    for turn in snapshot.turns
                ),
            ),
            resource_refs=(ResourceRef(resource_type="workspace", resource_id=workspace_id),),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/research-paths/{research_path_id}/results",
        response_model=ResultIndexResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def result_index(workspace_id: str, research_path_id: str) -> ResultIndexResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            typed_path_id = ResearchPathId(research_path_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                snapshot = SqliteResearchViewQuery(connection, journal_cursor_codec).results(
                    typed_path_id
                )
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="RESEARCH_PATH_NOT_FOUND") from error
        items = tuple(
            ResultIndexItemResponse(
                result_slot_id=item.result_slot_id,
                slot_key=item.slot_key,
                slot_display_name=item.slot_display_name,
                result_id=item.result_id,
                result_kind=cast(Literal["statistical", "visual"], item.result_kind),
                producing_stata_run_id=item.producing_stata_run_id,
                created_by_turn_id=item.created_by_turn_id.value,
                created_revision=item.created_revision.value,
                pointer_revision=item.pointer_revision,
                adopted_revision=item.adopted_revision.value,
                element_count=item.element_count,
                evidence_count=item.evidence_count,
                result_element_ids=item.result_element_ids,
            )
            for item in snapshot.items
        )
        return ResultIndexResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision.value,
            generated_at=datetime.now(UTC),
            data=ResultIndexData(research_path_id=research_path_id, items=items),
            resource_refs=tuple(
                ref
                for item in items
                for ref in (
                    ResourceRef(resource_type="result", resource_id=item.result_id),
                    ResourceRef(resource_type="stata_run", resource_id=item.producing_stata_run_id),
                )
            ),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/analysis-outputs",
        response_model=AnalysisOutputIndexResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def analysis_output_index(workspace_id: str) -> AnalysisOutputIndexResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                snapshot = SqliteResearchViewQuery(
                    connection, journal_cursor_codec
                ).analysis_outputs()
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND") from error
        items = tuple(
            AnalysisOutputItemResponse(
                analysis_output_id=item.analysis_output_id,
                output_kind=cast(
                    Literal[
                        "visual",
                        "scalar",
                        "table",
                        "test",
                        "custom",
                        "regression",
                        "unknown",
                    ],
                    item.output_kind,
                ),
                output_fingerprint=item.output_fingerprint,
                runtime_kind=cast(Literal["python", "shell"], item.runtime_kind),
                method_summary=item.method_summary,
                created_by_turn_id=item.created_by_turn_id.value,
                created_revision=item.created_revision.value,
                document_eligible=item.document_eligible,
                adoption_id=item.adoption_id,
                evidence_record_id=item.evidence_record_id,
                artifacts=tuple(
                    AnalysisOutputArtifactResponse(
                        artifact_id=artifact.artifact_id,
                        role=cast(Literal["primary", "preview", "supporting"], artifact.role),
                        artifact_kind=cast(
                            Literal[
                                "dataset",
                                "code",
                                "log",
                                "table",
                                "document",
                                "diagnostic",
                            ],
                            artifact.artifact_kind,
                        ),
                        media_type=artifact.media_type,
                        size_bytes=artifact.size_bytes,
                        content_sha256=artifact.content_sha256,
                        verification_receipt_id=artifact.verification_receipt_id,
                    )
                    for artifact in item.artifacts
                ),
            )
            for item in snapshot.items
        )
        return AnalysisOutputIndexResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision.value,
            generated_at=datetime.now(UTC),
            data=AnalysisOutputIndexData(items=items),
            resource_refs=tuple(
                ref
                for item in items
                for ref in (
                    ResourceRef(
                        resource_type="analysis_output",
                        resource_id=item.analysis_output_id,
                    ),
                    *(
                        ResourceRef(resource_type="artifact", resource_id=artifact.artifact_id)
                        for artifact in item.artifacts
                    ),
                )
            ),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/content",
        response_class=Response,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    def artifact_content(workspace_id: str, artifact_id: str) -> Response:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            ArtifactId(artifact_id)
            database = host.database(typed_workspace_id)
            connection = database.open(writable=False)
            try:
                row = connection.execute(
                    """
                    SELECT artifact.media_type, artifact.size_bytes,
                           artifact.content_hash, state.availability,
                           location.managed_handle
                    FROM artifacts AS artifact
                    JOIN artifact_states AS state USING (artifact_id)
                    JOIN artifact_locations AS location USING (artifact_id)
                    WHERE artifact.artifact_id = ?
                    """,
                    (artifact_id,),
                ).fetchone()
            finally:
                connection.close()
            if row is None or str(row["availability"]) != "available":
                raise ValueError("Artifact is unavailable")
            if int(row["size_bytes"]) > 20 * 1024 * 1024:
                raise HTTPException(status_code=409, detail="ARTIFACT_PREVIEW_TOO_LARGE")
            raw = Path(str(row["managed_handle"]))
            if raw.is_absolute() or ".." in raw.parts:
                raise ValueError("Artifact handle is unsafe")
            path = (database.root / raw).resolve(strict=True)
            objects = (database.root / ".stata-agent" / "objects").resolve()
            if not path.is_relative_to(objects):
                raise ValueError("Artifact escaped the managed store")
            payload = path.read_bytes()
            if len(payload) != int(row["size_bytes"]) or hashlib.sha256(payload).hexdigest() != str(
                row["content_hash"]
            ):
                raise HTTPException(status_code=409, detail="ARTIFACT_INTEGRITY_MISMATCH")
        except HTTPException:
            raise
        except (OSError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND") from error
        media_type = str(row["media_type"])
        inline_types = {
            "application/json",
            "image/jpeg",
            "image/png",
            "text/csv",
            "text/plain",
        }
        disposition = "inline" if media_type in inline_types else "attachment"
        extension = (
            ".docx"
            if media_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            else ""
        )
        return Response(
            content=payload,
            media_type=media_type,
            headers={
                "Content-Disposition": f'{disposition}; filename="{artifact_id}{extension}"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/research-paths/{research_path_id}/documents",
        response_model=DocumentIndexResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def document_index(workspace_id: str, research_path_id: str) -> DocumentIndexResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            typed_path_id = ResearchPathId(research_path_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                snapshot = SqliteResearchViewQuery(connection, journal_cursor_codec).documents(
                    typed_path_id
                )
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="RESEARCH_PATH_NOT_FOUND") from error
        items = tuple(
            DocumentSlotResponse(
                document_slot_id=item.document_slot_id,
                slot_key=cast(
                    Literal["manuscript.main.working", "manuscript.main.delivery"],
                    item.slot_key,
                ),
                pointer_revision=item.pointer_revision,
                document_id=item.document_id,
                document_revision_id=item.document_revision_id,
                origin_kind=cast(
                    Literal["agent_generated", "user_returned", "system_merged", "imported"] | None,
                    item.origin_kind,
                ),
                docx_artifact_id=item.docx_artifact_id,
                delivery_gate_verdict=cast(
                    Literal["pass", "fail", "unknown"] | None,
                    item.delivery_gate_verdict,
                ),
                adopted_revision=None
                if item.adopted_revision is None
                else item.adopted_revision.value,
            )
            for item in snapshot.items
        )
        return DocumentIndexResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision.value,
            generated_at=datetime.now(UTC),
            data=DocumentIndexData(research_path_id=research_path_id, items=items),
            resource_refs=tuple(
                ref
                for item in items
                for ref in (
                    ()
                    if item.document_revision_id is None
                    else (
                        ResourceRef(
                            resource_type="document_revision",
                            resource_id=item.document_revision_id,
                        ),
                        ResourceRef(
                            resource_type="artifact", resource_id=item.docx_artifact_id or ""
                        ),
                    )
                )
            ),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/turns/{turn_id}/investigation",
        response_model=TurnInvestigationResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def turn_investigation(
        workspace_id: str,
        turn_id: str,
    ) -> TurnInvestigationResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                snapshot = SqliteInvestigationQuery(connection).turn(turn_id)
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="TURN_NOT_FOUND") from error
        return TurnInvestigationResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision,
            generated_at=datetime.now(UTC),
            data=TurnInvestigationData(
                turn_id=snapshot.turn_id,
                turn_status=snapshot.turn_status,
                turn_revision=snapshot.turn_revision,
                last_journal_event_type=snapshot.last_journal_event_type,
                last_workspace_revision=snapshot.last_workspace_revision,
                findings=tuple(
                    InvestigationFindingResponse(
                        layer=item.layer,
                        code=item.code,
                        severity=item.severity,
                        summary=item.summary,
                        object_type=item.object_type,
                        object_id=item.object_id,
                        next_query=f"/api/v1/workspaces/{workspace_id}{item.next_query}",
                    )
                    for item in snapshot.findings
                ),
                tool_call_ids=snapshot.tool_call_ids,
                investigation_note=snapshot.investigation_note,
            ),
            resource_refs=(ResourceRef(resource_type="turn", resource_id=turn_id),),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/turns/{turn_id}/tool-decisions/{tool_call_id}",
        response_model=ToolDecisionTraceResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def tool_decision_trace(
        workspace_id: str,
        turn_id: str,
        tool_call_id: str,
    ) -> ToolDecisionTraceResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                query = SqliteInvestigationQuery(connection)
                trace = query.tool_decision(turn_id, tool_call_id)
                authoritative_revision = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="TOOL_CALL_NOT_FOUND") from error
        return ToolDecisionTraceResponse(
            workspace_id=workspace_id,
            authoritative_revision=authoritative_revision,
            generated_at=datetime.now(UTC),
            data=ToolDecisionTraceData(
                turn_id=trace.turn_id,
                step_id=trace.step_id,
                step_ordinal=trace.step_ordinal,
                context_manifest_id=trace.context_manifest_id,
                model_invocation_id=trace.model_invocation_id,
                model_invocation_status=trace.model_invocation_status,
                provider_attempt_id=trace.provider_attempt_id,
                assistant_output_id=trace.assistant_output_id,
                assistant_public_text=trace.assistant_public_text,
                tool_call_id=trace.tool_call_id,
                call_ordinal=trace.call_ordinal,
                requested_tool_name=trace.requested_tool_name,
                provider_tool_call_id=trace.provider_tool_call_id,
                proposal_status=trace.proposal_status,
                raw_arguments_text=trace.raw_arguments_text,
                canonical_arguments=trace.canonical_arguments,
                normalization_diff=trace.normalization_diff,
                tool_contract_id=trace.tool_contract_id,
                tool_version=trace.tool_version,
                operation_kind=trace.operation_kind,
                effect_class=trace.effect_class,
                execution_owner=trace.execution_owner,
                dispatch_plan_id=trace.dispatch_plan_id,
                execution_batch_ordinal=trace.execution_batch_ordinal,
                admission_id=trace.admission_id,
                admission_policy_revision=trace.admission_policy_revision,
                status_history=tuple(
                    ToolStatusObservationResponse(
                        status_ordinal=item.status_ordinal,
                        proposal_status=item.proposal_status,
                        reason_code=item.reason_code,
                        commit_revision=item.commit_revision,
                    )
                    for item in trace.status_history
                ),
                operations=tuple(
                    ToolOperationTraceResponse(
                        operation_id=item.operation_id,
                        operation_kind=item.operation_kind,
                        status=item.status,
                        created_revision=item.created_revision,
                        terminal_revision=item.terminal_revision,
                        attempts=item.attempts,
                    )
                    for item in trace.operations
                ),
                result_kind=trace.result_kind,
                result_summary=trace.result_summary,
                result_payload=trace.result_payload,
                artifact_references=trace.artifact_references,
                evaluation_findings=trace.evaluation_findings,
                journal_references=trace.journal_references,
                structural_diagnosis=trace.structural_diagnosis,
            ),
            resource_refs=(
                ResourceRef(resource_type="turn", resource_id=turn_id),
                *tuple(
                    ResourceRef(resource_type="operation", resource_id=item.operation_id)
                    for item in trace.operations
                ),
            ),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/journal-entries",
        response_model=JournalEntryPageResponse,
        responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    def journal_entries(
        workspace_id: str,
        turn_id: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
        event_type: str | None = None,
        actor_type: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        sort: Literal["asc", "desc"] = "desc",
        page_size: int = Query(default=50, ge=1, le=200),
        as_of_workspace_revision: int | None = Query(default=None, ge=0),
        cursor: str | None = None,
    ) -> JournalEntryPageResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                page = SqliteResearchViewQuery(connection, journal_cursor_codec).journal_page(
                    workspace_id=workspace_id,
                    filters=JournalFilters(
                        turn_id=turn_id,
                        operation_id=operation_id,
                        attempt_id=attempt_id,
                        event_type=event_type,
                        actor_type=actor_type,
                        object_type=object_type,
                        object_id=object_id,
                    ),
                    sort_direction=sort,
                    page_size=page_size,
                    as_of_workspace_revision=as_of_workspace_revision,
                    cursor=cursor,
                )
            finally:
                connection.close()
        except JournalCursorInvalidError as error:
            raise HTTPException(status_code=400, detail="CURSOR_INVALID") from error
        except JournalCursorQueryMismatchError as error:
            raise HTTPException(status_code=409, detail="CURSOR_QUERY_MISMATCH") from error
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND") from error
        return JournalEntryPageResponse(
            workspace_id=workspace_id,
            generated_at=datetime.now(UTC),
            items=tuple(
                JournalEntryResponse(
                    journal_entry_id=item.journal_entry_id,
                    workspace_revision=item.workspace_revision.value,
                    ordinal=item.ordinal,
                    event_type=item.event_type,
                    object_type=item.object_type,
                    object_id=item.object_id,
                    summary=item.summary,
                    payload_json=item.payload_json,
                )
                for item in page.items
            ),
            next_cursor=page.next_cursor,
            page_size=page.page_size,
            sort_direction=cast(Literal["asc", "desc"], page.sort_direction),
            as_of_workspace_revision=page.as_of_workspace_revision.value,
            authoritative_revision=page.authoritative_revision.value,
            workspace_advanced=page.workspace_advanced,
            newer_matching_entries_available=page.newer_matching_entries_available,
        )

    def read_outbox_batch(
        workspace_id: str, cursor: str, *, limit: int = 100
    ) -> DurableReplayBatch:
        typed_workspace_id = WorkspaceId(workspace_id)
        connection = host.database(typed_workspace_id).open(writable=False)
        try:
            return SqliteOutboxStreamQuery(connection).replay_after(cursor, limit=limit)
        finally:
            connection.close()

    def durable_notification_response(
        notification: DurableNotification,
    ) -> DurableNotificationResponse:
        return DurableNotificationResponse(
            event_id=notification.event_id,
            stream_cursor=notification.stream_cursor,
            workspace_id=notification.workspace_id,
            workspace_revision=notification.workspace_revision.value,
            event_type=notification.event_type,
            resource_refs=tuple(
                ResourceRef(
                    resource_type=cast(
                        Literal[
                            "workspace",
                            "conversation",
                            "message",
                            "turn",
                            "waiting_request",
                            "research_path",
                            "result",
                            "stata_run",
                            "document_revision",
                            "artifact",
                            "journal_entry",
                            "operation",
                        ],
                        ref.resource_type,
                    ),
                    resource_id=ref.resource_id,
                )
                for ref in notification.resource_refs
            ),
            summary_payload=cast(dict[str, object], json.loads(notification.summary_payload_json)),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/stream-head",
        response_model=WorkspaceStreamHeadResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    def stream_head(workspace_id: str, after: str) -> WorkspaceStreamHeadResponse:
        try:
            batch = read_outbox_batch(workspace_id, after, limit=1)
        except StreamResyncRequiredError as error:
            raise HTTPException(status_code=409, detail="RESYNC_REQUIRED") from error
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND") from error
        return WorkspaceStreamHeadResponse(
            workspace_id=workspace_id,
            authoritative_revision=batch.authoritative_revision.value,
            requested_cursor=after,
            current_cursor=batch.current_cursor,
            pending_notifications=bool(batch.notifications),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/events",
        responses={
            200: {
                "model": DurableNotificationResponse,
                "content": {"text/event-stream": {}},
                "description": "Versioned durable SSE notification stream",
            },
            409: {"model": ErrorResponse},
        },
    )
    async def workspace_events(
        request: Request, workspace_id: str, after: str | None = None
    ) -> StreamingResponse:
        cursor = request.headers.get("last-event-id") or after
        if cursor is None:
            raise HTTPException(status_code=409, detail="RESYNC_REQUIRED")
        try:
            read_outbox_batch(workspace_id, cursor, limit=1)
        except StreamResyncRequiredError as error:
            raise HTTPException(status_code=409, detail="RESYNC_REQUIRED") from error
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="WORKSPACE_NOT_FOUND") from error

        async def event_stream() -> AsyncIterator[str]:
            current_cursor = cursor
            yield "retry: 1000\n\n"
            while not await request.is_disconnected():
                try:
                    batch = read_outbox_batch(workspace_id, current_cursor, limit=100)
                except (StreamResyncRequiredError, PersistenceBootstrapError, ValueError):
                    resync = json.dumps(
                        {"schema_version": "1", "error_code": "RESYNC_REQUIRED"},
                        separators=(",", ":"),
                    )
                    yield f"event: resync_required\ndata: {resync}\n\n"
                    return
                if batch.notifications:
                    for notification in batch.notifications:
                        response = durable_notification_response(notification)
                        data = response.model_dump_json()
                        yield (
                            f"id: {notification.stream_cursor}\nevent: durable\ndata: {data}\n\n"
                        )
                        current_cursor = notification.stream_cursor
                    continue
                yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/turns/{turn_id}/model-deltas",
        responses={
            200: {
                "content": {"text/event-stream": {}},
                "description": "Best-effort non-authoritative live model response deltas",
            },
            404: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def model_deltas(request: Request, workspace_id: str, turn_id: str) -> StreamingResponse:
        if model_delta_hub is None:
            raise HTTPException(status_code=503, detail="MODEL_DELTA_STREAM_UNAVAILABLE")
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=False)
            try:
                exists = connection.execute(
                    "SELECT 1 FROM turns WHERE turn_id = ?", (TurnId(turn_id).value,)
                ).fetchone()
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="TURN_NOT_FOUND") from error
        if exists is None:
            raise HTTPException(status_code=404, detail="TURN_NOT_FOUND")

        async def event_stream() -> AsyncIterator[str]:
            iterator = model_delta_hub.subscribe(workspace_id, turn_id)
            try:
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(anext(iterator), timeout=10)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    data = json.dumps(
                        {
                            "schema_version": "model-response-delta/v1",
                            "authoritative": False,
                            "workspace_id": event.workspace_id,
                            "turn_id": event.turn_id,
                            "provider_attempt_id": event.provider_attempt_id,
                            "sequence": event.sequence,
                            "channel": event.channel,
                            "content": event.content,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield (
                        f"id: {event.provider_attempt_id}:{event.sequence}"
                        f"\nevent: model_delta\ndata: {data}\n\n"
                    )
            finally:
                close_iterator = getattr(iterator, "aclose", None)
                if close_iterator is not None:
                    await close_iterator()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/lineage",
        response_model=EvidenceLineageResponse,
        responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    )
    def lineage(
        workspace_id: str,
        entry_kind: LineageEntryKind,
        entry_id: str,
        entry_subkey: str | None = None,
    ) -> EvidenceLineageResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            database = host.database(typed_workspace_id)
            connection = database.open(writable=False)
            try:
                result = EvidenceLineageService(SqliteEvidenceLineageQuery(connection)).load(
                    LineageSelector(entry_kind, entry_id, entry_subkey)
                )
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="LINEAGE_NOT_FOUND") from error
        journal = result.source_journal
        return EvidenceLineageResponse(
            workspace_id=workspace_id,
            authoritative_revision=result.authoritative_revision.value,
            entry_kind=result.selector.entry_kind.value,
            entry_id=result.selector.entry_id,
            entry_subkey=result.selector.entry_subkey,
            data=EvidenceLineageData(
                source_chain_sha256=result.source_chain_sha256,
                evidence_record_id=result.evidence_record_id,
                result_element_id=result.result_element_id,
                semantic_key=result.semantic_key,
                canonical_binary64_bits=result.canonical_binary64_bits,
                canonical_decimal_text=result.canonical_decimal_text,
                result_source_locator_id=result.result_source_locator_id,
                locator_type=result.locator_type,
                locator_json=result.locator_json,
                result_id=result.result_id,
                result_candidate_id=result.result_candidate_id,
                result_capture_snapshot_id=result.result_capture_snapshot_id,
                stata_run_id=result.stata_run_id,
                research_command_instance_id=result.research_command_instance_id,
                command_text=result.command_text,
                command_sha256=result.command_sha256,
                executable_source_id=result.executable_source_id,
                operation_id=result.operation_id,
                operation_attempt_id=result.operation_attempt_id,
                completion_manifest_id=result.completion_manifest_id,
                data_version_id=result.data_version_id,
                data_artifact_id=result.data_artifact_id,
                input_data_slot_key=result.input_data_slot_key,
                environment_snapshot_id=result.environment_snapshot_id,
                data_state_steps=tuple(
                    DataStateStepResponse(
                        operation_id=step.operation_id,
                        execution_purpose=cast(
                            Literal[
                                "data_load",
                                "data_step",
                                "formal_estimation",
                                "formal_post_estimation",
                            ],
                            step.execution_purpose,
                        ),
                        command_text=step.command_text,
                        command_sha256=step.command_sha256,
                        completion_manifest_id=step.completion_manifest_id,
                        data_state_token=step.data_state_token,
                        session_generation=step.session_generation,
                    )
                    for step in result.data_state_steps
                ),
                source_journal=LineageJournalResponse(
                    journal_entry_id=journal.journal_entry_id,
                    workspace_revision=journal.workspace_revision.value,
                    ordinal=journal.ordinal,
                    event_type=journal.event_type,
                    object_type=journal.object_type,
                    object_id=journal.object_id,
                ),
                formal_result_block_id=result.formal_result_block_id,
                presentation_use_id=result.presentation_use_id,
                rendered_text=result.rendered_text,
                document_revision_id=result.document_revision_id,
                table_cell_evidence_use_id=result.table_cell_evidence_use_id,
                semantic_cell_slot=result.semantic_cell_slot,
            ),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/memories",
        response_model=MemoryIndexResponse,
        responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    )
    def memories(
        workspace_id: str,
        lifecycle: Literal["proposed", "active", "retracted"] | None = None,
        search: str | None = Query(default=None, max_length=500),
        research_path_id: str | None = None,
    ) -> MemoryIndexResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            connection = host.database(typed_workspace_id).open(writable=False)
            try:
                snapshot = SqliteMemoryQuery(connection).index(
                    lifecycle=lifecycle,
                    search=search,
                    research_path_id=research_path_id,
                )
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="MEMORY_NOT_FOUND") from error
        return MemoryIndexResponse(
            workspace_id=workspace_id,
            authoritative_revision=snapshot.authoritative_revision,
            items=tuple(
                MemoryItemResponse(
                    memory_item_id=item.memory_item_id,
                    memory_revision_id=item.memory_revision_id,
                    pointer_revision=item.pointer_revision,
                    lifecycle=cast(Literal["proposed", "active", "retracted"], item.lifecycle),
                    access_tier=cast(Any, item.access_tier),
                    pinned=item.pinned,
                    retention_revision=item.retention_revision,
                    superseded_by_memory_item_id=item.superseded_by_memory_item_id,
                    scope_kind=cast(Literal["workspace", "research_path"], item.scope_kind),
                    scope_object_id=item.scope_object_id,
                    kind=cast(Any, item.kind),
                    title=item.title,
                    content=item.content,
                    origin=cast(Any, item.origin),
                    created_revision=item.created_revision,
                    updated_revision=item.updated_revision,
                    last_used_revision=item.last_used_revision,
                    recall_count=item.recall_count,
                    quality_flags=cast(Any, item.quality_flags),
                    sources=tuple(
                        MemorySourceResponse(
                            object_type=source.object_type,
                            object_id=source.object_id,
                            object_revision=source.object_revision,
                            role=source.role,
                        )
                        for source in item.sources
                    ),
                )
                for item in snapshot.items
            ),
            summaries=tuple(
                MemorySummaryResponse(
                    scope_kind=cast(Literal["workspace", "research_path"], summary[0]),
                    scope_object_id=summary[1],
                    summary_text=summary[2],
                    source_revision=summary[3],
                    projection_revision=summary[4],
                )
                for summary in snapshot.summaries
            ),
        )

    @app.post(
        "/api/v1/workspaces/{workspace_id}/memories",
        response_model=MemoryMutationResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    def create_memory(workspace_id: str, request: MemoryCreateRequest) -> MemoryMutationResponse:
        try:
            typed_workspace_id = WorkspaceId(workspace_id)
            database = host.database(typed_workspace_id)
            connection = database.open(writable=True)
            try:
                result = MemoryService(
                    SqliteMemoryRepository(connection), UuidIdentityGenerator()
                ).create(
                    CreateMemoryCommand(
                        CommandId(request.command_id),
                        MemoryKind(request.kind),
                        request.title,
                        request.content,
                        MemoryOriginKind.EXPLICIT_USER,
                        (
                            MemorySource(
                                "message",
                                request.source_message_id,
                                str(request.source_message_revision),
                                MemorySourceRole.USER_STATEMENT,
                            ),
                        ),
                        MemoryScopeKind.WORKSPACE
                        if request.research_path_id is None
                        else MemoryScopeKind.RESEARCH_PATH,
                        None
                        if request.research_path_id is None
                        else ResearchPathId(request.research_path_id),
                    )
                )
                FilesystemMemoryStore(database.root).synchronize(connection)
            finally:
                connection.close()
        except CommandConflictError as error:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_REUSED") from error
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="MEMORY_MUTATION_REJECTED") from error
        return _memory_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/memories/{memory_item_id}/revise",
        response_model=MemoryMutationResponse,
    )
    def revise_memory(
        workspace_id: str, memory_item_id: str, request: MemoryRevisionRequest
    ) -> MemoryMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = MemoryService(
                    SqliteMemoryRepository(connection), UuidIdentityGenerator()
                ).revise(
                    ReviseMemoryCommand(
                        CommandId(request.command_id),
                        MemoryItemId(memory_item_id),
                        request.expected_pointer_revision,
                        request.title,
                        request.content,
                        MemoryOriginKind.CONFIRMED,
                        (
                            MemorySource(
                                "memory_control_command",
                                request.command_id,
                                str(request.expected_pointer_revision),
                                MemorySourceRole.USER_CONFIRMATION,
                            ),
                        ),
                        MemoryLifecycle(request.lifecycle),
                    )
                )
                FilesystemMemoryStore(database.root).synchronize(connection)
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="MEMORY_MUTATION_REJECTED") from error
        return _memory_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/memories/{memory_item_id}/activate",
        response_model=MemoryMutationResponse,
    )
    def activate_memory(
        workspace_id: str, memory_item_id: str, request: MemoryActivationRequest
    ) -> MemoryMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = MemoryService(
                    SqliteMemoryRepository(connection), UuidIdentityGenerator()
                ).activate(
                    ActivateMemoryCommand(
                        CommandId(request.command_id),
                        MemoryItemId(memory_item_id),
                        MemoryRevisionId(request.memory_revision_id),
                        request.expected_pointer_revision,
                    )
                )
                FilesystemMemoryStore(database.root).synchronize(connection)
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="MEMORY_MUTATION_REJECTED") from error
        return _memory_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/memories/{memory_item_id}/retract",
        response_model=MemoryMutationResponse,
    )
    def retract_memory(
        workspace_id: str, memory_item_id: str, request: MemoryRetractionRequest
    ) -> MemoryMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = MemoryService(
                    SqliteMemoryRepository(connection), UuidIdentityGenerator()
                ).retract(
                    RetractMemoryCommand(
                        CommandId(request.command_id),
                        MemoryItemId(memory_item_id),
                        request.expected_pointer_revision,
                        request.reason,
                    )
                )
                FilesystemMemoryStore(database.root).synchronize(connection)
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="MEMORY_MUTATION_REJECTED") from error
        return _memory_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/memories/{memory_item_id}/access-tier",
        response_model=MemoryRetentionResponse,
    )
    def set_memory_access_tier(
        workspace_id: str,
        memory_item_id: str,
        request: MemoryAccessTierRequest,
    ) -> MemoryRetentionResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = MemoryService(
                    SqliteMemoryRepository(connection), UuidIdentityGenerator()
                ).set_access_tier(
                    SetMemoryAccessTierCommand(
                        CommandId(request.command_id),
                        MemoryItemId(memory_item_id),
                        request.expected_retention_revision,
                        MemoryAccessTier(request.access_tier),
                        request.reason,
                    )
                )
                FilesystemMemoryStore(database.root).synchronize(connection)
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="MEMORY_RETENTION_REJECTED") from error
        return MemoryRetentionResponse(
            memory_item_id=result.memory_item_id.value,
            access_tier=cast(Any, result.access_tier.value),
            retention_revision=result.retention_revision,
            superseded_by_memory_item_id=(
                None
                if result.superseded_by_memory_item_id is None
                else result.superseded_by_memory_item_id.value
            ),
            commit_revision=result.commit_revision.value,
            replayed=result.replayed,
        )

    @app.post(
        "/api/v1/workspaces/{workspace_id}/memories/{memory_item_id}/supersede",
        response_model=MemoryRetentionResponse,
    )
    def supersede_memory(
        workspace_id: str,
        memory_item_id: str,
        request: MemorySupersessionRequest,
    ) -> MemoryRetentionResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = MemoryService(
                    SqliteMemoryRepository(connection), UuidIdentityGenerator()
                ).supersede(
                    SupersedeMemoryCommand(
                        CommandId(request.command_id),
                        MemoryItemId(memory_item_id),
                        MemoryItemId(request.successor_memory_item_id),
                        request.expected_retention_revision,
                        request.reason,
                    )
                )
                FilesystemMemoryStore(database.root).synchronize(connection)
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="MEMORY_SUPERSESSION_REJECTED") from error
        return MemoryRetentionResponse(
            memory_item_id=result.memory_item_id.value,
            access_tier=cast(Any, result.access_tier.value),
            retention_revision=result.retention_revision,
            superseded_by_memory_item_id=(
                None
                if result.superseded_by_memory_item_id is None
                else result.superseded_by_memory_item_id.value
            ),
            commit_revision=result.commit_revision.value,
            replayed=result.replayed,
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/memory-policy",
        response_model=ConversationMemoryPolicyResponse,
    )
    def get_memory_policy(
        workspace_id: str, conversation_id: str
    ) -> ConversationMemoryPolicyResponse:
        try:
            connection = host.database(WorkspaceId(workspace_id)).open(writable=False)
            try:
                policy = SqliteMemoryQuery(connection).conversation_policy(conversation_id)
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
        return ConversationMemoryPolicyResponse(
            conversation_id=policy.conversation_id,
            use_memory=policy.use_memory,
            contribute_memory=policy.contribute_memory,
            policy_revision=policy.policy_revision,
        )

    @app.put(
        "/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/memory-policy",
        response_model=ConversationMemoryPolicyResponse,
    )
    def put_memory_policy(
        workspace_id: str,
        conversation_id: str,
        request: ConversationMemoryPolicyRequest,
    ) -> ConversationMemoryPolicyResponse:
        try:
            connection = host.database(WorkspaceId(workspace_id)).open(writable=True)
            try:
                result = MemoryService(
                    SqliteMemoryRepository(connection), UuidIdentityGenerator()
                ).set_conversation_policy(
                    SetConversationMemoryPolicyCommand(
                        CommandId(request.command_id),
                        ConversationId(conversation_id),
                        request.use_memory,
                        request.contribute_memory,
                        request.expected_policy_revision,
                    )
                )
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="MEMORY_POLICY_REJECTED") from error
        return ConversationMemoryPolicyResponse(
            conversation_id=result.conversation_id.value,
            use_memory=result.use_memory,
            contribute_memory=result.contribute_memory,
            policy_revision=result.policy_revision,
            commit_revision=result.commit_revision.value,
            replayed=result.replayed,
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/knowledge-index",
        response_model=KnowledgeIndexResponse,
    )
    def knowledge_index(workspace_id: str) -> KnowledgeIndexResponse:
        try:
            connection = host.database(WorkspaceId(workspace_id)).open(writable=False)
            try:
                catalog = connection.execute(
                    """
                    SELECT document.relative_path, state.availability,
                           COALESCE(revision.content_sha256, '') AS content_sha256,
                           revision.page_count, parse.parser_profile,
                           parse.canonical_ir_version,
                           json_extract(parse.quality_findings_json,
                                        '$[0].ingestion_policy_revision')
                               AS ingestion_policy_revision,
                           COALESCE((
                               SELECT count(*) FROM knowledge_nodes AS node
                               WHERE node.knowledge_parse_revision_id =
                                     source_state.current_parse_revision_id
                           ), 0) AS structured_node_count,
                           CASE WHEN parse.quality_findings_json IS NULL THEN 0
                                ELSE MAX(json_array_length(parse.quality_findings_json) - 1, 0)
                           END AS enrichment_finding_count
                    FROM knowledge_documents AS document
                    JOIN knowledge_document_states AS state USING (knowledge_document_id)
                    LEFT JOIN knowledge_document_revisions AS revision
                      ON revision.knowledge_document_revision_id = state.current_revision_id
                    LEFT JOIN knowledge_sources AS source
                      ON source.legacy_knowledge_document_id = document.knowledge_document_id
                    LEFT JOIN knowledge_source_states AS source_state
                      ON source_state.knowledge_source_id = source.knowledge_source_id
                    LEFT JOIN knowledge_parse_revisions AS parse
                      ON parse.knowledge_parse_revision_id =
                         source_state.current_parse_revision_id
                    ORDER BY document.relative_path
                    """
                ).fetchall()
                revision = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                    ).fetchone()[0]
                )
                last_policy_row = connection.execute(
                    """
                    SELECT policy_revision FROM knowledge_index_runs
                    ORDER BY created_revision DESC LIMIT 1
                    """
                ).fetchone()
                last_policy = None if last_policy_row is None else str(last_policy_row[0])
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="KNOWLEDGE_INDEX_NOT_FOUND") from error
        return KnowledgeIndexResponse(
            workspace_id=workspace_id,
            authoritative_revision=revision,
            last_index_policy_revision=last_policy,
            documents=tuple(
                KnowledgeDocumentResponse(
                    relative_path=str(row["relative_path"]),
                    availability=cast(Any, row["availability"]),
                    content_sha256=str(row["content_sha256"]),
                    page_count=(None if row["page_count"] is None else int(row["page_count"])),
                    parser_profile=(
                        None if row["parser_profile"] is None else str(row["parser_profile"])
                    ),
                    canonical_ir_version=(
                        None
                        if row["canonical_ir_version"] is None
                        else str(row["canonical_ir_version"])
                    ),
                    ingestion_policy_revision=(
                        None
                        if row["ingestion_policy_revision"] is None
                        else str(row["ingestion_policy_revision"])
                    ),
                    structured_node_count=int(row["structured_node_count"]),
                    enrichment_finding_count=int(row["enrichment_finding_count"]),
                )
                for row in catalog
            ),
        )

    @app.get(
        "/api/v1/workspaces/{workspace_id}/skill-evolution-candidates",
        response_model=SkillEvolutionIndexResponse,
    )
    def skill_evolution_candidates(workspace_id: str) -> SkillEvolutionIndexResponse:
        try:
            connection = host.database(WorkspaceId(workspace_id)).open(writable=False)
            try:
                query = SqliteSkillEvolutionQuery(connection)
                revision, candidates = query.index()
                adoptions = query.adoptions()
                evaluations = SqliteSkillEvaluationQuery(connection).index()
                change_candidates = SqliteSkillChangeQuery(connection).index()
            finally:
                connection.close()
        except (PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=404, detail="SKILL_CANDIDATES_NOT_FOUND") from error
        return SkillEvolutionIndexResponse(
            workspace_id=workspace_id,
            authoritative_revision=revision,
            items=tuple(
                SkillEvolutionCandidateResponse(
                    candidate_id=item.candidate_id,
                    skill_name=item.skill_name,
                    proposed_version=item.proposed_version,
                    description=item.description,
                    instruction_body=item.instruction_body,
                    rationale=item.rationale,
                    policy_revision=item.policy_revision,
                    validation_status=cast(Any, item.validation_status),
                    validation_findings=item.validation_findings,
                    lifecycle=cast(Any, item.lifecycle),
                    pointer_revision=item.pointer_revision,
                    source_memory_item_ids=item.source_memory_item_ids,
                    created_revision=item.created_revision,
                    updated_revision=item.updated_revision,
                    relative_skill_path=item.relative_skill_path,
                )
                for item in candidates
            ),
            adoptions=tuple(
                SkillAdoptionResponse(
                    skill_name=adoption.skill_name,
                    lifecycle=cast(Any, adoption.lifecycle),
                    current_skill_version_id=adoption.current_skill_version_id,
                    pointer_revision=adoption.pointer_revision,
                    updated_revision=adoption.updated_revision,
                    versions=tuple(
                        SkillVersionResponse(
                            skill_version_id=version.skill_version_id,
                            version_label=version.version_label,
                            content_sha256=version.content_sha256,
                            source_candidate_id=version.source_candidate_id,
                            predecessor_skill_version_id=(version.predecessor_skill_version_id),
                            created_revision=version.created_revision,
                            outcome_observation_count=(version.outcome_observation_count),
                        )
                        for version in adoption.versions
                    ),
                )
                for adoption in adoptions
            ),
            evaluations=tuple(_skill_evaluation_response(item) for item in evaluations),
            change_candidates=tuple(
                SkillChangeCandidateResponse(
                    candidate_id=item.candidate_id,
                    source_proposal_id=item.source_proposal_id,
                    change_kind=cast(Any, item.change_kind),
                    skill_name=item.skill_name,
                    base_skill_version_id=item.base_skill_version_id,
                    merge_source_skill_version_id=item.merge_source_skill_version_id,
                    proposed_version=item.proposed_version,
                    description=item.description,
                    instruction_body=item.instruction_body,
                    rationale=item.rationale,
                    validation_status=cast(Any, item.validation_status),
                    validation_findings=item.validation_findings,
                    lifecycle=cast(Any, item.lifecycle),
                    pointer_revision=item.pointer_revision,
                    created_revision=item.created_revision,
                    updated_revision=item.updated_revision,
                    activated_skill_version_id=item.activated_skill_version_id,
                )
                for item in change_candidates
            ),
        )

    @app.post(
        "/api/v1/workspaces/{workspace_id}/skills/{skill_name}/evaluations",
        response_model=SkillEvaluationResponse,
    )
    async def evaluate_workspace_skill(
        workspace_id: str,
        skill_name: str,
        request: SkillEvaluationRequest,
    ) -> SkillEvaluationResponse:
        if model_configuration is None or provider_credentials is None:
            raise HTTPException(status_code=503, detail="SKILL_EVALUATOR_UNAVAILABLE")
        try:
            database = host.database(WorkspaceId(workspace_id))
            outcome = await ProductionSkillEvaluationRunner(
                host,
                model_configuration,
                provider_credentials,
            ).evaluate(
                WorkspaceId(workspace_id),
                EvaluateSkillCommand(
                    CommandId(request.command_id),
                    skill_name,
                    None
                    if request.candidate_skill_version_id is None
                    else SkillVersionId(request.candidate_skill_version_id),
                    None
                    if request.baseline_skill_version_id is None
                    else SkillVersionId(request.baseline_skill_version_id),
                ),
            )
            connection = database.open(writable=False)
            try:
                snapshot = SqliteSkillEvaluationQuery(connection).by_run(outcome.run_id)
            finally:
                connection.close()
        except ProviderDispatchError as error:
            raise HTTPException(
                status_code=503, detail="SKILL_EVALUATOR_PROVIDER_FAILED"
            ) from error
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="SKILL_EVALUATION_REJECTED") from error
        return _skill_evaluation_response(snapshot)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/skill-improvement-proposals/{proposal_id}/materialize",
        response_model=SkillChangeMutationResponse,
    )
    def materialize_skill_change(
        workspace_id: str,
        proposal_id: str,
        request: SkillChangeMaterializeRequest,
    ) -> SkillChangeMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = SkillChangeService(
                    SqliteSkillChangeRepository(connection),
                    WorkspaceSkillPublisher(),
                    UuidIdentityGenerator(),
                    database.root,
                ).materialize(
                    MaterializeSkillChangeCommand(
                        CommandId(request.command_id),
                        SkillImprovementProposalId(proposal_id),
                    )
                )
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(
                status_code=409, detail="SKILL_CHANGE_MATERIALIZATION_REJECTED"
            ) from error
        return _skill_change_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/skill-change-candidates/{candidate_id}/activate",
        response_model=SkillChangeMutationResponse,
    )
    def activate_skill_change(
        workspace_id: str,
        candidate_id: str,
        request: SkillChangeActivateRequest,
    ) -> SkillChangeMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = SkillChangeService(
                    SqliteSkillChangeRepository(connection),
                    WorkspaceSkillPublisher(),
                    UuidIdentityGenerator(),
                    database.root,
                ).activate(
                    ActivateSkillChangeCommand(
                        CommandId(request.command_id),
                        SkillChangeCandidateId(candidate_id),
                        request.expected_pointer_revision,
                    )
                )
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(
                status_code=409, detail="SKILL_CHANGE_ACTIVATION_REJECTED"
            ) from error
        return _skill_change_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/skill-change-candidates/{candidate_id}/reject",
        response_model=SkillChangeMutationResponse,
    )
    def reject_skill_change(
        workspace_id: str,
        candidate_id: str,
        request: SkillChangeRejectRequest,
    ) -> SkillChangeMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = SkillChangeService(
                    SqliteSkillChangeRepository(connection),
                    WorkspaceSkillPublisher(),
                    UuidIdentityGenerator(),
                    database.root,
                ).reject(
                    RejectSkillChangeCommand(
                        CommandId(request.command_id),
                        SkillChangeCandidateId(candidate_id),
                        request.expected_pointer_revision,
                        request.reason,
                    )
                )
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(
                status_code=409, detail="SKILL_CHANGE_REJECTION_REJECTED"
            ) from error
        return _skill_change_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/skill-evolution-candidates/{candidate_id}/activate",
        response_model=SkillEvolutionMutationResponse,
    )
    def activate_skill_evolution_candidate(
        workspace_id: str,
        candidate_id: str,
        request: SkillEvolutionApproveRequest,
    ) -> SkillEvolutionMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = SkillEvolutionService(
                    SqliteSkillEvolutionRepository(connection),
                    WorkspaceSkillPublisher(),
                    UuidIdentityGenerator(),
                    database.root,
                ).approve_and_activate(
                    ApproveSkillEvolutionCommand(
                        CommandId(request.command_id),
                        SkillEvolutionCandidateId(candidate_id),
                        request.expected_pointer_revision,
                    )
                )
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="SKILL_ACTIVATION_REJECTED") from error
        return _skill_evolution_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/skills/{skill_name}/rollback",
        response_model=SkillAdoptionMutationResponse,
    )
    def rollback_workspace_skill(
        workspace_id: str,
        skill_name: str,
        request: SkillRollbackRequest,
    ) -> SkillAdoptionMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = SkillEvolutionService(
                    SqliteSkillEvolutionRepository(connection),
                    WorkspaceSkillPublisher(),
                    UuidIdentityGenerator(),
                    database.root,
                ).rollback(
                    RollbackSkillCommand(
                        CommandId(request.command_id),
                        skill_name,
                        SkillVersionId(request.target_skill_version_id),
                        request.expected_pointer_revision,
                        request.reason,
                    )
                )
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="SKILL_ROLLBACK_REJECTED") from error
        return _skill_adoption_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/skills/{skill_name}/deactivate",
        response_model=SkillAdoptionMutationResponse,
    )
    def deactivate_workspace_skill(
        workspace_id: str,
        skill_name: str,
        request: SkillDeactivateRequest,
    ) -> SkillAdoptionMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = SkillEvolutionService(
                    SqliteSkillEvolutionRepository(connection),
                    WorkspaceSkillPublisher(),
                    UuidIdentityGenerator(),
                    database.root,
                ).deactivate(
                    DeactivateSkillCommand(
                        CommandId(request.command_id),
                        skill_name,
                        request.expected_pointer_revision,
                        request.reason,
                    )
                )
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="SKILL_DEACTIVATION_REJECTED") from error
        return _skill_adoption_mutation_response(result)

    @app.post(
        "/api/v1/workspaces/{workspace_id}/skill-evolution-candidates/{candidate_id}/reject",
        response_model=SkillEvolutionMutationResponse,
    )
    def reject_skill_evolution_candidate(
        workspace_id: str,
        candidate_id: str,
        request: SkillEvolutionRejectRequest,
    ) -> SkillEvolutionMutationResponse:
        try:
            database = host.database(WorkspaceId(workspace_id))
            connection = database.open(writable=True)
            try:
                result = SkillEvolutionService(
                    SqliteSkillEvolutionRepository(connection),
                    WorkspaceSkillPublisher(),
                    UuidIdentityGenerator(),
                    database.root,
                ).reject(
                    RejectSkillEvolutionCommand(
                        CommandId(request.command_id),
                        SkillEvolutionCandidateId(candidate_id),
                        request.expected_pointer_revision,
                        request.reason,
                    )
                )
            finally:
                connection.close()
        except (CommandConflictError, PersistenceBootstrapError, ValueError) as error:
            raise HTTPException(status_code=409, detail="SKILL_REJECTION_REJECTED") from error
        return _skill_evolution_mutation_response(result)

    if static_directory is not None:
        resolved_static = static_directory.resolve(strict=True)
        if not resolved_static.is_dir():
            raise ValueError("static_directory must be a directory")
        # Registered last so explicit /api/v1 routes always win. The browser bundle
        # is a same-origin client, not an alternate application API.
        app.mount("/", StaticFiles(directory=resolved_static, html=True), name="web")

    return app


def _memory_mutation_response(result: Any) -> MemoryMutationResponse:
    return MemoryMutationResponse(
        memory_item_id=result.memory_item_id.value,
        memory_revision_id=result.memory_revision_id.value,
        lifecycle=cast(Literal["proposed", "active", "retracted"], result.lifecycle.value),
        pointer_revision=result.pointer_revision,
        commit_revision=result.commit_revision.value,
        replayed=result.replayed,
    )


def _skill_evolution_mutation_response(result: Any) -> SkillEvolutionMutationResponse:
    return SkillEvolutionMutationResponse(
        candidate_id=result.candidate_id.value,
        lifecycle=cast(Any, result.lifecycle),
        pointer_revision=result.pointer_revision,
        commit_revision=result.commit_revision.value,
        replayed=result.replayed,
        activation_manifest_id=(
            None if result.activation_manifest_id is None else result.activation_manifest_id.value
        ),
        skill_version_id=(
            None if result.skill_version_id is None else result.skill_version_id.value
        ),
        adoption_pointer_revision=result.adoption_pointer_revision,
    )


def _skill_change_mutation_response(result: Any) -> SkillChangeMutationResponse:
    return SkillChangeMutationResponse(
        candidate_id=result.candidate_id.value,
        lifecycle=cast(Any, result.lifecycle),
        pointer_revision=result.pointer_revision,
        skill_name=result.skill_name,
        base_skill_version_id=result.base_skill_version_id.value,
        merge_source_skill_version_id=(
            None
            if result.merge_source_skill_version_id is None
            else result.merge_source_skill_version_id.value
        ),
        proposed_version=result.proposed_version,
        validation_status=cast(Any, result.validation_status),
        validation_findings=result.validation_findings,
        commit_revision=result.commit_revision.value,
        replayed=result.replayed,
        activated_skill_version_id=(
            None
            if result.activated_skill_version_id is None
            else result.activated_skill_version_id.value
        ),
    )


def _skill_adoption_mutation_response(result: Any) -> SkillAdoptionMutationResponse:
    return SkillAdoptionMutationResponse(
        skill_name=result.skill_name,
        lifecycle=cast(Any, result.lifecycle),
        current_skill_version_id=(
            None
            if result.current_skill_version_id is None
            else result.current_skill_version_id.value
        ),
        pointer_revision=result.pointer_revision,
        commit_revision=result.commit_revision.value,
        replayed=result.replayed,
        publication_manifest_id=result.publication_manifest_id.value,
    )


def _skill_evaluation_response(item: SkillEvaluationSnapshot) -> SkillEvaluationResponse:
    arms = item.evidence.get("arms", [])
    candidate: dict[str, Any] = {}
    baseline: dict[str, Any] | None = None
    if isinstance(arms, list):
        for arm in arms:
            if not isinstance(arm, dict):
                continue
            if arm.get("role") == "candidate":
                candidate = arm
            elif arm.get("role") == "baseline":
                baseline = arm
    return SkillEvaluationResponse(
        run_id=item.run_id,
        status=cast(Any, item.status),
        skill_name=item.skill_name,
        candidate_skill_version_id=item.candidate_skill_version_id,
        baseline_skill_version_id=item.baseline_skill_version_id,
        evaluation_kind=cast(Any, item.evaluation_kind),
        policy_revision=item.policy_revision,
        source_start_revision=item.source_start_revision,
        source_end_revision=item.source_end_revision,
        candidate_use_turn_count=int(candidate.get("exact_use_turn_count", 0)),
        candidate_feedback_count=int(candidate.get("explicit_feedback_count", 0)),
        baseline_use_turn_count=(
            None if baseline is None else int(baseline.get("exact_use_turn_count", 0))
        ),
        baseline_feedback_count=(
            None if baseline is None else int(baseline.get("explicit_feedback_count", 0))
        ),
        verdict=cast(Any, item.verdict),
        rationale=item.rationale,
        limitations=item.limitations,
        proposals=tuple(
            SkillImprovementProposalResponse(
                proposal_id=proposal.proposal_id.value,
                kind=proposal.kind,
                title=proposal.title,
                rationale=proposal.rationale,
                suggested_instruction_body=proposal.suggested_instruction_body,
                merge_target_skill_name=proposal.merge_target_skill_name,
            )
            for proposal in item.proposals
        ),
        created_revision=item.created_revision,
        updated_revision=item.updated_revision,
    )
