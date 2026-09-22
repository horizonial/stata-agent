"""End-to-end baseline for Literature, Stata Help, Style, and corpus isolation."""

from __future__ import annotations

import json
from pathlib import Path

from stata_research_agent.application.control import CreateWorkspaceCommand
from stata_research_agent.application.dense_retrieval import (
    DenseKnowledgeIndexService,
    EmbeddingProfile,
)
from stata_research_agent.application.knowledge_retrieval import (
    CorpusRole,
    ExtractedKnowledgeDocument,
    ExtractedKnowledgePage,
    KnowledgeRetrievalHit,
    PageParseFinding,
    SyncKnowledgeIndexCommand,
)
from stata_research_agent.application.rag_evaluation import (
    RagAcceptanceThresholds,
    RagGoldCase,
    evaluate_rag,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.knowledge_runtime import (
    StataHelpIndexService,
    WorkspaceKnowledgeIndexService,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.dense_knowledge_store import (
    SqliteDenseKnowledgeIndexRepository,
)
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _FixtureEmbeddingGateway:
    profile = EmbeddingProfile("fixture-embedding-v1", "local_fixture", "fixture-2d", 2)

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            (1.0, 0.0)
            if "parallel" in text.casefold()
            or "identifying assumption" in text.casefold()
            or "causal proxy" in text.casefold()
            else (0.0, 1.0)
            for text in texts
        )

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self.embed_documents((text,))[0]


def test_adaptive_page_findings_are_persisted_on_parse_revision(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_adaptive_parse_findings")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_adaptive_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        repository.sync(
            SyncKnowledgeIndexCommand(
                CommandId("cmd_adaptive_sync"),
                (
                    ExtractedKnowledgeDocument(
                        "literature/paper.pdf",
                        "application/pdf",
                        "a" * 64,
                        1234,
                        (
                            ExtractedKnowledgePage(1, "Ordinary introduction text."),
                            ExtractedKnowledgePage(2, "$$ E[y] = a + bx $$"),
                        ),
                        parser_name="adaptive-pdf",
                        parser_version="1",
                        parser_profile="pypdf_fast+mineru_basic_selective-v1",
                        page_parse_findings=(
                            PageParseFinding(
                                2,
                                ("formula_layout_candidate",),
                                3,
                                "mineru-cli",
                            ),
                        ),
                    ),
                ),
                ("literature/paper.pdf",),
                (),
            )
        )
        row = connection.execute(
            "SELECT quality_findings_json FROM knowledge_parse_revisions"
        ).fetchone()
        assert row is not None
        assert json.loads(str(row["quality_findings_json"])) == [
            {
                "finding_kind": "ingestion_metadata",
                "ingestion_policy_revision": "knowledge-ingestion-v2",
                "canonical_ir_version": "canonical-ir-v2",
                "corpus_roles": ["literature_evidence"],
            },
            {
                "page_number": 2,
                "reason_codes": ["formula_layout_candidate"],
                "diagnostic_score": 3,
                "selected_parser": "mineru-cli",
            }
        ]
    finally:
        connection.close()


def test_three_corpus_rag_baseline_meets_thresholds(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_rag_eval_baseline")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    style = database.root / "style-references"
    help_root = tmp_path / "stata-help"
    literature.mkdir()
    style.mkdir()
    help_root.mkdir()
    (literature / "design.md").write_text(
        "# Identification\n\nParallel trends is the identifying assumption.\n\n"
        "# Diagnostics\n\nEvent-study leads can expose pre-trend violations.",
        encoding="utf-8",
    )
    (style / "selected-paper.md").write_text(
        "# Discussion\n\nWe first summarize the estimate, then discuss its practical magnitude.",
        encoding="utf-8",
    )
    (help_root / "regress.sthlp").write_text(
        "{title:regress}\n\n{pstd}Use {cmd:vce(robust)} for robust standard errors.\n\n"
        "{title:Stored results}\n\n{pstd}The coefficient vector is stored in {cmd:e(b)}.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_rag_eval_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()
        StataHelpIndexService(repository, (("base", help_root),), identities).synchronize()
        dense = DenseKnowledgeIndexService(
            SqliteDenseKnowledgeIndexRepository(connection), _FixtureEmbeddingGateway()
        )
        dense_index_id = dense.rebuild(
            CommandId("cmd_rag_dense_baseline"), (CorpusRole.LITERATURE_EVIDENCE,)
        )
        assert dense_index_id.startswith("knowledgeembedidx_")
        dense_hits = dense.search(
            "identifying assumption", (CorpusRole.LITERATURE_EVIDENCE,), 3
        )
        assert dense_hits
        assert "Parallel trends" in repository.read_canonical_nodes(
            (dense_hits[0].node_id,)
        )[0].content
        hybrid_repository = SqliteKnowledgeRepository(connection, dense)
        hybrid_hits = hybrid_repository.search_canonical(
            "causal proxy",
            corpus_roles=(CorpusRole.LITERATURE_EVIDENCE,),
            limit=3,
        )
        assert hybrid_hits
        assert hybrid_hits[0].lexical_rank is None
        assert hybrid_hits[0].dense_rank == 1

        def retrieve(
            query: str, roles: tuple[CorpusRole, ...], limit: int
        ) -> tuple[KnowledgeRetrievalHit, ...]:
            return tuple(
                KnowledgeRetrievalHit(
                    hit.node_id,
                    hit.source_revision_id,
                    hit.parse_revision_id,
                    hit.source_locator,
                    hit.corpus_role,
                    hit.node_kind,
                    hit.page_start,
                    hit.page_end,
                    hit.section_title,
                    hit.content,
                    hit.lexical_rank,
                    hit.fused_score,
                )
                for hit in repository.search_canonical(
                    query, corpus_roles=roles, limit=limit
                )
            )

        report = evaluate_rag(
            (
                RagGoldCase(
                    "literature-multihop",
                    "parallel trends event-study leads pre-trend",
                    (CorpusRole.LITERATURE_EVIDENCE,),
                    expected_source_locators=("literature/design.md",),
                    expected_content_markers=("parallel trends", "event-study leads"),
                    evidence_groups=(("parallel trends",), ("event-study leads",)),
                    k=6,
                ),
                RagGoldCase(
                    "stata-stored-results",
                    "regress robust coefficient vector e(b)",
                    (CorpusRole.STATA_HELP,),
                    expected_source_locators=("stata-help/base/regress.sthlp",),
                    expected_content_markers=("e(b)",),
                    k=6,
                ),
                RagGoldCase(
                    "style-discussion",
                    "summarize estimate practical magnitude",
                    (CorpusRole.STYLE_EXEMPLAR,),
                    expected_source_locators=("style-references/selected-paper.md",),
                    expected_content_markers=("practical magnitude",),
                    k=6,
                ),
            ),
            retrieve,
        )

        assert report.accepts(RagAcceptanceThresholds())
        assert report.total_role_leaks == 0
        assert report.macro_evidence_group_coverage == 1
    finally:
        connection.close()
