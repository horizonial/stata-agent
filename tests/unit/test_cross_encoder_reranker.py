"""Cross-encoder evidence reranker contracts."""

from stata_research_agent.application.retrieval_pipeline import RerankCandidate
from stata_research_agent.interfaces.cross_encoder_reranker import (
    AdaptiveFusionEvidenceReranker,
    AdaptiveRerankPolicy,
    CrossEncoderEvidenceReranker,
)


class _FakeCrossEncoder:
    def predict(
        self,
        sentences: object,
        *,
        batch_size: int,
        show_progress_bar: bool,
    ) -> list[float]:
        assert batch_size == 2
        assert show_progress_bar is False
        assert len(sentences) == 2  # type: ignore[arg-type]
        return [-2.0, 3.0]


class _FailingCrossEncoder:
    def predict(
        self,
        sentences: object,
        *,
        batch_size: int,
        show_progress_bar: bool,
    ) -> list[float]:
        del sentences, batch_size, show_progress_bar
        raise RuntimeError("simulated model failure")


def test_cross_encoder_reranks_top_n_and_demotes_unscored_candidates() -> None:
    candidates = tuple(
        RerankCandidate(
            f"node-{index}",
            f"paper-{index}.pdf",
            "paragraph",
            None,
            f"candidate {index}",
            float(4 - index),
        )
        for index in range(3)
    )
    reranker = CrossEncoderEvidenceReranker(
        model_name="example/reranker",
        model_revision="revision-1",
        cache_folder="unused",
        batch_size=2,
        top_n=2,
        model=_FakeCrossEncoder(),
    )

    assessments = reranker.rerank("cash holdings", "cash holdings", candidates)

    assert tuple(item.node_id for item in assessments) == ("node-1", "node-0", "node-2")
    assert assessments[0].score > assessments[1].score > assessments[2].score
    assert assessments[2].reason_codes == ("outside_cross_encoder_top_n",)


def test_adaptive_fusion_preserves_neural_signal_without_discarding_priors() -> None:
    candidates = tuple(
        RerankCandidate(
            f"node-{index}",
            f"paper-{index}.pdf",
            "paragraph",
            None,
            f"cash holdings candidate {index}",
            float(4 - index),
            lexical_rank=index + 1,
            dense_rank=3 - index,
        )
        for index in range(3)
    )
    neural = CrossEncoderEvidenceReranker(
        model_name="example/reranker",
        model_revision="revision-1",
        cache_folder="unused",
        batch_size=2,
        top_n=2,
        model=_FakeCrossEncoder(),
    )
    reranker = AdaptiveFusionEvidenceReranker(
        neural,
        policy=AdaptiveRerankPolicy(force=True),
    )

    assessments = reranker.rerank("cash holdings", "cash holdings", candidates)

    assert assessments[0].node_id == "node-1"
    assert "neural_rank_fusion" in assessments[0].reason_codes
    assert reranker.diagnostics_snapshot == {
        "query_count": 1,
        "trigger_count": 1,
        "trigger_rate": 1.0,
        "fallback_count": 0,
    }


def test_adaptive_fusion_falls_back_when_neural_model_fails() -> None:
    candidates = (
        RerankCandidate("node-1", "a.pdf", "paragraph", None, "cash holdings", 2.0),
        RerankCandidate("node-2", "b.pdf", "paragraph", None, "unrelated", 1.0),
    )
    neural = CrossEncoderEvidenceReranker(
        model_name="example/reranker",
        model_revision="revision-1",
        cache_folder="unused",
        batch_size=2,
        top_n=2,
        model=_FailingCrossEncoder(),
    )
    reranker = AdaptiveFusionEvidenceReranker(
        neural,
        policy=AdaptiveRerankPolicy(force=True),
    )

    assessments = reranker.rerank("cash holdings", "cash holdings", candidates)

    assert assessments[0].node_id == "node-1"
    assert "neural_failure_deterministic_fallback" in assessments[0].reason_codes
    assert reranker.diagnostics_snapshot["fallback_count"] == 1
