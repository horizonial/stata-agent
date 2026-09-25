"""Evaluate real dense retrieval with user-like single-hop, multi-hop, and no-answer cases."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from stata_research_agent.application.dense_retrieval import DenseKnowledgeIndexService
from stata_research_agent.application.knowledge_retrieval import CorpusRole
from stata_research_agent.application.retrieval_pipeline import (
    EvidenceSufficiencyGate,
    RerankAssessment,
)
from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.interfaces.cross_encoder_reranker import (
    AdaptiveFusionEvidenceReranker,
    AdaptiveRerankPolicy,
    CrossEncoderEvidenceReranker,
    Qwen3CausalRerankerModel,
    RerankFusionWeights,
)
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


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--workspace-id", default="ws_real_rag_benchmark_v1")
    parser.add_argument("--embedding-cache", type=Path, required=True)
    parser.add_argument("--model-name", default="intfloat/multilingual-e5-small")
    parser.add_argument(
        "--model-revision",
        default="614241f622f53c4eeff9890bdc4f31cfecc418b3",
    )
    parser.add_argument("--query-prefix", default="query: ")
    parser.add_argument("--document-prefix", default="passage: ")
    parser.add_argument("--profile-family", default="e5-prefix-v1")
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--max-sequence-length", type=int)
    parser.add_argument("--reranker-model")
    parser.add_argument("--reranker-revision")
    parser.add_argument(
        "--reranker-family",
        choices=("cross_encoder", "qwen3_causal"),
        default="cross_encoder",
    )
    parser.add_argument("--reranker-device", default="cpu")
    parser.add_argument("--reranker-batch-size", type=int, default=8)
    parser.add_argument("--reranker-max-length", type=int, default=1024)
    parser.add_argument("--reranker-top-n", type=int, default=32)
    parser.add_argument("--reranker-trust-remote-code", action="store_true")
    parser.add_argument(
        "--reranker-strategy",
        choices=("replace", "fusion_always", "fusion_adaptive"),
        default="replace",
    )
    parser.add_argument("--fusion-neural-weight", type=float, default=0.55)
    parser.add_argument("--fusion-deterministic-weight", type=float, default=0.25)
    parser.add_argument("--fusion-retrieval-weight", type=float, default=0.20)
    parser.add_argument("--adaptive-maximum-margin", type=float, default=0.12)
    parser.add_argument("--adaptive-maximum-confidence", type=float, default=0.52)
    parser.add_argument("--adaptive-maximum-abstention-confidence", type=float, default=0.18)
    parser.add_argument("--adaptive-minimum-rank-disagreement", type=int, default=6)
    parser.add_argument("--adaptive-minimum-query-tokens", type=int, default=7)
    parser.add_argument(
        "--disable-dense",
        action="store_true",
        help="Evaluate lexical/RRF behavior without loading an embedding model.",
    )
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
    gateway = None
    dense = None
    if not arguments.disable_dense:
        gateway = SentenceTransformerEmbeddingGateway(
            model_name=arguments.model_name,
            model_revision=arguments.model_revision,
            query_prefix=arguments.query_prefix,
            document_prefix=arguments.document_prefix,
            profile_family=arguments.profile_family,
            trust_remote_code=arguments.trust_remote_code,
            cache_folder=arguments.embedding_cache.resolve(),
            device=arguments.device,
            batch_size=arguments.batch_size,
            max_sequence_length=arguments.max_sequence_length,
        )
        dense = DenseKnowledgeIndexService(
            SqliteDenseKnowledgeIndexRepository(connection), gateway
        )
    reranker = None
    sufficiency_gate = EvidenceSufficiencyGate()
    if arguments.reranker_model:
        if not arguments.reranker_revision:
            raise ValueError("--reranker-revision is required with --reranker-model")
        reranker_model = None
        if arguments.reranker_family == "qwen3_causal":
            reranker_model = Qwen3CausalRerankerModel(
                model_name=arguments.reranker_model,
                model_revision=arguments.reranker_revision,
                cache_folder=str(arguments.embedding_cache.resolve()),
                device=arguments.reranker_device,
                max_length=arguments.reranker_max_length,
            )
        neural_reranker = CrossEncoderEvidenceReranker(
            model_name=arguments.reranker_model,
            model_revision=arguments.reranker_revision,
            cache_folder=str(arguments.embedding_cache.resolve()),
            device=arguments.reranker_device,
            batch_size=arguments.reranker_batch_size,
            max_length=arguments.reranker_max_length,
            top_n=arguments.reranker_top_n,
            trust_remote_code=arguments.reranker_trust_remote_code,
            model=reranker_model,
        )
        reranker = neural_reranker
        if arguments.reranker_strategy != "replace":
            reranker = AdaptiveFusionEvidenceReranker(
                neural_reranker,
                policy=AdaptiveRerankPolicy(
                    force=(
                        True
                        if arguments.reranker_strategy == "fusion_always"
                        else None
                    ),
                    maximum_normalized_top_margin=arguments.adaptive_maximum_margin,
                    maximum_deterministic_confidence=(
                        arguments.adaptive_maximum_confidence
                    ),
                    maximum_abstention_confidence=(
                        arguments.adaptive_maximum_abstention_confidence
                    ),
                    minimum_rank_disagreement=(
                        arguments.adaptive_minimum_rank_disagreement
                    ),
                    minimum_complex_query_tokens=(
                        arguments.adaptive_minimum_query_tokens
                    ),
                ),
                weights=RerankFusionWeights(
                    neural=arguments.fusion_neural_weight,
                    deterministic=arguments.fusion_deterministic_weight,
                    retrieval=arguments.fusion_retrieval_weight,
                ),
            )
    repository = SqliteKnowledgeRepository(connection, dense, reranker=reranker)
    scores: list[dict[str, object]] = []
    query_latencies: list[float] = []
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
            measured_hops: list[tuple[object, ...]] = []
            case_latencies: list[float] = []
            for query in queries:
                started = time.perf_counter()
                hits = repository.search_canonical(
                    query,
                    corpus_roles=(role,),
                    limit=int(raw_case["k"]),
                )
                elapsed = time.perf_counter() - started
                measured_hops.append(hits)
                case_latencies.append(elapsed)
                query_latencies.append(elapsed)
            hop_hits = tuple(measured_hops)
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
                "hop_latency_seconds": tuple(case_latencies),
                "total_latency_seconds": sum(case_latencies),
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
                        "rerank_score": hit.rerank_score,
                        "relevance_label": hit.relevance_label,
                        "rerank_reason_codes": hit.rerank_reason_codes,
                    }
                    for hit in unique_hits
                ),
            }
            sufficiency = sufficiency_gate.assess(
                tuple(
                    RerankAssessment(
                        hit.node_id,
                        hit.rerank_score or 0.0,
                        hit.relevance_label or "low_relevance",
                        hit.rerank_reason_codes,
                    )
                    for hit in unique_hits
                )
            )
            payload["evidence_sufficiency_status"] = sufficiency.status
            payload["answer_allowed"] = sufficiency.answer_allowed
            payload["highest_rerank_score"] = sufficiency.highest_score
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
    negative = tuple(score for score in scores if score["mode"] == "no_answer")
    metrics = {
        "positive_case_count": len(positive),
        "no_answer_case_count": sum(score["mode"] == "no_answer" for score in scores),
        "no_answer_accuracy": sum(not bool(score["answer_allowed"]) for score in negative)
        / len(negative),
        "false_abstention_rate": sum(
            not bool(score["answer_allowed"]) for score in positive
        )
        / len(positive),
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
        "query_count": len(query_latencies),
        "mean_query_latency_seconds": sum(query_latencies) / len(query_latencies),
        "p50_query_latency_seconds": _percentile(query_latencies, 0.50),
        "p95_query_latency_seconds": _percentile(query_latencies, 0.95),
        "total_query_latency_seconds": sum(query_latencies),
        "reranker_policy_revision": (
            reranker.policy_revision
            if reranker is not None
            else "deterministic-evidence-reranker/v1"
        ),
        "reranker_runtime": (
            getattr(reranker, "diagnostics_snapshot", {})
            if reranker is not None
            else {}
        ),
    }
    thresholds = {
        "minimum_hit_at_k": 0.90,
        "minimum_macro_source_recall": 0.85,
        "minimum_macro_marker_recall": 0.80,
        "minimum_macro_precision_at_k": 0.20,
        "minimum_mean_reciprocal_rank": 0.60,
        "minimum_multi_hop_evidence_group_coverage": 0.80,
        "minimum_multi_hop_novel_node_ratio": 0.25,
        "minimum_no_answer_accuracy": 1.0,
        "maximum_false_abstention_rate": 0.0,
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
        and metrics["no_answer_accuracy"]
        >= thresholds["minimum_no_answer_accuracy"]
        and metrics["false_abstention_rate"]
        <= thresholds["maximum_false_abstention_rate"]
        and metrics["total_role_leaks"] <= thresholds["maximum_role_leaks"]
    )
    report = {
        "schema_version": "rag-real-scenario-evaluation/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "question_set": str(arguments.questions.resolve()),
        "embedding_profile": (
            None if gateway is None else gateway.profile.profile_revision
        ),
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
