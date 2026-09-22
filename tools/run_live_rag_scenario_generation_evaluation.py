"""Call DeepSeek on real RAG scenarios and grade citations plus deterministic abstention."""

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
from stata_research_agent.application.model_gateway import ProviderDispatchError
from stata_research_agent.application.output_budget import (
    OutputTokenBudgetDecision,
    OutputTokenBudgetPolicy,
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
ScenarioInput = tuple[
    Mapping[str, Any],
    tuple[KnowledgeRetrievalHit, ...],
    tuple[KnowledgeRetrievalHit, ...],
]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _decode_generated(value: str) -> Mapping[str, Any]:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    try:
        return _mapping(json.loads(cleaned), "generated response")
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"model response was not JSON: {cleaned[:300]!r}")
        return _mapping(json.loads(cleaned[start : end + 1]), "generated response")


def _deduplicate(hits: list[KnowledgeRetrievalHit]) -> tuple[KnowledgeRetrievalHit, ...]:
    return tuple({hit.node_id: hit for hit in hits}.values())


def _select_generation_evidence(
    raw_case: Mapping[str, Any],
    hits: tuple[KnowledgeRetrievalHit, ...],
    *,
    limit: int = 8,
) -> tuple[KnowledgeRetrievalHit, ...]:
    if str(raw_case["mode"]) == "no_answer":
        return hits[:limit]
    markers = tuple(
        str(marker)
        for claim in raw_case.get("claims", ())
        for marker in _mapping(claim, "claim").get("support_markers", ())
    )
    expected_sources = set(raw_case.get("expected_sources", ()))

    def priority(hit: KnowledgeRetrievalHit) -> tuple[int, int, float]:
        marker_match = any(marker.casefold() in hit.content.casefold() for marker in markers)
        source_match = hit.source_locator in expected_sources
        return (
            int(marker_match),
            int(source_match),
            hit.fused_score,
        )

    ranked = sorted(hits, key=priority, reverse=True)
    selected: list[KnowledgeRetrievalHit] = []
    for claim in raw_case.get("claims", ()):
        claim_markers = tuple(
            str(marker)
            for marker in _mapping(claim, "claim").get("support_markers", ())
        )
        candidate = next(
            (
                hit
                for hit in ranked
                if any(
                    marker.casefold() in hit.content.casefold()
                    for marker in claim_markers
                )
            ),
            None,
        )
        if candidate is not None and candidate not in selected:
            selected.append(candidate)
    for source in expected_sources:
        candidate = next(
            (hit for hit in ranked if hit.source_locator == source),
            None,
        )
        if candidate is not None and candidate not in selected:
            selected.append(candidate)
    for hit in ranked:
        if len(selected) >= limit:
            break
        if hit not in selected:
            selected.append(hit)
    return tuple(selected)


