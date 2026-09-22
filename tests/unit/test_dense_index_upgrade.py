from __future__ import annotations

from stata_research_agent.application.dense_retrieval import (
    DenseCandidate,
    DenseUpgradeEvaluationCase,
    assess_dense_index_upgrade,
)


def _search(mapping: dict[str, tuple[str, ...]]):
    def search(query: str, limit: int) -> tuple[DenseCandidate, ...]:
        return tuple(
            DenseCandidate(node_id, rank, 1.0 / rank)
            for rank, node_id in enumerate(mapping[query][:limit], start=1)
        )

    return search


def test_dense_upgrade_requires_no_case_regression_before_adoption() -> None:
    cases = (
        DenseUpgradeEvaluationCase("parallel trends", ("node_a",)),
        DenseUpgradeEvaluationCase("staggered timing", ("node_b",)),
    )
    accepted = assess_dense_index_upgrade(
        old_index_revision_id="knowledgeembedidx_old",
        candidate_index_revision_id="knowledgeembedidx_new",
        cases=cases,
        old_search=_search({"parallel trends": ("node_a",), "staggered timing": ("noise",)}),
        candidate_search=_search({"parallel trends": ("node_a",), "staggered timing": ("node_b",)}),
        k=1,
    )
    assert accepted.eligible_for_adoption
    assert accepted.candidate_recall_at_k == 1.0

    rejected = assess_dense_index_upgrade(
        old_index_revision_id="knowledgeembedidx_old",
        candidate_index_revision_id="knowledgeembedidx_bad",
        cases=cases,
        old_search=_search({"parallel trends": ("node_a",), "staggered timing": ("node_b",)}),
        candidate_search=_search({"parallel trends": ("noise",), "staggered timing": ("node_b",)}),
        k=1,
    )
    assert not rejected.eligible_for_adoption
    assert rejected.regressed_case_count == 1
