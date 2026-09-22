"""SQLite metadata and flat-cosine projection for versioned Knowledge embeddings."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct

from stata_research_agent.application.dense_retrieval import (
    DenseCandidate,
    DenseIndexNode,
    EmbeddingProfile,
)
from stata_research_agent.application.knowledge_retrieval import CorpusRole
from stata_research_agent.domain.identifiers import (
    CommandId,
    KnowledgeEmbeddingIndexRevisionId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator

from .atomic_commit import AtomicCommitService, JournalDraft, MutationPayload, OutboxDraft


class SqliteDenseKnowledgeIndexRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)
        self._identities = UuidIdentityGenerator()

    def current_nodes(
        self, corpus_roles: tuple[CorpusRole, ...]
    ) -> tuple[DenseIndexNode, ...]:
        if not corpus_roles:
            raise ValueError("dense index requires corpus roles")
        placeholders = ",".join("?" for _ in corpus_roles)
        return tuple(
            DenseIndexNode(
                str(row["knowledge_node_id"]),
                str(row["content_sha256"]),
                str(row["content"]),
            )
            for row in self._connection.execute(
                f"""
                SELECT DISTINCT node.knowledge_node_id, node.content_sha256, node.content,
                                source.canonical_locator, node.ordinal
                FROM knowledge_nodes AS node
                JOIN knowledge_parse_revisions AS parse USING (knowledge_parse_revision_id)
                JOIN knowledge_source_revisions AS source_revision
                  USING (knowledge_source_revision_id)
                JOIN knowledge_sources AS source USING (knowledge_source_id)
                JOIN knowledge_source_states AS state USING (knowledge_source_id)
                JOIN knowledge_source_memberships AS membership USING (knowledge_source_id)
                WHERE membership.corpus_role IN ({placeholders})
                  AND state.availability = 'indexed'
                  AND state.current_source_revision_id =
                      source_revision.knowledge_source_revision_id
                  AND state.current_parse_revision_id = parse.knowledge_parse_revision_id
                  AND node.node_kind != 'document'
                ORDER BY source.canonical_locator, node.ordinal
                """,
                tuple(role.value for role in corpus_roles),
            ).fetchall()
        )

    def replace_index(
        self,
        command_id: CommandId,
        profile: EmbeddingProfile,
        corpus_roles: tuple[CorpusRole, ...],
        nodes: tuple[DenseIndexNode, ...],
        vectors: tuple[tuple[float, ...], ...],
        source_set_fingerprint: str,
    ) -> str:
        roles_json = self._roles_json(corpus_roles)
        request = {
            "embedding_profile_revision": profile.profile_revision,
            "corpus_roles": json.loads(roles_json),
            "source_set_fingerprint": source_set_fingerprint,
            "node_count": len(nodes),
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            index_id = self._identities.new(KnowledgeEmbeddingIndexRevisionId).value
            connection.execute(
                """
                INSERT INTO knowledge_embedding_index_revisions(
                    knowledge_embedding_index_revision_id, corpus_roles_json,
                    embedding_profile_revision, provider_kind, model_name,
                    vector_dimension, normalization, node_selection_policy,
                    source_set_fingerprint, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'current-canonical-nodes-v1', ?, ?)
                """,
                (
                    index_id,
                    roles_json,
                    profile.profile_revision,
                    profile.provider_kind,
                    profile.model_name,
                    profile.dimension,
                    profile.normalization,
                    source_set_fingerprint,
                    revision.value,
                ),
            )
            connection.executemany(
                """
                INSERT INTO knowledge_node_embeddings(
                    knowledge_embedding_index_revision_id, knowledge_node_id,
                    vector_blob, created_revision
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        index_id,
                        node.node_id,
                        self._pack(vector),
                        revision.value,
                    )
                    for node, vector in zip(nodes, vectors, strict=True)
                ],
            )
            return MutationPayload(
                {"knowledge_embedding_index_revision_id": index_id},
                (
                    JournalDraft(
                        "knowledge.dense_index_built",
                        "knowledge_embedding_index_revision",
                        index_id,
                        {
                            "embedding_profile_revision": profile.profile_revision,
                            "node_count": len(nodes),
                            "source_set_fingerprint": source_set_fingerprint,
                        },
                    ),
                ),
                (
                    OutboxDraft(
                        "knowledge.dense_index_built",
                        {
                            "knowledge_embedding_index_revision_id": index_id,
                            "embedding_profile_revision": profile.profile_revision,
                            "node_count": len(nodes),
                        },
                    ),
                ),
            )

        receipt = self._commits.commit_mutation(
            command_id=command_id,
            command_type="knowledge.rebuild_dense_index",
            request=request,
            mutation=mutate,
        )
        return str(receipt.response["knowledge_embedding_index_revision_id"])

    def search_index(
        self,
        profile: EmbeddingProfile,
        corpus_roles: tuple[CorpusRole, ...],
        query_vector: tuple[float, ...],
        limit: int,
    ) -> tuple[DenseCandidate, ...]:
        row = self._connection.execute(
            """
            SELECT knowledge_embedding_index_revision_id, source_set_fingerprint
            FROM knowledge_embedding_index_revisions
            WHERE corpus_roles_json = ? AND embedding_profile_revision = ?
              AND provider_kind = ? AND model_name = ?
              AND vector_dimension = ? AND normalization = ?
            ORDER BY created_revision DESC LIMIT 1
            """,
            (
                self._roles_json(corpus_roles),
                profile.profile_revision,
                profile.provider_kind,
                profile.model_name,
                profile.dimension,
                profile.normalization,
            ),
        ).fetchone()
        if row is None:
            return ()
        current = self.current_nodes(corpus_roles)
        current_fingerprint = hashlib.sha256(
            "\n".join(f"{node.node_id}:{node.content_sha256}" for node in current).encode()
        ).hexdigest()
        if current_fingerprint != str(row["source_set_fingerprint"]):
            return ()
        scored: list[tuple[float, str]] = []
        for embedded in self._connection.execute(
            """
            SELECT knowledge_node_id, vector_blob
            FROM knowledge_node_embeddings
            WHERE knowledge_embedding_index_revision_id = ?
            """,
            (str(row["knowledge_embedding_index_revision_id"]),),
        ).fetchall():
            vector = self._unpack(bytes(embedded["vector_blob"]), profile.dimension)
            score = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            scored.append((score, str(embedded["knowledge_node_id"])))
        ranked = sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]
        return tuple(
            DenseCandidate(node_id, rank, score)
            for rank, (score, node_id) in enumerate(ranked, start=1)
        )

    def current_index_id(
        self,
        profile: EmbeddingProfile,
        corpus_roles: tuple[CorpusRole, ...],
        source_set_fingerprint: str,
    ) -> str | None:
        row = self._connection.execute(
            """
            SELECT knowledge_embedding_index_revision_id
            FROM knowledge_embedding_index_revisions
            WHERE corpus_roles_json = ? AND embedding_profile_revision = ?
              AND provider_kind = ? AND model_name = ? AND vector_dimension = ?
              AND normalization = ? AND source_set_fingerprint = ?
            ORDER BY created_revision DESC LIMIT 1
            """,
            (
                self._roles_json(corpus_roles),
                profile.profile_revision,
                profile.provider_kind,
                profile.model_name,
                profile.dimension,
                profile.normalization,
                source_set_fingerprint,
            ),
        ).fetchone()
        return None if row is None else str(row["knowledge_embedding_index_revision_id"])

    @staticmethod
    def _roles_json(corpus_roles: tuple[CorpusRole, ...]) -> str:
        return json.dumps(
            sorted({role.value for role in corpus_roles}),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _pack(vector: tuple[float, ...]) -> bytes:
        return struct.pack(f"<{len(vector)}f", *vector)

    @staticmethod
    def _unpack(payload: bytes, dimension: int) -> tuple[float, ...]:
        if len(payload) != dimension * 4:
            raise ValueError("stored embedding dimension does not match its profile")
        return tuple(struct.unpack(f"<{dimension}f", payload))
