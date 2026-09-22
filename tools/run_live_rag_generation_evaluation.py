"""Run a live DeepSeek grounded-generation evaluation over the real dense corpus."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from run_live_product_e2e import _deepseek_secret

from stata_research_agent.application.dense_retrieval import DenseKnowledgeIndexService
from stata_research_agent.application.knowledge_retrieval import (
    CorpusRole,
    KnowledgeRetrievalHit,
)
from stata_research_agent.application.rag_evaluation import (
    RagGroundedClaim,
    RagGroundingCase,
    evaluate_grounded_answer,
)
from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.interfaces.openai_compatible_transport import (
    OpenAICompatibleChatTransport,
)
from stata_research_agent.interfaces.sentence_transformer_embedding import (
    SentenceTransformerEmbeddingGateway,
)
from stata_research_agent.persistence.dense_knowledge_store import (
    SqliteDenseKnowledgeIndexRepository,
)
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase

ENDPOINT = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
CLAIM_MARKERS = {
    "twfe": ("weighted average",),
    "group_time": ("doubly robust", "doubly-robust"),
    "imputation": ("imputation",),
}


def _deduplicate(hits: list[KnowledgeRetrievalHit]) -> tuple[KnowledgeRetrievalHit, ...]:
    return tuple({hit.node_id: hit for hit in hits}.values())


def _request(
    evidence_hits: tuple[KnowledgeRetrievalHit, ...],
    style_hits: tuple[KnowledgeRetrievalHit, ...],
) -> str:
    context: list[dict[str, object]] = [
        {
            "kind": "user_message",
            "content": (
                "用中文解释三个问题：传统错位处理时点下 TWFE 的分解风险；"
                "Callaway-Sant'Anna 的 group-time 方法；Borusyak-Jaravel-Spiess 的"
                "imputation 方法。只能使用给定 evidence nodes。返回三个 claim，ID 必须"
                "依次为 twfe、group_time、imputation，每个 claim 必须列出真正支持它的"
                "node_id。style nodes 只能学习行文节奏，禁止作为事实引用。"
            ),
        }
    ]
    context.extend(
        {
            "kind": "retrieved_evidence",
            "node_id": hit.node_id,
            "source_locator": hit.source_locator,
            "corpus_role": hit.corpus_role,
            "page_start": hit.page_start,
            "content": hit.content,
        }
        for hit in evidence_hits
    )
    context.extend(
        {
            "kind": "style_exemplar",
            "node_id": hit.node_id,
            "source_locator": hit.source_locator,
            "corpus_role": hit.corpus_role,
            "content": hit.content,
        }
        for hit in style_hits
    )
    return json.dumps(
        {
            "model": MODEL,
            "normalized_input": {
                "system": {
                    "revision": "live-rag-grounding-v1",
                    "content": (
                        "You are a traceable research writer. Use only supplied evidence nodes "
                        "for factual claims. Put in the outer response's text field a serialized "
                        "JSON object with keys answer and claims. claims must be an array of "
                        "objects with claim_id, text, and cited_node_ids. Cite the smallest set "
                        "of exact node IDs that support each claim. Do not cite style exemplars."
                    ),
                },
                "main_skill": {
                    "name": "grounded-literature-synthesis",
                    "revision": "live-rag-grounding-v1",
                    "content": (
                        "The three required claim IDs are twfe, group_time, and imputation. "
                        "Do not add uncited factual claims or outside knowledge."
                    ),
                },
                "context": context,
                "tools": {"catalog_revision": "none", "schemas": []},
                "runtime": {"remaining_step_budget": 1, "remaining_tool_budget": 0},
            },
            "policy": {"temperature": 0, "max_tokens": 1200},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


async def _run(arguments: argparse.Namespace) -> dict[str, object]:
    database = WorkspaceDatabase(
        arguments.workspace_root.resolve(), WorkspaceId(arguments.workspace_id)
    )
    connection = database.open(writable=True)
    gateway = SentenceTransformerEmbeddingGateway(
        cache_folder=arguments.embedding_cache.resolve()
    )
    dense = DenseKnowledgeIndexService(
        SqliteDenseKnowledgeIndexRepository(connection), gateway
    )
    repository = SqliteKnowledgeRepository(connection, dense)
    try:
        evidence: list[KnowledgeRetrievalHit] = []
        for query in (
            "TWFE weighted average two group two period treatment timing",
            "group time average treatment effects doubly robust estimands",
            "robust efficient imputation estimator heterogeneous treatment effects",
        ):
            evidence.extend(
                repository.search_canonical(
                    query,
                    corpus_roles=(CorpusRole.LITERATURE_EVIDENCE,),
                    limit=6,
                )
            )
        evidence_hits = _deduplicate(evidence)
        for claim_id, markers in CLAIM_MARKERS.items():
            if not any(
                any(marker.casefold() in hit.content.casefold() for marker in markers)
                for hit in evidence_hits
            ):
                raise RuntimeError(f"retrieval omitted required evidence for {claim_id}")
        style_hits = tuple(
            repository.search_canonical(
                "organizing framework empirical practice clear methodological guidance",
                corpus_roles=(CorpusRole.STYLE_EXEMPLAR,),
                limit=4,
            )
        )
    finally:
        connection.close()

    secret = _deepseek_secret()
    try:
        response = await OpenAICompatibleChatTransport(timeout_seconds=180).send(
            endpoint=ENDPOINT,
            request_json=_request(evidence_hits, style_hits),
            credential=secret,
        )
    finally:
        del secret
    serialized = response.output.get("text")
    if not isinstance(serialized, str):
        raise RuntimeError("live model omitted grounded response text")
    generated = _mapping(json.loads(serialized), "generated grounded response")
    answer = generated.get("answer")
    raw_claims = generated.get("claims")
    if not isinstance(answer, str) or not isinstance(raw_claims, list):
        raise RuntimeError("live model returned an invalid grounded response schema")
    claim_rows = {
        str(_mapping(row, "claim").get("claim_id")): _mapping(row, "claim")
        for row in raw_claims
    }
    claims: list[RagGroundedClaim] = []
    for claim_id, markers in CLAIM_MARKERS.items():
        row = claim_rows.get(claim_id)
        if row is None:
            raise RuntimeError(f"live model omitted required claim {claim_id}")
        citations = row.get("cited_node_ids")
        text = row.get("text")
        if not isinstance(citations, list) or not all(
            isinstance(value, str) for value in citations
        ):
            raise RuntimeError(f"live model returned invalid citations for {claim_id}")
        if not isinstance(text, str):
            raise RuntimeError(f"live model returned invalid text for {claim_id}")
        claims.append(
            RagGroundedClaim(
                claim_id,
                text,
                tuple(citations),
                markers,
            )
        )
    all_nodes = {hit.node_id: hit for hit in (*evidence_hits, *style_hits)}
    score = evaluate_grounded_answer(
        RagGroundingCase(
            "live-deepseek-did-grounding",
            answer,
            tuple(claims),
            maximum_style_copy_run=16,
        ),
        all_nodes,
        style_nodes=style_hits,
    )
    return {
        "schema_version": "live-rag-generation-evaluation/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "deepseek",
        "model": MODEL,
        "embedding_profile": asdict(gateway.profile),
        "evidence_node_count": len(evidence_hits),
        "style_node_count": len(style_hits),
        "retrieved_evidence": [
            {
                "node_id": hit.node_id,
                "source_locator": hit.source_locator,
                "page_start": hit.page_start,
                "dense_rank": hit.dense_rank,
                "dense_score": hit.dense_score,
            }
            for hit in evidence_hits
        ],
        "generated": generated,
        "grounding_score": asdict(score),
        "usage": {
            "kind": response.usage_kind,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cached_input_tokens": response.cached_input_tokens,
            "uncached_input_tokens": response.uncached_input_tokens,
        },
        "accepted": score.passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--workspace-id", default="ws_real_rag_benchmark_v1")
    parser.add_argument("--embedding-cache", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    output_root = arguments.output_root.resolve()
    if output_root.exists():
        raise ValueError("evaluation output root already exists")
    output_root.mkdir(parents=True)
    report = asyncio.run(_run(arguments))
    report_path = output_root / "live-rag-generation-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "accepted": report["accepted"],
                "grounding_score": report["grounding_score"],
                "usage": report["usage"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
