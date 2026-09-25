"""Optional local Sentence Transformers adapter for canonical dense retrieval."""

from __future__ import annotations

import math
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
        query_prefix: str = "query: ",
        document_prefix: str = "passage: ",
        profile_family: str = "e5-prefix-v1",
        trust_remote_code: bool = False,
        cache_folder: Path | None = None,
        device: str = "cpu",
        batch_size: int = 16,
        max_sequence_length: int | None = None,
        model: SentenceTransformerModel | None = None,
    ) -> None:
        if (
            not model_name.strip()
            or not model_revision.strip()
            or not profile_family.strip()
            or batch_size < 1
            or (max_sequence_length is not None and max_sequence_length < 32)
        ):
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
                    trust_remote_code=trust_remote_code,
                ),
            )
        if max_sequence_length is not None:
            setattr(model, "max_seq_length", max_sequence_length)
        # sentence-transformers exposes ``get_sentence_embedding_dimension``.
        # Keep the older narrow test-double method as a compatibility fallback.
        dimension_reader = getattr(model, "get_sentence_embedding_dimension", None)
        dimension = (
            dimension_reader()
            if callable(dimension_reader)
            else model.get_embedding_dimension()
        )
        if dimension is None or dimension < 1:
            raise ValueError("embedding model did not expose a valid dimension")
        self._model = model
        self._batch_size = batch_size
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._profile = EmbeddingProfile(
            f"{model_name}@{model_revision}:{profile_family}"
            + (
                ""
                if max_sequence_length is None
                else f":max-{max_sequence_length}"
            ),
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
        return self._encode([f"{self._document_prefix}{text}" for text in texts])

    def embed_query(self, text: str) -> tuple[float, ...]:
        if not text.strip():
            raise ValueError("query embedding requires non-empty text")
        return self._encode([f"{self._query_prefix}{text}"])[0]

    def _encode(self, texts: list[str]) -> tuple[tuple[float, ...], ...]:
        encoded = self._model.encode(
            texts,
            batch_size=self._batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        rows = encoded.tolist()
        normalized_rows: list[tuple[float, ...]] = []
        for row in rows:
            values = tuple(float(value) for value in row)
            norm = math.sqrt(sum(value * value for value in values))
            if not math.isfinite(norm) or norm == 0:
                raise ValueError("embedding model returned an invalid vector")
            # Some fp16 decoder-style models remain outside the authority layer's
            # strict 1e-3 norm tolerance after library-side normalization. Re-normalize
            # the serialized float values so every backend satisfies the same contract.
            normalized_rows.append(tuple(value / norm for value in values))
        return tuple(normalized_rows)
