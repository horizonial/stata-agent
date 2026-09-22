"""Replaceable embedding and flat-cosine contracts for canonical Knowledge Nodes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from stata_research_agent.application.knowledge_retrieval import CorpusRole
from stata_research_agent.domain.identifiers import CommandId


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    profile_revision: str
    provider_kind: str
    model_name: str
    dimension: int
    normalization: str = "l2"

    def __post_init__(self) -> None:
        if not all(
            value.strip() for value in (self.profile_revision, self.provider_kind, self.model_name)
        ):
            raise ValueError("embedding profile identity is incomplete")
        if self.dimension < 1 or self.normalization != "l2":
            raise ValueError("embedding profile requires a positive L2-normalized dimension")


@dataclass(frozen=True, slots=True)
class DenseIndexNode:
    node_id: str
    content_sha256: str
    content: str


@dataclass(frozen=True, slots=True)
class DenseCandidate:
    node_id: str
    dense_rank: int
    dense_score: float


@dataclass(frozen=True, slots=True)
class DenseUpgradeEvaluationCase:
    query: str
    relevant_node_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.query.strip() or not self.relevant_node_ids:
            raise ValueError("dense upgrade evaluation case is incomplete")


@dataclass(frozen=True, slots=True)
class DenseIndexUpgradeAssessment:
    old_index_revision_id: str
    candidate_index_revision_id: str
    evaluation_set_sha256: str
    old_recall_at_k: float
    candidate_recall_at_k: float
    regressed_case_count: int
    eligible_for_adoption: bool


class EmbeddingGateway(Protocol):
    @property
    def profile(self) -> EmbeddingProfile: ...

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...

    def embed_query(self, text: str) -> tuple[float, ...]: ...


class DenseIndexRepository(Protocol):
    def current_nodes(self, corpus_roles: tuple[CorpusRole, ...]) -> tuple[DenseIndexNode, ...]: ...

    def replace_index(
        self,
        command_id: CommandId,
        profile: EmbeddingProfile,
        corpus_roles: tuple[CorpusRole, ...],
        nodes: tuple[DenseIndexNode, ...],
        vectors: tuple[tuple[float, ...], ...],
        source_set_fingerprint: str,
    ) -> str: ...

    def current_index_id(
        self,
        profile: EmbeddingProfile,
        corpus_roles: tuple[CorpusRole, ...],
        source_set_fingerprint: str,
    ) -> str | None: ...

    def search_index(
        self,
        profile: EmbeddingProfile,
        corpus_roles: tuple[CorpusRole, ...],
        query_vector: tuple[float, ...],
        limit: int,
    ) -> tuple[DenseCandidate, ...]: ...


class DenseCandidateProvider(Protocol):
    def search(
        self, query: str, corpus_roles: tuple[CorpusRole, ...], limit: int
    ) -> tuple[DenseCandidate, ...]: ...


class DenseKnowledgeIndexService:
    def __init__(self, repository: DenseIndexRepository, gateway: EmbeddingGateway) -> None:
        self._repository = repository
        self._gateway = gateway

    def rebuild(self, command_id: CommandId, corpus_roles: tuple[CorpusRole, ...]) -> str:
        if not corpus_roles or len(set(corpus_roles)) != len(corpus_roles):
            raise ValueError("dense index requires unique corpus roles")
        nodes = self._repository.current_nodes(corpus_roles)
        if not nodes:
            raise ValueError("dense index has no current canonical nodes")
        vectors = self._gateway.embed_documents(tuple(node.content for node in nodes))
        self._validate_vectors(vectors, len(nodes))
        fingerprint = self._fingerprint(nodes)
        return self._repository.replace_index(
            command_id,
            self._gateway.profile,
            corpus_roles,
            nodes,
            vectors,
            fingerprint,
        )

    def ensure_current(
        self, command_id: CommandId, corpus_roles: tuple[CorpusRole, ...]
    ) -> str | None:
        nodes = self._repository.current_nodes(corpus_roles)
        if not nodes:
            return None
        fingerprint = self._fingerprint(nodes)
        existing = self._repository.current_index_id(
            self._gateway.profile, corpus_roles, fingerprint
        )
        if existing is not None:
            return existing
        return self.rebuild(command_id, corpus_roles)

    def search(
        self, query: str, corpus_roles: tuple[CorpusRole, ...], limit: int
    ) -> tuple[DenseCandidate, ...]:
        if not query.strip() or not 1 <= limit <= 96:
            raise ValueError("dense query and limit are invalid")
        vectors = (self._gateway.embed_query(query),)
        self._validate_vectors(vectors, 1)
        return self._repository.search_index(self._gateway.profile, corpus_roles, vectors[0], limit)

    def _validate_vectors(
        self, vectors: tuple[tuple[float, ...], ...], expected_count: int
    ) -> None:
        if len(vectors) != expected_count or any(
            len(vector) != self._gateway.profile.dimension for vector in vectors
        ):
            raise ValueError("embedding response does not match the versioned profile")
        for vector in vectors:
            norm = sum(value * value for value in vector) ** 0.5
            if abs(norm - 1.0) > 1e-3:
                raise ValueError("embedding vector is not L2-normalized")

    @staticmethod
    def _fingerprint(nodes: tuple[DenseIndexNode, ...]) -> str:
        return hashlib.sha256(
            "\n".join(f"{node.node_id}:{node.content_sha256}" for node in nodes).encode()
        ).hexdigest()


def assess_dense_index_upgrade(
    *,
    old_index_revision_id: str,
    candidate_index_revision_id: str,
    cases: tuple[DenseUpgradeEvaluationCase, ...],
    old_search: Callable[[str, int], tuple[DenseCandidate, ...]],
    candidate_search: Callable[[str, int], tuple[DenseCandidate, ...]],
    k: int = 10,
    minimum_candidate_recall: float = 0.8,
    maximum_recall_regression: float = 0.02,
) -> DenseIndexUpgradeAssessment:
    """Compare immutable old/candidate indexes before an explicit configuration cutover."""

    if not old_index_revision_id or not candidate_index_revision_id or not cases:
        raise ValueError("dense upgrade assessment identities and cases are required")
    if old_index_revision_id == candidate_index_revision_id or k < 1:
        raise ValueError("dense upgrade requires distinct revisions and positive k")
    if not 0 <= minimum_candidate_recall <= 1 or not 0 <= maximum_recall_regression <= 1:
        raise ValueError("dense upgrade thresholds must be proportions")

    old_total = candidate_total = relevant_total = regressed = 0
    serialized_cases: list[dict[str, object]] = []
    for case in cases:
        relevant = set(case.relevant_node_ids)
        old_hits = {item.node_id for item in old_search(case.query, k)}
        candidate_hits = {item.node_id for item in candidate_search(case.query, k)}
        old_case_hits = len(relevant & old_hits)
        candidate_case_hits = len(relevant & candidate_hits)
        old_total += old_case_hits
        candidate_total += candidate_case_hits
        relevant_total += len(relevant)
        regressed += candidate_case_hits < old_case_hits
        serialized_cases.append(
            {"query": case.query, "relevant_node_ids": list(case.relevant_node_ids)}
        )
    old_recall = old_total / relevant_total
    candidate_recall = candidate_total / relevant_total
    eligible = (
        candidate_recall >= minimum_candidate_recall
        and candidate_recall + maximum_recall_regression >= old_recall
        and regressed == 0
    )
    evaluation_hash = hashlib.sha256(repr(serialized_cases).encode("utf-8")).hexdigest()
    return DenseIndexUpgradeAssessment(
        old_index_revision_id,
        candidate_index_revision_id,
        evaluation_hash,
        old_recall,
        candidate_recall,
        regressed,
        eligible,
    )
