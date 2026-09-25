"""Run a reproducible RAG benchmark with real papers and installed Stata Help.

This is intentionally separate from the small deterministic fixture suite.  It downloads
public primary-source papers into an isolated Workspace, indexes the real local Stata 18 help
tree, executes retrieval and multi-hop sessions, and writes the observed scores and failures.
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stata_research_agent.application.control import CreateWorkspaceCommand
from stata_research_agent.application.dense_retrieval import DenseKnowledgeIndexService
from stata_research_agent.application.knowledge_retrieval import (
    ContinueKnowledgeRetrievalCommand,
    CorpusRole,
    KnowledgeRetrievalHit,
    RetrievalMode,
    StartKnowledgeRetrievalCommand,
)
from stata_research_agent.application.rag_evaluation import (
    RagAcceptanceThresholds,
    RagGoldCase,
    RagTraceGoldCase,
    RagTraceHop,
    evaluate_rag,
    evaluate_retrieval_trace,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.interfaces.knowledge_runtime import (
    StataHelpIndexService,
    WorkspaceKnowledgeIndexService,
)
from stata_research_agent.interfaces.literature_catalog import (
    MineruCliParser,
    PdfPageEscalationPolicy,
    discover_stata_help_roots,
)
from stata_research_agent.interfaces.sentence_transformer_embedding import (
    SentenceTransformerEmbeddingGateway,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.dense_knowledge_store import (
    SqliteDenseKnowledgeIndexRepository,
)
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def _prepare_corpus(corpus_root: Path, workspace_root: Path) -> dict[str, object]:
    manifest = json.loads((corpus_root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "stata-research-agent/paper-corpus/v1":
        raise ValueError("unsupported paper corpus manifest")
    for paper in manifest["papers"]:
        source = corpus_root / paper["relative_path"]
        destination_folder = (
            "style-references"
            if paper["benchmark_role"] == "selected_style_and_synthesis"
            else "literature"
        )
        destination = workspace_root / destination_folder / paper["filename"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return manifest


def _hit(hit: KnowledgeRetrievalHit) -> KnowledgeRetrievalHit:
    return KnowledgeRetrievalHit(
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
        hit.dense_rank,
        hit.dense_score,
    )


def _parser_observations(connection: Any) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        """
        SELECT source.canonical_locator, parse.parser_name, parse.parser_profile,
               count(*) AS node_count,
               sum(node.page_start IS NOT NULL) AS located_node_count,
               sum(node.node_kind = 'table') AS table_count,
               sum(node.node_kind = 'equation') AS equation_count,
               sum(node.node_kind = 'reference') AS reference_count
        FROM knowledge_sources AS source
        JOIN knowledge_source_revisions AS revision USING (knowledge_source_id)
        JOIN knowledge_parse_revisions AS parse USING (knowledge_source_revision_id)
        JOIN knowledge_nodes AS node USING (knowledge_parse_revision_id)
        WHERE source.canonical_locator LIKE 'literature/%'
           OR source.canonical_locator LIKE 'style-references/%'
        GROUP BY source.canonical_locator, parse.parser_name, parse.parser_profile
        ORDER BY source.canonical_locator
        """
    ).fetchall()
    return tuple(
        {
            "source_locator": str(row["canonical_locator"]),
            "parser_name": str(row["parser_name"]),
            "parser_profile": str(row["parser_profile"]),
            "node_count": int(row["node_count"]),
            "located_node_count": int(row["located_node_count"]),
            "locator_coverage": int(row["located_node_count"]) / int(row["node_count"]),
            "table_count": int(row["table_count"]),
            "equation_count": int(row["equation_count"]),
            "reference_count": int(row["reference_count"]),
        }
        for row in rows
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--corpus-root",
        type=Path,
        default=Path("verification/corpora/did-methods-v1"),
    )
    parser.add_argument("--workspace-id", default="ws_real_rag_benchmark_v1")
    parser.add_argument("--mineru-executable", type=Path)
    parser.add_argument("--mineru-version", default="4.0.4")
    parser.add_argument("--mineru-minimum-score", type=int, default=3)
    parser.add_argument("--enable-dense", action="store_true")
    parser.add_argument(
        "--embedding-cache",
        type=Path,
        default=Path(".e2e-runtime/rag-real/huggingface"),
    )
    arguments = parser.parse_args()
    output_root = arguments.output_root.resolve()
    corpus_root = arguments.corpus_root.resolve()
    if output_root.exists():
        raise ValueError("benchmark output root already exists; use a new immutable run directory")
    workspace_id = WorkspaceId(arguments.workspace_id)
    database = WorkspaceDatabase(output_root / "workspace", workspace_id)
    database.create()
    corpus_manifest = _prepare_corpus(corpus_root, database.root)
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    try:
        WorkspaceControlService(SqliteControlStore(connection), identities).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_real_rag_benchmark_workspace"), workspace_id)
        )
        repository = SqliteKnowledgeRepository(connection)
        mineru = (
            None
            if arguments.mineru_executable is None
            else MineruCliParser(
                arguments.mineru_executable,
                version=arguments.mineru_version,
                tier="basic",
                timeout_seconds=3600,
            )
        )
        WorkspaceKnowledgeIndexService(
            repository,
            database.root,
            identities,
            pdf_parser=mineru,
            pdf_enrichment_policy=(
                None
                if mineru is None
                else PdfPageEscalationPolicy(
                    minimum_score=arguments.mineru_minimum_score
                )
            ),
        ).synchronize()
        help_roots = discover_stata_help_roots()
        help_service = StataHelpIndexService(repository, help_roots, identities)
        help_queries = (
            "regress robust standard errors vce robust",
            "estimates store restore stored estimation results",
            "margins after probit predict probabilities",
        )
        for query in help_queries:
            help_service.synchronize_for_query(query)
        dense_indexes: dict[str, str] = {}
        embedding_profile: dict[str, object] | None = None
        if arguments.enable_dense:
            gateway = SentenceTransformerEmbeddingGateway(
                cache_folder=arguments.embedding_cache.resolve()
            )
            dense_service = DenseKnowledgeIndexService(
                SqliteDenseKnowledgeIndexRepository(connection), gateway
            )
            for roles in (
                (CorpusRole.LITERATURE_EVIDENCE,),
                (CorpusRole.STATA_HELP,),
                (CorpusRole.STYLE_EXEMPLAR,),
            ):
                index_id = dense_service.ensure_current(
                    identities.new(CommandId), roles
                )
                if index_id is not None:
                    dense_indexes[roles[0].value] = index_id
            repository = SqliteKnowledgeRepository(connection, dense_service)
            embedding_profile = asdict(gateway.profile)
        cases = (
            RagGoldCase(
                "paper-twfe-decomposition",
                "two way fixed effects weighted average two group two period estimators",
                (CorpusRole.LITERATURE_EVIDENCE,),
                expected_source_locators=(
                    "literature/01-goodman-bacon-treatment-timing.pdf",
                ),
                expected_content_markers=("weighted average",),
                evidence_groups=(("weighted average",),),
                k=8,
            ),
            RagGoldCase(
                "paper-doubly-robust-did",
                "outcome regression inverse probability weighting doubly robust estimands",
                (CorpusRole.LITERATURE_EVIDENCE,),
                expected_source_locators=(
                    "literature/02-callaway-santanna-multiple-periods.pdf",
                ),
                expected_content_markers=("doubly robust",),
                evidence_groups=(("doubly robust", "doubly-robust"),),
                k=8,
            ),
            RagGoldCase(
                "paper-event-study-contamination",
                "event study lead lag contaminated heterogeneous treatment effects",
                (CorpusRole.LITERATURE_EVIDENCE,),
                expected_source_locators=(
                    "literature/03-sun-abraham-event-studies.pdf",
                ),
                expected_content_markers=("contaminated",),
                k=8,
            ),
            RagGoldCase(
                "paper-imputation-estimator",
                "efficient imputation estimator unrestricted treatment effect heterogeneity",
                (CorpusRole.LITERATURE_EVIDENCE,),
                expected_source_locators=(
                    "literature/04-borusyak-jaravel-spiess-event-study.pdf",
                ),
                expected_content_markers=("imputation",),
                k=8,
            ),
            RagGoldCase(
                "paper-estimator-survey",
                "eventstudyinteract csdid did_imputation did_multiplegt Stata commands",
                (CorpusRole.LITERATURE_EVIDENCE,),
                expected_source_locators=(
                    "literature/05-de-chaisemartin-dhaultfoeuille-survey.pdf",
                ),
                expected_content_markers=("eventstudyinteract",),
                k=8,
            ),
            RagGoldCase(
                "paper-pretrend-pretest",
                "conditioning after passing pre trends test bias inference",
                (CorpusRole.LITERATURE_EVIDENCE,),
                expected_source_locators=("literature/06-roth-pretrends.pdf",),
                expected_content_markers=("pre-trends",),
                k=8,
            ),
            RagGoldCase(
                "paper-honest-parallel-trends",
                "sensitivity analysis restrictions violations parallel trends robust inference",
                (CorpusRole.LITERATURE_EVIDENCE,),
                expected_source_locators=(
                    "literature/07-rambachan-roth-parallel-trends.pdf",
                ),
                expected_content_markers=("sensitivity analysis",),
                k=8,
            ),
            RagGoldCase(
                "paper-did-synthesis",
                (
                    "canonical assumptions multiple periods parallel trends alternative "
                    "inference frameworks"
                ),
                (CorpusRole.LITERATURE_EVIDENCE,),
                expected_source_locators=(
                    "literature/08-roth-et-al-whats-trending.pdf",
                ),
                expected_content_markers=("canonical",),
                k=8,
            ),
            RagGoldCase(
                "paper-continuous-treatment",
                "continuous treatment dose difference in differences generalized parallel trends",
                (CorpusRole.LITERATURE_EVIDENCE,),
                expected_source_locators=(
                    "literature/09-callaway-goodman-bacon-santanna-continuous.pdf",
                ),
                expected_content_markers=("continuous treatment",),
                k=8,
            ),
            RagGoldCase(
                "stata-regress-robust",
                help_queries[0],
                (CorpusRole.STATA_HELP,),
                expected_source_locators=("stata-help/base/r/regress.sthlp",),
                expected_content_markers=("vce(robust)",),
                k=8,
            ),
            RagGoldCase(
                "stata-estimates-store",
                help_queries[1],
                (CorpusRole.STATA_HELP,),
                expected_source_locators=("stata-help/base/e/estimates.sthlp",),
                expected_content_markers=("estimates store",),
                k=8,
            ),
            RagGoldCase(
                "style-selected-paper-only",
                "organizing framework empirical practice covariates weights",
                (CorpusRole.STYLE_EXEMPLAR,),
                expected_source_locators=(
                    "style-references/10-baker-et-al-practitioners-guide.pdf",
                ),
                expected_content_markers=("organizing framework",),
                k=8,
            ),
        )

        observed_hits: list[KnowledgeRetrievalHit] = []

        def retrieve(
            query: str, roles: tuple[CorpusRole, ...], limit: int
        ) -> tuple[KnowledgeRetrievalHit, ...]:
            hits = tuple(
                _hit(hit)
                for hit in repository.search_canonical(
                    query, corpus_roles=roles, limit=limit
                )
            )
            observed_hits.extend(hits)
            return hits

        retrieval_report = evaluate_rag(cases, retrieve, policy_revision="real-rag-v1")
        first = repository.retrieve(
            StartKnowledgeRetrievalCommand(
                CommandId("cmd_real_rag_hop_1"),
                "TWFE weighted average treatment timing",
                "Identify the TWFE decomposition problem and then retrieve an alternative",
                (CorpusRole.LITERATURE_EVIDENCE,),
                RetrievalMode.MULTI_HOP,
                4,
            )
        )
        second = repository.continue_retrieval(
            ContinueKnowledgeRetrievalCommand(
                CommandId("cmd_real_rag_hop_2"),
                first.retrieval_session_id,
                "doubly robust estimands multiple time periods",
                "Which alternative estimands address heterogeneous treatment timing?",
                4,
                True,
            )
        )
        trace_score = evaluate_retrieval_trace(
            RagTraceGoldCase(
                "real-did-method-chain",
                (CorpusRole.LITERATURE_EVIDENCE,),
                (("weighted average",), ("doubly robust", "doubly-robust")),
                minimum_novel_node_ratio=0.25,
            ),
            (
                RagTraceHop(
                    "What is the TWFE decomposition problem?",
                    first.hits,
                    first.stop_reason,
                ),
                RagTraceHop(
                    "Which alternative estimands address heterogeneous timing?",
                    second.hits,
                    second.stop_reason,
                ),
            ),
        )
        thresholds = RagAcceptanceThresholds()
        payload = {
            "schema_version": "real-rag-benchmark/v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "workspace_id": workspace_id.value,
            "retrieval_backend": (
                "sqlite_fts5_plus_flat_cosine_rrf"
                if arguments.enable_dense
                else "sqlite_fts5_only"
            ),
            "dense_embedding_enabled": arguments.enable_dense,
            "embedding_profile": embedding_profile,
            "dense_indexes": dense_indexes,
            "observed_dense_hit_count": sum(
                hit.dense_rank is not None for hit in observed_hits
            ),
            "mineru_enabled": False,
            "paper_corpus": {
                "corpus_id": corpus_manifest["corpus_id"],
                "manifest_path": str(corpus_root / "manifest.json"),
                "paper_count": corpus_manifest["paper_count"],
                "papers": corpus_manifest["papers"],
            },
            "stata_help_roots": [
                {"scope": scope, "path": str(path)} for scope, path in help_roots
            ],
            "indexed_source_counts": {
                str(row["corpus_role"]): int(row["source_count"])
                for row in connection.execute(
                    """
                    SELECT membership.corpus_role, count(*) AS source_count
                    FROM knowledge_source_memberships AS membership
                    JOIN knowledge_source_states AS state USING (knowledge_source_id)
                    WHERE state.availability = 'indexed'
                    GROUP BY membership.corpus_role
                    """
                ).fetchall()
            },
            "parser_observations": _parser_observations(connection),
            "retrieval_report": asdict(retrieval_report),
            "retrieval_thresholds": asdict(thresholds),
            "retrieval_accepted": retrieval_report.accepts(thresholds),
            "multi_hop_trace": asdict(trace_score),
            "limitations": [
                "PDF parsing uses pypdf because MinerU is not installed on this host.",
                (
                    "Generation grounding and writing-style preference require "
                    "a live model run and are not scored here."
                ),
            ]
            + (
                []
                if arguments.enable_dense
                else ["No real embedding model was enabled for this run."]
            ),
        }
    finally:
        connection.close()
    report_path = output_root / "real-rag-report.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "retrieval_accepted": payload["retrieval_accepted"],
                "macro_recall_at_k": retrieval_report.macro_recall_at_k,
                "macro_precision_at_k": retrieval_report.macro_precision_at_k,
                "mean_reciprocal_rank": retrieval_report.mean_reciprocal_rank,
                "total_role_leaks": retrieval_report.total_role_leaks,
                "multi_hop_passed": trace_score.passed,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
