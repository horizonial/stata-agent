"""Local embedding adapter contracts without downloading a model in unit tests."""

from __future__ import annotations

from typing import Any

from stata_research_agent.interfaces.sentence_transformer_embedding import (
    SentenceTransformerEmbeddingGateway,
)


class _Rows:
    def __init__(self, values: list[list[float]]) -> None:
        self._values = values

    def tolist(self) -> list[list[float]]:
        return self._values


class _Model:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def get_embedding_dimension(self) -> int:
        return 2

    def encode(self, sentences: list[str], **kwargs: Any) -> _Rows:
        self.calls.append((sentences, kwargs))
        return _Rows([[1.0, 0.0] for _ in sentences])


def test_e5_gateway_uses_distinct_query_and_passage_prefixes() -> None:
    model = _Model()
    gateway = SentenceTransformerEmbeddingGateway(model=model)

    assert gateway.embed_documents(("paper text",)) == ((1.0, 0.0),)
    assert gateway.embed_query("research question") == (1.0, 0.0)
    assert model.calls[0][0] == ["passage: paper text"]
    assert model.calls[1][0] == ["query: research question"]
    assert model.calls[0][1]["normalize_embeddings"] is True
    assert gateway.profile.dimension == 2


def test_gateway_can_use_raw_text_for_non_e5_model_families() -> None:
    model = _Model()
    gateway = SentenceTransformerEmbeddingGateway(
        model_name="example/raw-embedding",
        model_revision="revision-1",
        query_prefix="",
        document_prefix="",
        profile_family="raw-text-v1",
        max_sequence_length=1024,
        model=model,
    )

    gateway.embed_documents(("paper text",))
    gateway.embed_query("research question")

    assert model.calls[0][0] == ["paper text"]
    assert model.calls[1][0] == ["research question"]
    assert gateway.profile.profile_revision.endswith(":raw-text-v1:max-1024")
    assert model.max_seq_length == 1024
