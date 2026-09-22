"""Optional local Sentence Transformers adapter for canonical dense retrieval."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, cast

from stata_research_agent.application.dense_retrieval import EmbeddingProfile


class SentenceTransformerModel(Protocol):
    def get_embedding_dimension(self) -> int | None: ...

    def encode(
        self,
        sentences: list[str],
        *,
        batch_size: int,
        show_progress_bar: bool,
        convert_to_numpy: bool,
        normalize_embeddings: bool,
    ) -> Any: ...


class SentenceTransformerEmbeddingGateway:
    """Run a pinned local E5-family model behind the product embedding port."""

    def __init__(
        self,
        *,
        model_name: str = "intfloat/multilingual-e5-small",
        model_revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        cache_folder: Path | None = None,
        device: str = "cpu",
        batch_size: int = 16,
        model: SentenceTransformerModel | None = None,
    ) -> None:
        if not model_name.strip() or not model_revision.strip() or batch_size < 1:
            raise ValueError("embedding model configuration is invalid")
        if model is None:
            try:
                from sentence_transformers import (  # type: ignore[import-not-found]
                    SentenceTransformer,
                )
            except ImportError as error:
                raise RuntimeError(
                    "sentence-transformers is not installed in the embedding runtime"
                ) from error
            model = cast(
                SentenceTransformerModel,
                SentenceTransformer(
                    model_name,
                    revision=model_revision,
                    cache_folder=None if cache_folder is None else str(cache_folder),
                    device=device,
                    trust_remote_code=False,
                ),
            )
        dimension = model.get_embedding_dimension()
        if dimension is None or dimension < 1:
            raise ValueError("embedding model did not expose a valid dimension")
        self._model = model
        self._batch_size = batch_size
        self._profile = EmbeddingProfile(
            f"{model_name}@{model_revision}:e5-prefix-v1",
            "local_sentence_transformers",
            model_name,
            dimension,
        )

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("document embeddings require non-empty texts")
        return self._encode([f"passage: {text}" for text in texts])

    def embed_query(self, text: str) -> tuple[float, ...]:
        if not text.strip():
            raise ValueError("query embedding requires non-empty text")
        return self._encode([f"query: {text}"])[0]

    def _encode(self, texts: list[str]) -> tuple[tuple[float, ...], ...]:
        encoded = self._model.encode(
            texts,
            batch_size=self._batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        rows = encoded.tolist()
        return tuple(tuple(float(value) for value in row) for row in rows)
