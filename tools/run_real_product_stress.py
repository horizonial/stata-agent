"""Repeat real Stata work across isolated Workspaces and emit a machine-readable report."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stata_research_agent.application.control import CreateWorkspaceCommand, SubmitMessageCommand
from stata_research_agent.application.stata_operation import (
    ArtifactOutputExpectation,
    ExecuteStataCommand,
)
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.completion_manifest_store import (
    FilesystemCompletionManifestStore,
)
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.execution_scope_query import SqliteExecutionScopeAuthority
from stata_research_agent.persistence.stata_operation_store import SqliteStataOperationRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.runtime.workspace_execution import WorkspaceExecutionPool
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_STATA_HOME = Path(r"C:\Program Files\Stata18")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mcp_python() -> Path:
    root = Path.home() / "stata-mcp"
    for relative in (".venv/Scripts/python.exe", ".venv-uv/Scripts/python.exe"):
        candidate = root / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("certified Stata MCP Python runtime is unavailable")


@dataclass(slots=True)
class StressWorkspace:
    name: str
    database: WorkspaceDatabase
    connection: Any
    runtime: StdioStataRuntime
    service: StataOperationService
    turn_id: Any


def _create_workspace(
    root: Path,
    name: str,
    pool: WorkspaceExecutionPool,
    auto_data: Path,
) -> StressWorkspace:
    workspace_id = WorkspaceId(name)
    database = WorkspaceDatabase(root / name, workspace_id)
    database.create()
    connection = database.open(writable=True)
    control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    control.create_workspace(CreateWorkspaceCommand(CommandId(f"cmd_create_{name}"), workspace_id))
    turn = control.submit_message(
        SubmitMessageCommand(CommandId(f"cmd_turn_{name}"), "Real Stata stability stress")
    )
    runtime = pool.runtime_for_active_write_turn(
        SqliteExecutionScopeAuthority(database), turn.turn_id
    )
    runtime.working_directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(auto_data, runtime.working_directory / "auto.dta")
    service = StataOperationService(
        SqliteStataOperationRepository(connection),
        runtime,
        UuidIdentityGenerator(),
        FilesystemCompletionManifestStore(
            database.root, execution_root=runtime.working_directory
        ),
        FilesystemManagedArtifactStore(
            database.root, execution_root=runtime.working_directory
        ),
    )
    return StressWorkspace(name, database, connection, runtime, service, turn.turn_id)


def _command(cycle: int, dependent: str, predictor: str) -> str:
    return "\n".join(
        (
            'use "auto.dta", clear',
            f"regress {dependent} {predictor}",
            'esttab using "<ATTEMPT_STAGING>/tables/model.rtf", '
            f'replace rtf title("Stress cycle {cycle}")',
            'save "<ATTEMPT_STAGING>/data/analysis.dta", replace',
            f"scatter {dependent} {predictor}, name(stress_graph, replace)",
            'graph export "<ATTEMPT_STAGING>/figures/scatter.png", replace width(800)',
        )
    )


OUTPUTS = (
    ArtifactOutputExpectation("table.model", "tables/model.rtf", "table", "application/rtf"),
    ArtifactOutputExpectation(
        "data.analysis", "data/analysis.dta", "dataset", "application/x-stata-dta"
    ),
    ArtifactOutputExpectation(
        "figure.scatter", "figures/scatter.png", "diagnostic", "image/png"
    ),
)


def _verify_manifest(workspace: StressWorkspace, manifest_id: str) -> dict[str, Any]:
    manifest = workspace.connection.execute(
        """
        SELECT execution_status, structured_result_json, receipt_json
        FROM completion_manifests WHERE completion_manifest_id = ?
        """,
        (manifest_id,),
    ).fetchone()
    if manifest is None or manifest["execution_status"] != "succeeded":
        raise RuntimeError(f"{workspace.name}: completion manifest is not successful")
    structured = json.loads(str(manifest["structured_result_json"]))
    if structured.get("N") != 74.0:
        raise RuntimeError(f"{workspace.name}: regression sample size drifted")
    receipt = json.loads(str(manifest["receipt_json"]))
    rows = workspace.connection.execute(
        """
        SELECT artifact.artifact_id, artifact.content_hash, artifact.size_bytes,
               location.managed_handle
        FROM completion_manifest_artifacts AS member
        JOIN artifact_candidate_sources AS source
          ON source.artifact_candidate_id = member.artifact_candidate_id
        JOIN artifacts AS artifact ON artifact.artifact_id = source.artifact_id
        JOIN artifact_locations AS location ON location.artifact_id = artifact.artifact_id
        WHERE member.completion_manifest_id = ?
        ORDER BY member.output_slot
        """,
        (manifest_id,),
    ).fetchall()
    if len(rows) != 3:
        raise RuntimeError(f"{workspace.name}: expected three captured Artifacts")
    verified = []
    for row in rows:
        managed = Path(str(row["managed_handle"]))
        path = managed if managed.is_absolute() else workspace.database.root / managed
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(workspace.database.root.resolve()):
            raise RuntimeError(f"{workspace.name}: Artifact escaped or is missing")
        if resolved.stat().st_size != int(row["size_bytes"]):
            raise RuntimeError(f"{workspace.name}: Artifact size does not match the ledger")
        if _sha256(resolved) != str(row["content_hash"]):
            raise RuntimeError(f"{workspace.name}: Artifact hash does not match the ledger")
        verified.append(str(row["artifact_id"]))
    proof = receipt.get("supervision_proof", {})
    return {
        "manifest_id": manifest_id,
        "artifact_ids": verified,
        "worker_pid": proof.get("worker_pid"),
        "session_id": receipt.get("session_id"),
    }


async def _run_stata_stress(
    output_root: Path,
    *,
    cycles: int,
    workspace_count: int,
    timeout_seconds: float,
    stata_home: Path,
    candidate: Path | None,
) -> dict[str, Any]:
    if cycles < 1 or workspace_count < 2:
        raise ValueError("stress requires at least one cycle and two Workspaces")
    auto_data = stata_home / "auto.dta"
    if not auto_data.is_file():
        raise FileNotFoundError("Stata auto.dta fixture is unavailable")
    if candidate is None:
        python_executable = _mcp_python()
        mcp_source_root: Path | None = Path.home() / "stata-mcp" / "src"
        candidate_manifest = None
    else:
        candidate_root = candidate.resolve()
        python_executable = candidate_root / "payload" / "executor" / "python.exe"
        mcp_source_root = None
        receipt = json.loads(
            (candidate_root / "evidence" / "candidate-build-receipt.json").read_text(
                encoding="utf-8"
            )
        )
        candidate_manifest = receipt["payload_manifest_sha256"]
    if not python_executable.is_file():
        raise FileNotFoundError("Stata MCP execution runtime is unavailable")

    def factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=python_executable,
            mcp_source_root=mcp_source_root,
            working_directory=working_directory,
            stata_home=stata_home,
        )

    pool = WorkspaceExecutionPool(factory)
    workspaces: list[StressWorkspace] = []
    started = time.perf_counter()
    try:
        for index in range(workspace_count):
            workspaces.append(
                _create_workspace(
                    output_root / "workspaces",
                    f"ws_stress_{index + 1}",
                    pool,
                    auto_data,
                )
            )
        await asyncio.gather(*(workspace.runtime.prepare() for workspace in workspaces))
        identities = [
            {
                "workspace": workspace.name,
                "session_id": workspace.runtime.session_id,
                "working_directory": str(workspace.runtime.working_directory),
            }
            for workspace in workspaces
        ]
        if len({item["session_id"] for item in identities}) != workspace_count:
            raise RuntimeError("Workspace Stata session identities are not isolated")
        if len({item["working_directory"] for item in identities}) != workspace_count:
            raise RuntimeError("Workspace working directories are not isolated")

        verified_runs: list[dict[str, Any]] = []
        for cycle in range(1, cycles + 1):
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        workspace.service.execute(
                            ExecuteStataCommand(
                                CommandId(f"cmd_{workspace.name}_{cycle}"),
                                workspace.turn_id,
                                workspace.runtime.session_id,
                                _command(
                                    cycle,
                                    "price" if index % 2 == 0 else "mpg",
                                    "weight" if index % 2 == 0 else "length",
                                ),
                                timeout_seconds,
                                expected_outputs=OUTPUTS,
                            )
                        )
                        for index, workspace in enumerate(workspaces)
                    )
                ),
                timeout=timeout_seconds + 30,
            )
            for workspace, outcome in zip(workspaces, outcomes, strict=True):
                if outcome.status != "completed" or outcome.manifest_id is None:
                    raise RuntimeError(f"{workspace.name}: cycle {cycle} did not complete")
                item = _verify_manifest(workspace, outcome.manifest_id.value)
                item.update({"workspace": workspace.name, "cycle": cycle})
                verified_runs.append(item)

        worker_pids = {item["worker_pid"] for item in verified_runs}
        if None in worker_pids or len(worker_pids) != workspace_count:
            raise RuntimeError("Workspace MCP worker identities are not isolated")
        for workspace in workspaces:
            active = int(
                workspace.connection.execute(
                    """
                    SELECT COUNT(*) FROM operations
                    WHERE status IN ('proposed', 'authorized', 'admitted', 'handoff_committed')
                    """
                ).fetchone()[0]
            )
            if active:
                raise RuntimeError(f"{workspace.name}: active Operations remain after stress")
            foreign_files = [
                path
                for other in workspaces
                if other is not workspace
                for path in workspace.runtime.working_directory.rglob(f"*{other.name}*")
            ]
            if foreign_files:
                raise RuntimeError(f"{workspace.name}: cross-Workspace file contamination")
        return {
            "schema_version": "stata-research-agent.real-product-stress/v1",
            "started_at": datetime.now(UTC).isoformat(),
            "environment": {
                "os": platform.platform(),
                "stata_home": str(stata_home),
                "candidate_manifest_sha256": candidate_manifest,
            },
            "fixture": {"path": str(auto_data), "sha256": _sha256(auto_data)},
            "configuration": {
                "cycles": cycles,
                "workspace_count": workspace_count,
                "timeout_seconds": timeout_seconds,
            },
            "workspace_isolation": {"passed": True, "identities": identities},
            "artifact_integrity": {
                "passed": True,
                "verified_manifest_count": len(verified_runs),
                "verified_artifact_count": len(verified_runs) * 3,
            },
            "process_cleanup": {"passed": True, "active_operation_count": 0},
            "duration_seconds": round(time.perf_counter() - started, 3),
            "overall": "passed",
        }
    finally:
        await pool.close()
        for workspace in workspaces:
            workspace.connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--workspaces", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument("--stata-home", type=Path, default=DEFAULT_STATA_HOME)
    parser.add_argument("--candidate", type=Path)
    arguments = parser.parse_args()
    output = arguments.output_root.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "stress-report.json"
    try:
        report = asyncio.run(
            _run_stata_stress(
                output,
                cycles=arguments.cycles,
                workspace_count=arguments.workspaces,
                timeout_seconds=arguments.timeout_seconds,
                stata_home=arguments.stata_home.resolve(),
                candidate=arguments.candidate,
            )
        )
    except Exception as error:
        report = {
            "schema_version": "stata-research-agent.real-product-stress/v1",
            "overall": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
