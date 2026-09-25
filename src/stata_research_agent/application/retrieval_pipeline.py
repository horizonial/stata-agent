"""Replaceable query planning, reranking, and evidence selection for local RAG.

The deterministic implementations are deliberately useful without another model call.  The
contracts are the stable boundary: a future model query planner or cross-encoder reranker can
replace them without changing retrieval sessions, provenance, or the Workspace ledger.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from stata_research_agent.application.knowledge_retrieval import RetrievalMode, retrieval_tokens


class QueryVariantKind(StrEnum):
    ORIGINAL = "original"
    OBJECTIVE = "objective"
    KEYWORD = "keyword"
    SUBQUESTION = "subquestion"


@dataclass(frozen=True, slots=True)
class RetrievalQueryVariant:
    ordinal: int
    kind: QueryVariantKind
    query: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.ordinal < 1 or not self.query.strip() or not self.reason_code.strip():
            raise ValueError("query variant requires order, query text, and a reason")


@dataclass(frozen=True, slots=True)
class RetrievalQueryPlan:
    original_query: str
    planner_policy_revision: str
    variants: tuple[RetrievalQueryVariant, ...]

    def __post_init__(self) -> None:
        if not self.original_query.strip() or not self.planner_policy_revision.strip():
            raise ValueError("query plan identity is incomplete")
        if not self.variants or self.variants[0].kind is not QueryVariantKind.ORIGINAL:
            raise ValueError("query plan must preserve the original query as its first variant")
        if tuple(item.ordinal for item in self.variants) != tuple(
            range(1, len(self.variants) + 1)
        ):
            raise ValueError("query variants must have contiguous ordinals")


class RetrievalQueryPlanner(Protocol):
    @property
    def policy_revision(self) -> str: ...

    def plan(
        self,
        query: str,
        objective: str,
        mode: RetrievalMode,
        *,
        public_subquestion: str | None = None,
    ) -> RetrievalQueryPlan: ...


class DeterministicQueryPlanner:
    """Conservative local query expansion that never invents facts or translations."""

    policy_revision = "deterministic-query-planner/v1"
    _stop_words = {
        "a",
        "an",
        "and",
        "are",
        "for",
        "from",
        "how",
        "in",
        "is",
        "of",
        "the",
        "to",
        "what",
        "which",
        "with",
        "为什么",
        "什么",
        "如何",
        "是否",
        "可以",
        "需要",
        "进行",
    }

    def plan(
        self,
        query: str,
        objective: str,
        mode: RetrievalMode,
        *,
        public_subquestion: str | None = None,
    ) -> RetrievalQueryPlan:
        original = _normalize_space(query)
        if not original:
            raise ValueError("query planner requires an original query")
        candidates: list[tuple[QueryVariantKind, str, str]] = [
            (QueryVariantKind.ORIGINAL, original, "preserve_user_or_agent_query")
        ]
        subquestion = _normalize_space(public_subquestion or "")
        if subquestion and not _semantically_duplicate(original, subquestion):
            candidates.append(
                (
                    QueryVariantKind.SUBQUESTION,
                    subquestion,
                    "retrieve_against_current_public_subquestion",
                )
            )
        normalized_objective = _normalize_space(objective)
        objective_is_current_scope = mode is not RetrievalMode.MULTI_HOP or bool(subquestion)
        if (
            objective_is_current_scope
            and
            normalized_objective
            and not _semantically_duplicate(original, normalized_objective)
            and not any(
                _semantically_duplicate(normalized_objective, item[1]) for item in candidates
            )
        ):
            candidates.append(
                (
                    QueryVariantKind.OBJECTIVE,
                    normalized_objective,
                    "recover_terms_present_in_retrieval_objective",
                )
            )
        keyword_query = self._keyword_query(
            original,
            subquestion,
            normalized_objective if objective_is_current_scope else "",
        )
        if keyword_query and not any(
            keyword_query.casefold() == item[1].casefold() for item in candidates
        ):
            candidates.append(
                (
                    QueryVariantKind.KEYWORD,
                    keyword_query,
                    "remove_question_scaffolding_and_preserve_domain_terms",
                )
            )
        # Direct lookup should stay narrow.  Broader modes may use the complete conservative
        # plan, while all modes retain the exact original query.
        maximum = 2 if mode is RetrievalMode.HELP else 3 if mode is RetrievalMode.DIRECT else 4
        variants = tuple(
            RetrievalQueryVariant(index, kind, text, reason)
            for index, (kind, text, reason) in enumerate(candidates[:maximum], start=1)
        )
        return RetrievalQueryPlan(original, self.policy_revision, variants)

    def _keyword_query(self, *values: str) -> str:
        ordered: list[str] = []
        seen: set[str] = set()
        for value in values:
            for token in re.findall(r"[A-Za-z0-9][\w.-]{1,}|[\u4e00-\u9fff]{2,}", value):
                normalized = token.casefold()
                if normalized in self._stop_words or normalized in seen:
                    continue
                seen.add(normalized)
                ordered.append(token)
                if len(ordered) == 18:
                    return " ".join(ordered)
        return " ".join(ordered)


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    node_id: str
    source_locator: str
    node_kind: str
    section_title: str | None
    content: str
    retrieval_score: float
    lexical_rank: int | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    query_variant_ordinals: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RerankAssessment:
    node_id: str
    score: float
    relevance_label: str
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.node_id or not 0 <= self.score <= 1:
            raise ValueError("rerank assessment is invalid")
        if self.relevance_label not in {
            "direct_support",
            "partial_support",
            "background",
            "low_relevance",
        }:
            raise ValueError("rerank relevance label is invalid")


class EvidenceReranker(Protocol):
    @property
    def policy_revision(self) -> str: ...

    def rerank(
        self,
        original_query: str,
        objective: str,
        candidates: tuple[RerankCandidate, ...],
    ) -> tuple[RerankAssessment, ...]: ...


@dataclass(frozen=True, slots=True)
class EvidenceSufficiencyDecision:
    status: str
    highest_score: float
    reason_codes: tuple[str, ...]

    @property
    def answer_allowed(self) -> bool:
        return self.status == "supported"


@dataclass(frozen=True, slots=True)
class EvidenceSufficiencyGate:
    """Hard retrieval-to-generation boundary for grounded factual answers."""

    minimum_support_score: float = 0.35
    policy_revision: str = "evidence-sufficiency-gate/v1"

    def assess(
        self, assessments: tuple[RerankAssessment, ...]
    ) -> EvidenceSufficiencyDecision:
        if not assessments:
            return EvidenceSufficiencyDecision(
                "insufficient_evidence", 0.0, ("no_retrieved_evidence",)
            )
        highest_score = max(item.score for item in assessments)
        supported = any(
            item.score >= self.minimum_support_score
            and item.relevance_label in {"direct_support", "partial_support"}
            for item in assessments
        )
        if not supported:
            return EvidenceSufficiencyDecision(
                "insufficient_evidence",
                highest_score,
                ("no_candidate_crossed_support_boundary",),
            )
        return EvidenceSufficiencyDecision(
            "supported", highest_score, ("supported_candidate_present",)
        )


class DeterministicEvidenceReranker:
    """Local second-stage reranker used until an evaluated model profile is adopted.

    Unlike RRF, this stage scores candidate content against the original question and objective.
    It intentionally exposes a model-neutral contract for a future cross-encoder.
    """

    policy_revision = "deterministic-evidence-reranker/v1"

    def rerank(
        self,
        original_query: str,
        objective: str,
        candidates: tuple[RerankCandidate, ...],
    ) -> tuple[RerankAssessment, ...]:
        query_tokens = retrieval_tokens(original_query)
        objective_tokens = retrieval_tokens(objective)
        maximum_retrieval_score = max(
            (candidate.retrieval_score for candidate in candidates), default=1.0
        )
        assessments: list[RerankAssessment] = []
        for candidate in candidates:
            content_tokens = retrieval_tokens(candidate.content)
            section_tokens = retrieval_tokens(candidate.section_title or "")
            query_coverage = _coverage(query_tokens, content_tokens | section_tokens)
            objective_coverage = _coverage(objective_tokens, content_tokens | section_tokens)
            exact_phrase = int(
                len(original_query.strip()) >= 8
                and original_query.casefold() in candidate.content.casefold()
            )
            section_match = _coverage(query_tokens, section_tokens)
            retrieval_prior = (
                candidate.retrieval_score / maximum_retrieval_score
                if maximum_retrieval_score > 0
                else 0.0
            )
            score = min(
                1.0,
                0.48 * query_coverage
                + 0.22 * objective_coverage
                + 0.10 * section_match
                + 0.15 * retrieval_prior
                + 0.05 * exact_phrase,
            )
            if query_coverage >= 0.6 or exact_phrase:
                label = "direct_support"
            elif query_coverage >= 0.25 or objective_coverage >= 0.35:
                label = "partial_support"
            elif score >= 0.18:
                label = "background"
            else:
                label = "low_relevance"
            reasons = ["original_query_overlap", "retrieval_prior"]
            if objective_coverage:
                reasons.append("objective_overlap")
            if section_match:
                reasons.append("section_match")
            if exact_phrase:
                reasons.append("exact_phrase")
            assessments.append(
                RerankAssessment(candidate.node_id, score, label, tuple(reasons))
            )
        return tuple(
            sorted(assessments, key=lambda item: (-item.score, item.node_id))
        )


def select_diverse_evidence(
    ordered: tuple[RerankAssessment, ...],
    candidates: tuple[RerankCandidate, ...],
    *,
    limit: int,
) -> tuple[RerankAssessment, ...]:
    """Prefer source and text diversity, then fill remaining slots by rerank score."""

    if not 1 <= limit <= 24:
        raise ValueError("evidence selection limit is invalid")
    candidate_by_id = {candidate.node_id: candidate for candidate in candidates}
    per_source_soft_cap = max(2, math.ceil(limit * 0.5))
    source_counts: dict[str, int] = {}
    content_fingerprints: set[str] = set()
    selected: list[RerankAssessment] = []
    deferred: list[RerankAssessment] = []
    for assessment in ordered:
        candidate = candidate_by_id[assessment.node_id]
        fingerprint = _normalized_fingerprint(candidate.content)
        if fingerprint in content_fingerprints:
            continue
        if source_counts.get(candidate.source_locator, 0) >= per_source_soft_cap:
            deferred.append(assessment)
            continue
        selected.append(assessment)
        content_fingerprints.add(fingerprint)
        source_counts[candidate.source_locator] = (
            source_counts.get(candidate.source_locator, 0) + 1
        )
        if len(selected) == limit:
            return tuple(selected)
    for assessment in deferred:
        candidate = candidate_by_id[assessment.node_id]
        fingerprint = _normalized_fingerprint(candidate.content)
        if fingerprint in content_fingerprints:
            continue
        selected.append(assessment)
        content_fingerprints.add(fingerprint)
        if len(selected) == limit:
            break
    return tuple(selected)


def _normalize_space(value: str) -> str:
    return " ".join(value.split())


def _semantically_duplicate(left: str, right: str) -> bool:
    left_tokens = retrieval_tokens(left)
    right_tokens = retrieval_tokens(right)
    if not left_tokens or not right_tokens:
        return left.casefold() == right.casefold()
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) >= 0.86


def _coverage(needles: set[str], haystack: set[str]) -> float:
    return 0.0 if not needles else len(needles & haystack) / len(needles)


def _normalized_fingerprint(content: str) -> str:
    return re.sub(r"\W+", "", content.casefold())[:1000]