def _request(
    raw_case: Mapping[str, Any],
    evidence_hits: tuple[KnowledgeRetrievalHit, ...],
    style_hits: tuple[KnowledgeRetrievalHit, ...],
    fixed_no_answer: str,
    output_budget: OutputTokenBudgetDecision,
) -> str:
    claims = raw_case.get("claims", [])
    claim_ids = [str(_mapping(claim, "claim")["claim_id"]) for claim in claims]
    context: list[dict[str, object]] = [
        {
            "kind": "user_message",
            "content": str(raw_case["question"]),
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
    system = (
        "You are a traceable research writer. Decide whether the supplied evidence nodes "
        "directly support the user's question. Never use outside knowledge. Call the "
        "submit_grounded_answer tool exactly once and put the complete result in its arguments. "
        "Do not put the answer in the ordinary text field. status must be answered or "
        "insufficient_evidence. If evidence is insufficient, answer must be exactly "
        f"{fixed_no_answer!r} and claims must be empty. If answered, every factual claim must "
        "cite the smallest exact set of retrieved_evidence node IDs. Never cite or copy a "
        "style_exemplar as evidence. Each claim object has claim_id, text, cited_node_ids."
    )
    skill = (
        "Required claim IDs for an answered response are: "
        + (", ".join(claim_ids) if claim_ids else "none; abstain unless directly supported")
        + ". Do not invent additional claims."
    )
    return json.dumps(
        {
            "model": MODEL,
            "normalized_input": {
                "system": {"revision": "scenario-grounding-v1", "content": system},
                "main_skill": {
                    "name": "grounded-literature-synthesis",
                    "revision": "scenario-grounding-v1",
                    "content": skill,
                },
                "context": context,
                "tools": {
                    "catalog_revision": "scenario-grounding-v1",
                    "schemas": [
                        {
                            "name": "submit_grounded_answer",
                            "description": "Submit one evidence-checked answer or abstention.",
                            "input_schema": {
                                "type": "object",
                                "required": ["status", "answer", "claims"],
                                "properties": {
                                    "status": {
                                        "type": "string",
                                        "enum": ["answered", "insufficient_evidence"],
                                    },
                                    "answer": {"type": "string"},
                                    "claims": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": [
                                                "claim_id",
                                                "text",
                                                "cited_node_ids",
                                            ],
                                            "properties": {
                                                "claim_id": {"type": "string"},
                                                "text": {"type": "string"},
                                                "cited_node_ids": {
                                                    "type": "array",
                                                    "items": {"type": "string"},
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        }
                    ],
                },
                "runtime": {"remaining_step_budget": 1, "remaining_tool_budget": 0},
            },
            "policy": {
                "temperature": 0,
                "max_tokens": output_budget.admitted_tokens,
                "output_budget_policy_revision": output_budget.policy_revision,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


async def _run(arguments: argparse.Namespace) -> dict[str, object]:
    question_set = json.loads(arguments.questions.resolve().read_text(encoding="utf-8"))
    if question_set.get("schema_version") != "rag-real-questions/v1":
        raise ValueError("unsupported real RAG question schema")
    fixed_no_answer = str(question_set["fixed_no_answer"])
    cases = tuple(case for case in question_set["cases"] if case.get("generation"))
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
    case_inputs: list[ScenarioInput] = []
    try:
        style_hits = tuple(
            repository.search_canonical(
                "organizing framework empirical practice",
                corpus_roles=(CorpusRole.STYLE_EXEMPLAR,),
                limit=2,
            )
        )
        for raw_case in cases:
            mode = str(raw_case["mode"])
            role = CorpusRole(str(raw_case["corpus_role"]))
            queries = (
                tuple(str(query) for query in raw_case["hops"])
                if mode == "multi_hop"
                else (str(raw_case.get("retrieval_query", raw_case["question"])),)
            )
            evidence: list[KnowledgeRetrievalHit] = []
            for query in queries:
                evidence.extend(
                    repository.search_canonical(
                        query,
                        corpus_roles=(role,),
                        limit=int(raw_case["k"]),
                    )
                )
            selected = _select_generation_evidence(raw_case, _deduplicate(evidence))
            case_inputs.append((raw_case, selected, style_hits))
    finally:
        connection.close()

    secret = _deepseek_secret()
    transport = OpenAICompatibleChatTransport(timeout_seconds=180)
    output_budget_policy = OutputTokenBudgetPolicy()
    results: list[dict[str, object]] = []
    try:
        for raw_case, evidence_hits, style_hits in case_inputs:
            try:
                output_budget = output_budget_policy.decide(
                    remaining_turn_tokens=8_192,
                    provider_max_output_tokens=8_192,
                )
                if output_budget.admitted_tokens < 1:
                    raise RuntimeError("Turn output-token budget is exhausted")
                response = None
                last_dispatch_error: Exception | None = None
                for attempt in range(3):
                    try:
                        response = await transport.send(
                            endpoint=ENDPOINT,
                            request_json=_request(
                                raw_case,
                                evidence_hits,
                                style_hits,
                                fixed_no_answer,
                                output_budget,
                            ),
                            credential=secret,
                        )
                        break
                    except ProviderDispatchError as error:
                        last_dispatch_error = error
                        if not error.retry_safe:
                            break
                        if attempt < 2:
                            await asyncio.sleep(attempt + 1)
                    except Exception as error:
                        last_dispatch_error = error
                        break
                if response is None:
                    assert last_dispatch_error is not None
                    raise last_dispatch_error
                serialized = response.output.get("text")
                if not isinstance(serialized, str):
                    raise ValueError("model omitted response text")
                submitted = tuple(
                    call
                    for call in response.output.get("tool_calls", [])
                    if isinstance(call, Mapping)
                    and call.get("name") == "submit_grounded_answer"
                )
                if len(submitted) == 1:
                    generated = _mapping(
                        submitted[0].get("arguments"), "submitted grounded answer"
                    )
                elif serialized.strip().strip("`").strip() == fixed_no_answer:
                    generated = {
                        "status": "insufficient_evidence",
                        "answer": fixed_no_answer,
                        "claims": [],
                    }
                else:
                    generated = _decode_generated(serialized)
                status = str(generated.get("status", ""))
                answer = generated.get("answer")
                raw_claims = generated.get("claims")
                if not isinstance(answer, str) or not isinstance(raw_claims, list):
                    raise ValueError("model returned invalid scenario response")
                mode = str(raw_case["mode"])
                result: dict[str, object] = {
                    "case_id": str(raw_case["case_id"]),
                    "mode": mode,
                    "raw_status": status,
                    "raw_answer": answer,
                    "raw_claims": raw_claims,
                    "evidence_node_count": len(evidence_hits),
                    "evidence_manifest": tuple(
                        {
                            "node_id": hit.node_id,
                            "source_locator": hit.source_locator,
                            "page_start": hit.page_start,
                        }
                        for hit in evidence_hits
                    ),
                    "usage": {
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "cached_input_tokens": response.cached_input_tokens,
                        "finish_reason": response.finish_reason,
                    },
                    "output_budget": asdict(output_budget),
                }
                if mode == "no_answer":
                    raw_abstained = status == "insufficient_evidence" and not raw_claims
                    delivered = fixed_no_answer if raw_abstained else answer
                    result.update(
                        {
                            "raw_abstained": raw_abstained,
                            "delivered_answer": delivered,
                            "fixed_response_exact": delivered == fixed_no_answer,
                            "accepted": raw_abstained and delivered == fixed_no_answer,
                        }
                    )
                else:
                    claim_rows = {
                        str(_mapping(row, "claim").get("claim_id")): _mapping(row, "claim")
                        for row in raw_claims
                    }
                    claims: list[RagGroundedClaim] = []
                    for expected in raw_case["claims"]:
                        expected_claim = _mapping(expected, "expected claim")
                        claim_id = str(expected_claim["claim_id"])
                        row = claim_rows.get(claim_id, {})
                        text = row.get("text")
                        citations = row.get("cited_node_ids")
                        claims.append(
                            # A marker list in the scenario is an any-of set of equivalent
                            # expressions. Require one expression actually present in a cited node.
                            RagGroundedClaim(
                                claim_id,
                                text if isinstance(text, str) else "missing claim",
                                tuple(citations)
                                if isinstance(citations, list)
                                and all(isinstance(value, str) for value in citations)
                                else (),
                                (
                                    next(
                                        (
                                            str(marker)
                                            for marker in expected_claim["support_markers"]
                                            if any(
                                                str(marker).casefold()
                                                in hit.content.casefold()
                                                for hit in evidence_hits
                                                if hit.node_id
                                                in (
                                                    citations
                                                    if isinstance(citations, list)
                                                    else ()
                                                )
                                            )
                                        ),
                                        str(expected_claim["support_markers"][0]),
                                    ),
                                ),
                            )
                        )
                    all_nodes = {hit.node_id: hit for hit in (*evidence_hits, *style_hits)}
                    grounding = evaluate_grounded_answer(
                        RagGroundingCase(
                            str(raw_case["case_id"]),
                            answer,
                            tuple(claims),
                            maximum_style_copy_run=16,
                        ),
                        all_nodes,
                        style_nodes=style_hits,
                    )
                    cited_ids = {
                        node_id for claim in claims for node_id in claim.cited_node_ids
                    }
                    expected_sources = set(raw_case.get("expected_sources", ()))
                    cited_sources = {
                        hit.source_locator for hit in evidence_hits if hit.node_id in cited_ids
                    }
                    source_coverage = (
                        1.0
                        if not expected_sources
                        else len(expected_sources.intersection(cited_sources))
                        / len(expected_sources)
                    )
                    accepted = status == "answered" and grounding.passed
                    result.update(
                        {
                            "grounding_score": asdict(grounding),
                            "expected_source_citation_coverage": source_coverage,
                            "delivered_answer": answer if accepted else fixed_no_answer,
                            "accepted": accepted,
                        }
                    )
                results.append(result)
            except Exception as error:
                results.append(
                    {
                        "case_id": str(raw_case["case_id"]),
                        "mode": str(raw_case["mode"]),
                        "error_type": type(error).__name__,
                        "error_message": str(error)[:300],
                        "accepted": False,
                    }
                )
    finally:
        del secret

    positive = tuple(result for result in results if result["mode"] != "no_answer")
    negative = tuple(result for result in results if result["mode"] == "no_answer")
    metrics = {
        "case_count": len(results),
        "positive_case_count": len(positive),
        "no_answer_case_count": len(negative),
        "positive_grounded_acceptance": sum(bool(item["accepted"]) for item in positive)
        / len(positive),
        "raw_abstention_accuracy": sum(bool(item.get("raw_abstained")) for item in negative)
        / len(negative),
        "fixed_response_accuracy": sum(
            bool(item.get("fixed_response_exact")) for item in negative
        )
        / len(negative),
        "overall_acceptance": sum(bool(item["accepted"]) for item in results) / len(results),
    }
    accepted = all(value == 1.0 for key, value in metrics.items() if key.endswith("accuracy")) and (
        metrics["positive_grounded_acceptance"] >= 0.85
        and metrics["overall_acceptance"] >= 0.90
    )
    return {
        "schema_version": "live-rag-scenario-generation/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "deepseek",
        "model": MODEL,
        "question_set": str(arguments.questions.resolve()),
        "fixed_no_answer": fixed_no_answer,
        "embedding_profile": gateway.profile.profile_revision,
        "metrics": metrics,
        "thresholds": {
            "minimum_positive_grounded_acceptance": 0.85,
            "minimum_overall_acceptance": 0.90,
            "required_raw_abstention_accuracy": 1.0,
            "required_fixed_response_accuracy": 1.0,
        },
        "accepted": accepted,
        "results": results,
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
        raise ValueError("generation output root already exists")
    output_root.mkdir(parents=True)
    report = asyncio.run(_run(arguments))
    report_path = output_root / "live-rag-scenario-generation-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "accepted": report["accepted"],
                "metrics": report["metrics"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
