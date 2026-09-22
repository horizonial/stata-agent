"""Production Turn exposes official research files through traced read-only tools."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.model_configuration import (
    WorkspaceModelConfigurationService,
)
from stata_research_agent.application.model_gateway import ProviderResponse
from stata_research_agent.application.provider_credentials import (
    CredentialUnavailableError,
    ProviderCredentialService,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost
from stata_research_agent.interfaces.production_turn_runner import ProductionTurnRunner
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.global_credentials import (
    GlobalCredentialDatabase,
    SqliteProviderCredentialRepository,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.runtime.workspace_execution import WorkspaceExecutionPool
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

PROJECT = Path(__file__).parents[2]
MCP_ROOT = Path.home() / "stata-mcp"
MCP_PYTHON = MCP_ROOT / ".venv-uv" / "Scripts" / "python.exe"
STATA_HOME = Path(r"C:\Program Files\Stata18")
AUTO_DATA = STATA_HOME / "auto.dta"
WORKER_PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def write(self, target_name: str, secret: str) -> None:
        self.values[target_name] = secret

    def read(self, target_name: str) -> str:
        try:
            return self.values[target_name]
        except KeyError as error:
            raise CredentialUnavailableError("credential unavailable") from error

    def contains(self, target_name: str) -> bool:
        return target_name in self.values

    def delete(self, target_name: str) -> None:
        self.values.pop(target_name, None)


class _WorkspaceFileModel:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, credential
        if "independent research checkpoint evaluator" in request_json:
            return ProviderResponse(
                {
                    "text": "",
                    "tool_calls": [],
                    "evaluation": {"verdict": "pass", "findings": ["completion_ready"]},
                }
            )
        self.calls += 1
        if self.calls == 1:
            assert "workspace.list_files" in request_json
            assert "workspace.read_text" in request_json
            return ProviderResponse(
                {
                    "text": "Find the supplied replication program.",
                    "tool_calls": [
                        {
                            "name": "workspace.list_files",
                            "arguments": {"relative_directory": ".", "recursive": True},
                        }
                    ],
                }
            )
        if self.calls == 2:
            assert "official-replication.do" in request_json
            return ProviderResponse(
                {
                    "text": "Read the official Stata program before proposing execution.",
                    "tool_calls": [
                        {
                            "name": "workspace.read_text",
                            "arguments": {
                                "relative_path": "official-replication.do",
                                "start_line": 1,
                                "max_lines": 50,
                            },
                        }
                    ],
                }
            )
        if self.calls == 3:
            assert "regress price mpg weight" in request_json
            return ProviderResponse(
                {
                    "text": "Run the inspected official program in the isolated Stata scope.",
                    "tool_calls": [
                        {
                            "name": "stata.execute",
                            "arguments": {
                                "dataset_relative_path": "auto.dta",
                                "workspace_file_inputs": ["official-replication.do"],
                                "code": 'do "official-replication.do"',
                                "reset_data": True,
                                "execution_role": "formal_result_candidate",
                                "plan_node_key": "formal_result.candidate.1",
                            },
                        }
                    ],
                }
            )
        if self.calls == 4:
            assert "term.mpg.coefficient" in request_json
            operation_ids = re.findall(r"op_[0-9a-f-]{36}", request_json)
            assert operation_ids
            return ProviderResponse(
                {
                    "text": "Adopt the exact Stata results produced by the official program.",
                    "tool_calls": [
                        {
                            "name": "research.promote_stata_result",
                            "arguments": {
                                "operation_id": operation_ids[-1],
                                "selected_source_keys": [
                                    "term.mpg.coefficient",
                                    "term.mpg.se",
                                    "term.weight.coefficient",
                                    "term.weight.se",
                                    "scalar.N",
                                    "scalar.r2",
                                ],
                                "result_slot_key": "replication.primary",
                                "plan_summary": "Official supplied Stata replication model.",
                            },
                        }
                    ],
                }
            )
        assert "result_id" in request_json
        return ProviderResponse(
            {
                "text": "The official replication program is readable and ready for review.",
                "tool_calls": [],
                "completion": {
                    "disposition": "succeed",
                    "summary": "Inspected the supplied official Stata program.",
                },
            }
        )


def test_production_turn_lists_and_reads_workspace_do_file(tmp_path: Path) -> None:
    if not all(path.is_file() for path in (MCP_PYTHON, AUTO_DATA, WORKER_PYTHON)):
        pytest.skip("certified local Agent runtime is unavailable")
    host = WorkspaceHost(tmp_path / "workspaces")
    workspace_id = WorkspaceId("ws_workspace_file_tools")
    database = host.database(workspace_id)
    database.create()
    shutil.copy2(AUTO_DATA, database.root / "auto.dta")
    (database.root / "official-replication.do").write_text(
        "version 18\nregress price mpg weight\n",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_file_workspace"), workspace_id))
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_file_message"),
            "Inspect and execute the supplied official replication program, then summarize it.",
        )
    )
    connection.close()

    global_connection = GlobalCredentialDatabase(tmp_path / "control.sqlite3").open()
    repository = SqliteProviderCredentialRepository(global_connection)
    credentials = ProviderCredentialService(repository, _MemorySecretStore())
    profile = credentials.create_profile(
        provider_kind="deepseek",
        endpoint="https://provider.invalid/chat/completions",
        secret="sk-workspace-file-test",
    )
    models = WorkspaceModelConfigurationService(repository)
    models.select(
        workspace_id=workspace_id.value,
        provider_profile_id=profile.provider_profile_id,
        model_name="deterministic-workspace-file-reader",
    )

    pool = WorkspaceExecutionPool(
        lambda working_directory: StdioStataRuntime(
            python_executable=MCP_PYTHON,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=working_directory,
            stata_home=STATA_HOME,
        )
    )
    model = _WorkspaceFileModel()
    runner = ProductionTurnRunner(
        host,
        pool,
        models,
        credentials,
        WORKER_PYTHON,
        transport=model,
    )

    async def scenario() -> None:
        try:
            await runner.run_turn(workspace_id, turn.turn_id)
        finally:
            await pool.close()

    asyncio.run(scenario())
    verified = database.open(writable=False)
    try:
        status = verified.execute(
            "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
        ).fetchone()[0]
        tools = tuple(
            row[0]
            for row in verified.execute(
                "SELECT requested_tool_name FROM tool_calls ORDER BY call_ordinal"
            ).fetchall()
        )
        result_payloads = tuple(
            json.loads(str(row[0]))
            for row in verified.execute(
                "SELECT structured_payload_json FROM canonical_tool_results ORDER BY rowid"
            ).fetchall()
        )
        assert status == "succeeded"
        assert tools == (
            "workspace.list_files",
            "workspace.read_text",
            "stata.execute",
            "research.promote_stata_result",
        )
        assert any(
            payload.get("content") == "version 18\nregress price mpg weight"
            for payload in result_payloads
        )
        stata_payload = next(
            payload
            for payload in result_payloads
            if payload.get("execution_status") == "succeeded"
            and isinstance(payload.get("structured"), dict)
        )
        assert stata_payload["structured"]["N"] == 74.0
        arguments = verified.execute(
            """
            SELECT snapshot.arguments_json
            FROM tool_calls AS call
            JOIN canonical_tool_argument_snapshots AS snapshot
              ON snapshot.canonical_arguments_snapshot_id =
                 call.canonical_arguments_snapshot_id
            WHERE call.requested_tool_name = 'stata.execute'
            """
        ).fetchone()[0]
        assert json.loads(str(arguments))["workspace_file_inputs"] == [
            "official-replication.do"
        ]
        execution_copy = next(
            database.root.glob(".runtime/scopes/*/official-replication.do")
        )
        assert execution_copy.read_text(encoding="utf-8") == (
            "version 18\nregress price mpg weight\n"
        )
        assert model.calls == 5
    finally:
        verified.close()
        global_connection.close()
