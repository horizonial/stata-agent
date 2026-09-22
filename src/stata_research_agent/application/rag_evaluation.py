"""Deterministic offline evaluation for canonical knowledge retrieval.

The evaluator scores retrieval and corpus isolation.  It does not grade research quality,
replace Runtime Evaluation, or use generated prose as a gold source.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from stata_research_agent.application.knowledge_retrieval import (
    CanonicalKnowledgeNodeDraft,
    CorpusRole,
    KnowledgeRetrievalHit,
)


@dataclass(frozen=True, slots=True)
class ParserGoldCase:
    case_id: str
    required_node_kinds: tuple[str, ...]
    required_content_markers: tuple[str, ...]
    locator_required_markers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.required_node_kinds:
            raise ValueError("parser gold case requires identity and node kinds")


@dataclass(frozen=True, slots=True)
class ParserCaseScore:
    case_id: str
    node_kind_recall: float
    content_marker_recall: float
    locator_coverage: float
    missing_node_kinds: tuple[str, ...]
    missing_content_markers: tuple[str, ...]
    missing_locators: tuple[str, ...]


def evaluate_parser_case(
    case: ParserGoldCase, nodes: tuple[CanonicalKnowledgeNodeDraft, ...]
) -> ParserCaseScore:
    observed_kinds = {node.node_kind.value for node in nodes}
    missing_kinds = tuple(
        kind for kind in case.required_node_kinds if kind not in observed_kinds
    )
    missing_markers = tuple(
        marker
        for marker in case.required_content_markers
        if not any(marker.casefold() in node.text.casefold() for node in nodes)
    )
    missing_locators = tuple(
        marker
        for marker in case.locator_required_markers
        if not any(
            marker.casefold() in node.text.casefold()
            and node.page_start is not None
            and node.page_end is not None
            for node in nodes
        )
    )
    return ParserCaseScore(
        case.case_id,
        1 - (len(missing_kinds) / len(case.required_node_kinds)),
        (
            1.0
            if not case.required_content_markers
            else 1 - (len(missing_markers) / len(case.required_content_markers))
        ),
        (
            1.0
            if not case.locator_required_markers
            else 1 - (len(missing_locators) / len(case.locator_required_markers))
        ),
        missing_kinds,
        missing_markers,
        missing_locators,
    )


@dataclass(frozen=True, slots=True)
class RagGoldCase:
    case_id: str
    query: str
    corpus_roles: tuple[CorpusRole, ...]
    expected_source_locators: tuple[str, ...] = ()
    expected_content_markers: tuple[str, ...] = ()
    evidence_groups: tuple[tuple[str, ...], ...] = ()
    k: int = 6

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.query.strip():
            raise ValueError("RAG gold cases require identity and query")
        if not self.corpus_roles or not 1 <= self.k <= 24:
            raise ValueError("RAG gold cases require corpus roles and a valid k")
        if not self.expected_source_locators and not self.expected_content_markers:
            raise ValueError("RAG gold cases require at least one expected evidence unit")


@dataclass(frozen=True, slots=True)
class RagCaseScore:
    case_id: str
    recall_at_k: float
    precision_at_k: float
    reciprocal_rank: float
    evidence_group_coverage: float
    source_diversity: float
    role_leak_count: int
    retrieved_count: int
    missing_expectations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RagEvaluationReport:
    policy_revision: str
    case_scores: tuple[RagCaseScore, ...]
    macro_recall_at_k: float
    macro_precision_at_k: float
    mean_reciprocal_rank: float
    macro_evidence_group_coverage: float
    total_role_leaks: int

    @property
    def passed_role_isolation(self) -> bool:
        return self.total_role_leaks == 0

    def accepts(self, thresholds: RagAcceptanceThresholds) -> bool:
        return (
            self.macro_recall_at_k >= thresholds.minimum_macro_recall_at_k
            and self.macro_precision_at_k >= thresholds.minimum_macro_precision_at_k
            and self.mean_reciprocal_rank >= thresholds.minimum_mean_reciprocal_rank
            and self.macro_evidence_group_coverage
            >= thresholds.minimum_evidence_group_coverage
            and self.total_role_leaks <= thresholds.maximum_role_leaks
        )


@dataclass(frozen=True, slots=True)
class RagAcceptanceThresholds:
    minimum_macro_recall_at_k: float = 0.80
    minimum_macro_precision_at_k: float = 0.10
    minimum_mean_reciprocal_rank: float = 0.50
    minimum_evidence_group_coverage: float = 0.80
    maximum_role_leaks: int = 0

    def __post_init__(self) -> None:
        rates = (
            self.minimum_macro_recall_at_k,
            self.minimum_macro_precision_at_k,
            self.minimum_mean_reciprocal_rank,
            self.minimum_evidence_group_coverage,
        )
        if any(rate < 0 or rate > 1 for rate in rates) or self.maximum_role_leaks < 0:
            raise ValueError("RAG acceptance thresholds are outside valid bounds")


@dataclass(frozen=True, slots=True)
class RagTraceHop:
    """One public, auditable hop from a persisted Retrieval Session."""

    public_subquestion: str
    hits: tuple[KnowledgeRetrievalHit, ...]
    stop_reason: str

    def __post_init__(self) -> None:
        if not self.public_subquestion.strip() or not self.stop_reason.strip():
            raise ValueError("RAG trace hops require a public subquestion and stop reason")


@dataclass(frozen=True, slots=True)
class RagTraceGoldCase:
    case_id: str
    corpus_roles: tuple[CorpusRole, ...]
    evidence_groups: tuple[tuple[str, ...], ...]
    minimum_hops: int = 2
    maximum_hops: int = 6
    minimum_novel_node_ratio: float = 0.25
    accepted_terminal_reasons: tuple[str, ...] = ("agent_concluded", "coverage_satisfied")

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.corpus_roles or not self.evidence_groups:
            raise ValueError("RAG trace gold cases require identity, roles, and evidence groups")
        if not 1 <= self.minimum_hops <= self.maximum_hops:
            raise ValueError("RAG trace hop bounds are invalid")
        if not 0 <= self.minimum_novel_node_ratio <= 1:
            raise ValueError("RAG trace novelty threshold is invalid")
        if not self.accepted_terminal_reasons:
            raise ValueError("RAG trace requires an accepted terminal reason")


@dataclass(frozen=True, slots=True)
class RagTraceScore:
    case_id: str
    hop_count: int
    evidence_group_coverage: float
    novel_node_ratio: float
    source_diversity: float
    role_leak_count: int
    terminal_reason: str | None
    missing_evidence_groups: tuple[tuple[str, ...], ...]
    passed: bool


@dataclass(frozen=True, slots=True)
class RagGroundedClaim:
    claim_id: str
    text: str
    cited_node_ids: tuple[str, ...]
    required_support_markers: tuple[str, ...]
    allowed_corpus_roles: tuple[CorpusRole, ...] = (CorpusRole.LITERATURE_EVIDENCE,)

    def __post_init__(self) -> None:
        if not self.claim_id.strip() or not self.text.strip():
            raise ValueError("grounded claims require identity and text")
        if not self.required_support_markers or not self.allowed_corpus_roles:
            raise ValueError("grounded claims require support markers and allowed roles")


@dataclass(frozen=True, slots=True)
class RagGroundingCase:
    case_id: str
    answer_text: str
    claims: tuple[RagGroundedClaim, ...]
    maximum_style_copy_run: int = 16

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.answer_text.strip() or not self.claims:
            raise ValueError("RAG grounding cases require identity, answer, and claims")
        if self.maximum_style_copy_run < 4:
            raise ValueError("style-copy threshold must be at least four tokens")


@dataclass(frozen=True, slots=True)
class RagGroundingScore:
    case_id: str
    claim_citation_coverage: float
    citation_identity_validity: float
    support_marker_coverage: float
    evidence_role_leak_count: int
    longest_style_copy_run: int
    missing_claim_citations: tuple[str, ...]
    missing_support_markers: tuple[str, ...]
    unknown_citation_node_ids: tuple[str, ...]
    passed: bool


def evaluate_retrieval_trace(
    case: RagTraceGoldCase, hops: tuple[RagTraceHop, ...]
) -> RagTraceScore:
    """Score multi-hop retrieval without using an LLM-as-judge.

    The Agent remains free to choose its subquestions.  The evaluator only checks the
    externally visible consequences: bounded hops, new canonical nodes, evidence-group
    coverage, corpus isolation, and an explicit terminal reason.
    """

    allowed_roles = {role.value for role in case.corpus_roles}
    flattened_hits = tuple(hit for hop in hops for hit in hop.hits)
    role_leaks = sum(hit.corpus_role not in allowed_roles for hit in flattened_hits)
    missing_groups = tuple(
        group
        for group in case.evidence_groups
        if not any(
            marker.casefold() in hit.content.casefold()
            for marker in group
            for hit in flattened_hits
        )
    )
    group_coverage = 1 - (len(missing_groups) / len(case.evidence_groups))
    first_hop_nodes = {hit.node_id for hit in hops[0].hits} if hops else set()
    later_nodes = {hit.node_id for hop in hops[1:] for hit in hop.hits}
    novel_nodes = later_nodes - first_hop_nodes
    novel_ratio = 0.0 if not later_nodes else len(novel_nodes) / len(later_nodes)
    unique_sources = {hit.source_locator for hit in flattened_hits}
    source_diversity = (
        0.0 if not flattened_hits else len(unique_sources) / len(flattened_hits)
    )
    terminal_reason = hops[-1].stop_reason if hops else None
    passed = (
        case.minimum_hops <= len(hops) <= case.maximum_hops
        and group_coverage == 1.0
        and novel_ratio >= case.minimum_novel_node_ratio
        and role_leaks == 0
        and terminal_reason in case.accepted_terminal_reasons
    )
    return RagTraceScore(
        case.case_id,
        len(hops),
        group_coverage,
        novel_ratio,
        source_diversity,
        role_leaks,
        terminal_reason,
        missing_groups,
        passed,
    )


def evaluate_grounded_answer(
    case: RagGroundingCase,
    evidence_nodes: dict[str, KnowledgeRetrievalHit],
    *,
    style_nodes: tuple[KnowledgeRetrievalHit, ...] = (),
) -> RagGroundingScore:
    """Check answer attribution and style contamination without an LLM judge."""

    missing_claim_citations = tuple(
        claim.claim_id for claim in case.claims if not claim.cited_node_ids
    )
    all_citations = tuple(
        node_id for claim in case.claims for node_id in claim.cited_node_ids
    )
    unknown_citations = tuple(
        dict.fromkeys(node_id for node_id in all_citations if node_id not in evidence_nodes)
    )
    unknown_citation_occurrences = sum(
        node_id not in evidence_nodes for node_id in all_citations
    )
    missing_support: list[str] = []
    role_leaks = 0
    support_total = 0
    for claim in case.claims:
        cited = tuple(
            evidence_nodes[node_id]
            for node_id in claim.cited_node_ids
            if node_id in evidence_nodes
        )
        allowed_roles = {role.value for role in claim.allowed_corpus_roles}
        role_leaks += sum(node.corpus_role not in allowed_roles for node in cited)
        for marker in claim.required_support_markers:
            support_total += 1
            if not any(marker.casefold() in node.content.casefold() for node in cited):
                missing_support.append(f"{claim.claim_id}:{marker}")
    longest_style_copy = max(
        (_longest_common_token_run(case.answer_text, node.content) for node in style_nodes),
        default=0,
    )
    citation_count = len(all_citations)
    claim_coverage = 1 - (len(missing_claim_citations) / len(case.claims))
    identity_validity = (
        1.0
        if citation_count == 0
        else 1 - (unknown_citation_occurrences / citation_count)
    )
    support_coverage = (
        1.0 if support_total == 0 else 1 - (len(missing_support) / support_total)
    )
    passed = (
        claim_coverage == 1.0
        and identity_validity == 1.0
        and support_coverage == 1.0
        and role_leaks == 0
        and longest_style_copy <= case.maximum_style_copy_run
    )
    return RagGroundingScore(
        case.case_id,
        claim_coverage,
        identity_validity,
        support_coverage,
        role_leaks,
        longest_style_copy,
        missing_claim_citations,
        tuple(missing_support),
        unknown_citations,
        passed,
    )


def _longest_common_token_run(left: str, right: str) -> int:
    left_tokens = tuple(re.findall(r"[\w'-]+", left.casefold()))
    right_tokens = tuple(re.findall(r"[\w'-]+", right.casefold()))
    if not left_tokens or not right_tokens:
        return 0
    previous = [0] * (len(right_tokens) + 1)
    longest = 0
    for left_token in left_tokens:
        current = [0]
        for index, right_token in enumerate(right_tokens, start=1):
            run = previous[index - 1] + 1 if left_token == right_token else 0
            current.append(run)
            longest = max(longest, run)
        previous = current
    return longest


RetrievalFunction = Callable[
    [str, tuple[CorpusRole, ...], int], tuple[KnowledgeRetrievalHit, ...]
]


def evaluate_rag(
    cases: tuple[RagGoldCase, ...],
    retrieve: RetrievalFunction,
    *,
    policy_revision: str = "rag-eval-v1",
) -> RagEvaluationReport:
    if not cases:
        raise ValueError("RAG evaluation requires at least one case")
    scores: list[RagCaseScore] = []
    for case in cases:
        hits = retrieve(case.query, case.corpus_roles, case.k)
        scores.append(_score_case(case, hits))
    count = len(scores)
    return RagEvaluationReport(
        policy_revision,
        tuple(scores),
        sum(score.recall_at_k for score in scores) / count,
        sum(score.precision_at_k for score in scores) / count,
        sum(score.reciprocal_rank for score in scores) / count,
        sum(score.evidence_group_coverage for score in scores) / count,
        sum(score.role_leak_count for score in scores),
    )


def _score_case(
    case: RagGoldCase, hits: tuple[KnowledgeRetrievalHit, ...]
) -> RagCaseScore:
    allowed_roles = {role.value for role in case.corpus_roles}
    role_leaks = sum(hit.corpus_role not in allowed_roles for hit in hits)
    expected = tuple(
        [f"source:{locator}" for locator in case.expected_source_locators]
        + [f"text:{marker}" for marker in case.expected_content_markers]
    )
    satisfied: set[str] = set()
    relevant_hit_count = 0
    first_relevant_rank = 0
    lowered_markers = {
        marker: marker.casefold() for marker in case.expected_content_markers
    }
    for rank, hit in enumerate(hits, start=1):
        matched = False
        if hit.source_locator in case.expected_source_locators:
            satisfied.add(f"source:{hit.source_locator}")
            matched = True
        lowered_content = hit.content.casefold()
        for marker, lowered in lowered_markers.items():
            if lowered in lowered_content:
                satisfied.add(f"text:{marker}")
                matched = True
        if matched and first_relevant_rank == 0:
            first_relevant_rank = rank
        if matched:
            relevant_hit_count += 1
    groups_satisfied = 0
    for group in case.evidence_groups:
        if any(marker.casefold() in hit.content.casefold() for marker in group for hit in hits):
            groups_satisfied += 1
    group_coverage = (
        groups_satisfied / len(case.evidence_groups) if case.evidence_groups else 1.0
    )
    unique_sources = len({hit.source_locator for hit in hits})
    return RagCaseScore(
        case.case_id,
        len(satisfied) / len(expected),
        relevant_hit_count / case.k,
        0.0 if first_relevant_rank == 0 else 1.0 / first_relevant_rank,
        group_coverage,
        0.0 if not hits else unique_sources / len(hits),
        role_leaks,
        len(hits),
        tuple(item for item in expected if item not in satisfied),
    )
