"""Canonical knowledge roles, retrieval sessions, and local Stata Help."""

from __future__ import annotations

import hashlib
from pathlib import Path

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.knowledge_retrieval import (
    ConcludeKnowledgeRetrievalCommand,
    ContinueKnowledgeRetrievalCommand,
    CorpusRole,
    ExtractedKnowledgeDocument,
    ExtractedKnowledgePage,
    RetrievalMode,
    StartKnowledgeRetrievalCommand,
    SyncKnowledgeIndexCommand,
)
from stata_research_agent.application.rag_evaluation import (
    RagTraceGoldCase,
    RagTraceHop,
    evaluate_retrieval_trace,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.knowledge_runtime import (
    StataHelpIndexService,
    WorkspaceKnowledgeIndexService,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def test_corpus_roles_are_isolated_and_retrieval_is_journaled(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_canonical_knowledge")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    style = database.root / "style-references"
    literature.mkdir()
    style.mkdir()
    (literature / "identification.md").write_text(
        "# Identification\n\nParallel trends supports difference-in-differences identification.\n\n"
        "# Diagnostics\n\nEvent-study leads can expose pre-trend violations.",
        encoding="utf-8",
    )
    (style / "target-paper.md").write_text(
        "# Results\n\nWe report the estimates in a deliberately concise empirical style.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_canonical_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()

        evidence_hits = repository.search_canonical(
            "parallel trends identification",
            corpus_roles=(CorpusRole.LITERATURE_EVIDENCE,),
        )
        assert evidence_hits
        assert {hit.corpus_role for hit in evidence_hits} == {"literature_evidence"}
        assert all(hit.source_locator.startswith("literature/") for hit in evidence_hits)
        assert any(hit.section_title == "Identification" for hit in evidence_hits)
        exact = repository.read_canonical_nodes((evidence_hits[0].node_id,))
        assert exact[0].content == evidence_hits[0].content
        expanded = repository.expand_canonical_nodes(
            (evidence_hits[0].node_id,), direction="both", limit=8
        )
        assert expanded
        assert all(hit.node_id != evidence_hits[0].node_id for hit in expanded)

        style_hits = repository.search_canonical(
            "concise empirical style",
            corpus_roles=(CorpusRole.STYLE_EXEMPLAR,),
        )
        assert style_hits
        assert {hit.corpus_role for hit in style_hits} == {"style_exemplar"}
        assert all(hit.source_locator.startswith("style-references/") for hit in style_hits)
        assert repository.search_canonical(
            "concise empirical style",
            corpus_roles=(CorpusRole.LITERATURE_EVIDENCE,),
        ) == ()

        retrieval = repository.retrieve(
            StartKnowledgeRetrievalCommand(
                CommandId("cmd_canonical_retrieval"),
                "event study pre-trend",
                "Find a diagnostic for the identifying assumption",
                (CorpusRole.LITERATURE_EVIDENCE,),
                RetrievalMode.HIERARCHICAL,
                4,
            )
        )
        assert retrieval.hits
        assert retrieval.stop_reason == "candidate_exhausted"
        assert connection.execute(
            "SELECT count(*) FROM knowledge_retrieval_sessions"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM knowledge_retrieval_hops"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM knowledge_retrieval_selections"
        ).fetchone()[0] == len(retrieval.hits)
        assert connection.execute(
            "SELECT count(*) FROM knowledge_retrieval_query_variants"
        ).fetchone()[0] >= 2
        assert connection.execute(
            "SELECT count(*) FROM knowledge_retrieval_candidates"
        ).fetchone()[0] >= len(retrieval.hits)
        assert connection.execute(
            """
            SELECT count(*) FROM knowledge_retrieval_candidates
            WHERE rerank_score IS NOT NULL AND reranker_policy_revision != ''
            """
        ).fetchone()[0] >= len(retrieval.hits)
        assert all(hit.rerank_score is not None for hit in retrieval.hits)
        assert retrieval.query_variants[0] == "event study pre-trend"
        assert connection.execute(
            """
            SELECT count(*) FROM journal_entries
            WHERE event_type = 'knowledge.retrieval_completed'
            """
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_ingestion_policy_upgrade_reparses_unchanged_source_bytes(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_knowledge_policy_upgrade")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    literature.mkdir()
    (literature / "paper.md").write_text(
        "# Results\n\nA stable source should be reparsed when the canonical IR changes.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_policy_upgrade_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        payload = (literature / "paper.md").read_bytes()
        repository.sync(
            SyncKnowledgeIndexCommand(
                CommandId("cmd_policy_upgrade_v1"),
                (
                    ExtractedKnowledgeDocument(
                        "literature/paper.md",
                        "text/markdown",
                        hashlib.sha256(payload).hexdigest(),
                        len(payload),
                        (ExtractedKnowledgePage(None, payload.decode("utf-8")),),
                        canonical_ir_version="canonical-ir-v1",
                        ingestion_policy_revision="knowledge-ingestion-v1",
                    ),
                ),
                ("literature/paper.md",),
                (),
                policy_revision="knowledge-ingestion-v1",
            )
        )
        service = WorkspaceKnowledgeIndexService(repository, database.root, identities)

        service.synchronize()

        versions = {
            str(row[0])
            for row in connection.execute(
                "SELECT canonical_ir_version FROM knowledge_parse_revisions"
            ).fetchall()
        }
        assert versions == {"canonical-ir-v1", "canonical-ir-v2"}
        assert repository.current_hashes(("literature/",))
    finally:
        connection.close()


def test_extraction_failure_preserves_last_known_good_index(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_knowledge_last_good")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    literature.mkdir()
    source = literature / "paper.md"
    source.write_text("# Result\n\nLast known good evidence remains searchable.", encoding="utf-8")
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_last_good_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()
        before = repository.current_hashes(("literature/",))

        outcome = repository.sync(
            SyncKnowledgeIndexCommand(
                CommandId("cmd_last_good_failure"),
                (),
                ("literature/paper.md",),
                (("literature/paper.md", "parser_failed"),),
            )
        )

        assert outcome is not None and outcome.error_count == 1
        assert repository.current_hashes(("literature/",)) == before
        state = connection.execute(
            "SELECT availability FROM knowledge_document_states"
        ).fetchone()[0]
        assert state == "indexed"
        assert repository.search_canonical(
            "known good evidence", corpus_roles=(CorpusRole.LITERATURE_EVIDENCE,)
        )
    finally:
        connection.close()


def test_local_stata_help_is_a_separate_searchable_corpus(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_stata_help")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    help_root = tmp_path / "stata-plus"
    help_root.mkdir()
    (help_root / "regress.sthlp").write_text(
        "{smcl}\n{title:regress — Linear regression}\n\n"
        "{pstd}{cmd:regress} depvar indepvars [{cmd:, vce(robust)}]\n\n"
        "{title:Stored results}\n\n{pstd}regress stores the coefficient vector in {cmd:e(b)}.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_help_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        StataHelpIndexService(repository, (("plus", help_root),), identities).synchronize()

        hits = repository.search_canonical(
            "regress robust stored coefficient vector",
            corpus_roles=(CorpusRole.STATA_HELP,),
            limit=8,
        )
        assert hits
        assert {hit.corpus_role for hit in hits} == {"stata_help"}
        assert all(hit.source_locator.startswith("stata-help/plus/") for hit in hits)
        assert any("e(b)" in hit.content for hit in hits)
        assert repository.search_canonical(
            "regress robust",
            corpus_roles=(CorpusRole.LITERATURE_EVIDENCE,),
        ) == ()
    finally:
        connection.close()


def test_query_bootstrap_adds_help_without_marking_previous_candidates_missing(
    tmp_path: Path,
) -> None:
    workspace_id = WorkspaceId("ws_stata_help_bootstrap")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    help_root = tmp_path / "stata-bootstrap"
    help_root.mkdir()
    (help_root / "alphaonly.sthlp").write_text(
        "{title:alphaonly}\n{pstd}alphaonly performs alpha diagnostics.", encoding="utf-8"
    )
    (help_root / "betaonly.sthlp").write_text(
        "{title:betaonly}\n{pstd}betaonly performs beta diagnostics.", encoding="utf-8"
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_help_bootstrap_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        service = StataHelpIndexService(repository, (("plus", help_root),), identities)
        service.synchronize_for_query("alphaonly", max_sources=1)
        service.synchronize_for_query("betaonly", max_sources=1)

        assert set(repository.current_hashes(("stata-help/",))) == {
            "stata-help/plus/alphaonly.sthlp",
            "stata-help/plus/betaonly.sthlp",
        }
    finally:
        connection.close()


def test_multi_hop_retrieval_continues_same_session_and_closes_explicitly(
    tmp_path: Path,
) -> None:
    workspace_id = WorkspaceId("ws_multi_hop_knowledge")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    literature.mkdir()
    (literature / "identification.md").write_text(
        "# Identification\n\nParallel trends identifies a difference-in-differences design.",
        encoding="utf-8",
    )
    (literature / "diagnostics.md").write_text(
        "# Diagnostics\n\nPlacebo leads diagnose possible pre-trends before treatment.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_multi_hop_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()

        first = repository.retrieve(
            StartKnowledgeRetrievalCommand(
                CommandId("cmd_multi_hop_first"),
                "parallel trends identification",
                "Establish the assumption and then find a diagnostic",
                (CorpusRole.LITERATURE_EVIDENCE,),
                RetrievalMode.MULTI_HOP,
                2,
            )
        )
        assert first.stop_reason == "awaiting_next_hop"
        second = repository.continue_retrieval(
            ContinueKnowledgeRetrievalCommand(
                CommandId("cmd_multi_hop_second"),
                first.retrieval_session_id,
                "placebo leads pre-trends",
                "How can the identifying assumption be diagnosed?",
                2,
                True,
            )
        )
        assert second.retrieval_session_id == first.retrieval_session_id
        assert second.retrieval_hop_id != first.retrieval_hop_id
        assert second.stop_reason == "agent_concluded"
        assert second.hop_ordinal == 2
        assert second.novel_hit_count > 0
        assert second.cumulative_hit_count >= second.novel_hit_count
        assert second.session_status == "completed"
        assert connection.execute(
            "SELECT status FROM knowledge_retrieval_session_states"
        ).fetchone()[0] == "completed"
        assert connection.execute(
            "SELECT count(*) FROM knowledge_retrieval_session_state_history"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM knowledge_retrieval_hops"
        ).fetchone()[0] == 2
        trace_score = evaluate_retrieval_trace(
            RagTraceGoldCase(
                "did-assumption-to-diagnostic",
                (CorpusRole.LITERATURE_EVIDENCE,),
                (("parallel trends",), ("placebo leads", "pre-trends")),
            ),
            (
                RagTraceHop(
                    "What identifies the design?",
                    first.hits,
                    first.stop_reason,
                ),
                RagTraceHop(
                    "How can the identifying assumption be diagnosed?",
                    second.hits,
                    second.stop_reason,
                ),
            ),
        )
        assert trace_score.passed
    finally:
        connection.close()


def test_multi_hop_retrieval_stops_when_a_hop_adds_no_new_evidence(
    tmp_path: Path,
) -> None:
    workspace_id = WorkspaceId("ws_multi_hop_no_progress")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    literature.mkdir()
    (literature / "one.md").write_text(
        "# Identification\n\nParallel trends identifies the design.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_no_progress_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()
        first = repository.retrieve(
            StartKnowledgeRetrievalCommand(
                CommandId("cmd_no_progress_first"),
                "parallel trends identification",
                "Find identifying evidence",
                (CorpusRole.LITERATURE_EVIDENCE,),
                RetrievalMode.MULTI_HOP,
                6,
            )
        )
        repeated = repository.continue_retrieval(
            ContinueKnowledgeRetrievalCommand(
                CommandId("cmd_no_progress_repeat"),
                first.retrieval_session_id,
                "parallel trends identification",
                "Does the same search add any evidence?",
                6,
            )
        )
        assert repeated.stop_reason == "no_new_evidence"
        assert repeated.novel_hit_count == 0
        assert repeated.session_status == "partial"
    finally:
        connection.close()


def test_open_retrieval_session_can_append_an_explicit_conclusion_hop(
    tmp_path: Path,
) -> None:
    workspace_id = WorkspaceId("ws_multi_hop_conclusion")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    literature.mkdir()
    (literature / "evidence.md").write_text(
        "# Evidence\n\nGroup-time average treatment effects support staggered adoption.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_conclusion_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()
        first = repository.retrieve(
            StartKnowledgeRetrievalCommand(
                CommandId("cmd_conclusion_first"),
                "group time average treatment",
                "Find an estimator",
                (CorpusRole.LITERATURE_EVIDENCE,),
                RetrievalMode.MULTI_HOP,
                6,
            )
        )
        concluded = repository.conclude_retrieval(
            ConcludeKnowledgeRetrievalCommand(
                CommandId("cmd_conclusion_finish"), first.retrieval_session_id
            )
        )
        assert concluded.stop_reason == "agent_concluded"
        assert concluded.hop_ordinal == 2
        assert concluded.hits == ()
        assert concluded.session_status == "completed"
        assert connection.execute(
            "SELECT count(*) FROM knowledge_retrieval_hops"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_completing_turn_closes_its_open_retrieval_sessions(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_turn_retrieval_conclusion")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    literature = database.root / "literature"
    literature.mkdir()
    (literature / "evidence.md").write_text(
        "# Evidence\n\nParallel trends identifies difference in differences.",
        encoding="utf-8",
    )
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_turn_close_workspace"), workspace_id)
        )
        turn = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_turn_close_message"), "Find literature evidence."
            )
        )
        repository = SqliteKnowledgeRepository(connection)
        WorkspaceKnowledgeIndexService(repository, database.root, identities).synchronize()
        first = repository.retrieve(
            StartKnowledgeRetrievalCommand(
                CommandId("cmd_turn_close_retrieval"),
                "parallel trends",
                "Find identifying evidence",
                (CorpusRole.LITERATURE_EVIDENCE,),
                RetrievalMode.MULTI_HOP,
                6,
                turn.turn_id,
            )
        )
        assert repository.conclude_open_sessions(turn.turn_id) == (
            first.retrieval_session_id,
        )
        state = connection.execute(
            "SELECT status, stop_reason FROM knowledge_retrieval_session_states"
        ).fetchone()
        assert tuple(state) == ("completed", "agent_concluded")
    finally:
        connection.close()
