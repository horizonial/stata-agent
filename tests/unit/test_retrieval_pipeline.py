"""Query planning, second-stage reranking, and evidence diversity contracts."""

from stata_research_agent.application.knowledge_retrieval import RetrievalMode
from stata_research_agent.application.retrieval_pipeline import (
    DeterministicEvidenceReranker,
    DeterministicQueryPlanner,
    EvidenceSufficiencyGate,
    QueryVariantKind,
    RerankAssessment,
    RerankCandidate,
    select_diverse_evidence,
)


def test_query_plan_preserves_original_and_adds_auditable_variants() -> None:
    plan = DeterministicQueryPlanner().plan(
        "How do event-study leads diagnose pre-trends?",
        "Find evidence about parallel-trends diagnostics and placebo leads.",
        RetrievalMode.MULTI_HOP,
        public_subquestion="Which diagnostic can reveal violations of parallel trends?",
    )

    assert plan.variants[0].kind is QueryVariantKind.ORIGINAL
    assert plan.variants[0].query == "How do event-study leads diagnose pre-trends?"
    assert {variant.kind for variant in plan.variants} >= {
        QueryVariantKind.ORIGINAL,
        QueryVariantKind.SUBQUESTION,
        QueryVariantKind.OBJECTIVE,
    }
    assert len({variant.query for variant in plan.variants}) == len(plan.variants)


def test_reranker_uses_question_support_not_only_first_stage_rank() -> None:
    candidates = (
        RerankCandidate(
            "node_irrelevant",
            "literature/a.md",
            "paragraph",
            "Background",
            "A highly ranked paragraph about unrelated sample construction.",
            10.0,
        ),
        RerankCandidate(
            "node_direct",
            "literature/b.md",
            "paragraph",
            "Identification",
            "Parallel trends identifies the design; event-study leads diagnose pre-trends.",
            1.0,
        ),
    )

    ranked = DeterministicEvidenceReranker().rerank(
        "parallel trends event-study leads",
        "Find a diagnostic for the identifying assumption",
        candidates,
    )

    assert ranked[0].node_id == "node_direct"
    assert ranked[0].relevance_label == "direct_support"
    assert ranked[0].score > ranked[1].score


def test_evidence_selector_prefers_source_diversity_before_backfill() -> None:
    reranker = DeterministicEvidenceReranker()
    candidates = tuple(
        RerankCandidate(
            f"node_{index}",
            "literature/a.md" if index < 4 else "literature/b.md",
            "paragraph",
            "Results",
            f"Parallel trends diagnostic evidence number {index}.",
            10.0 - index,
        )
        for index in range(1, 5)
    )
    ranked = reranker.rerank(
        "parallel trends diagnostic",
        "Find diagnostic evidence",
        candidates,
    )

    selected = select_diverse_evidence(ranked, candidates, limit=3)

    assert len(selected) == 3
    assert "node_4" in {item.node_id for item in selected}


def test_evidence_sufficiency_gate_rejects_weak_retrieval() -> None:
    decision = EvidenceSufficiencyGate().assess(
        (RerankAssessment("weak-node", 0.15, "background", ("retrieval_prior",)),)
    )

    assert decision.status == "insufficient_evidence"
    assert decision.answer_allowed is False


def test_evidence_sufficiency_gate_allows_supported_retrieval() -> None:
    decision = EvidenceSufficiencyGate().assess(
        (
            RerankAssessment(
                "supported-node", 0.72, "direct_support", ("exact_phrase",)
            ),
        )
    )

    assert decision.status == "supported"
    assert decision.answer_allowed is True
