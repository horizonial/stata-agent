"""Evaluate real dense retrieval with user-like single-hop, multi-hop, and no-answer cases."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from stata_research_agent.application.dense_retrieval import DenseKnowledgeIndexService
from stata_research_agent.application.knowledge_retrieval import CorpusRole
from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.interfaces.sentence_transformer_embedding import (
    SentenceTransformerEmbeddingGateway,
)
from stata_research_agent.persistence.dense_knowledge_store import (
    SqliteDenseKnowledgeIndexRepository,
)
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase


def _score_hits(
    hits: tuple[object, ...],
    expected_sources: tuple[str, ...],
    expected_markers: tuple[str, ...],
) -> dict[str, object]:
    source_matches = {
        source
        for source in expected_sources
        if any(getattr(hit, "source_locator") == source for hit in hits)
    }
    marker_matches = {
        marker
        for marker in expected_markers
        if any(marker.casefold() in getattr(hit, "content").casefold() for hit in hits)
    }
    relevant: list[bool] = []
    for hit in hits:
        relevant.append(
            getattr(hit, "source_locator") in expected_sources
            or any(
                marker.casefold() in getattr(hit, "content").casefold()
                for marker in expected_markers
            )
        )
    first_rank = next((index for index, value in enumerate(relevant, 1) if value), 0)
    return {
        "source_recall": 1.0
        if not expected_sources
        else len(source_matches) / len(expected_sources),
        "marker_recall": 1.0
        if not expected_markers
        else len(marker_matches) / len(expected_markers),
        "precision_at_k": 0.0 if not hits else sum(relevant) / len(hits),
        "reciprocal_rank": 0.0 if first_rank == 0 else 1.0 / first_rank,
        "hit_at_k": bool(first_rank),
        "missing_sources": tuple(sorted(set(expected_sources) - source_matches)),
        "missing_markers": tuple(sorted(set(expected_markers) - marker_matches)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--workspace-id", default="ws_real_rag_benchmark_v1")
    parser.add_argument("--embedding-cache", type=Path, required=True)
    parser.add_argument(
        "--questions",
        type=Path,
        default=Path("verification/rag-real-questions.v1.json"),
    )
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    output_root = arguments.output_root.resolve()
    if output_root.exists():
        raise ValueError("scenario output root already exists")
    output_root.mkdir(parents=True)
    question_set = json.loads(arguments.questions.resolve().read_text(encoding="utf-8"))
    if question_set.get("schema_version") != "rag-real-questions/v1":
        raise ValueError("unsupported real RAG question schema")

    database = WorkspaceDatabase(
        arguments.workspace_root.resolve(), WorkspaceId(arguments.workspace_id)
    )
    connection = database.open(writable=False)
    gateway = SentenceTransformerEmbeddingGateway(
        cache_folder=arguments.embedding_cache.resolve()
    )
    dense = DenseKnowledgeIndexService(
        SqliteDenseKnowledgeIndexRepository(connection), gateway
    )
    repository = SqliteKnowledgeRepository(connection, dense)
    scores: list[dict[str, object]] = []
    try:
        for raw_case in question_set["cases"]:
            role = CorpusRole(str(raw_case["corpus_role"]))
            mode = str(raw_case["mode"])
            queries = (
                tuple(str(query) for query in raw_case["hops"])
                if mode == "multi_hop"
                else (
                    str(raw_case.get("retrieval_query", raw_case["question"])),
                )
            )
            hop_hits = tuple(
                repository.search_canonical(
                    query,
                    corpus_roles=(role,),
                    limit=int(raw_case["k"]),
                )
                for query in queries
            )
            unique_hits = tuple(
                {hit.node_id: hit for hits in hop_hits for hit in hits}.values()
            )
            observed_roles = {hit.corpus_role for hit in unique_hits}
            payload: dict[str, object] = {
                "case_id": str(raw_case["case_id"]),
                "mode": mode,
                "question": str(raw_case["question"]),
                "retrieval_queries": queries,
                "hop_count": len(hop_hits),
                "retrieved_count": len(unique_hits),
                "role_leak_count": sum(value != role.value for value in observed_roles),
                "top_dense_scores": tuple(
                    hit.dense_score for hit in unique_hits[:3] if hit.dense_score is not None
                ),
                "retrieved": tuple(
                    {
                        "node_id": hit.node_id,
                        "source_locator": hit.source_locator,
                        "page_start": hit.page_start,
                        "lexical_rank": hit.lexical_rank,
                        "dense_rank": hit.dense_rank,
                        "fused_score": hit.fused_score,
                    }
                    for hit in unique_hits
                ),
            }
            if mode != "no_answer":
                expected_sources = tuple(
                    str(value) for value in raw_case.get("expected_sources", ())
                )
                expected_markers = tuple(
                    str(value) for value in raw_case.get("expected_markers", ())
                )
                if mode == "multi_hop":
                    evidence_groups = tuple(
                        tuple(str(marker) for marker in group)
                        for group in raw_case["evidence_groups"]
                    )
                    group_coverage = sum(
                        any(
                            marker.casefold() in hit.content.casefold()
                            for marker in group
                            for hit in unique_hits
                        )
                        for group in evidence_groups
                    ) / len(evidence_groups)
                    first_nodes = {hit.node_id for hit in hop_hits[0]}
                    later_nodes = {hit.node_id for hits in hop_hits[1:] for hit in hits}
                    payload["evidence_group_coverage"] = group_coverage
                    payload["later_hop_novel_node_ratio"] = (
                        0.0
                        if not later_nodes
                        else len(later_nodes - first_nodes) / len(later_nodes)
                    )
                payload.update(_score_hits(unique_hits, expected_sources, expected_markers))
                if mode == "multi_hop":
                    payload["marker_recall"] = group_coverage
                    payload["missing_markers"] = tuple(
                        " | ".join(group)
                        for group in evidence_groups
                        if not any(
                            marker.casefold() in hit.content.casefold()
                            for marker in group
                            for hit in unique_hits
                        )
                    )
            scores.append(payload)
    finally:
        connection.close()

    positive = tuple(score for score in scores if score["mode"] != "no_answer")
    multi = tuple(score for score in positive if score["mode"] == "multi_hop")
    metrics = {
        "positive_case_count": len(positive),
        "no_answer_case_count": sum(score["mode"] == "no_answer" for score in scores),
        "hit_at_k": sum(bool(score["hit_at_k"]) for score in positive) / len(positive),
        "macro_source_recall": sum(float(score["source_recall"]) for score in positive)
        / len(positive),
        "macro_marker_recall": sum(float(score["marker_recall"]) for score in positive)
        / len(positive),
        "macro_precision_at_k": sum(float(score["precision_at_k"]) for score in positive)
        / len(positive),
        "mean_reciprocal_rank": sum(float(score["reciprocal_rank"]) for score in positive)
        / len(positive),
        "multi_hop_evidence_group_coverage": sum(
            float(score["evidence_group_coverage"]) for score in multi
        )
        / len(multi),
        "multi_hop_novel_node_ratio": sum(
            float(score["later_hop_novel_node_ratio"]) for score in multi
        )
        / len(multi),
        "total_role_leaks": sum(int(score["role_leak_count"]) for score in scores),
    }
    thresholds = {
        "minimum_hit_at_k": 0.90,
        "minimum_macro_source_recall": 0.85,
        "minimum_macro_marker_recall": 0.80,
        "minimum_macro_precision_at_k": 0.20,
        "minimum_mean_reciprocal_rank": 0.60,
        "minimum_multi_hop_evidence_group_coverage": 0.80,
        "minimum_multi_hop_novel_node_ratio": 0.25,
        "maximum_role_leaks": 0,
    }
    accepted = (
        metrics["hit_at_k"] >= thresholds["minimum_hit_at_k"]
        and metrics["macro_source_recall"] >= thresholds["minimum_macro_source_recall"]
        and metrics["macro_marker_recall"] >= thresholds["minimum_macro_marker_recall"]
        and metrics["macro_precision_at_k"] >= thresholds["minimum_macro_precision_at_k"]
        and metrics["mean_reciprocal_rank"] >= thresholds["minimum_mean_reciprocal_rank"]
        and metrics["multi_hop_evidence_group_coverage"]
        >= thresholds["minimum_multi_hop_evidence_group_coverage"]
        and metrics["multi_hop_novel_node_ratio"]
        >= thresholds["minimum_multi_hop_novel_node_ratio"]
        and metrics["total_role_leaks"] <= thresholds["maximum_role_leaks"]
    )
    report = {
        "schema_version": "rag-real-scenario-evaluation/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "question_set": str(arguments.questions.resolve()),
        "embedding_profile": gateway.profile.profile_revision,
        "metrics": metrics,
        "thresholds": thresholds,
        "accepted": accepted,
        "scores": scores,
    }
    report_path = output_root / "rag-scenario-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"report_path": str(report_path), **metrics, "accepted": accepted}, indent=2))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
