"""Evaluate autonomous multi-hop retrieval planning against the real Workspace corpus."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from run_live_product_e2e import _deepseek_secret

from stata_research_agent.application.dense_retrieval import DenseKnowledgeIndexService
from stata_research_agent.application.knowledge_retrieval import (
    ConcludeKnowledgeRetrievalCommand,
    ContinueKnowledgeRetrievalCommand,
    CorpusRole,
    KnowledgeRetrievalHit,
    RetrievalMode,
    StartKnowledgeRetrievalCommand,
)
from stata_research_agent.application.model_gateway import ProviderDispatchError
from stata_research_agent.application.output_budget import OutputTokenBudgetPolicy
from stata_research_agent.application.rag_evaluation import RagTraceHop
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
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
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator

ENDPOINT = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _tool_calls(output: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    calls = output.get("tool_calls", ())
    if not isinstance(calls, list):
        raise ValueError("model tool_calls must be an array")
    return tuple(_mapping(call, "tool call") for call in calls)


def _decode_json_object(raw: str, label: str) -> Mapping[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    try:
        return _mapping(json.loads(cleaned), label)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"{label} is not a JSON object")
        return _mapping(json.loads(cleaned[start : end + 1]), label)


def _request(
    raw_case: Mapping[str, Any],
    hop_records: tuple[Mapping[str, Any], ...],
    fixed_no_answer: str,
    *,
    max_output_tokens: int,
) -> str:
    system = (
        "You are an autonomous, traceable literature-retrieval agent. Use only supplied "
        "Workspace evidence; never answer a factual research question from memory. At each "
        "step call exactly one registered tool. Start by calling search_knowledge. For a "
        "complex question, create your own focused public subquestion and a concise English "
        "retrieval query. After every Hop, inspect the evidence and unresolved gaps. Continue "
        "the same retrieval_session_id with a materially different query when another source "
        "or concept is needed. Set conclude_session=true on the final useful search. Do not "
        "repeat a query after no_new_evidence. Once session_status is not open, do not search "
        "again: answer from supported nodes or abstain. Before answering, verify that every "
        "central claim is directly stated in at least one returned node; if it is only an "
        "inference, run a narrower query for explicit support. When evidence is sufficient, call "
        "submit_grounded_answer and cite only exact knowledge_node_ids returned by search. "
        "If the corpus cannot directly support the requested answer, submit status "
        f"insufficient_evidence, answer exactly {fixed_no_answer!r}, and an empty claims array."
    )
    context: list[dict[str, object]] = [
        {"kind": "user_message", "content": str(raw_case["question"])}
    ]
    context.extend(dict(record) for record in hop_records)
    return json.dumps(
        {
            "model": MODEL,
            "normalized_input": {
                "system": {"revision": "agentic-rag-eval-v1", "content": system},
                "main_skill": {
                    "name": "agentic-grounded-retrieval",
                    "revision": "agentic-rag-eval-v1",
                    "content": (
                        "Research-method choice remains open. Search until the user's actual "
                        "question is supported or the bounded corpus is exhausted."
                    ),
                },
                "context": context,
                "tools": {
                    "catalog_revision": "agentic-rag-eval-v1",
                    "schemas": [
                        {
                            "name": "search_knowledge",
                            "description": "Start or continue one auditable retrieval session.",
                            "input_schema": {
                                "type": "object",
                                "required": ["query", "public_subquestion"],
                                "properties": {
                                    "query": {"type": "string"},
                                    "public_subquestion": {"type": "string"},
                                    "retrieval_session_id": {"type": "string"},
                                    "unresolved_items": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "conclude_session": {"type": "boolean"},
                                },
                            },
                        },
                        {
                            "name": "submit_grounded_answer",
                            "description": "Submit the final evidence-grounded answer.",
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
                                            "required": ["text", "cited_node_ids"],
                                            "properties": {
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
                        },
                    ],
                },
                "runtime": {"remaining_step_budget": 8, "remaining_tool_budget": 8},
            },
            "policy": {"temperature": 0, "max_tokens": max_output_tokens},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


async def _invoke(
    transport: OpenAICompatibleChatTransport,
    secret: str,
    request_json: str,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return await transport.send(
                endpoint=ENDPOINT,
                request_json=request_json,
                credential=secret,
            )
        except ProviderDispatchError as error:
            last_error = error
            if not error.retry_safe:
                break
            if attempt < 2:
                await asyncio.sleep(attempt + 1)
    assert last_error is not None
    raise last_error


def _support_request(
    claims: list[object],
    evidence_nodes: Mapping[str, KnowledgeRetrievalHit],
    *,
    max_output_tokens: int,
) -> str:
    claim_packets: list[dict[str, object]] = []
    for ordinal, raw_claim in enumerate(claims, start=1):
        claim = _mapping(raw_claim, "answer claim")
        raw_ids = claim.get("cited_node_ids", ())
        cited_ids = raw_ids if isinstance(raw_ids, list) else []
        claim_packets.append(
            {
                "claim_ordinal": ordinal,
                "claim_text": str(claim.get("text", "")),
                "cited_nodes": [
                    {
                        "knowledge_node_id": node_id,
                        "source_locator": evidence_nodes[node_id].source_locator,
                        "content": evidence_nodes[node_id].content,
                    }
                    for node_id in cited_ids
                    if isinstance(node_id, str) and node_id in evidence_nodes
                ],
            }
        )
    system = (
        "You are a strict citation-support evaluator. Judge only whether each cited node set "
        "semantically supports its claim. Translation, paraphrase, synthesis, and equivalent "
        "terminology are allowed; verbatim word overlap is not required. Return supported only "
        "when the cited text supports the full material claim without outside knowledge. Use "
        "partial when only part is supported or extra quantitative/causal detail is added, and "
        "unsupported when the citation is irrelevant or contradicts the claim. Call "
        "submit_support_evaluation exactly once."
    )
    return json.dumps(
        {
            "model": MODEL,
            "normalized_input": {
                "system": {"revision": "citation-support-v1", "content": system},
                "main_skill": {"name": "citation-support", "content": ""},
                "context": [{"kind": "claim_citation_packets", "content": claim_packets}],
                "tools": {
                    "schemas": [
                        {
                            "name": "submit_support_evaluation",
                            "input_schema": {
                                "type": "object",
                                "required": ["assessments"],
                                "properties": {
                                    "assessments": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": [
                                                "claim_ordinal",
                                                "verdict",
                                                "reason",
                                            ],
                                            "properties": {
                                                "claim_ordinal": {"type": "integer"},
                                                "verdict": {
                                                    "type": "string",
                                                    "enum": [
                                                        "supported",
                                                        "partial",
                                                        "unsupported",
                                                    ],
                                                },
                                                "reason": {"type": "string"},
                                            },
                                        },
                                    }
                                },
                            },
                        }
                    ]
                },
                "runtime": {"remaining_step_budget": 1, "remaining_tool_budget": 0},
            },
            "policy": {"temperature": 0, "max_tokens": max_output_tokens},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


async def _evaluate_semantic_support(
    transport: OpenAICompatibleChatTransport,
    secret: str,
    claims: list[object],
    evidence_nodes: Mapping[str, KnowledgeRetrievalHit],
    *,
    max_output_tokens: int,
) -> tuple[bool, tuple[Mapping[str, Any], ...]]:
    response = await _invoke(
        transport,
        secret,
        _support_request(
            claims,
            evidence_nodes,
            max_output_tokens=max_output_tokens,
        ),
    )
    calls = tuple(
        call
        for call in _tool_calls(response.output)
        if str(call.get("name", "")) == "submit_support_evaluation"
    )
    if len(calls) != 1:
        raise ValueError("citation evaluator omitted its structured verdict")
    arguments = _mapping(calls[0].get("arguments"), "support evaluation")
    raw_assessments = arguments.get("assessments")
    if not isinstance(raw_assessments, list):
        raise ValueError("citation evaluator assessments must be an array")
    assessments = tuple(
        _mapping(assessment, "support assessment") for assessment in raw_assessments
    )
    ordinals = {int(row.get("claim_ordinal", 0)) for row in assessments}
    complete = ordinals == set(range(1, len(claims) + 1))
    supported = complete and all(row.get("verdict") == "supported" for row in assessments)
    return supported, assessments


def _revision_request(
    raw_case: Mapping[str, Any],
    previous_answer: Mapping[str, Any],
    assessments: tuple[Mapping[str, Any], ...],
    evidence_nodes: Mapping[str, KnowledgeRetrievalHit],
    *,
    max_output_tokens: int,
) -> str:
    cited_ids = {
        str(node_id)
        for claim in previous_answer.get("claims", ())
        if isinstance(claim, Mapping)
        for node_id in claim.get("cited_node_ids", ())
        if isinstance(node_id, str) and node_id in evidence_nodes
    }
    system = (
        "Revise a grounded research answer after citation evaluation. Preserve supported "
        "content, but remove or narrow every partial/unsupported claim so that every remaining "
        "claim is fully supported by its cited nodes. Translation and paraphrase are allowed. "
        "Do not introduce outside facts or new node IDs. Call submit_grounded_answer exactly "
        "once with the complete revised answer."
    )
    context = [
        {"kind": "user_question", "content": str(raw_case["question"])},
        {"kind": "previous_answer", "content": dict(previous_answer)},
        {"kind": "citation_evaluation", "content": list(assessments)},
        {
            "kind": "allowed_evidence_nodes",
            "content": [
                {
                    "knowledge_node_id": node_id,
                    "source_locator": evidence_nodes[node_id].source_locator,
                    "content": evidence_nodes[node_id].content,
                }
                for node_id in sorted(cited_ids)
            ],
        },
    ]
    return json.dumps(
        {
            "model": MODEL,
            "normalized_input": {
                "system": {"revision": "citation-revision-v1", "content": system},
                "main_skill": {"name": "grounded-answer-revision", "content": ""},
                "context": context,
                "tools": {
                    "schemas": [
                        {
                            "name": "submit_grounded_answer",
                            "input_schema": {
                                "type": "object",
                                "required": ["status", "answer", "claims"],
                                "properties": {
                                    "status": {"type": "string", "enum": ["answered"]},
                                    "answer": {"type": "string"},
                                    "claims": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": ["text", "cited_node_ids"],
                                            "properties": {
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
                    ]
                },
                "runtime": {"remaining_step_budget": 1, "remaining_tool_budget": 0},
            },
            "policy": {"temperature": 0, "max_tokens": max_output_tokens},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


async def _revise_grounded_answer(
    transport: OpenAICompatibleChatTransport,
    secret: str,
    raw_case: Mapping[str, Any],
    previous_answer: Mapping[str, Any],
    assessments: tuple[Mapping[str, Any], ...],
    evidence_nodes: Mapping[str, KnowledgeRetrievalHit],
    *,
    max_output_tokens: int,
) -> Mapping[str, Any]:
    response = await _invoke(
        transport,
        secret,
        _revision_request(
            raw_case,
            previous_answer,
            assessments,
            evidence_nodes,
            max_output_tokens=max_output_tokens,
        ),
    )
    calls = tuple(
        call
        for call in _tool_calls(response.output)
        if str(call.get("name", "")) == "submit_grounded_answer"
    )
    if len(calls) != 1:
        text = response.output.get("text")
        if isinstance(text, str) and text.strip():
            return _decode_json_object(text, "revised grounded answer")
        raise ValueError("answer revision omitted its structured submission")
    return _mapping(calls[0].get("arguments"), "revised grounded answer")


def _hit_payload(hit: KnowledgeRetrievalHit) -> dict[str, object]:
    return {
        "knowledge_node_id": hit.node_id,
        "source_locator": hit.source_locator,
        "corpus_role": hit.corpus_role,
        "page_start": hit.page_start,
        "content": hit.content,
    }


async def _run(arguments: argparse.Namespace) -> dict[str, object]:
    question_set = json.loads(arguments.questions.resolve().read_text(encoding="utf-8"))
    fixed_no_answer = str(question_set["fixed_no_answer"])
    cases = tuple(
        case
        for case in question_set["cases"]
        if case.get("generation") and case["mode"] in {"multi_hop", "no_answer"}
    )
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
    identities = UuidIdentityGenerator()
    transport = OpenAICompatibleChatTransport(timeout_seconds=180)
    budget = OutputTokenBudgetPolicy().decide(
        remaining_turn_tokens=8_192,
        provider_max_output_tokens=8_192,
    )
    secret = _deepseek_secret()
    results: list[dict[str, object]] = []
    try:
        for raw_case in cases:
            records: list[Mapping[str, Any]] = []
            trace_hops: list[RagTraceHop] = []
            all_hits: dict[str, KnowledgeRetrievalHit] = {}
            session_id: str | None = None
            final: Mapping[str, Any] | None = None
            actions: list[dict[str, object]] = []
            error: str | None = None
            for step_ordinal in range(1, 9):
                try:
                    response = await _invoke(
                        transport,
                        secret,
                        _request(
                            raw_case,
                            tuple(records),
                            fixed_no_answer,
                            max_output_tokens=budget.admitted_tokens,
                        ),
                    )
                    calls = _tool_calls(response.output)
                    if len(calls) != 1:
                        raise ValueError("agentic RAG step must produce exactly one tool call")
                    call = calls[0]
                    name = str(call.get("name", ""))
                    arguments_row = _mapping(call.get("arguments"), "tool arguments")
                    if name == "submit_grounded_answer":
                        final = arguments_row
                        actions.append(
                            {"step_ordinal": step_ordinal, "action": "submit_grounded_answer"}
                        )
                        if session_id is not None and trace_hops:
                            terminal_reasons = {
                                "agent_concluded",
                                "coverage_satisfied",
                                "no_evidence",
                                "no_new_evidence",
                                "hop_budget_exhausted",
                            }
                            if trace_hops[-1].stop_reason not in terminal_reasons:
                                concluded = repository.conclude_retrieval(
                                    ConcludeKnowledgeRetrievalCommand(
                                        identities.new(CommandId), session_id
                                    )
                                )
                                trace_hops.append(
                                    RagTraceHop(
                                        "Conclude from accumulated evidence.",
                                        (),
                                        concluded.stop_reason,
                                    )
                                )
                                actions.append(
                                    {
                                        "step_ordinal": step_ordinal,
                                        "action": "conclude_retrieval_session",
                                        "stop_reason": concluded.stop_reason,
                                    }
                                )
                        break
                    if name != "search_knowledge":
                        raise ValueError(f"unregistered agentic RAG tool: {name}")
                    query = str(arguments_row.get("query", "")).strip()
                    subquestion = str(arguments_row.get("public_subquestion", "")).strip()
                    if not query or not subquestion:
                        raise ValueError("search action omitted query or public subquestion")
                    conclude = bool(arguments_row.get("conclude_session", False))
                    raw_unresolved = arguments_row.get("unresolved_items", ())
                    if not isinstance(raw_unresolved, list):
                        raise ValueError("unresolved_items must be an array")
                    if session_id is None:
                        retrieval = repository.retrieve(
                            StartKnowledgeRetrievalCommand(
                                identities.new(CommandId),
                                query,
                                str(raw_case["question"]),
                                (CorpusRole.LITERATURE_EVIDENCE,),
                                RetrievalMode.MULTI_HOP,
                                int(raw_case["k"]),
                            )
                        )
                        session_id = retrieval.retrieval_session_id
                    else:
                        proposed_session = arguments_row.get("retrieval_session_id")
                        if proposed_session is not None and str(proposed_session) != session_id:
                            raise ValueError("model attempted to switch retrieval session")
                        retrieval = repository.continue_retrieval(
                            ContinueKnowledgeRetrievalCommand(
                                identities.new(CommandId),
                                session_id,
                                query,
                                subquestion,
                                int(raw_case["k"]),
                                conclude,
                                tuple(str(item) for item in raw_unresolved),
                            )
                        )
                    trace_hops.append(
                        RagTraceHop(subquestion, retrieval.hits, retrieval.stop_reason)
                    )
                    all_hits.update((hit.node_id, hit) for hit in retrieval.hits)
                    record = {
                        "kind": "retrieval_hop_result",
                        "retrieval_session_id": retrieval.retrieval_session_id,
                        "retrieval_hop_id": retrieval.retrieval_hop_id,
                        "hop_ordinal": retrieval.hop_ordinal,
                        "public_subquestion": subquestion,
                        "query": query,
                        "stop_reason": retrieval.stop_reason,
                        "novel_hit_count": retrieval.novel_hit_count,
                        "cumulative_hit_count": retrieval.cumulative_hit_count,
                        "session_status": retrieval.session_status,
                        "hits": [_hit_payload(hit) for hit in retrieval.hits],
                    }
                    records.append(record)
                    actions.append(
                        {
                            "step_ordinal": step_ordinal,
                            "action": "search_knowledge",
                            "query": query,
                            "public_subquestion": subquestion,
                            "conclude_session": conclude,
                            "stop_reason": retrieval.stop_reason,
                            "novel_hit_count": retrieval.novel_hit_count,
                        }
                    )
                except ValueError as step_error:
                    feedback = f"Tool proposal rejected: {step_error}. Revise the next action."
                    records.append(
                        {
                            "kind": "driver_feedback",
                            "step_ordinal": step_ordinal,
                            "content": feedback,
                        }
                    )
                    actions.append(
                        {
                            "step_ordinal": step_ordinal,
                            "action": "proposal_rejected",
                            "reason": str(step_error),
                        }
                    )
                    continue
                except Exception as step_error:
                    error = f"{type(step_error).__name__}: {step_error}"
                    break

            mode = str(raw_case["mode"])
            accepted = False
            diagnostics: dict[str, object] = {}
            if error is None and final is not None:
                status = str(final.get("status", ""))
                answer = final.get("answer")
                claims = final.get("claims")
                if isinstance(answer, str) and isinstance(claims, list):
                    if mode == "no_answer":
                        accepted = (
                            status == "insufficient_evidence"
                            and answer == fixed_no_answer
                            and not claims
                        )
                    else:
                        cited_ids = {
                            str(node_id)
                            for claim in claims
                            if isinstance(claim, Mapping)
                            for node_id in claim.get("cited_node_ids", ())
                            if isinstance(node_id, str)
                        }
                        citation_ids_valid = bool(cited_ids) and cited_ids <= all_hits.keys()
                        expected_sources = set(raw_case.get("expected_sources", ()))
                        retrieved_sources = {hit.source_locator for hit in all_hits.values()}
                        cited_sources = {
                            all_hits[node_id].source_locator
                            for node_id in cited_ids
                            if node_id in all_hits
                        }
                        retrieved_source_coverage = (
                            len(expected_sources & retrieved_sources) / len(expected_sources)
                        )
                        cited_source_coverage = (
                            len(expected_sources & cited_sources) / len(expected_sources)
                        )
                        semantic_supported, support_assessments = (
                            await _evaluate_semantic_support(
                                transport,
                                secret,
                                claims,
                                all_hits,
                                max_output_tokens=budget.admitted_tokens,
                            )
                        )
                        revision_count = 0
                        while not semantic_supported and revision_count < 2:
                            final = await _revise_grounded_answer(
                                transport,
                                secret,
                                raw_case,
                                final,
                                support_assessments,
                                all_hits,
                                max_output_tokens=budget.admitted_tokens,
                            )
                            revision_count += 1
                            revised_claims = final.get("claims")
                            if not isinstance(revised_claims, list):
                                break
                            claims = revised_claims
                            cited_ids = {
                                str(node_id)
                                for claim in claims
                                if isinstance(claim, Mapping)
                                for node_id in claim.get("cited_node_ids", ())
                                if isinstance(node_id, str)
                            }
                            citation_ids_valid = bool(cited_ids) and cited_ids <= all_hits.keys()
                            semantic_supported, support_assessments = (
                                await _evaluate_semantic_support(
                                    transport,
                                    secret,
                                    claims,
                                    all_hits,
                                    max_output_tokens=budget.admitted_tokens,
                                )
                            )
                            actions.append(
                                {
                                    "action": "revise_after_citation_evaluation",
                                    "revision_ordinal": revision_count,
                                    "semantic_support_passed": semantic_supported,
                                }
                            )
                        search_hops = tuple(hop for hop in trace_hops if hop.hits)
                        first_nodes = (
                            {hit.node_id for hit in search_hops[0].hits}
                            if search_hops
                            else set()
                        )
                        later_nodes = {
                            hit.node_id for hop in search_hops[1:] for hit in hop.hits
                        }
                        novel_ratio = (
                            0.0
                            if not later_nodes
                            else len(later_nodes - first_nodes) / len(later_nodes)
                        )
                        terminal_reason = trace_hops[-1].stop_reason if trace_hops else None
                        trace_passed = (
                            len(search_hops) >= 2
                            and novel_ratio >= 0.25
                            and terminal_reason in {"agent_concluded", "coverage_satisfied"}
                        )
                        diagnostics = {
                            "citation_ids_valid": citation_ids_valid,
                            "retrieved_expected_source_coverage": retrieved_source_coverage,
                            "cited_expected_source_coverage": cited_source_coverage,
                            "semantic_claim_support_passed": semantic_supported,
                            "semantic_support_assessments": support_assessments,
                            "trace_score": {
                                "search_hop_count": len(search_hops),
                                "novel_node_ratio": novel_ratio,
                                "terminal_reason": terminal_reason,
                                "passed": trace_passed,
                            },
                        }
                        accepted = (
                            status == "answered"
                            and citation_ids_valid
                            and semantic_supported
                            and trace_passed
                        )
            results.append(
                {
                    "case_id": str(raw_case["case_id"]),
                    "mode": mode,
                    "accepted": accepted,
                    "error": error,
                    "retrieval_session_id": session_id,
                    "actions": actions,
                    "retrieval_trace": records,
                    "final": final,
                    "diagnostics": diagnostics,
                }
            )
    finally:
        del secret
        connection.close()

    positive = tuple(result for result in results if result["mode"] == "multi_hop")
    negative = tuple(result for result in results if result["mode"] == "no_answer")
    metrics = {
        "case_count": len(results),
        "autonomous_multi_hop_acceptance": (
            sum(bool(item["accepted"]) for item in positive) / len(positive)
        ),
        "autonomous_no_answer_accuracy": (
            sum(bool(item["accepted"]) for item in negative) / len(negative)
        ),
        "overall_acceptance": sum(bool(item["accepted"]) for item in results) / len(results),
    }
    return {
        "schema_version": "live-agentic-rag-evaluation/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "deepseek",
        "model": MODEL,
        "question_set": str(arguments.questions.resolve()),
        "important_constraint": "No gold hop query was exposed to the model.",
        "metrics": metrics,
        "accepted": all(value == 1.0 for value in metrics.values() if isinstance(value, float)),
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
        raise ValueError("agentic RAG output root already exists")
    output_root.mkdir(parents=True)
    report = asyncio.run(_run(arguments))
    report_path = output_root / "live-agentic-rag-report.json"
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
