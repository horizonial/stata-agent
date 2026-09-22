"""Claim-oriented RAG evaluation metrics and corpus-role leakage checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from stata_research_agent.application.knowledge_retrieval import (
    CorpusRole,
    KnowledgeRetrievalHit,
)
from stata_research_agent.application.rag_evaluation import (
    RagAcceptanceThresholds,
    RagGoldCase,
    RagGroundedClaim,
    RagGroundingCase,
    RagTraceGoldCase,
    RagTraceHop,
    evaluate_grounded_answer,
    evaluate_rag,
    evaluate_retrieval_trace,
)
from tools.run_rag_evaluation import load_gold_cases


def _hit(
    node: str,
    source: str,
    role: str,
    content: str,
    rank: int,
) -> KnowledgeRetrievalHit:
    return KnowledgeRetrievalHit(
        node,
        f"knowledgesrcrev_{node}",
        f"knowledgeparse_{node}",
        source,
        role,
        "paragraph",
        1,
        1,
        "Identification",
        content,
        rank,
        1 / (60 + rank),
    )


def test_rag_evaluation_scores_recall_multihop_coverage_and_role_isolation() -> None:
    cases = (
        RagGoldCase(
            "did-identification",
            "parallel trends and diagnostics",
            (CorpusRole.LITERATURE_EVIDENCE,),
            ("literature/design.md",),
            ("parallel trends", "event-study leads"),
            (("parallel trends",), ("event-study leads", "pre-trend")),
            4,
        ),
    )

    def retrieve(
        query: str, roles: tuple[CorpusRole, ...], limit: int
    ) -> tuple[KnowledgeRetrievalHit, ...]:
        del query, roles, limit
        return (
            _hit(
                "node_a",
                "literature/design.md",
                "literature_evidence",
                "Parallel trends is the identifying assumption.",
                1,
            ),
            _hit(
                "node_b",
                "literature/diagnostics.md",
                "literature_evidence",
                "Event-study leads can reveal a pre-trend.",
                2,
            ),
        )

    report = evaluate_rag(cases, retrieve)
    assert report.macro_recall_at_k == 1
    assert report.macro_precision_at_k == 0.5
    assert report.mean_reciprocal_rank == 1
    assert report.macro_evidence_group_coverage == 1
    assert report.total_role_leaks == 0
    assert report.passed_role_isolation
    assert report.accepts(RagAcceptanceThresholds())


def test_rag_evaluation_reports_style_leak_and_missing_evidence() -> None:
    case = RagGoldCase(
        "role-isolation",
        "instrument relevance",
        (CorpusRole.LITERATURE_EVIDENCE,),
        expected_content_markers=("exclusion restriction",),
    )

    def retrieve(
        query: str, roles: tuple[CorpusRole, ...], limit: int
    ) -> tuple[KnowledgeRetrievalHit, ...]:
        del query, roles, limit
        return (
            _hit(
                "node_style",
                "style-references/target.md",
                "style_exemplar",
                "We organize the section in a concise way.",
                1,
            ),
        )

    report = evaluate_rag((case,), retrieve)
    assert report.macro_recall_at_k == 0
    assert report.total_role_leaks == 1
    assert not report.passed_role_isolation
    assert not report.accepts(RagAcceptanceThresholds())
    assert report.case_scores[0].missing_expectations == ("text:exclusion restriction",)


def test_versioned_rag_gold_example_is_loadable() -> None:
    cases = load_gold_cases(
        Path(__file__).parents[2] / "verification" / "rag-gold.example.json"
    )
    assert len(cases) == 2
    assert cases[0].corpus_roles == (CorpusRole.LITERATURE_EVIDENCE,)
    assert cases[1].corpus_roles == (CorpusRole.STATA_HELP,)


def test_rag_gold_loader_rejects_unknown_schema(tmp_path: Path) -> None:
    gold = tmp_path / "future.json"
    gold.write_text('{"schema_version":"rag-gold-v2","cases":[]}', encoding="utf-8")

    with pytest.raises(ValueError, match="rag-gold-v1"):
        load_gold_cases(gold)


def test_multi_hop_trace_requires_novel_evidence_and_explicit_completion() -> None:
    first = RagTraceHop(
        "What assumption identifies the design?",
        (
            _hit(
                "node_assumption",
                "literature/design.md",
                "literature_evidence",
                "Parallel trends is the identifying assumption.",
                1,
            ),
        ),
        "awaiting_next_hop",
    )
    second = RagTraceHop(
        "How can that assumption be diagnosed?",
        (
            _hit(
                "node_diagnostic",
                "literature/diagnostics.md",
                "literature_evidence",
                "Event-study leads can reveal a pre-trend.",
                1,
            ),
        ),
        "agent_concluded",
    )
    case = RagTraceGoldCase(
        "did-assumption-to-diagnostic",
        (CorpusRole.LITERATURE_EVIDENCE,),
        (("parallel trends",), ("event-study leads", "pre-trend")),
    )

    score = evaluate_retrieval_trace(case, (first, second))

    assert score.passed
    assert score.evidence_group_coverage == 1
    assert score.novel_node_ratio == 1
    assert score.terminal_reason == "agent_concluded"


def test_multi_hop_trace_rejects_role_leak_and_fake_second_hop() -> None:
    repeated = _hit(
        "node_repeated",
        "literature/design.md",
        "literature_evidence",
        "Parallel trends is the identifying assumption.",
        1,
    )
    case = RagTraceGoldCase(
        "invalid-trace",
        (CorpusRole.LITERATURE_EVIDENCE,),
        (("parallel trends",), ("pre-trend",)),
    )
    score = evaluate_retrieval_trace(
        case,
        (
            RagTraceHop("Assumption?", (repeated,), "awaiting_next_hop"),
            RagTraceHop(
                "Diagnostic?",
                (
                    repeated,
                    _hit(
                        "node_style",
                        "style-references/paper.md",
                        "style_exemplar",
                        "We write with a measured cadence.",
                        2,
                    ),
                ),
                "agent_concluded",
            ),
        ),
    )

    assert not score.passed
    assert score.role_leak_count == 1
    assert score.evidence_group_coverage == 0.5
    assert score.novel_node_ratio == 0.5


def test_grounded_answer_requires_valid_support_and_does_not_treat_style_as_fact() -> None:
    literature = _hit(
        "node_literature",
        "literature/design.md",
        "literature_evidence",
        "Parallel trends is the identifying assumption for this design.",
        1,
    )
    style = _hit(
        "node_style",
        "style-references/paper.md",
        "style_exemplar",
        "We first state the identifying assumption and then report diagnostics.",
        1,
    )
    case = RagGroundingCase(
        "grounded-discussion",
        "The design relies on parallel trends. We next report diagnostics.",
        (
            RagGroundedClaim(
                "claim-identification",
                "The design relies on parallel trends.",
                (literature.node_id,),
                ("parallel trends", "identifying assumption"),
            ),
        ),
    )

    score = evaluate_grounded_answer(
        case, {literature.node_id: literature}, style_nodes=(style,)
    )

    assert score.passed
    assert score.claim_citation_coverage == 1
    assert score.citation_identity_validity == 1
    assert score.support_marker_coverage == 1
    assert score.evidence_role_leak_count == 0


def test_grounded_answer_rejects_unknown_citation_role_leak_and_long_style_copy() -> None:
    style_text = (
        "We first state the identifying assumption and then carefully report diagnostics "
        "before discussing the practical magnitude of every estimate in the table."
    )
    style = _hit(
        "node_style",
        "style-references/paper.md",
        "style_exemplar",
        style_text,
        1,
    )
    case = RagGroundingCase(
        "contaminated-answer",
        style_text,
        (
            RagGroundedClaim(
                "claim-identification",
                "The design is identified.",
                (style.node_id, "node_missing"),
                ("parallel trends",),
            ),
        ),
        maximum_style_copy_run=8,
    )

    score = evaluate_grounded_answer(
        case, {style.node_id: style}, style_nodes=(style,)
    )

    assert not score.passed
    assert score.evidence_role_leak_count == 1
    assert score.unknown_citation_node_ids == ("node_missing",)
    assert score.missing_support_markers == ("claim-identification:parallel trends",)
    assert score.longest_style_copy_run > 8
