"""Measure baseline and adaptive reranking in one process with paired queries."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from stata_research_agent.application.dense_retrieval import DenseKnowledgeIndexService
from stata_research_agent.application.knowledge_retrieval import CorpusRole
from stata_research_agent.application.retrieval_pipeline import (
    DeterministicEvidenceReranker,
)
from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.interfaces.cross_encoder_reranker import (
    AdaptiveFusionEvidenceReranker,
    AdaptiveRerankPolicy,
    CrossEncoderEvidenceReranker,
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


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--embedding-cache", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--reranker-model", required=True)
    parser.add_argument("--reranker-revision", required=True)
    parser.add_argument("--reranker-device", default="cuda")
    arguments = parser.parse_args()
    if arguments.repeats < 1:
        raise ValueError("repeats must be positive")
    output_root = arguments.output_root.resolve()
    if output_root.exists():
        raise ValueError("paired output root already exists")
    output_root.mkdir(parents=True)

    question_set = json.loads(arguments.questions.resolve().read_text(encoding="utf-8"))
    queries: list[tuple[str, CorpusRole, int]] = []
    for case in question_set["cases"]:
        raw_queries = (
            case["hops"]
            if case["mode"] == "multi_hop"
            else (case.get("retrieval_query", case["question"]),)
        )
        queries.extend(
            (str(query), CorpusRole(str(case["corpus_role"])), int(case["k"]))
            for query in raw_queries
        )

    database = WorkspaceDatabase(
        arguments.workspace_root.resolve(), WorkspaceId(arguments.workspace_id)
    )
    connection = database.open(writable=False)
    gateway = SentenceTransformerEmbeddingGateway(
        model_name="intfloat/multilingual-e5-small",
        model_revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
        query_prefix="query: ",
        document_prefix="passage: ",
        profile_family="e5-prefix-v1",
        cache_folder=arguments.embedding_cache.resolve(),
        device=arguments.device,
        batch_size=64,
    )
    dense = DenseKnowledgeIndexService(
        SqliteDenseKnowledgeIndexRepository(connection), gateway
    )
    baseline = SqliteKnowledgeRepository(
        connection, dense, reranker=DeterministicEvidenceReranker()
    )
    neural = CrossEncoderEvidenceReranker(
        model_name=arguments.reranker_model,
        model_revision=arguments.reranker_revision,
        cache_folder=str(arguments.embedding_cache.resolve()),
        device=arguments.reranker_device,
        batch_size=32,
        max_length=512,
        top_n=32,
    )
    adaptive = AdaptiveFusionEvidenceReranker(
        neural,
        policy=AdaptiveRerankPolicy(minimum_rank_disagreement=6),
        weights=RerankFusionWeights(),
    )
    candidate = SqliteKnowledgeRepository(connection, dense, reranker=adaptive)

    baseline_latencies: list[float] = []
    candidate_latencies: list[float] = []
    pairs: list[dict[str, object]] = []
    try:
        warm_query, warm_role, warm_limit = queries[0]
        baseline.search_canonical(warm_query, corpus_roles=(warm_role,), limit=warm_limit)
        candidate.search_canonical(warm_query, corpus_roles=(warm_role,), limit=warm_limit)
        for repeat in range(arguments.repeats):
            for ordinal, (query, role, limit) in enumerate(queries):
                order = ("baseline", "candidate")
                if (repeat + ordinal) % 2:
                    order = tuple(reversed(order))
                observed: dict[str, float] = {}
                for variant in order:
                    repository = baseline if variant == "baseline" else candidate
                    started = time.perf_counter()
                    repository.search_canonical(query, corpus_roles=(role,), limit=limit)
                    observed[variant] = time.perf_counter() - started
                baseline_latencies.append(observed["baseline"])
                candidate_latencies.append(observed["candidate"])
                pairs.append(
                    {
                        "repeat": repeat + 1,
                        "query_ordinal": ordinal + 1,
                        "baseline_seconds": observed["baseline"],
                        "candidate_seconds": observed["candidate"],
                        "delta_seconds": observed["candidate"] - observed["baseline"],
                    }
                )
    finally:
        connection.close()

    deltas = [
        candidate_value - baseline_value
        for baseline_value, candidate_value in zip(
            baseline_latencies, candidate_latencies, strict=True
        )
    ]
    report = {
        "schema_version": "paired-reranker-latency/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "query_count": len(queries),
        "repeats": arguments.repeats,
        "measurement_count_per_variant": len(baseline_latencies),
        "baseline": {
            "mean_seconds": statistics.fmean(baseline_latencies),
            "median_seconds": statistics.median(baseline_latencies),
            "p95_seconds": _percentile(baseline_latencies, 0.95),
        },
        "candidate": {
            "mean_seconds": statistics.fmean(candidate_latencies),
            "median_seconds": statistics.median(candidate_latencies),
            "p95_seconds": _percentile(candidate_latencies, 0.95),
            "policy_revision": adaptive.policy_revision,
            "runtime": adaptive.diagnostics_snapshot,
        },
        "paired_delta": {
            "mean_seconds": statistics.fmean(deltas),
            "median_seconds": statistics.median(deltas),
            "p95_seconds": _percentile(deltas, 0.95),
        },
        "pairs": pairs,
    }
    report_path = output_root / "paired-latency-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "pairs"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
