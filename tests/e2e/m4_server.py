"""Deterministic M4 browser host using the production Command/Scheduler/Stata path."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import uvicorn

from stata_research_agent.application.evaluation import (
    ContractObligationCandidate,
    NormalizeCompletionContractCommand,
    ObligationProvenance,
    RequirementLevel,
)
from stata_research_agent.application.evaluation_service import RuntimeEvaluationService
from stata_research_agent.application.model_gateway import ContextItemCandidate, ProviderResponse
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.stata_tool_executor import StataToolExecutor
from stata_research_agent.application.tool_broker import (
    RegisterToolContractCommand,
    ResourceClaimTemplate,
    ToolContractDefinition,
)
from stata_research_agent.application.tool_broker_service import ToolBrokerService
from stata_research_agent.application.turn_driver import TurnDriverConfig, TurnDriverModelConfig
from stata_research_agent.artifacts.completion_manifest_store import (
    FilesystemCompletionManifestStore,
)
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.identifiers import CommandId, TurnId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.persistence.evaluation_store import SqliteEvaluationRepository
from stata_research_agent.persistence.execution_scope_query import SqliteExecutionScopeAuthority
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.persistence.stata_operation_store import SqliteStataOperationRepository
from stata_research_agent.persistence.tool_broker_store import SqliteToolBrokerRepository
from stata_research_agent.runtime.agent_turn_driver import AgentTurnDriver
from stata_research_agent.runtime.application_runtime import WorkspaceTurnSupervisor
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.runtime.workspace_execution import WorkspaceExecutionPool
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

PROJECT_ROOT = Path(__file__).parents[2]
MCP_ROOT = Path.home() / "stata-mcp"
MCP_PYTHON = MCP_ROOT / ".venv-uv" / "Scripts" / "python.exe"
STATA_HOME = Path(r"C:\Program Files\Stata18")
AUTO_DATA = STATA_HOME / "auto.dta"
WORKER_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


class Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id,
            "credentialversion_test",
            endpoint,
            "m4-browser-deterministic-credential",
        )


class BrowserResearchModel:
    def __init__(self, *, session_id: str, code: str) -> None:
        self._session_id = session_id
        self._code = code
        self._calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        self._calls += 1
        if self._calls == 1:
            return ProviderResponse(
                {
                    "text": "Run the requested traceable Stata check.",
                    "plan": {
                        "summary": "Execute Stata then verify completion",
                        "structured_plan": {"nodes": ["stata", "finish"]},
                    },
                    "tool_calls": [
                        {
                            "name": "stata.run",
                            "arguments": {
                                "session_id": self._session_id,
                                "code": self._code,
                                "timeout_seconds": 30,
                            },
                        }
                    ],
                }
            )
        if "completion_manifest_id" not in request_json:
            raise AssertionError("Stata Completion Manifest was not returned to the Agent")
        return ProviderResponse(
            {
                "text": "The traceable Stata check completed.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "The required Stata operation completed.",
                },
            }
        )


def stata_contract() -> ToolContractDefinition:
    return ToolContractDefinition(
        "stata.run",
        "1.0.0",
        "Run Stata",
        "stata.execute",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "session_id": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["code", "session_id"],
            "additionalProperties": False,
        },
        {"type": "object"},
        "local_runtime",
        "workspace_write",
        "exclusive_scope",
        "non_replayable",
        "never",
        "not_interruptible",
        300,
        3600,
        1_000_000,
        (ResourceClaimTemplate("stata-session:{session_id}", "exclusive", "session_id"),),
    )


class BrowserStataTurnRunner:
    def __init__(self, host: WorkspaceHost, pool: WorkspaceExecutionPool) -> None:
        self._host = host
        self._pool = pool

    async def run_turn(self, workspace_id, turn_id: TurnId) -> None:
        database = self._host.database(workspace_id)
        connection = database.open(writable=True)
        identities = UuidIdentityGenerator()
        try:
            row = connection.execute(
                """
                SELECT turn.turn_revision, turn.research_path_id, turn.execution_scope_id,
                       message.message_id, message.content
                FROM turns AS turn
                JOIN messages AS message ON message.message_id = turn.triggering_message_id
                WHERE turn.turn_id = ?
                """,
                (turn_id.value,),
            ).fetchone()
            if row is None:
                raise ValueError("Turn disappeared before browser execution")
            content = str(row["content"])
            runtime = self._pool.runtime_for_active_write_turn(
                SqliteExecutionScopeAuthority(database), turn_id
            )
            runtime.working_directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(AUTO_DATA, runtime.working_directory / "auto.dta")
            if "Workspace B" in content:
                specification = "regress price length"
            elif "queued" in content.lower():
                specification = "regress turn trunk"
            else:
                specification = "regress mpg weight"
            delay = 0 if "queued" in content.lower() else 6000
            code = (
                f"sleep {delay}\n"
                'use "auto.dta", clear\n'
                f"{specification}\n"
                f'display "@@WORKSPACE {database.workspace_id.value}"'
            )

            evaluator = RuntimeEvaluationService(SqliteEvaluationRepository(connection), identities)
            normalized = evaluator.normalize_contract(
                NormalizeCompletionContractCommand(
                    identities.new(CommandId),
                    turn_id,
                    int(row["turn_revision"]),
                    "Run one traceable Stata operation for the browser M4 profile.",
                    (
                        ContractObligationCandidate(
                            "stata.browser.profile",
                            "Complete the browser-triggered Stata operation",
                            ObligationProvenance.USER_EXPLICIT,
                            "message",
                            str(row["message_id"]),
                            RequirementLevel.REQUIRED,
                            "A successful Completion Manifest exists.",
                        ),
                    ),
                )
            )
            broker_repository = SqliteToolBrokerRepository(connection)
            broker = ToolBrokerService(broker_repository, identities)
            contract = stata_contract()
            if broker_repository.load_contract(contract.tool_name) is None:
                broker.register_contract(
                    RegisterToolContractCommand(identities.new(CommandId), turn_id, contract)
                )
            operation_service = StataOperationService(
                SqliteStataOperationRepository(connection),
                runtime,
                identities,
                FilesystemCompletionManifestStore(
                    database.root, execution_root=runtime.working_directory
                ),
                FilesystemManagedArtifactStore(
                    database.root, execution_root=runtime.working_directory
                ),
            )
            driver = AgentTurnDriver(
                ModelGatewayService(
                    SqliteModelGatewayRepository(connection),
                    identities,
                    Credential(),
                    BrowserResearchModel(session_id=runtime.session_id, code=code),
                ),
                broker,
                evaluator,
                StataToolExecutor(
                    operation_service,
                    identities,
                    authoritative_session_id=runtime.session_id,
                ),
                identities,
                WORKER_PYTHON,
            )
            outcome = await driver.run(
                TurnDriverConfig(
                    turn_id,
                    normalized.turn_revision,
                    database.workspace_id.value,
                    str(row["execution_scope_id"]),
                    str(row["research_path_id"]),
                    TurnDriverModelConfig(
                        "system-m4-browser-v1",
                        "You are the deterministic M4 browser verification agent.",
                        "research-main",
                        "skill-m4-browser-v1",
                        "Use the registered Stata tool and finish only after its receipt.",
                        "catalog-m4-browser-v1",
                        ({"name": "stata.run", "input_schema": contract.input_schema},),
                        "permission-m4-browser-v1",
                        {"stata_execute": True},
                        "model-policy-m4-browser-v1",
                        "test-provider",
                        "test",
                        "deterministic-m4-browser",
                        "https://provider.invalid/responses",
                        "credential://m4-browser",
                        {},
                    ),
                    (
                        ContextItemCandidate(
                            "user_message",
                            "message",
                            str(row["message_id"]),
                            str(normalized.turn_revision),
                            "remote_allowed",
                            content,
                        ),
                    ),
                    {
                        "resource_identities": {
                            f"stata-session:{runtime.session_id}": runtime.session_id
                        }
                    },
                    ("workspace_write",),
                    {"stata.run": normalized.obligation_ids},
                    4,
                )
            )
            if outcome.status not in {"succeeded", "waiting"}:
                raise RuntimeError(f"browser Turn did not reach a stable state: {outcome.status}")
        finally:
            connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    host = WorkspaceHost(args.workspace_root)
    for existing_workspace_id in host.workspace_ids():
        # This is a disposable pre-release E2E fixture upgrade into the first schema that
        # contains the stable staged-migration control tables. Production Workspace open never
        # auto-migrates and later upgrades must use WorkspaceMigrationCoordinator.
        database = host.database(existing_workspace_id)
        migrated = database.connection_contract.connect(database.database_path, writable=True)
        try:
            database.migration_runner.migrate(migrated, existing_workspace_id)
            database.migration_runner.validate(migrated, existing_workspace_id)
        finally:
            migrated.close()

    def runtime_factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=working_directory,
            stata_home=STATA_HOME,
        )

    pool = WorkspaceExecutionPool(runtime_factory)
    supervisor = WorkspaceTurnSupervisor(host, BrowserStataTurnRunner(host, pool))
    app = create_app(
        host,
        static_directory=PROJECT_ROOT / "web" / "dist",
        turn_dispatcher=supervisor,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
