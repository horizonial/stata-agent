"""Run the real DeepSeek + Stata + Word product loop without exposing credentials."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from stata_research_agent.application.control import CreateWorkspaceCommand, SubmitMessageCommand
from stata_research_agent.application.model_configuration import (
    WorkspaceModelConfigurationService,
)
from stata_research_agent.application.provider_credentials import ProviderCredentialService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost
from stata_research_agent.interfaces.production_turn_runner import ProductionTurnRunner
from stata_research_agent.interfaces.sentence_transformer_embedding import (
    SentenceTransformerEmbeddingGateway,
)
from stata_research_agent.interfaces.windows_credential_store import WindowsCredentialStore
from stata_research_agent.interfaces.workspace_skills import FilesystemMainSkillCatalog
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.global_credentials import (
    GlobalCredentialDatabase,
    SqliteProviderCredentialRepository,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.runtime.workspace_execution import WorkspaceExecutionPool
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

PROJECT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secret_occurrences(root: Path, secret: str) -> list[str]:
    needle = secret.encode("utf-8")
    findings: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        previous = b""
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                combined = previous + chunk
                if needle in combined:
                    findings.append(str(path.relative_to(root)))
                    break
                previous = combined[-max(0, len(needle) - 1) :]
    return findings


def _deepseek_secret() -> str:
    secret = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not secret:
        raise RuntimeError("DEEPSEEK_API_KEY is required for the live-model evaluation")
    return secret


async def _run(
    output_root: Path,
    *,
    timeout_seconds: float,
    max_input_tokens: int | None,
    max_output_tokens: int | None,
    candidate: Path | None,
    prompt: str,
    goal_mode: str,
    literature_source: Path | None,
    workspace_input_source: Path | None,
    embedding_cache: Path | None,
) -> dict[str, object]:
    stata_home = Path(r"C:\Program Files\Stata18")
    mcp_root = Path.home() / "stata-mcp"
    if candidate is not None:
        mcp_python = candidate.resolve() / "payload" / "executor" / "python.exe"
        mcp_source_root: Path | None = None
    else:
        candidates = (
            mcp_root / ".venv-uv" / "Scripts" / "python.exe",
            mcp_root / ".venv" / "Scripts" / "python.exe",
        )
        mcp_python = next((path for path in candidates if path.is_file()), candidates[0])
        mcp_source_root = mcp_root / "src"
    worker_python = PROJECT / ".venv" / "Scripts" / "python.exe"
    required = (stata_home / "auto.dta", mcp_python, worker_python)
    if not all(path.is_file() for path in required):
        raise RuntimeError("the certified local Stata/MCP runtime is incomplete")

    host = WorkspaceHost(output_root / "workspaces")
    workspace_id = WorkspaceId("ws_live_product")
    database = host.database(workspace_id)
    database.create()
    shutil.copy2(stata_home / "auto.dta", database.root / "auto.dta")
    if workspace_input_source is not None:
        shutil.copytree(
            workspace_input_source.resolve(), database.root / "research-inputs"
        )
    if literature_source is not None:
        shutil.copytree(literature_source.resolve(), database.root / "literature")
    connection = database.open(writable=True)
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
        control.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_live_product_workspace"), workspace_id)
        )
        turn = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_live_product_research"),
                prompt,
                goal_mode=goal_mode,
            )
        )
    finally:
        connection.close()

    credential_connection = GlobalCredentialDatabase(output_root / "control.sqlite3").open()
    repository = SqliteProviderCredentialRepository(credential_connection)
    credentials = ProviderCredentialService(repository, WindowsCredentialStore())
    secret = _deepseek_secret()
    profile = None
    pool: WorkspaceExecutionPool | None = None

    def runtime_factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=mcp_python,
            mcp_source_root=mcp_source_root,
            working_directory=working_directory,
            stata_home=stata_home,
        )

    try:
        profile = credentials.create_profile(
            provider_kind="deepseek",
            endpoint="https://api.deepseek.com/chat/completions",
            account_label="live product e2e",
            secret=secret,
        )
        models = WorkspaceModelConfigurationService(repository)
        models.select(
            workspace_id=workspace_id.value,
            provider_profile_id=profile.provider_profile_id,
            model_name="deepseek-chat",
            reasoning_effort="medium",
            permission_mode="workspace_only",
        )
        pool = WorkspaceExecutionPool(runtime_factory)
        runner = ProductionTurnRunner(
            host,
            pool,
            models,
            credentials,
            worker_python,
            main_skills=FilesystemMainSkillCatalog(PROJECT / "skills"),
            embedding_gateway=(
                None
                if embedding_cache is None
                else SentenceTransformerEmbeddingGateway(
                    cache_folder=embedding_cache.resolve()
                )
            ),
        )
        await asyncio.wait_for(
            runner.run_turn(workspace_id, turn.turn_id), timeout=timeout_seconds
        )
    finally:
        if pool is not None:
            await pool.close()
        if profile is not None:
            credentials.delete_profile(profile.provider_profile_id)
        credential_connection.close()

    verified = database.open(writable=False)
    try:
        turn_row = verified.execute(
            "SELECT status, turn_revision FROM turns WHERE turn_id = ?",
            (turn.turn_id.value,),
        ).fetchone()
        waiting = verified.execute(
            "SELECT wait_reason, prompt FROM waiting_requests "
            "WHERE turn_id = ? AND status = 'open'",
            (turn.turn_id.value,),
        ).fetchone()
        counts = {
            "steps": int(verified.execute("SELECT COUNT(*) FROM steps").fetchone()[0]),
            "stata_runs": int(verified.execute("SELECT COUNT(*) FROM stata_runs").fetchone()[0]),
            "results": int(verified.execute("SELECT COUNT(*) FROM results").fetchone()[0]),
            "evidence": int(
                verified.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0]
            ),
            "documents": int(
                verified.execute("SELECT COUNT(*) FROM document_revisions").fetchone()[0]
            ),
            "retrieval_sessions": int(
                verified.execute(
                    "SELECT COUNT(*) FROM knowledge_retrieval_sessions"
                ).fetchone()[0]
            ),
            "retrieval_hops": int(
                verified.execute("SELECT COUNT(*) FROM knowledge_retrieval_hops").fetchone()[0]
            ),
            "retrieval_node_context_uses": int(
                verified.execute(
                    "SELECT COUNT(*) FROM knowledge_node_context_uses "
                    "WHERE retrieval_session_id IS NOT NULL"
                ).fetchone()[0]
            ),
        }
        open_retrieval_sessions = int(
            verified.execute(
                "SELECT COUNT(*) FROM knowledge_retrieval_session_states "
                "WHERE status = 'open'"
            ).fetchone()[0]
        )
        retrieval_trace = tuple(
            dict(row)
            for row in verified.execute(
                """
                SELECT session.knowledge_retrieval_session_id AS retrieval_session_id,
                       hop.hop_ordinal, hop.public_subquestion, hop.query,
                       hop.stop_reason,
                       (SELECT COUNT(*) FROM knowledge_retrieval_selections AS selection
                        WHERE selection.knowledge_retrieval_hop_id =
                              hop.knowledge_retrieval_hop_id) AS selected_nodes
                FROM knowledge_retrieval_sessions AS session
                JOIN knowledge_retrieval_hops AS hop
                  USING (knowledge_retrieval_session_id)
                ORDER BY session.created_revision, hop.hop_ordinal
                """
            ).fetchall()
        )
        provider_usage = verified.execute(
            """
            SELECT COUNT(*) AS attempts,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   SUM(CASE WHEN state NOT IN ('completed', 'failed', 'blocked', 'cancelled',
                                                'delivery_unknown') THEN 1 ELSE 0 END) AS active
            FROM provider_attempts
            """
        ).fetchone()
        input_tokens = int(provider_usage["input_tokens"])
        output_tokens = int(provider_usage["output_tokens"])
        usage: dict[str, object] = {
            "attempts": int(provider_usage["attempts"]),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "active_attempts": int(provider_usage["active"]),
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "aggregate_limits_enforced": {
                "input_tokens": max_input_tokens is not None,
                "output_tokens": max_output_tokens is not None,
            },
        }
        input_limit_exceeded = (
            max_input_tokens is not None
            and input_tokens > max_input_tokens
        )
        output_limit_exceeded = (
            max_output_tokens is not None
            and output_tokens > max_output_tokens
        )
        if input_limit_exceeded or output_limit_exceeded:
            raise RuntimeError("live Agent token budget exceeded the stress-test ceiling")
        if usage["active_attempts"] != 0:
            raise RuntimeError("live Agent left a Provider Attempt non-terminal")
        active_operations = int(
            verified.execute(
                """
                SELECT COUNT(*) FROM operations
                WHERE status IN ('proposed', 'authorized', 'admitted', 'handoff_committed')
                """
            ).fetchone()[0]
        )
        if active_operations:
            raise RuntimeError("live Agent left an Operation non-terminal")
        valid_result_sources = int(
            verified.execute(
                """
                SELECT COUNT(*) FROM results AS result
                JOIN stata_runs AS run ON run.stata_run_id = result.producing_stata_run_id
                WHERE run.run_status = 'succeeded'
                """
            ).fetchone()[0]
        )
        sourced_evidence = int(
            verified.execute(
                """
                SELECT COUNT(*) FROM evidence_records AS evidence
                JOIN evidence_statistical_sources AS source USING (evidence_record_id)
                JOIN result_elements AS element USING (result_element_id)
                JOIN results AS result USING (result_id)
                """
            ).fetchone()[0]
        )
        document = verified.execute(
            """
            SELECT revision.document_revision_id, location.managed_handle,
                   artifact.content_hash, artifact.size_bytes
            FROM document_revisions AS revision
            JOIN artifacts AS artifact ON artifact.artifact_id = revision.docx_artifact_id
            JOIN artifact_locations AS location USING (artifact_id)
            ORDER BY revision.created_revision DESC LIMIT 1
            """
        ).fetchone()
        if document is not None:
            document_path = (database.root / str(document["managed_handle"])).resolve()
            if (
                not document_path.is_file()
                or document_path.stat().st_size != int(document["size_bytes"])
                or _sha256(document_path) != str(document["content_hash"])
            ):
                raise RuntimeError("delivered Word Artifact does not match its ledger identity")
            friendly_document = output_root / "delivered-research-draft.docx"
            shutil.copy2(document_path, friendly_document)
        else:
            friendly_document = None
        secret_findings = _secret_occurrences(output_root, secret)
        if secret_findings:
            raise RuntimeError("Provider secret was persisted by the live product run")
        result = {
            "status": str(turn_row["status"]),
            "turn_revision": int(turn_row["turn_revision"]),
            "counts": counts,
            "budget": usage,
            "integrity": {
                "active_operations": active_operations,
                "stata_sourced_results": valid_result_sources,
                "stata_sourced_evidence": sourced_evidence,
                "secret_scan_passed": True,
                "open_retrieval_sessions": open_retrieval_sessions,
            },
            "retrieval_trace": retrieval_trace,
            "waiting": None
            if waiting is None
            else {"reason": str(waiting["wait_reason"]), "prompt": str(waiting["prompt"])},
            "document": None
            if document is None
            else {
                "document_revision_id": str(document["document_revision_id"]),
                "path": str((database.root / str(document["managed_handle"])).resolve()),
                "sha256": str(document["content_hash"]),
                "size_bytes": int(document["size_bytes"]),
                "delivery_copy": str(friendly_document),
            },
            "workspace_root": str(database.root),
        }
        del secret
        return result
    finally:
        verified.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        type=Path,
        default=(
            PROJECT
            / ".real-product-e2e"
            / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    parser.add_argument(
        "--max-input-tokens",
        type=int,
        help=(
            "Optional aggregate stress-test ceiling. Usage is recorded without gating "
            "when omitted."
        ),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        help=(
            "Optional aggregate stress-test ceiling. Usage is recorded without gating "
            "when omitted."
        ),
    )
    parser.add_argument("--candidate", type=Path)
    parser.add_argument(
        "--prompt",
        default=(
            "Use auto.dta to study how fuel economy and vehicle weight relate to price. "
            "Design a defensible initial empirical specification, execute it in Stata, "
            "and continue autonomously to a traceable Word draft. Pause only if a "
            "consequential choice cannot be made from the available evidence."
        ),
    )
    parser.add_argument(
        "--goal-mode", choices=("research_loop", "deliver_word"), default="deliver_word"
    )
    parser.add_argument("--literature-source", type=Path)
    parser.add_argument(
        "--workspace-input-source",
        type=Path,
        help="Copy a trusted evaluation input directory into research-inputs/.",
    )
    parser.add_argument("--embedding-cache", type=Path)
    arguments = parser.parse_args()
    root = arguments.output_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    result = asyncio.run(
        _run(
            root,
            timeout_seconds=arguments.timeout_seconds,
            max_input_tokens=arguments.max_input_tokens,
            max_output_tokens=arguments.max_output_tokens,
            candidate=arguments.candidate,
            prompt=arguments.prompt,
            goal_mode=arguments.goal_mode,
            literature_source=arguments.literature_source,
            workspace_input_source=arguments.workspace_input_source,
            embedding_cache=arguments.embedding_cache,
        )
    )
    report = root / "live-product-report.json"
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["status"] != "succeeded":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
