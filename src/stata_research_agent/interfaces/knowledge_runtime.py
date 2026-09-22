"""Production indexing and admitted retrieval tool for Workspace literature."""

from __future__ import annotations

import json
from pathlib import Path

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.application.broker_execution_service import BrokerExecutionService
from stata_research_agent.application.knowledge_retrieval import (
    ContinueKnowledgeRetrievalCommand,
    CorpusRole,
    ExtractedKnowledgeDocument,
    RetrievalMode,
    StartKnowledgeRetrievalCommand,
    SyncKnowledgeIndexCommand,
)
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.turn_driver import (
    ToolExecutionRequest,
    ToolExecutionResult,
)
from stata_research_agent.domain.identifiers import CommandId
from stata_research_agent.persistence.knowledge_store import SqliteKnowledgeRepository

from .literature_catalog import FilesystemLiteratureCatalog, MineruCliParser, StataHelpCatalog


class WorkspaceKnowledgeIndexService:
    def __init__(
        self,
        repository: SqliteKnowledgeRepository,
        workspace_root: Path,
        identities: IdentityGenerator,
        pdf_parser: MineruCliParser | None = None,
    ) -> None:
        self._repository = repository
        self._catalogs = (
            FilesystemLiteratureCatalog(workspace_root, pdf_parser=pdf_parser),
            FilesystemLiteratureCatalog(
                workspace_root,
                folder_name="style-references",
                corpus_role=CorpusRole.STYLE_EXEMPLAR,
                pdf_parser=pdf_parser,
            ),
        )
        self._identities = identities

    def synchronize(self) -> None:
        prefixes = ("literature/", "style-references/")
        known = self._repository.current_hashes(prefixes)
        documents: list[ExtractedKnowledgeDocument] = []
        observed: list[str] = []
        errors: list[tuple[str, str]] = []
        for catalog in self._catalogs:
            extracted, seen, failures = catalog.extract_changed(known)
            documents.extend(extracted)
            observed.extend(seen)
            errors.extend(failures)
        self._repository.sync(
            SyncKnowledgeIndexCommand(
                self._identities.new(CommandId),
                tuple(documents),
                tuple(observed),
                tuple(errors),
                prefixes,
            )
        )


class StataHelpIndexService:
    def __init__(
        self,
        repository: SqliteKnowledgeRepository,
        help_roots: tuple[tuple[str, Path], ...],
        identities: IdentityGenerator,
    ) -> None:
        self._repository = repository
        self._catalog = StataHelpCatalog(help_roots)
        self._identities = identities

    def synchronize(self) -> None:
        prefixes = ("stata-help/",)
        documents, observed, errors = self._catalog.extract_changed(
            self._repository.current_hashes(prefixes)
        )
        self._repository.sync(
            SyncKnowledgeIndexCommand(
                self._identities.new(CommandId),
                documents,
                observed,
                errors,
                prefixes,
                "stata-help-index-v1",
            )
        )

    def synchronize_for_query(self, query: str, *, max_sources: int = 24) -> None:
        prefixes = ("stata-help/",)
        documents, observed, errors = self._catalog.extract_candidates(
            query,
            self._repository.current_hashes(prefixes),
            max_sources=max_sources,
        )
        self._repository.sync(
            SyncKnowledgeIndexCommand(
                self._identities.new(CommandId),
                documents,
                observed,
                errors,
                prefixes,
                "stata-help-query-bootstrap-v1",
                False,
            )
        )


