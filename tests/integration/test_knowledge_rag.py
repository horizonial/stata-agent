"""Workspace literature RAG, soft intent routing, and revision provenance."""

from __future__ import annotations

import asyncio
from pathlib import Path

from stata_research_agent.application.context_compiler import ContextCompiler
from stata_research_agent.application.control import CreateWorkspaceCommand, SubmitMessageCommand
from stata_research_agent.application.knowledge_retrieval import infer_retrieval_intent
from stata_research_agent.application.model_gateway import ProviderResponse, StartModelStepCommand
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.knowledge_runtime import WorkspaceKnowledgeIndexService
from stata_research_agent.persistence.context_authority import SqliteContextAuthorityReader
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id, "credentialversion_rag", endpoint, "test-secret"
        )


class _Transport:
    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        return ProviderResponse({"text": "Retrieved context inspected.", "tool_calls": []})


def test_literature_index_prefetch_and_revision_replacement(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_knowledge_rag")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    literature.mkdir()
    paper = literature / "identification-notes.md"
    paper.write_text(
        "# Identification\n\nParallel trends supports the difference in differences design.\n"
        "Event-study coefficients can expose pre-trend violations.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_rag_ws"), workspace_id))
        turn = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_rag_turn"),
                "请参考文献判断 difference in differences 的平行趋势依据。",
            )
        )
        repository = SqliteKnowledgeRepository(connection)
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()
        hits = repository.search("difference in differences parallel trends")
        assert len(hits) == 1
        assert hits[0].relative_path == "literature/identification-notes.md"
        assert "Parallel trends" in hits[0].content

        compiled = ContextCompiler(SqliteContextAuthorityReader(connection)).compile(
            turn.turn_id, input_token_budget=96_000
        )
        kinds = {item.item_kind for item in compiled.items}
        assert {"retrieval_intent_hint", "knowledge_catalog", "knowledge_node"} <= kinds
        chunk = next(item for item in compiled.items if item.item_kind == "knowledge_node")
        old_revision = chunk.source_revision
        assert chunk.source_object_type == "knowledge_node"
        asyncio.run(
            ModelGatewayService(
                SqliteModelGatewayRepository(connection), identities, _Credential(), _Transport()
            ).execute_step(
                StartModelStepCommand(
                    CommandId("cmd_rag_step"),
                    turn.turn_id,
                    1,
                    "system-v1",
                    "system",
                    "main",
                    "main-v1",
                    "main skill",
                    "tools-v1",
                    (),
                    compiled.items,
                    compiled.decisions,
                    "permission-v1",
                    {"workspace_read": True},
                    "model-v1",
                    "provider",
                    "openai-compatible",
                    "model",
                    "https://provider.example/chat/completions",
                    "credential://rag",
                    {},
                )
            )
        )
        assert connection.execute(
            "SELECT count(*) FROM knowledge_node_context_uses"
        ).fetchone()[0] == 1

        # A transient extraction failure must be recoverable without trying to duplicate an
        # immutable revision whose bytes were already indexed earlier.
        connection.execute(
            "UPDATE knowledge_document_states SET availability = 'extraction_failed'"
        )
        connection.commit()
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()
        assert connection.execute(
            "SELECT availability FROM knowledge_document_states"
        ).fetchone()[0] == "indexed"
        assert connection.execute(
            "SELECT count(*) FROM knowledge_document_revisions"
        ).fetchone()[0] == 1

        paper.write_text(
            "# Identification\n\nSynthetic control uses a weighted comparison unit.\n",
            encoding="utf-8",
        )
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()
        assert repository.search("parallel trends") == ()
        replacement = repository.search("synthetic control")
        assert replacement and replacement[0].document_revision_id != old_revision
        assert len(
            connection.execute(
                "SELECT * FROM knowledge_document_revisions"
            ).fetchall()
        ) == 2
    finally:
        connection.close()


def test_intent_hint_is_advisory_and_not_a_fixed_workflow() -> None:
    hint = infer_retrieval_intent(
        "Try another specification and explain why it changes the result.",
        literature_available=True,
    )
    assert "workspace_literature" in hint.suggested_sources
    assert "source_explanation_language" in hint.reason_codes
    assert hint.policy_revision == "retrieval-intent-v1"


def test_repeated_extraction_failure_does_not_pollute_workspace_revisions(
    tmp_path: Path,
) -> None:
    workspace_id = WorkspaceId("ws_knowledge_error")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    literature.mkdir()
    (literature / "broken.pdf").write_bytes(b"not-a-pdf")
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_rag_error_ws"), workspace_id)
        )
        service = WorkspaceKnowledgeIndexService(
            SqliteKnowledgeRepository(connection), database.root, identities
        )
        service.synchronize()
        revision = connection.execute(
            "SELECT MAX(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        service.synchronize()
        assert connection.execute(
            "SELECT MAX(workspace_revision) FROM workspace_commits"
        ).fetchone()[0] == revision
        assert connection.execute(
            "SELECT availability FROM knowledge_document_states"
        ).fetchone()[0] == "extraction_failed"
    finally:
        connection.close()
