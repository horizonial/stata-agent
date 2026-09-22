"""Open, provenance-preserving retrieval contracts for Workspace literature.

Retrieval hints help the Agent decide what to inspect. They never select a research method,
authorize a tool, or qualify a statistical result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from stata_research_agent.domain.identifiers import CommandId, TurnId


class CorpusRole(StrEnum):
    LITERATURE_EVIDENCE = "literature_evidence"
    STATA_HELP = "stata_help"
    STYLE_EXEMPLAR = "style_exemplar"


class KnowledgeSourceKind(StrEnum):
    LOCAL_FILE = "local_file"
    WEB_CAPTURE = "web_capture"
    STATA_HELP = "stata_help"
    IMPORTED = "imported"


class KnowledgeNodeKind(StrEnum):
    DOCUMENT = "document"
    TITLE = "title"
    SECTION = "section"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    TABLE_CELL = "table_cell"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    FIGURE = "figure"
    EQUATION = "equation"
    REFERENCE = "reference"
    CITATION = "citation"
    CODE = "code"
    HELP_TOPIC = "help_topic"
    LEGACY_CHUNK = "legacy_chunk"


class RetrievalMode(StrEnum):
    DIRECT = "direct"
    HIERARCHICAL = "hierarchical"
    MULTI_HOP = "multi_hop"
    GLOBAL_SYNTHESIS = "global_synthesis"
    STYLE = "style"
    HELP = "help"


@dataclass(frozen=True, slots=True)
class AgenticRetrievalPolicy:
    """Execution guardrails for an Agent-directed retrieval session.

    This policy bounds loops and detects lack of progress.  It does not prescribe research
    questions, methods, search queries, or what evidence the Agent should accept.
    """

    policy_revision: str = "agentic-retrieval/v1"
    maximum_hops: int = 6

    def __post_init__(self) -> None:
        if not self.policy_revision.strip() or not 2 <= self.maximum_hops <= 32:
            raise ValueError("invalid agentic retrieval policy")


@dataclass(frozen=True, slots=True)
class ExtractedKnowledgePage:
    page_number: int | None
    text: str


@dataclass(frozen=True, slots=True)
class PageParseFinding:
    """Auditable reason for retaining or enriching one PDF page."""

    page_number: int
    reason_codes: tuple[str, ...]
    diagnostic_score: int
    selected_parser: str

    def __post_init__(self) -> None:
        if self.page_number < 1 or self.diagnostic_score < 0:
            raise ValueError("page parse finding requires a valid page and score")
        if not self.reason_codes or len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("page parse finding requires unique reason codes")
        if not self.selected_parser.strip():
            raise ValueError("page parse finding requires a selected parser")


@dataclass(frozen=True, slots=True)
class ExtractedKnowledgeDocument:
    relative_path: str
    media_type: str
    content_sha256: str
    source_size: int
    pages: tuple[ExtractedKnowledgePage, ...]
    corpus_roles: tuple[CorpusRole, ...] = (CorpusRole.LITERATURE_EVIDENCE,)
    source_kind: KnowledgeSourceKind = KnowledgeSourceKind.LOCAL_FILE
    parser_name: str = "builtin-fast"
    parser_version: str = "1"
    parser_profile: str = "fast"
    page_parse_findings: tuple[PageParseFinding, ...] = ()
    ingestion_policy_revision: str = "knowledge-ingestion-v2"
    canonical_ir_version: str = "canonical-ir-v2"

    def __post_init__(self) -> None:
        if not self.relative_path.strip() or not self.pages:
            raise ValueError("knowledge document requires a path and extracted pages")
        if not self.corpus_roles or len(set(self.corpus_roles)) != len(self.corpus_roles):
            raise ValueError("knowledge document requires unique corpus roles")
        if not self.ingestion_policy_revision.strip() or not self.canonical_ir_version.strip():
            raise ValueError("knowledge document requires ingestion policy identities")
        finding_pages = tuple(finding.page_number for finding in self.page_parse_findings)
        if len(set(finding_pages)) != len(finding_pages):
            raise ValueError("knowledge document requires at most one finding per page")


@dataclass(frozen=True, slots=True)
class CanonicalKnowledgeNodeDraft:
    local_key: str
    parent_local_key: str | None
    node_kind: KnowledgeNodeKind
    ordinal: int
    text: str
    page_start: int | None = None
    page_end: int | None = None
    semantic_role: str | None = None
    source_span_start: int | None = None
    source_span_end: int | None = None
    structured_payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.local_key or self.ordinal < 1 or not self.text.strip():
            raise ValueError("canonical knowledge nodes require identity, order, and text")
        if (self.source_span_start is None) != (self.source_span_end is None):
            raise ValueError("knowledge node source spans require both bounds")
        if (
            self.source_span_start is not None
            and self.source_span_end is not None
            and (self.source_span_start < 0 or self.source_span_end < self.source_span_start)
        ):
            raise ValueError("knowledge node source span is invalid")


@dataclass(frozen=True, slots=True)
class SyncKnowledgeIndexCommand:
    command_id: CommandId
    documents: tuple[ExtractedKnowledgeDocument, ...]
    observed_relative_paths: tuple[str, ...]
    extraction_errors: tuple[tuple[str, str], ...]
    managed_path_prefixes: tuple[str, ...] = ("literature/", "style-references/")
    policy_revision: str = "knowledge-ingestion-v2"
    reconcile_missing: bool = True


@dataclass(frozen=True, slots=True)
class KnowledgeIndexOutcome:
    scanned_count: int
    indexed_count: int
    unchanged_count: int
    missing_count: int
    error_count: int
    commit_revision: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class RetrievalIntentHint:
    query: str
    concepts: tuple[str, ...]
    suggested_sources: tuple[str, ...]
    reason_codes: tuple[str, ...]
    policy_revision: str = "retrieval-intent-v1"


@dataclass(frozen=True, slots=True)
class StartKnowledgeRetrievalCommand:
    command_id: CommandId
    query: str
    objective: str
    corpus_roles: tuple[CorpusRole, ...] = (CorpusRole.LITERATURE_EVIDENCE,)
    mode: RetrievalMode = RetrievalMode.DIRECT
    limit: int = 6
    turn_id: TurnId | None = None
    policy_revision: str = "hybrid-retrieval-v1"

    def __post_init__(self) -> None:
        if not self.query.strip() or not self.objective.strip():
            raise ValueError("retrieval requires a query and objective")
        if not self.corpus_roles or len(set(self.corpus_roles)) != len(self.corpus_roles):
            raise ValueError("retrieval requires unique corpus roles")
        if not 1 <= self.limit <= 24:
            raise ValueError("retrieval limit must be between 1 and 24")


@dataclass(frozen=True, slots=True)
class ContinueKnowledgeRetrievalCommand:
    command_id: CommandId
    retrieval_session_id: str
    query: str
    public_subquestion: str
    limit: int = 6
    conclude_session: bool = False
    unresolved_items: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.retrieval_session_id.startswith("retrievalsession_"):
            raise ValueError("continuation requires a retrieval session identity")
        if not self.query.strip() or not self.public_subquestion.strip():
            raise ValueError("continuation requires query and public subquestion")
        if not 1 <= self.limit <= 24:
            raise ValueError("retrieval limit must be between 1 and 24")


@dataclass(frozen=True, slots=True)
class ConcludeKnowledgeRetrievalCommand:
    command_id: CommandId
    retrieval_session_id: str
    stop_reason: str = "agent_concluded"

    def __post_init__(self) -> None:
        if not self.retrieval_session_id.startswith("retrievalsession_"):
            raise ValueError("conclusion requires a retrieval session identity")
        if self.stop_reason not in {"agent_concluded", "coverage_satisfied"}:
            raise ValueError("unsupported retrieval conclusion reason")


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalHit:
    node_id: str
    source_revision_id: str
    parse_revision_id: str
    source_locator: str
    corpus_role: str
    node_kind: str
    page_start: int | None
    page_end: int | None
    section_title: str | None
    content: str
    lexical_rank: int | None
    fused_score: float
    dense_rank: int | None = None
    dense_score: float | None = None
    rerank_score: float | None = None
    relevance_label: str | None = None
    query_variant_ordinals: tuple[int, ...] = ()
    rerank_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalOutcome:
    retrieval_session_id: str
    retrieval_hop_id: str
    hits: tuple[KnowledgeRetrievalHit, ...]
    stop_reason: str
    commit_revision: int
    replayed: bool
    hop_ordinal: int = 1
    novel_hit_count: int = 0
    cumulative_hit_count: int = 0
    session_status: str = "completed"
    query_variants: tuple[str, ...] = ()
    query_planner_policy_revision: str = ""
    reranker_policy_revision: str = ""


def infer_retrieval_intent(user_text: str, *, literature_available: bool) -> RetrievalIntentHint:
    """Build a soft retrieval lens without pretending to classify research intent."""

    concepts = tuple(sorted(_tokens(user_text), key=lambda item: (-len(item), item))[:24])
    lowered = user_text.lower()
    reasons: list[str] = ["current_user_message"]
    sources = ["project_memory", "research_state"]
    literature_markers = (
        "literature",
        "paper",
        "citation",
        "reference",
        "theory",
        "method",
        "文献",
        "论文",
        "引用",
        "参考",
        "理论",
        "方法",
        "机制",
    )
    if literature_available:
        sources.append("workspace_literature")
        reasons.append(
            "explicit_literature_language"
            if any(marker in lowered for marker in literature_markers)
            else "corpus_available_for_soft_prefetch"
        )
    if any(marker in lowered for marker in ("之前", "上次", "继续", "earlier", "previous")):
        reasons.append("continuity_language")
    if any(marker in lowered for marker in ("为什么", "依据", "why", "evidence", "source")):
        reasons.append("source_explanation_language")
    return RetrievalIntentHint(user_text.strip(), concepts, tuple(sources), tuple(reasons))


def retrieval_tokens(value: str) -> set[str]:
    return _tokens(value)


def build_canonical_nodes(
    document: ExtractedKnowledgeDocument,
) -> tuple[CanonicalKnowledgeNodeDraft, ...]:
    """Create stable semantic nodes for the built-in FAST parse profile.

    Parser-specific rich structures are normalized into the same contract later.  This
    fallback intentionally prefers complete paragraphs over fixed-size character chunks.
    """

    evidence_eligible = CorpusRole.LITERATURE_EVIDENCE in document.corpus_roles
    common_payload: dict[str, Any] = {
        "canonical_ir_version": document.canonical_ir_version,
        "ingestion_policy_revision": document.ingestion_policy_revision,
        "corpus_roles": [role.value for role in document.corpus_roles],
        "evidence_eligible": evidence_eligible,
    }
    nodes: list[CanonicalKnowledgeNodeDraft] = [
        CanonicalKnowledgeNodeDraft(
            "document",
            None,
            KnowledgeNodeKind.DOCUMENT,
            1,
            document.relative_path,
            structured_payload={**common_payload, "source_kind": document.source_kind.value},
        )
    ]
    ordinal = 1
    section_key: str | None = None
    section_title: str | None = None
    paragraph_number = 0
    section_number = 0
    for page in document.pages:
        blocks = tuple(re.finditer(r"\S(?:.*?)(?=\n\s*\n|\Z)", page.text, re.S))
        for match in blocks:
            block = match.group(0).strip()
            leading = len(match.group(0)) - len(match.group(0).lstrip())
            source_start = match.start() + leading
            source_end = source_start + len(block)
            heading = re.fullmatch(r"#{1,6}\s+(.+)", block)
            help_heading = re.fullmatch(r"\{title:([^}]+)\}", block)
            if heading or help_heading:
                section_number += 1
                ordinal += 1
                section_key = f"section:{section_number}"
                title = (heading or help_heading).group(1).strip()  # type: ignore[union-attr]
                section_title = title
                kind = (
                    KnowledgeNodeKind.HELP_TOPIC
                    if CorpusRole.STATA_HELP in document.corpus_roles
                    else KnowledgeNodeKind.SECTION
                )
                nodes.append(
                    CanonicalKnowledgeNodeDraft(
                        section_key,
                        "document",
                        kind,
                        ordinal,
                        title,
                        page.page_number,
                        page.page_number,
                        "section_heading",
                        source_start,
                        source_end,
                        {**common_payload, "heading_level": _heading_level(block)},
                    )
                )
                continue
            paragraph_number += 1
            ordinal += 1
            normalized = "\n".join(line.strip() for line in block.splitlines() if line.strip())
            node_kind = KnowledgeNodeKind.PARAGRAPH
            semantic_role: str | None = None
            lines = normalized.splitlines()
            if normalized.startswith("```") and normalized.endswith("```"):
                node_kind = KnowledgeNodeKind.CODE
            elif normalized.startswith("$$") and normalized.endswith("$$"):
                node_kind = KnowledgeNodeKind.EQUATION
            elif len(lines) >= 2 and all("|" in line for line in lines[:2]):
                node_kind = KnowledgeNodeKind.TABLE
            elif lines and all(re.match(r"^(?:[-*+] |\d+[.)] )", line) for line in lines):
                node_kind = KnowledgeNodeKind.LIST
            elif re.match(r"^(?:figure|fig\.|table)\s+\d+", normalized, re.IGNORECASE):
                node_kind = KnowledgeNodeKind.CAPTION
            elif re.match(r"^(?:footnote|note:|notes:|注[:：]|注释[:：])", normalized, re.I):
                node_kind = KnowledgeNodeKind.FOOTNOTE
            if section_title is not None and re.search(
                r"\b(?:references|bibliography)\b|参考文献", section_title, re.IGNORECASE
            ):
                semantic_role = "reference_section"
                node_kind = KnowledgeNodeKind.REFERENCE
            if CorpusRole.STYLE_EXEMPLAR in document.corpus_roles:
                semantic_role = semantic_role or _style_semantic_role(section_title)
            payload = {
                **common_payload,
                "line_count": len(lines),
                "section_title": section_title,
            }
            if node_kind == KnowledgeNodeKind.TABLE:
                payload["rows"] = [
                    [cell.strip() for cell in line.strip().strip("|").split("|")]
                    for line in lines
                ]
            elif node_kind == KnowledgeNodeKind.EQUATION:
                payload["format"] = "display_math"
            nodes.append(
                CanonicalKnowledgeNodeDraft(
                    f"paragraph:{paragraph_number}",
                    section_key or "document",
                    node_kind,
                    ordinal,
                    normalized,
                    page.page_number,
                    page.page_number,
                    semantic_role,
                    source_start,
                    source_end,
                    payload,
                )
            )
    if len(nodes) == 1:
        raise ValueError("knowledge document produced no canonical content nodes")
    return tuple(nodes)


def _heading_level(block: str) -> int | None:
    match = re.match(r"^(#{1,6})\s", block)
    return len(match.group(1)) if match is not None else None


def _style_semantic_role(section_title: str | None) -> str:
    title = (section_title or "").lower()
    if re.search(r"introduction|引言|导论", title):
        return "style_introduction"
    if re.search(r"method|empirical|研究设计|方法|实证", title):
        return "style_method"
    if re.search(r"result|finding|结果|发现", title):
        return "style_results"
    if re.search(r"discussion|conclusion|讨论|结论", title):
        return "style_discussion"
    return "style_generic"


def _tokens(value: str) -> set[str]:
    words = {token.lower() for token in re.findall(r"[\w.-]{2,}", value)}
    han = set(re.findall(r"[\u4e00-\u9fff]", value))
    stop = {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "what",
        "how",
        "一个",
        "这个",
        "可以",
        "需要",
        "进行",
    }
    return (words | han) - stop