class KnowledgeSearchExecutor:
    def __init__(
        self,
        repository: SqliteKnowledgeRepository,
        bridge: BrokerExecutionService,
        identities: IdentityGenerator,
        *,
        forced_roles: tuple[CorpusRole, ...] | None = None,
        forced_mode: RetrievalMode | None = None,
        lazy_index: StataHelpIndexService | None = None,
    ) -> None:
        self._repository = repository
        self._bridge = bridge
        self._identities = identities
        self._forced_roles = forced_roles
        self._forced_mode = forced_mode
        self._lazy_index = lazy_index

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        handle = self._bridge.begin(
            BeginBrokerExecutionCommand(
                self._identities.new(CommandId),
                request.turn_id,
                request.tool_call_id,
                request.operation_id,
            )
        )
        try:
            query = str(request.arguments["query"])
            if self._lazy_index is not None:
                self._lazy_index.synchronize_for_query(query)
            limit = int(request.arguments.get("limit", 6))
            raw_session_id = request.arguments.get("retrieval_session_id")
            if raw_session_id is not None:
                raw_unresolved = request.arguments.get("unresolved_items", [])
                if not isinstance(raw_unresolved, list):
                    raise ValueError("unresolved_items must be an array")
                public_subquestion = str(
                    request.arguments.get("public_subquestion", query)
                ).strip()
                retrieval = self._repository.continue_retrieval(
                    ContinueKnowledgeRetrievalCommand(
                        self._identities.new(CommandId),
                        str(raw_session_id),
                        query,
                        public_subquestion,
                        limit,
                        bool(request.arguments.get("conclude_session", False)),
                        tuple(str(item) for item in raw_unresolved),
                    )
                )
                objective = public_subquestion
            else:
                if self._forced_roles is None:
                    raw_roles = request.arguments.get(
                        "corpus_roles", ["literature_evidence"]
                    )
                    if not isinstance(raw_roles, list):
                        raise ValueError("corpus_roles must be an array")
                    roles = tuple(CorpusRole(str(role)) for role in raw_roles)
                else:
                    roles = self._forced_roles
                mode = self._forced_mode or RetrievalMode(
                    str(request.arguments.get("mode", "direct"))
                )
                objective = str(request.arguments.get("objective", query)).strip()
                retrieval = self._repository.retrieve(
                    StartKnowledgeRetrievalCommand(
                        self._identities.new(CommandId),
                        query,
                        objective,
                        roles,
                        mode,
                        limit,
                        request.turn_id,
                    )
                )
            payload: dict[str, object] = {
                "boundary": (
                    "Knowledge retrieval material is not statistical Evidence. Literature "
                    "requires an exact Source Revision/Node before citation; Stata Help only "
                    "supports code diagnosis; Style Exemplars never support factual claims."
                ),
                "query": query,
                "objective": objective,
                "retrieval_session_id": retrieval.retrieval_session_id,
                "retrieval_hop_id": retrieval.retrieval_hop_id,
                "stop_reason": retrieval.stop_reason,
                "hop_ordinal": retrieval.hop_ordinal,
                "novel_hit_count": retrieval.novel_hit_count,
                "cumulative_hit_count": retrieval.cumulative_hit_count,
                "session_status": retrieval.session_status,
                "query_variants": list(retrieval.query_variants),
                "query_planner_policy_revision": retrieval.query_planner_policy_revision,
                "reranker_policy_revision": retrieval.reranker_policy_revision,
                "hits": [
                    {
                        "knowledge_node_id": hit.node_id,
                        "knowledge_source_revision_id": hit.source_revision_id,
                        "knowledge_parse_revision_id": hit.parse_revision_id,
                        "source_locator": hit.source_locator,
                        "corpus_role": hit.corpus_role,
                        "node_kind": hit.node_kind,
                        "page_start": hit.page_start,
                        "page_end": hit.page_end,
                        "section_title": hit.section_title,
                        "lexical_rank": hit.lexical_rank,
                        "dense_rank": hit.dense_rank,
                        "dense_score": hit.dense_score,
                        "fused_score": hit.fused_score,
                        "rerank_score": hit.rerank_score,
                        "relevance_label": hit.relevance_label,
                        "query_variant_ordinals": list(hit.query_variant_ordinals),
                        "rerank_reason_codes": list(hit.rerank_reason_codes),
                        "content": hit.content,
                    }
                    for hit in retrieval.hits
                ],
            }
            success = True
            summary = f"Retrieved {len(retrieval.hits)} canonical knowledge nodes"
        except Exception as error:
            payload = {"error_type": type(error).__name__, "message": str(error)}
            success = False
            summary = "Workspace literature search failed"
        outcome = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId), handle, success, summary, payload
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            success, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )


class KnowledgeNodeExecutor:
    def __init__(
        self,
        repository: SqliteKnowledgeRepository,
        bridge: BrokerExecutionService,
        identities: IdentityGenerator,
        *,
        action: str,
    ) -> None:
        if action not in {"read", "expand", "evidence_packet"}:
            raise ValueError("unknown Knowledge Node action")
        self._repository = repository
        self._bridge = bridge
        self._identities = identities
        self._action = action

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        handle = self._bridge.begin(
            BeginBrokerExecutionCommand(
                self._identities.new(CommandId),
                request.turn_id,
                request.tool_call_id,
                request.operation_id,
            )
        )
        try:
            raw_node_ids = request.arguments.get("knowledge_node_ids")
            if not isinstance(raw_node_ids, list):
                raise ValueError("knowledge_node_ids must be an array")
            node_ids = tuple(str(node_id) for node_id in raw_node_ids)
            if self._action == "expand":
                raw_edges = request.arguments.get(
                    "edge_kinds", ["contains", "next", "previous"]
                )
                if not isinstance(raw_edges, list):
                    raise ValueError("edge_kinds must be an array")
                hits = self._repository.expand_canonical_nodes(
                    node_ids,
                    edge_kinds=tuple(str(kind) for kind in raw_edges),
                    direction=str(request.arguments.get("direction", "both")),
                    limit=int(request.arguments.get("limit", 24)),
                )
            else:
                hits = self._repository.read_canonical_nodes(node_ids)
            payload: dict[str, object] = {
                "boundary": (
                    "Exact Literature nodes support citation only with their Source Revision "
                    "and locator; Help nodes support code diagnosis only; Style nodes support "
                    "rhetorical imitation only and never factual claims."
                ),
                "action": self._action,
                "hits": [
                    {
                        "knowledge_node_id": hit.node_id,
                        "knowledge_source_revision_id": hit.source_revision_id,
                        "knowledge_parse_revision_id": hit.parse_revision_id,
                        "source_locator": hit.source_locator,
                        "corpus_role": hit.corpus_role,
                        "node_kind": hit.node_kind,
                        "page_start": hit.page_start,
                        "page_end": hit.page_end,
                        "section_title": hit.section_title,
                        "content": hit.content,
                    }
                    for hit in hits
                ],
            }
            if self._action == "evidence_packet":
                payload["citation_requirements"] = {
                    "must_preserve_node_identity": True,
                    "must_preserve_source_revision": True,
                    "statistical_evidence": False,
                }
            success = True
            summary = f"Resolved {len(hits)} exact canonical knowledge nodes"
        except Exception as error:
            payload = {"error_type": type(error).__name__, "message": str(error)}
            success = False
            summary = "Canonical Knowledge Node operation failed"
        outcome = self._bridge.complete(
            CompleteBrokerExecutionCommand(
                self._identities.new(CommandId), handle, success, summary, payload
            )
        )
        payload["status"] = outcome.status
        return ToolExecutionResult(
            success, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
