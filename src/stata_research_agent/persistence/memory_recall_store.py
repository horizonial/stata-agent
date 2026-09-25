"""SQLite adapter for scoped, progressive Project Memory recall."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping

from stata_research_agent.application.dense_retrieval import EmbeddingGateway
from stata_research_agent.application.ports.identity import IdentityGenerator

from .filesystem_memory import FilesystemMemoryStore
from .memory_recommendation_query import (
    RankedMemoryCandidate,
    SqliteMemoryRecommendationQuery,
)


class SqliteMemoryRecallRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        files: FilesystemMemoryStore,
        identities: IdentityGenerator,
        embedding_gateway: EmbeddingGateway | None = None,
    ) -> None:
        self._connection = connection
        self._files = files
        self._identities = identities
        self._embedding_gateway = embedding_gateway

    def reconcile_external_edits(self) -> None:
        self._files.reconcile_external_edits(self._connection, self._identities)

    def search(
        self,
        arguments: Mapping[str, object],
        *,
        research_path_id: str,
    ) -> list[dict[str, object]]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(str(arguments.get("limit", 8))), 24))
        include_archived = bool(arguments.get("include_archived", False))
        retrieval_mode = str(arguments.get("retrieval_mode", "hybrid"))
        if retrieval_mode not in {"lexical", "hybrid"}:
            raise ValueError("retrieval_mode must be lexical or hybrid")
        ranked = SqliteMemoryRecommendationQuery(self._connection).search(
            query,
            research_path_id=research_path_id,
            limit=limit,
            include_archived=include_archived,
            semantic_scorer=(
                self._semantic_scores
                if retrieval_mode == "hybrid" and self._embedding_gateway is not None
                else None
            ),
        )
        return [self._payload(hit.row, include_content=False, ranked=hit) for hit in ranked]

    def _semantic_scores(
        self,
        query: str,
        documents: tuple[tuple[str, str], ...],
    ) -> Mapping[str, float]:
        gateway = self._embedding_gateway
        if gateway is None or not documents:
            return {}
        query_vector = gateway.embed_query(query)
        document_vectors = gateway.embed_documents(tuple(content for _, content in documents))
        if len(document_vectors) != len(documents):
            raise ValueError("Memory embedding response count does not match candidates")
        dimension = gateway.profile.dimension
        if len(query_vector) != dimension or any(
            len(vector) != dimension for vector in document_vectors
        ):
            raise ValueError("Memory embedding dimension does not match the active profile")
        return {
            memory_item_id: sum(
                left * right for left, right in zip(query_vector, vector, strict=True)
            )
            for (memory_item_id, _), vector in zip(documents, document_vectors, strict=True)
        }

    def open(
        self,
        arguments: Mapping[str, object],
        *,
        research_path_id: str,
    ) -> list[dict[str, object]]:
        raw_ids = arguments.get("memory_item_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("memory_item_ids must be a non-empty array")
        if len(raw_ids) > 24:
            raise ValueError("at most 24 Memory items can be opened at once")
        include_archived = bool(arguments.get("include_archived", False))
        by_id = {
            str(row["memory_item_id"]): row
            for row in self._rows(research_path_id, include_archived=include_archived)
        }
        hits: list[dict[str, object]] = []
        for raw_id in raw_ids:
            memory_item_id = str(raw_id)
            row = by_id.get(memory_item_id)
            if row is None:
                raise ValueError(
                    f"Memory item is unavailable, out of scope, or archived: {memory_item_id}"
                )
            hits.append(self._payload(row, include_content=True))
        return hits

    def _rows(self, research_path_id: str, *, include_archived: bool) -> Iterable[sqlite3.Row]:
        workspace_id = str(
            self._connection.execute(
                "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
            ).fetchone()[0]
        )
        return self._connection.execute(
            """
            SELECT item.memory_item_id, item.scope_kind, item.scope_object_id,
                   item.memory_kind, state.current_revision_id AS memory_revision_id,
                   state.lifecycle, revision.title, revision.content,
                   revision.content_sha256, revision.origin_kind, revision.created_revision,
                   retention.access_tier, retention.pinned,
                   retention.superseded_by_memory_item_id,
                   payload.relative_path, payload.payload_sha256, payload.size_bytes
            FROM memory_items AS item
            JOIN memory_current_states AS state USING (memory_item_id)
            JOIN memory_revisions AS revision
              ON revision.memory_revision_id = state.current_revision_id
            JOIN memory_retention_states AS retention USING (memory_item_id)
            LEFT JOIN memory_payload_files AS payload
              ON payload.memory_revision_id = revision.memory_revision_id
            WHERE state.lifecycle = 'active'
              AND (? = 1 OR retention.access_tier != 'archived')
              AND (? = 1 OR retention.superseded_by_memory_item_id IS NULL)
              AND (
                    (item.scope_kind = 'workspace' AND item.scope_object_id = ?)
                    OR
                    (item.scope_kind = 'research_path' AND item.scope_object_id = ?)
                  )
            ORDER BY revision.created_revision DESC
            """,
            (int(include_archived), int(include_archived), workspace_id, research_path_id),
        ).fetchall()

    def _payload(
        self,
        row: sqlite3.Row,
        *,
        include_content: bool,
        ranked: RankedMemoryCandidate | None = None,
    ) -> dict[str, object]:
        content = str(row["content"])
        if include_content:
            relative_path = row["relative_path"]
            if relative_path is None:
                raise ValueError("Memory revision has no managed filesystem payload")
            file_title, content = self._files.read_revision_content(
                str(relative_path),
                expected_memory_item_id=str(row["memory_item_id"]),
                expected_memory_revision_id=str(row["memory_revision_id"]),
                expected_content_sha256=str(row["content_sha256"]),
                expected_payload_sha256=str(row["payload_sha256"]),
                expected_size_bytes=int(row["size_bytes"]),
            )
            if file_title != str(row["title"]):
                raise ValueError("Memory revision file title does not match the ledger")
        return {
            "memory_item_id": str(row["memory_item_id"]),
            "memory_revision_id": str(row["memory_revision_id"]),
            "created_revision": int(row["created_revision"]),
            "scope_kind": str(row["scope_kind"]),
            "scope_object_id": str(row["scope_object_id"]),
            "memory_kind": str(row["memory_kind"]),
            "title": str(row["title"]),
            "content": content if include_content else None,
            "excerpt": content[:280],
            "excerpt_kind": "exact_prefix",
            "access_tier": str(row["access_tier"]),
            "pinned": bool(row["pinned"]),
            "origin_kind": str(row["origin_kind"]),
            "memory_file": row["relative_path"],
            "superseded_by_memory_item_id": row["superseded_by_memory_item_id"],
            "retrieval_score": None if ranked is None else round(ranked.score, 6),
            "retrieval_reasons": [] if ranked is None else list(ranked.reasons),
            "sources": (
                []
                if ranked is None
                else [
                    {
                        "object_type": str(source["source_object_type"]),
                        "object_id": str(source["source_object_id"]),
                        "object_revision": str(source["source_object_revision"]),
                        "role": str(source["source_role"]),
                    }
                    for source in ranked.source_rows
                ]
            ),
        }
