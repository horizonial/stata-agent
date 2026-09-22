"""SQLite authority and deterministic lexical retrieval for Workspace literature."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace

from stata_research_agent.application.dense_retrieval import DenseCandidateProvider
from stata_research_agent.application.knowledge_retrieval import (
    AgenticRetrievalPolicy,
    ConcludeKnowledgeRetrievalCommand,
    ContinueKnowledgeRetrievalCommand,
    CorpusRole,
    ExtractedKnowledgeDocument,
    KnowledgeIndexOutcome,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalOutcome,
    RetrievalMode,
    StartKnowledgeRetrievalCommand,
    SyncKnowledgeIndexCommand,
    build_canonical_nodes,
    retrieval_tokens,
)
from stata_research_agent.application.retrieval_pipeline import (
    DeterministicEvidenceReranker,
    DeterministicQueryPlanner,
    EvidenceReranker,
    RerankCandidate,
    RetrievalQueryPlan,
    RetrievalQueryPlanner,
    select_diverse_evidence,
)
from stata_research_agent.domain.identifiers import (
    CommandId,
    KnowledgeChunkId,
    KnowledgeDocumentId,
    KnowledgeDocumentRevisionId,
    KnowledgeEdgeId,
    KnowledgeIndexRunId,
    KnowledgeNodeId,
    KnowledgeParseRevisionId,
    KnowledgeRetrievalHopId,
    KnowledgeRetrievalSessionId,
    KnowledgeSourceId,
    KnowledgeSourceRevisionId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator

from .atomic_commit import AtomicCommitService, JournalDraft, MutationPayload, OutboxDraft


@dataclass(frozen=True, slots=True)
class KnowledgeSearchHit:
    chunk_id: str
    document_revision_id: str
    relative_path: str
    content_sha256: str
    page_start: int | None
    page_end: int | None
    content: str
    score: int


@dataclass(frozen=True, slots=True)
class CanonicalKnowledgeSearchHit:
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
class _RetrievalPipelineResult:
    query_plan: RetrievalQueryPlan
    candidates: tuple[CanonicalKnowledgeSearchHit, ...]
    selected: tuple[CanonicalKnowledgeSearchHit, ...]
    reranker_policy_revision: str


class SqliteKnowledgeRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        dense_candidates: DenseCandidateProvider | None = None,
        agentic_policy: AgenticRetrievalPolicy | None = None,
        query_planner: RetrievalQueryPlanner | None = None,
        reranker: EvidenceReranker | None = None,
    ) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)
        self._identities = UuidIdentityGenerator()
        self._dense_candidates = dense_candidates
        self._agentic_policy = agentic_policy or AgenticRetrievalPolicy()
        self._query_planner = query_planner or DeterministicQueryPlanner()
        self._reranker = reranker or DeterministicEvidenceReranker()

    def current_hashes(self, prefixes: tuple[str, ...] | None = None) -> dict[str, str]:
        result = {
            str(row["relative_path"]): str(row["content_sha256"])
            for row in self._connection.execute(
                """
                SELECT document.relative_path, revision.content_sha256
                FROM knowledge_documents AS document
                JOIN knowledge_document_states AS state USING (knowledge_document_id)
                JOIN knowledge_document_revisions AS revision
                  ON revision.knowledge_document_revision_id = state.current_revision_id
                JOIN knowledge_sources AS source
                  ON source.legacy_knowledge_document_id = document.knowledge_document_id
                JOIN knowledge_source_states AS source_state
                  ON source_state.knowledge_source_id = source.knowledge_source_id
                JOIN knowledge_parse_revisions AS parse
                  ON parse.knowledge_parse_revision_id = source_state.current_parse_revision_id
                WHERE state.availability = 'indexed'
                  AND source_state.availability = 'indexed'
                  AND parse.canonical_ir_version = 'canonical-ir-v2'
                  AND json_extract(parse.quality_findings_json,
                                   '$[0].ingestion_policy_revision') =
                      'knowledge-ingestion-v2'
                """
            ).fetchall()
        }
        if prefixes is None:
            return result
        return {
            path: digest
            for path, digest in result.items()
            if any(path.startswith(prefix) for prefix in prefixes)
        }

    def sync(self, command: SyncKnowledgeIndexCommand) -> KnowledgeIndexOutcome | None:
        known = self.current_hashes(command.managed_path_prefixes)
        already_failed_all = {
            str(row["relative_path"])
            for row in self._connection.execute(
                """
                SELECT document.relative_path
                FROM knowledge_documents AS document
                JOIN knowledge_document_states AS state USING (knowledge_document_id)
                WHERE state.availability = 'extraction_failed'
                """
            ).fetchall()
        }
        already_failed = {
            path
            for path in already_failed_all
            if any(path.startswith(prefix) for prefix in command.managed_path_prefixes)
        }
        observed = set(command.observed_relative_paths)
        missing = (
            tuple(sorted(set(known) - observed)) if command.reconcile_missing else ()
        )
        changed = tuple(
            document
            for document in command.documents
            if known.get(document.relative_path) != document.content_sha256
        )
        errors = tuple(
            error for error in command.extraction_errors if error[0] not in already_failed
        )
        unchanged = len(observed) - len(changed) - len(errors)
        if not changed and not missing and not errors:
            return None
        request = {
            "observed_relative_paths": sorted(observed),
            "changed": [
                [document.relative_path, document.content_sha256] for document in changed
            ],
            "missing": list(missing),
            "errors": [list(error) for error in errors],
            "managed_path_prefixes": list(command.managed_path_prefixes),
            "policy_revision": command.policy_revision,
            "reconcile_missing": command.reconcile_missing,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            indexed = 0
            for document in changed:
                self._index_document(connection, revision, document)
                indexed += 1
            missing_count = 0
            for relative_path in missing:
                row = connection.execute(
                    """
                    SELECT document.knowledge_document_id, state.pointer_revision,
                           state.current_revision_id, state.availability
                    FROM knowledge_documents AS document
                    JOIN knowledge_document_states AS state USING (knowledge_document_id)
                    WHERE document.relative_path = ? AND state.availability != 'missing'
                    """,
                    (relative_path,),
                ).fetchone()
                if row is None:
                    continue
                connection.execute(
                    """
                    UPDATE knowledge_document_states
                    SET availability = 'missing', pointer_revision = ?, updated_revision = ?
                    WHERE knowledge_document_id = ?
                    """,
                    (
                        int(row["pointer_revision"]) + 1,
                        revision.value,
                        str(row["knowledge_document_id"]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE knowledge_source_states
                    SET availability = 'missing', pointer_revision = pointer_revision + 1,
                        updated_revision = ?
                    WHERE knowledge_source_id = (
                        SELECT knowledge_source_id FROM knowledge_sources
                        WHERE legacy_knowledge_document_id = ?
                    )
                    """,
                    (revision.value, str(row["knowledge_document_id"])),
                )
                missing_count += 1
            for relative_path, _error_code in errors:
                row = connection.execute(
                    """
                    SELECT document.knowledge_document_id, state.pointer_revision,
                           state.current_revision_id, state.availability
                    FROM knowledge_documents AS document
                    JOIN knowledge_document_states AS state USING (knowledge_document_id)
                    WHERE document.relative_path = ?
                    """,
                    (relative_path,),
                ).fetchone()
                if row is None:
                    document_id = self._identities.new(KnowledgeDocumentId).value
                    connection.execute(
                        "INSERT INTO knowledge_documents VALUES (?, ?, ?)",
                        (document_id, relative_path, revision.value),
                    )
                    connection.execute(
                        """
                        INSERT INTO knowledge_document_states
                        VALUES (?, NULL, 'extraction_failed', 1, ?)
                        """,
                        (document_id, revision.value),
                    )
                    self._ensure_failed_canonical_source(
                        connection, revision, document_id, relative_path
                    )
                elif row["current_revision_id"] is None or str(row["availability"]) != "indexed":
                    connection.execute(
                        """
                        UPDATE knowledge_document_states
                        SET availability = 'extraction_failed', pointer_revision = ?,
                            updated_revision = ? WHERE knowledge_document_id = ?
                        """,
                        (
                            int(row["pointer_revision"]) + 1,
                            revision.value,
                            str(row["knowledge_document_id"]),
                        ),
                    )
                    self._ensure_failed_canonical_source(
                        connection,
                        revision,
                        str(row["knowledge_document_id"]),
                        relative_path,
                    )
                # An extraction failure for a previously indexed source is an observation
                # about the new candidate bytes, not authority to discard the last-known-good
                # parse.  The index run and Journal retain the failure for diagnosis.
            run_id = self._identities.new(KnowledgeIndexRunId).value
            payload = {
                "knowledge_index_run_id": run_id,
                "scanned_count": len(observed),
                "indexed_count": indexed,
                "unchanged_count": max(0, unchanged),
                "missing_count": missing_count,
                "error_count": len(errors),
            }
            connection.execute(
                """
                INSERT INTO knowledge_index_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    len(observed),
                    indexed,
                    max(0, unchanged),
                    missing_count,
                    json.dumps(errors, ensure_ascii=False, separators=(",", ":")),
                    command.policy_revision,
                    revision.value,
                ),
            )
            return MutationPayload(
                payload,
                (JournalDraft("knowledge.index_synchronized", "knowledge_index", run_id, payload),),
                (OutboxDraft("knowledge.index_changed", payload),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="knowledge.index.sync",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return KnowledgeIndexOutcome(
            int(response["scanned_count"]),
            int(response["indexed_count"]),
            int(response["unchanged_count"]),
            int(response["missing_count"]),
            int(response["error_count"]),
            receipt.commit_revision.value,
            receipt.replayed,
        )

    def search(self, query: str, *, limit: int = 6) -> tuple[KnowledgeSearchHit, ...]:
        if not query.strip() or not 1 <= limit <= 12:
            raise ValueError("knowledge search requires a query and limit between 1 and 12")
        query_tokens = retrieval_tokens(query)
        if not query_tokens:
            return ()
        rows = self._connection.execute(
            """
            SELECT chunk.knowledge_chunk_id, chunk.knowledge_document_revision_id,
                   document.relative_path, revision.content_sha256,
                   chunk.page_start, chunk.page_end, chunk.content
            FROM knowledge_chunks AS chunk
            JOIN knowledge_document_revisions AS revision
              USING (knowledge_document_revision_id)
            JOIN knowledge_documents AS document USING (knowledge_document_id)
            JOIN knowledge_document_states AS state USING (knowledge_document_id)
            JOIN knowledge_sources AS source
              ON source.legacy_knowledge_document_id = document.knowledge_document_id
            JOIN knowledge_source_memberships AS membership
              ON membership.knowledge_source_id = source.knowledge_source_id
            WHERE state.availability = 'indexed'
              AND state.current_revision_id = revision.knowledge_document_revision_id
              AND membership.corpus_role = 'literature_evidence'
            ORDER BY document.relative_path, chunk.chunk_ordinal
            """
        ).fetchall()
        lowered_query = query.strip().lower()
        hits: list[KnowledgeSearchHit] = []
        for row in rows:
            content = str(row["content"])
            overlap = len(query_tokens.intersection(retrieval_tokens(content)))
            phrase = 4 if lowered_query in content.lower() else 0
            score = overlap * 10 + phrase
            if score == 0:
                continue
            hits.append(
                KnowledgeSearchHit(
                    str(row["knowledge_chunk_id"]),
                    str(row["knowledge_document_revision_id"]),
                    str(row["relative_path"]),
                    str(row["content_sha256"]),
                    None if row["page_start"] is None else int(row["page_start"]),
                    None if row["page_end"] is None else int(row["page_end"]),
                    content,
                    score,
                )
            )
        return tuple(
            sorted(hits, key=lambda hit: (-hit.score, hit.relative_path, hit.chunk_id))[:limit]
        )

    def search_canonical(
        self,
        query: str,
        *,
        corpus_roles: tuple[CorpusRole, ...] = (CorpusRole.LITERATURE_EVIDENCE,),
        limit: int = 6,
    ) -> tuple[CanonicalKnowledgeSearchHit, ...]:
        """Search current canonical nodes without creating an interaction record."""

        return self._run_retrieval_pipeline(
            self._connection,
            query,
            query,
            RetrievalMode.DIRECT,
            corpus_roles,
            limit,
        ).selected

    def canonical_catalog(
        self, corpus_role: CorpusRole
    ) -> tuple[tuple[str, str, int | None], ...]:
        """Return the current source identities for one corpus role."""

        return tuple(
            (
                str(row["canonical_locator"]),
                str(row["content_sha256"]),
                None if row["page_count"] is None else int(row["page_count"]),
            )
            for row in self._connection.execute(
                """
                SELECT source.canonical_locator, revision.content_sha256,
                       MAX(node.page_end) AS page_count
                FROM knowledge_sources AS source
                JOIN knowledge_source_memberships AS membership USING (knowledge_source_id)
                JOIN knowledge_source_states AS state USING (knowledge_source_id)
                JOIN knowledge_source_revisions AS revision
                  ON revision.knowledge_source_revision_id = state.current_source_revision_id
                LEFT JOIN knowledge_nodes AS node
                  ON node.knowledge_parse_revision_id = state.current_parse_revision_id
                WHERE membership.corpus_role = ? AND state.availability = 'indexed'
                GROUP BY source.knowledge_source_id, source.canonical_locator,
                         revision.content_sha256
                ORDER BY source.canonical_locator
                """,
                (corpus_role.value,),
            ).fetchall()
        )

    def read_canonical_nodes(
        self, node_ids: tuple[str, ...]
    ) -> tuple[CanonicalKnowledgeSearchHit, ...]:
        if not node_ids or len(node_ids) > 64 or len(set(node_ids)) != len(node_ids):
            raise ValueError("exact knowledge read requires 1..64 unique node IDs")
        placeholders = ",".join("?" for _ in node_ids)
        rows = self._connection.execute(
            f"""
            SELECT node.knowledge_node_id, source_revision.knowledge_source_revision_id,
                   parse.knowledge_parse_revision_id, source.canonical_locator,
                   membership.corpus_role, node.node_kind, node.page_start, node.page_end,
                   CASE WHEN parent.node_kind IN ('title', 'section', 'help_topic')
                        THEN parent.content ELSE NULL END AS section_title,
                   node.content
            FROM knowledge_nodes AS node
            JOIN knowledge_parse_revisions AS parse USING (knowledge_parse_revision_id)
            JOIN knowledge_source_revisions AS source_revision
              USING (knowledge_source_revision_id)
            JOIN knowledge_sources AS source USING (knowledge_source_id)
            JOIN knowledge_source_memberships AS membership USING (knowledge_source_id)
            LEFT JOIN knowledge_nodes AS parent
              ON parent.knowledge_node_id = node.parent_node_id
            WHERE node.knowledge_node_id IN ({placeholders})
            ORDER BY source.canonical_locator, node.ordinal, membership.corpus_role
            """,
            node_ids,
        ).fetchall()
        requested = set(node_ids)
        found = {str(row["knowledge_node_id"]) for row in rows}
        if found != requested:
            raise ValueError("one or more Knowledge Node identities do not exist")
        return tuple(
            CanonicalKnowledgeSearchHit(
                str(row["knowledge_node_id"]),
                str(row["knowledge_source_revision_id"]),
                str(row["knowledge_parse_revision_id"]),
                str(row["canonical_locator"]),
                str(row["corpus_role"]),
                str(row["node_kind"]),
                None if row["page_start"] is None else int(row["page_start"]),
                None if row["page_end"] is None else int(row["page_end"]),
                None if row["section_title"] is None else str(row["section_title"]),
                str(row["content"]),
                rank,
                1.0,
            )
            for rank, row in enumerate(rows, start=1)
        )

    def expand_canonical_nodes(
        self,
        node_ids: tuple[str, ...],
        *,
        edge_kinds: tuple[str, ...] = ("contains", "next", "previous"),
        direction: str = "both",
        limit: int = 24,
    ) -> tuple[CanonicalKnowledgeSearchHit, ...]:
        if not node_ids or len(node_ids) > 32 or len(set(node_ids)) != len(node_ids):
            raise ValueError("knowledge expansion requires 1..32 unique seed nodes")
        allowed_edges = {"contains", "next", "previous", "cites", "references", "related"}
        if (
            not edge_kinds
            or any(kind not in allowed_edges for kind in edge_kinds)
            or direction not in {"outgoing", "incoming", "both"}
            or not 1 <= limit <= 64
        ):
            raise ValueError("knowledge expansion policy is invalid")
        node_placeholders = ",".join("?" for _ in node_ids)
        edge_placeholders = ",".join("?" for _ in edge_kinds)
        clauses: list[str] = []
        parameters: list[object] = []
        if direction in {"outgoing", "both"}:
            clauses.append(
                f"SELECT to_node_id AS node_id FROM knowledge_edges "
                f"WHERE from_node_id IN ({node_placeholders}) "
                f"AND edge_kind IN ({edge_placeholders})"
            )
            parameters.extend((*node_ids, *edge_kinds))
        if direction in {"incoming", "both"}:
            clauses.append(
                f"SELECT from_node_id AS node_id FROM knowledge_edges "
                f"WHERE to_node_id IN ({node_placeholders}) "
                f"AND edge_kind IN ({edge_placeholders})"
            )
            parameters.extend((*node_ids, *edge_kinds))
        rows = self._connection.execute(
            "SELECT DISTINCT node_id FROM (" + " UNION ALL ".join(clauses) + ") LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        expanded = tuple(str(row["node_id"]) for row in rows)
        return () if not expanded else self.read_canonical_nodes(expanded)

    def retrieve(self, command: StartKnowledgeRetrievalCommand) -> KnowledgeRetrievalOutcome:
        request = {
            "query": command.query,
            "objective": command.objective,
            "corpus_roles": [role.value for role in command.corpus_roles],
            "mode": command.mode.value,
            "limit": command.limit,
            "turn_id": None if command.turn_id is None else command.turn_id.value,
            "policy_revision": command.policy_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            pipeline = self._run_retrieval_pipeline(
                connection,
                command.query,
                command.objective,
                command.mode,
                command.corpus_roles,
                command.limit,
            )
            hits = pipeline.selected
            session_id = self._identities.new(KnowledgeRetrievalSessionId).value
            hop_id = self._identities.new(KnowledgeRetrievalHopId).value
            terminal_reason = (
                "no_evidence"
                if not hits
                else "candidate_exhausted"
                if len(hits) < command.limit
                else "limit_reached"
            )
            session_open = command.mode.value == "multi_hop" and bool(hits)
            status = "open" if session_open else "completed" if hits else "partial"
            hop_reason = "awaiting_next_hop" if session_open else terminal_reason
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_sessions(
                    knowledge_retrieval_session_id, turn_id, objective, retrieval_mode,
                    corpus_roles_json, policy_revision, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    None if command.turn_id is None else command.turn_id.value,
                    command.objective,
                    command.mode.value,
                    json.dumps(
                        [role.value for role in command.corpus_roles],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    command.policy_revision,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_session_states(
                    knowledge_retrieval_session_id, status, next_hop_ordinal,
                    stop_reason, pointer_revision, updated_revision
                ) VALUES (?, ?, 2, ?, 1, ?)
                """,
                (
                    session_id,
                    status,
                    None if session_open else terminal_reason,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_session_state_history(
                    knowledge_retrieval_session_id, state_revision, status,
                    next_hop_ordinal, stop_reason, created_revision
                ) VALUES (?, 1, ?, 2, ?, ?)
                """,
                (
                    session_id,
                    status,
                    None if session_open else terminal_reason,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_hops(
                    knowledge_retrieval_hop_id, knowledge_retrieval_session_id,
                    parent_hop_id, hop_ordinal, public_subquestion, query, strategy,
                    unresolved_items_json, stop_reason, created_revision
                ) VALUES (?, ?, NULL, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hop_id,
                    session_id,
                    command.objective,
                    command.query,
                    command.mode.value,
                    "[]" if hits else '["NO_EVIDENCE"]',
                    hop_reason,
                    revision.value,
                ),
            )
            self._record_retrieval_pipeline(connection, revision, hop_id, pipeline)
            for selected_ordinal, hit in enumerate(hits, start=1):
                connection.execute(
                    """
                    INSERT INTO knowledge_retrieval_selections(
                        knowledge_retrieval_hop_id, knowledge_node_id, selected_ordinal,
                        lexical_rank, dense_rank, dense_score, fused_score,
                        selection_reason, created_revision, rerank_score, relevance_label
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hop_id,
                        hit.node_id,
                        selected_ordinal,
                        hit.lexical_rank,
                        hit.dense_rank,
                        hit.dense_score,
                        hit.fused_score,
                        "reranked_diverse_selection",
                        revision.value,
                        hit.rerank_score,
                        hit.relevance_label,
                    ),
                )
            hit_payload = [
                {
                    "node_id": hit.node_id,
                    "source_revision_id": hit.source_revision_id,
                    "parse_revision_id": hit.parse_revision_id,
                    "source_locator": hit.source_locator,
                    "corpus_role": hit.corpus_role,
                    "node_kind": hit.node_kind,
                    "page_start": hit.page_start,
                    "page_end": hit.page_end,
                    "section_title": hit.section_title,
                    "content": hit.content,
                    "lexical_rank": hit.lexical_rank,
                    "dense_rank": hit.dense_rank,
                    "dense_score": hit.dense_score,
                    "fused_score": hit.fused_score,
                    "rerank_score": hit.rerank_score,
                    "relevance_label": hit.relevance_label,
                    "query_variant_ordinals": list(hit.query_variant_ordinals),
                    "rerank_reason_codes": list(hit.rerank_reason_codes),
                }
                for hit in hits
            ]
            payload = {
                "retrieval_session_id": session_id,
                "retrieval_hop_id": hop_id,
                "hits": hit_payload,
                "stop_reason": hop_reason,
                "hop_ordinal": 1,
                "novel_hit_count": len(hits),
                "cumulative_hit_count": len({hit.node_id for hit in hits}),
                "session_status": status,
                "query_variants": [item.query for item in pipeline.query_plan.variants],
                "query_planner_policy_revision": (
                    pipeline.query_plan.planner_policy_revision
                ),
                "reranker_policy_revision": pipeline.reranker_policy_revision,
            }
            return MutationPayload(
                payload,
                (
                    JournalDraft(
                        "knowledge.retrieval_completed",
                        "knowledge_retrieval_session",
                        session_id,
                        {
                            "retrieval_hop_id": hop_id,
                            "mode": command.mode.value,
                            "corpus_roles": [role.value for role in command.corpus_roles],
                            "selected_count": len(hits),
                            "stop_reason": payload["stop_reason"],
                        },
                    ),
                ),
                (OutboxDraft("knowledge.retrieval_recorded", payload),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="knowledge.retrieve",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        hits = tuple(
            KnowledgeRetrievalHit(
                str(hit["node_id"]),
                str(hit["source_revision_id"]),
                str(hit["parse_revision_id"]),
                str(hit["source_locator"]),
                str(hit["corpus_role"]),
                str(hit["node_kind"]),
                None if hit["page_start"] is None else int(hit["page_start"]),
                None if hit["page_end"] is None else int(hit["page_end"]),
                None if hit["section_title"] is None else str(hit["section_title"]),
                str(hit["content"]),
                None if hit["lexical_rank"] is None else int(hit["lexical_rank"]),
                float(hit["fused_score"]),
                None if hit.get("dense_rank") is None else int(hit["dense_rank"]),
                None if hit.get("dense_score") is None else float(hit["dense_score"]),
                None if hit.get("rerank_score") is None else float(hit["rerank_score"]),
                None if hit.get("relevance_label") is None else str(hit["relevance_label"]),
                tuple(int(item) for item in hit.get("query_variant_ordinals", [])),
                tuple(str(item) for item in hit.get("rerank_reason_codes", [])),
            )
            for hit in response["hits"]
        )
        return KnowledgeRetrievalOutcome(
            str(response["retrieval_session_id"]),
            str(response["retrieval_hop_id"]),
            hits,
            str(response["stop_reason"]),
            receipt.commit_revision.value,
            receipt.replayed,
            int(response.get("hop_ordinal", 1)),
            int(response.get("novel_hit_count", len(hits))),
            int(response.get("cumulative_hit_count", len(hits))),
            str(response.get("session_status", "completed")),
            tuple(str(item) for item in response.get("query_variants", [])),
            str(response.get("query_planner_policy_revision", "")),
            str(response.get("reranker_policy_revision", "")),
        )

    def continue_retrieval(
        self, command: ContinueKnowledgeRetrievalCommand
    ) -> KnowledgeRetrievalOutcome:
        """Append one auditable hop to an open multi-hop retrieval session."""

        request = {
            "retrieval_session_id": command.retrieval_session_id,
            "query": command.query,
            "public_subquestion": command.public_subquestion,
            "limit": command.limit,
            "conclude_session": command.conclude_session,
            "unresolved_items": list(command.unresolved_items),
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            session = connection.execute(
                """
                SELECT session.retrieval_mode, session.corpus_roles_json,
                       state.status, state.next_hop_ordinal, state.pointer_revision
                FROM knowledge_retrieval_sessions AS session
                JOIN knowledge_retrieval_session_states AS state
                  USING (knowledge_retrieval_session_id)
                WHERE session.knowledge_retrieval_session_id = ?
                """,
                (command.retrieval_session_id,),
            ).fetchone()
            if session is None:
                raise ValueError("retrieval session does not exist")
            if str(session["status"]) != "open":
                raise ValueError("only an open retrieval session can be continued")
            if str(session["retrieval_mode"]) != "multi_hop":
                raise ValueError("only a multi-hop retrieval session can be continued")
            raw_roles = json.loads(str(session["corpus_roles_json"]))
            roles = tuple(CorpusRole(str(role)) for role in raw_roles)
            pipeline = self._run_retrieval_pipeline(
                connection,
                command.query,
                command.public_subquestion,
                RetrievalMode.MULTI_HOP,
                roles,
                command.limit,
                public_subquestion=command.public_subquestion,
            )
            hits = pipeline.selected
            hop_ordinal = int(session["next_hop_ordinal"])
            prior_node_ids = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT selection.knowledge_node_id
                    FROM knowledge_retrieval_selections AS selection
                    JOIN knowledge_retrieval_hops AS hop
                      USING (knowledge_retrieval_hop_id)
                    WHERE hop.knowledge_retrieval_session_id = ?
                    """,
                    (command.retrieval_session_id,),
                ).fetchall()
            }
            current_node_ids = {hit.node_id for hit in hits}
            novel_node_ids = current_node_ids - prior_node_ids
            state_revision = int(session["pointer_revision"]) + 1
            parent = connection.execute(
                """
                SELECT knowledge_retrieval_hop_id
                FROM knowledge_retrieval_hops
                WHERE knowledge_retrieval_session_id = ?
                ORDER BY hop_ordinal DESC
                LIMIT 1
                """,
                (command.retrieval_session_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("open retrieval session has no preceding hop")
            hop_id = self._identities.new(KnowledgeRetrievalHopId).value
            if command.conclude_session:
                status = "completed" if hits else "partial"
                terminal_reason = "agent_concluded" if hits else "no_evidence"
                hop_reason = terminal_reason
            elif not hits:
                status = "partial"
                terminal_reason = "no_evidence"
                hop_reason = terminal_reason
            elif not novel_node_ids:
                status = "partial"
                terminal_reason = "no_new_evidence"
                hop_reason = terminal_reason
            elif hop_ordinal >= self._agentic_policy.maximum_hops:
                status = "partial"
                terminal_reason = "hop_budget_exhausted"
                hop_reason = terminal_reason
            else:
                status = "open"
                terminal_reason = None
                hop_reason = "awaiting_next_hop"
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_hops(
                    knowledge_retrieval_hop_id, knowledge_retrieval_session_id,
                    parent_hop_id, hop_ordinal, public_subquestion, query, strategy,
                    unresolved_items_json, stop_reason, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, 'multi_hop', ?, ?, ?)
                """,
                (
                    hop_id,
                    command.retrieval_session_id,
                    str(parent["knowledge_retrieval_hop_id"]),
                    hop_ordinal,
                    command.public_subquestion,
                    command.query,
                    json.dumps(
                        list(command.unresolved_items),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    hop_reason,
                    revision.value,
                ),
            )
            self._record_retrieval_pipeline(connection, revision, hop_id, pipeline)
            for selected_ordinal, hit in enumerate(hits, start=1):
                connection.execute(
                    """
                    INSERT INTO knowledge_retrieval_selections(
                        knowledge_retrieval_hop_id, knowledge_node_id, selected_ordinal,
                        lexical_rank, dense_rank, dense_score, fused_score,
                        selection_reason, created_revision, rerank_score, relevance_label
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hop_id,
                        hit.node_id,
                        selected_ordinal,
                        hit.lexical_rank,
                        hit.dense_rank,
                        hit.dense_score,
                        hit.fused_score,
                        "reranked_diverse_selection",
                        revision.value,
                        hit.rerank_score,
                        hit.relevance_label,
                    ),
                )
            connection.execute(
                """
                UPDATE knowledge_retrieval_session_states
                SET status = ?, next_hop_ordinal = ?, stop_reason = ?,
                    pointer_revision = ?, updated_revision = ?
                WHERE knowledge_retrieval_session_id = ? AND pointer_revision = ?
                """,
                (
                    status,
                    hop_ordinal + 1,
                    terminal_reason,
                    state_revision,
                    revision.value,
                    command.retrieval_session_id,
                    int(session["pointer_revision"]),
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError("retrieval session changed concurrently")
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_session_state_history(
                    knowledge_retrieval_session_id, state_revision, status,
                    next_hop_ordinal, stop_reason, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    command.retrieval_session_id,
                    state_revision,
                    status,
                    hop_ordinal + 1,
                    terminal_reason,
                    revision.value,
                ),
            )
            hit_payload = [
                {
                    "node_id": hit.node_id,
                    "source_revision_id": hit.source_revision_id,
                    "parse_revision_id": hit.parse_revision_id,
                    "source_locator": hit.source_locator,
                    "corpus_role": hit.corpus_role,
                    "node_kind": hit.node_kind,
                    "page_start": hit.page_start,
                    "page_end": hit.page_end,
                    "section_title": hit.section_title,
                    "content": hit.content,
                    "lexical_rank": hit.lexical_rank,
                    "dense_rank": hit.dense_rank,
                    "dense_score": hit.dense_score,
                    "fused_score": hit.fused_score,
                    "rerank_score": hit.rerank_score,
                    "relevance_label": hit.relevance_label,
                    "query_variant_ordinals": list(hit.query_variant_ordinals),
                    "rerank_reason_codes": list(hit.rerank_reason_codes),
                }
                for hit in hits
            ]
            payload = {
                "retrieval_session_id": command.retrieval_session_id,
                "retrieval_hop_id": hop_id,
                "hits": hit_payload,
                "stop_reason": hop_reason,
                "hop_ordinal": hop_ordinal,
                "novel_hit_count": len(novel_node_ids),
                "cumulative_hit_count": len(prior_node_ids | current_node_ids),
                "session_status": status,
                "query_variants": [item.query for item in pipeline.query_plan.variants],
                "query_planner_policy_revision": (
                    pipeline.query_plan.planner_policy_revision
                ),
                "reranker_policy_revision": pipeline.reranker_policy_revision,
            }
            return MutationPayload(
                payload,
                (
                    JournalDraft(
                        "knowledge.retrieval_hop_completed",
                        "knowledge_retrieval_session",
                        command.retrieval_session_id,
                        {
                            "retrieval_hop_id": hop_id,
                            "hop_ordinal": hop_ordinal,
                            "selected_count": len(hits),
                            "novel_hit_count": len(novel_node_ids),
                            "cumulative_hit_count": len(prior_node_ids | current_node_ids),
                            "stop_reason": hop_reason,
                        },
                    ),
                ),
                (OutboxDraft("knowledge.retrieval_hop_recorded", payload),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="knowledge.continue_retrieval",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        hits = tuple(
            KnowledgeRetrievalHit(
                str(hit["node_id"]),
                str(hit["source_revision_id"]),
                str(hit["parse_revision_id"]),
                str(hit["source_locator"]),
                str(hit["corpus_role"]),
                str(hit["node_kind"]),
                None if hit["page_start"] is None else int(hit["page_start"]),
                None if hit["page_end"] is None else int(hit["page_end"]),
                None if hit["section_title"] is None else str(hit["section_title"]),
                str(hit["content"]),
                None if hit["lexical_rank"] is None else int(hit["lexical_rank"]),
                float(hit["fused_score"]),
                None if hit.get("dense_rank") is None else int(hit["dense_rank"]),
                None if hit.get("dense_score") is None else float(hit["dense_score"]),
                None if hit.get("rerank_score") is None else float(hit["rerank_score"]),
                None if hit.get("relevance_label") is None else str(hit["relevance_label"]),
                tuple(int(item) for item in hit.get("query_variant_ordinals", [])),
                tuple(str(item) for item in hit.get("rerank_reason_codes", [])),
            )
            for hit in response["hits"]
        )
        return KnowledgeRetrievalOutcome(
            str(response["retrieval_session_id"]),
            str(response["retrieval_hop_id"]),
            hits,
            str(response["stop_reason"]),
            receipt.commit_revision.value,
            receipt.replayed,
            int(response.get("hop_ordinal", 1)),
            int(response.get("novel_hit_count", len(hits))),
            int(response.get("cumulative_hit_count", len(hits))),
            str(response.get("session_status", "completed")),
            tuple(str(item) for item in response.get("query_variants", [])),
            str(response.get("query_planner_policy_revision", "")),
            str(response.get("reranker_policy_revision", "")),
        )

    def conclude_retrieval(
        self, command: ConcludeKnowledgeRetrievalCommand
    ) -> KnowledgeRetrievalOutcome:
        """Append an explicit terminal Hop when the Agent answers after gathering evidence."""

        request = {
            "retrieval_session_id": command.retrieval_session_id,
            "stop_reason": command.stop_reason,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            session = connection.execute(
                """
                SELECT status, next_hop_ordinal, pointer_revision
                FROM knowledge_retrieval_session_states
                WHERE knowledge_retrieval_session_id = ?
                """,
                (command.retrieval_session_id,),
            ).fetchone()
            if session is None:
                raise ValueError("retrieval session does not exist")
            if str(session["status"]) != "open":
                raise ValueError("only an open retrieval session can be concluded")
            parent = connection.execute(
                """
                SELECT knowledge_retrieval_hop_id
                FROM knowledge_retrieval_hops
                WHERE knowledge_retrieval_session_id = ?
                ORDER BY hop_ordinal DESC LIMIT 1
                """,
                (command.retrieval_session_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("open retrieval session has no preceding Hop")
            hop_ordinal = int(session["next_hop_ordinal"])
            hop_id = self._identities.new(KnowledgeRetrievalHopId).value
            cumulative_hit_count = int(
                connection.execute(
                    """
                    SELECT count(DISTINCT selection.knowledge_node_id)
                    FROM knowledge_retrieval_selections AS selection
                    JOIN knowledge_retrieval_hops AS hop
                      USING (knowledge_retrieval_hop_id)
                    WHERE hop.knowledge_retrieval_session_id = ?
                    """,
                    (command.retrieval_session_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_hops(
                    knowledge_retrieval_hop_id, knowledge_retrieval_session_id,
                    parent_hop_id, hop_ordinal, public_subquestion, query, strategy,
                    unresolved_items_json, stop_reason, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, 'agentic_conclusion', '[]', ?, ?)
                """,
                (
                    hop_id,
                    command.retrieval_session_id,
                    str(parent["knowledge_retrieval_hop_id"]),
                    hop_ordinal,
                    "Conclude the retrieval session from the accumulated evidence.",
                    "[session conclusion]",
                    command.stop_reason,
                    revision.value,
                ),
            )
            state_revision = int(session["pointer_revision"]) + 1
            connection.execute(
                """
                UPDATE knowledge_retrieval_session_states
                SET status = 'completed', next_hop_ordinal = ?, stop_reason = ?,
                    pointer_revision = ?, updated_revision = ?
                WHERE knowledge_retrieval_session_id = ? AND pointer_revision = ?
                """,
                (
                    hop_ordinal + 1,
                    command.stop_reason,
                    state_revision,
                    revision.value,
                    command.retrieval_session_id,
                    int(session["pointer_revision"]),
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError("retrieval session changed concurrently")
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_session_state_history(
                    knowledge_retrieval_session_id, state_revision, status,
                    next_hop_ordinal, stop_reason, created_revision
                ) VALUES (?, ?, 'completed', ?, ?, ?)
                """,
                (
                    command.retrieval_session_id,
                    state_revision,
                    hop_ordinal + 1,
                    command.stop_reason,
                    revision.value,
                ),
            )
            payload = {
                "retrieval_session_id": command.retrieval_session_id,
                "retrieval_hop_id": hop_id,
                "hits": [],
                "stop_reason": command.stop_reason,
                "hop_ordinal": hop_ordinal,
                "novel_hit_count": 0,
                "cumulative_hit_count": cumulative_hit_count,
                "session_status": "completed",
            }
            return MutationPayload(
                payload,
                (
                    JournalDraft(
                        "knowledge.retrieval_concluded",
                        "knowledge_retrieval_session",
                        command.retrieval_session_id,
                        {
                            "retrieval_hop_id": hop_id,
                            "hop_ordinal": hop_ordinal,
                            "stop_reason": command.stop_reason,
                            "cumulative_hit_count": cumulative_hit_count,
                        },
                    ),
                ),
                (OutboxDraft("knowledge.retrieval_concluded", payload),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="knowledge.conclude_retrieval",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return KnowledgeRetrievalOutcome(
            str(response["retrieval_session_id"]),
            str(response["retrieval_hop_id"]),
            (),
            str(response["stop_reason"]),
            receipt.commit_revision.value,
            receipt.replayed,
            int(response["hop_ordinal"]),
            int(response["novel_hit_count"]),
            int(response["cumulative_hit_count"]),
            str(response["session_status"]),
        )

    def conclude_open_sessions(self, turn_id: TurnId) -> tuple[str, ...]:
        """Close every Retrieval Session that the completing Turn intentionally leaves open."""

        session_ids = tuple(
            str(row[0])
            for row in self._connection.execute(
                """
                SELECT session.knowledge_retrieval_session_id
                FROM knowledge_retrieval_sessions AS session
                JOIN knowledge_retrieval_session_states AS state
                  USING (knowledge_retrieval_session_id)
                WHERE session.turn_id = ? AND state.status = 'open'
                ORDER BY session.created_revision, session.knowledge_retrieval_session_id
                """,
                (turn_id.value,),
            ).fetchall()
        )
        for session_id in session_ids:
            self.conclude_retrieval(
                ConcludeKnowledgeRetrievalCommand(
                    self._identities.new(CommandId), session_id, "agent_concluded"
                )
            )
        return session_ids

    def _run_retrieval_pipeline(
        self,
        connection: sqlite3.Connection,
        query: str,
        objective: str,
        mode: RetrievalMode,
        corpus_roles: tuple[CorpusRole, ...],
        limit: int,
        *,
        public_subquestion: str | None = None,
    ) -> _RetrievalPipelineResult:
        if not query.strip() or not objective.strip() or not 1 <= limit <= 24:
            raise ValueError("retrieval pipeline requires query, objective, and valid limit")
        query_plan = self._query_planner.plan(
            query,
            objective,
            mode,
            public_subquestion=public_subquestion,
        )
        candidate_limit = min(96, max(24, limit * 6))
        by_node: dict[str, CanonicalKnowledgeSearchHit] = {}
        aggregate_scores: dict[str, float] = {}
        variant_ordinals: dict[str, set[int]] = {}
        for variant in query_plan.variants:
            variant_hits = self._search_single_variant(
                connection,
                variant.query,
                corpus_roles,
                candidate_limit,
            )
            weight = 1.0 if variant.ordinal == 1 else 0.9
            for variant_rank, hit in enumerate(variant_hits, start=1):
                existing = by_node.get(hit.node_id)
                if existing is None:
                    by_node[hit.node_id] = hit
                else:
                    lexical_ranks = tuple(
                        value
                        for value in (existing.lexical_rank, hit.lexical_rank)
                        if value is not None
                    )
                    dense_ranks = tuple(
                        value
                        for value in (existing.dense_rank, hit.dense_rank)
                        if value is not None
                    )
                    dense_scores = tuple(
                        value
                        for value in (existing.dense_score, hit.dense_score)
                        if value is not None
                    )
                    by_node[hit.node_id] = replace(
                        existing,
                        lexical_rank=min(lexical_ranks) if lexical_ranks else None,
                        dense_rank=min(dense_ranks) if dense_ranks else None,
                        dense_score=max(dense_scores) if dense_scores else None,
                    )
                aggregate_scores[hit.node_id] = aggregate_scores.get(hit.node_id, 0.0) + (
                    weight * (hit.fused_score + (1.0 / (60 + variant_rank)))
                )
                variant_ordinals.setdefault(hit.node_id, set()).add(variant.ordinal)
        fused_candidates = tuple(
            sorted(
                (
                    replace(
                        hit,
                        fused_score=aggregate_scores[node_id],
                        query_variant_ordinals=tuple(sorted(variant_ordinals[node_id])),
                    )
                    for node_id, hit in by_node.items()
                ),
                key=lambda item: (-item.fused_score, item.source_locator, item.node_id),
            )
        )
        rerank_candidates = tuple(
            RerankCandidate(
                hit.node_id,
                hit.source_locator,
                hit.node_kind,
                hit.section_title,
                hit.content,
                hit.fused_score,
            )
            for hit in fused_candidates
        )
        assessments = self._reranker.rerank(query, objective, rerank_candidates)
        assessment_by_node = {item.node_id: item for item in assessments}
        assessed_candidates = tuple(
            replace(
                hit,
                rerank_score=assessment_by_node[hit.node_id].score,
                relevance_label=assessment_by_node[hit.node_id].relevance_label,
                rerank_reason_codes=assessment_by_node[hit.node_id].reason_codes,
            )
            for hit in fused_candidates
        )
        selected_assessments = select_diverse_evidence(
            assessments,
            rerank_candidates,
            limit=limit,
        )
        candidate_by_node = {item.node_id: item for item in assessed_candidates}
        selected = tuple(candidate_by_node[item.node_id] for item in selected_assessments)
        return _RetrievalPipelineResult(
            query_plan,
            assessed_candidates,
            selected,
            self._reranker.policy_revision,
        )

    def _record_retrieval_pipeline(
        self,
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        hop_id: str,
        pipeline: _RetrievalPipelineResult,
    ) -> None:
        for variant in pipeline.query_plan.variants:
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_query_variants(
                    knowledge_retrieval_hop_id, variant_ordinal, variant_kind,
                    query, reason_code, planner_policy_revision, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hop_id,
                    variant.ordinal,
                    variant.kind.value,
                    variant.query,
                    variant.reason_code,
                    pipeline.query_plan.planner_policy_revision,
                    revision.value,
                ),
            )
        selected_ids = {item.node_id for item in pipeline.selected}
        for candidate_ordinal, hit in enumerate(pipeline.candidates, start=1):
            connection.execute(
                """
                INSERT INTO knowledge_retrieval_candidates(
                    knowledge_retrieval_hop_id, knowledge_node_id, candidate_ordinal,
                    query_variant_ordinals_json, lexical_rank, dense_rank, dense_score,
                    fused_score, rerank_score, relevance_label, rerank_reason_codes_json,
                    reranker_policy_revision, selected, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hop_id,
                    hit.node_id,
                    candidate_ordinal,
                    json.dumps(list(hit.query_variant_ordinals), separators=(",", ":")),
                    hit.lexical_rank,
                    hit.dense_rank,
                    hit.dense_score,
                    hit.fused_score,
                    hit.rerank_score,
                    hit.relevance_label,
                    json.dumps(list(hit.rerank_reason_codes), separators=(",", ":")),
                    pipeline.reranker_policy_revision,
                    int(hit.node_id in selected_ids),
                    revision.value,
                ),
            )

    def _search_single_variant(
        self,
        connection: sqlite3.Connection,
        query: str,
        corpus_roles: tuple[CorpusRole, ...],
        limit: int,
    ) -> tuple[CanonicalKnowledgeSearchHit, ...]:
        if not query.strip() or not 1 <= limit <= 96:
            raise ValueError("canonical knowledge search requires a query and valid limit")
        if not corpus_roles:
            raise ValueError("canonical knowledge search requires at least one corpus role")
        tokens = sorted(retrieval_tokens(query))
        if not tokens:
            return ()
        fts_query = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        placeholders = ",".join("?" for _ in corpus_roles)
        retrieval_depth = min(96, max(limit * 4, limit))
        rows = connection.execute(
            f"""
            SELECT node.knowledge_node_id, source_revision.knowledge_source_revision_id,
                   parse.knowledge_parse_revision_id, source.canonical_locator,
                   membership.corpus_role, node.node_kind, node.page_start, node.page_end,
                   CASE WHEN parent.node_kind IN ('title', 'section', 'help_topic')
                        THEN parent.content ELSE NULL END AS section_title,
                   node.content, bm25(knowledge_nodes_fts) AS lexical_score
            FROM knowledge_nodes_fts
            JOIN knowledge_nodes AS node
              ON node.knowledge_node_id = knowledge_nodes_fts.knowledge_node_id
            JOIN knowledge_parse_revisions AS parse USING (knowledge_parse_revision_id)
            JOIN knowledge_source_revisions AS source_revision
              USING (knowledge_source_revision_id)
            JOIN knowledge_sources AS source USING (knowledge_source_id)
            JOIN knowledge_source_states AS state USING (knowledge_source_id)
            JOIN knowledge_source_memberships AS membership USING (knowledge_source_id)
            LEFT JOIN knowledge_nodes AS parent
              ON parent.knowledge_node_id = node.parent_node_id
            WHERE knowledge_nodes_fts MATCH ?
              AND membership.corpus_role IN ({placeholders})
              AND state.availability = 'indexed'
              AND state.current_source_revision_id = source_revision.knowledge_source_revision_id
              AND state.current_parse_revision_id = parse.knowledge_parse_revision_id
              AND node.node_kind != 'document'
            ORDER BY lexical_score, source.canonical_locator, node.ordinal
            LIMIT ?
            """,
            (fts_query, *(role.value for role in corpus_roles), retrieval_depth),
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        result: list[CanonicalKnowledgeSearchHit] = []
        for lexical_rank, row in enumerate(rows, start=1):
            key = (str(row["knowledge_node_id"]), str(row["corpus_role"]))
            if key in seen:
                continue
            seen.add(key)
            content = str(row["content"])
            overlap = len(set(tokens).intersection(retrieval_tokens(content)))
            fused_score = (1.0 / (60 + lexical_rank)) + (overlap * 0.001)
            result.append(
                CanonicalKnowledgeSearchHit(
                    str(row["knowledge_node_id"]),
                    str(row["knowledge_source_revision_id"]),
                    str(row["knowledge_parse_revision_id"]),
                    str(row["canonical_locator"]),
                    str(row["corpus_role"]),
                    str(row["node_kind"]),
                    None if row["page_start"] is None else int(row["page_start"]),
                    None if row["page_end"] is None else int(row["page_end"]),
                    None if row["section_title"] is None else str(row["section_title"]),
                    content,
                    lexical_rank,
                    fused_score,
                )
            )
        if self._dense_candidates is None:
            return tuple(result[:limit])
        dense = self._dense_candidates.search(query, corpus_roles, retrieval_depth)
        if not dense:
            return tuple(result[:limit])
        allowed_roles = {role.value for role in corpus_roles}
        by_key = {(hit.node_id, hit.corpus_role): hit for hit in result}
        missing_ids = tuple(
            candidate.node_id
            for candidate in dense
            if not any(key[0] == candidate.node_id for key in by_key)
        )
        if missing_ids:
            for hit in self.read_canonical_nodes(tuple(dict.fromkeys(missing_ids))):
                if hit.corpus_role in allowed_roles:
                    by_key[(hit.node_id, hit.corpus_role)] = hit
        lexical_rank_by_key = {
            (hit.node_id, hit.corpus_role): hit.lexical_rank for hit in result
        }
        dense_by_node = {candidate.node_id: candidate for candidate in dense}
        fused: list[CanonicalKnowledgeSearchHit] = []
        for key, hit in by_key.items():
            lexical = lexical_rank_by_key.get(key)
            dense_candidate = dense_by_node.get(hit.node_id)
            score = 0.0
            if lexical is not None:
                # Exact language is especially valuable for source inspection and quotations.
                # Give lexical evidence a stable 2:1 RRF weight while retaining dense-only recall.
                score += hit.fused_score * 2.0
                token_coverage = len(
                    set(tokens).intersection(retrieval_tokens(hit.content))
                ) / len(tokens)
                if token_coverage == 1.0:
                    score += 0.04
            if dense_candidate is not None:
                score += 1.0 / (60 + dense_candidate.dense_rank)
            fused.append(
                CanonicalKnowledgeSearchHit(
                    hit.node_id,
                    hit.source_revision_id,
                    hit.parse_revision_id,
                    hit.source_locator,
                    hit.corpus_role,
                    hit.node_kind,
                    hit.page_start,
                    hit.page_end,
                    hit.section_title,
                    hit.content,
                    lexical,
                    score,
                    None if dense_candidate is None else dense_candidate.dense_rank,
                    None if dense_candidate is None else dense_candidate.dense_score,
                )
            )
        def ranking_key(
            hit: CanonicalKnowledgeSearchHit,
        ) -> tuple[float, str, str]:
            return (-hit.fused_score, hit.source_locator, hit.node_id)
        ranked = sorted(fused, key=ranking_key)
        selected = ranked[:limit]
        # Hybrid retrieval must not erase the strongest exact lexical evidence. Reserve up
        # to two slots for lexical seeds, then keep the final list ordered by fused score.
        lexical_seed_ids = {
            hit.node_id for hit in result[: min(2, limit)]
        }
        fused_by_node = {hit.node_id: hit for hit in ranked}
        for lexical_seed_id in lexical_seed_ids:
            if any(hit.node_id == lexical_seed_id for hit in selected):
                continue
            selected.append(fused_by_node[lexical_seed_id])
        while len(selected) > limit:
            removable = next(
                index
                for index in range(len(selected) - 1, -1, -1)
                if selected[index].node_id not in lexical_seed_ids
            )
            selected.pop(removable)
        return tuple(sorted(selected, key=ranking_key))

    def catalog(self) -> tuple[tuple[str, str, str, int | None], ...]:
        return tuple(
            (
                str(row["relative_path"]),
                str(row["availability"]),
                "" if row["content_sha256"] is None else str(row["content_sha256"]),
                None if row["page_count"] is None else int(row["page_count"]),
            )
            for row in self._connection.execute(
                """
                SELECT document.relative_path, state.availability,
                       revision.content_sha256, revision.page_count
                FROM knowledge_documents AS document
                JOIN knowledge_document_states AS state USING (knowledge_document_id)
                LEFT JOIN knowledge_document_revisions AS revision
                  ON revision.knowledge_document_revision_id = state.current_revision_id
                ORDER BY document.relative_path
                """
            ).fetchall()
        )

    def _index_document(
        self,
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        document: ExtractedKnowledgeDocument,
    ) -> None:
        current = connection.execute(
            """
            SELECT document.knowledge_document_id, state.pointer_revision,
                   state.current_revision_id
            FROM knowledge_documents AS document
            LEFT JOIN knowledge_document_states AS state USING (knowledge_document_id)
            WHERE document.relative_path = ?
            """,
            (document.relative_path,),
        ).fetchone()
        if current is None:
            document_id = self._identities.new(KnowledgeDocumentId).value
            pointer_revision = 1
            revision_number = 1
            connection.execute(
                "INSERT INTO knowledge_documents VALUES (?, ?, ?)",
                (document_id, document.relative_path, revision.value),
            )
        else:
            document_id = str(current["knowledge_document_id"])
            pointer_revision = int(current["pointer_revision"]) + 1
            existing_revision = connection.execute(
                """
                SELECT knowledge_document_revision_id
                FROM knowledge_document_revisions
                WHERE knowledge_document_id = ? AND content_sha256 = ?
                """,
                (document_id, document.content_sha256),
            ).fetchone()
            if existing_revision is not None:
                self._ensure_canonical_document(
                    connection,
                    revision,
                    document,
                    document_id,
                    str(existing_revision["knowledge_document_revision_id"]),
                    revision_number=None,
                )
                connection.execute(
                    """
                    UPDATE knowledge_document_states
                    SET current_revision_id = ?, availability = 'indexed', pointer_revision = ?,
                        updated_revision = ? WHERE knowledge_document_id = ?
                    """,
                    (
                        str(existing_revision["knowledge_document_revision_id"]),
                        pointer_revision,
                        revision.value,
                        document_id,
                    ),
                )
                return
            revision_number = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(revision_number), 0) + 1
                    FROM knowledge_document_revisions WHERE knowledge_document_id = ?
                    """,
                    (document_id,),
                ).fetchone()[0]
            )
        document_revision_id = self._identities.new(KnowledgeDocumentRevisionId).value
        extracted = "\n\n".join(page.text for page in document.pages)
        page_numbers = [page.page_number for page in document.pages if page.page_number is not None]
        connection.execute(
            """
            INSERT INTO knowledge_document_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_revision_id,
                document_id,
                revision_number,
                document.content_sha256,
                document.source_size,
                document.media_type,
                hashlib.sha256(extracted.encode("utf-8")).hexdigest(),
                max(page_numbers) if page_numbers else None,
                revision.value,
            ),
        )
        for ordinal, page_start, page_end, content in self._chunks(document):
            connection.execute(
                """
                INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._identities.new(KnowledgeChunkId).value,
                    document_revision_id,
                    ordinal,
                    page_start,
                    page_end,
                    content,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    max(1, (len(content.encode("utf-8")) + 3) // 4),
                    revision.value,
                ),
            )
        self._ensure_canonical_document(
            connection,
            revision,
            document,
            document_id,
            document_revision_id,
            revision_number=revision_number,
        )
        if current is None:
            connection.execute(
                """
                INSERT INTO knowledge_document_states VALUES (?, ?, 'indexed', 1, ?)
                """,
                (document_id, document_revision_id, revision.value),
            )
        else:
            connection.execute(
                """
                UPDATE knowledge_document_states
                SET current_revision_id = ?, availability = 'indexed', pointer_revision = ?,
                    updated_revision = ? WHERE knowledge_document_id = ?
                """,
                (document_revision_id, pointer_revision, revision.value, document_id),
            )

    def _ensure_failed_canonical_source(
        self,
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        legacy_document_id: str,
        relative_path: str,
    ) -> None:
        source = connection.execute(
            """
            SELECT knowledge_source_id FROM knowledge_sources
            WHERE legacy_knowledge_document_id = ?
            """,
            (legacy_document_id,),
        ).fetchone()
        if source is None:
            source_id = self._identities.new(KnowledgeSourceId).value
            source_kind = "stata_help" if relative_path.startswith("stata-help/") else "local_file"
            connection.execute(
                """
                INSERT INTO knowledge_sources(
                    knowledge_source_id, source_kind, canonical_locator,
                    legacy_knowledge_document_id, created_revision
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, source_kind, relative_path, legacy_document_id, revision.value),
            )
            role = self._role_for_path(relative_path)
            connection.execute(
                """
                INSERT INTO knowledge_source_memberships(
                    knowledge_source_id, corpus_role, membership_source, created_revision
                ) VALUES (?, ?, 'folder', ?)
                """,
                (source_id, role, revision.value),
            )
            connection.execute(
                """
                INSERT INTO knowledge_source_states(
                    knowledge_source_id, current_source_revision_id, current_parse_revision_id,
                    availability, pointer_revision, updated_revision
                ) VALUES (?, NULL, NULL, 'extraction_failed', 1, ?)
                """,
                (source_id, revision.value),
            )
            return
        connection.execute(
            """
            UPDATE knowledge_source_states
            SET availability = 'extraction_failed', pointer_revision = pointer_revision + 1,
                updated_revision = ? WHERE knowledge_source_id = ?
            """,
            (revision.value, str(source["knowledge_source_id"])),
        )

    def _ensure_canonical_document(
        self,
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        document: ExtractedKnowledgeDocument,
        legacy_document_id: str,
        legacy_document_revision_id: str,
        *,
        revision_number: int | None,
    ) -> None:
        source = connection.execute(
            """
            SELECT knowledge_source_id FROM knowledge_sources
            WHERE legacy_knowledge_document_id = ?
            """,
            (legacy_document_id,),
        ).fetchone()
        if source is None:
            source_id = self._identities.new(KnowledgeSourceId).value
            connection.execute(
                """
                INSERT INTO knowledge_sources(
                    knowledge_source_id, source_kind, canonical_locator,
                    legacy_knowledge_document_id, created_revision
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    document.source_kind.value,
                    document.relative_path,
                    legacy_document_id,
                    revision.value,
                ),
            )
        else:
            source_id = str(source["knowledge_source_id"])
        for role in document.corpus_roles:
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_source_memberships(
                    knowledge_source_id, corpus_role, membership_source, created_revision
                ) VALUES (?, ?, 'folder', ?)
                """,
                (source_id, role.value, revision.value),
            )
        source_revision = connection.execute(
            """
            SELECT knowledge_source_revision_id FROM knowledge_source_revisions
            WHERE legacy_knowledge_document_revision_id = ?
            """,
            (legacy_document_revision_id,),
        ).fetchone()
        if source_revision is None:
            source_revision_id = self._identities.new(KnowledgeSourceRevisionId).value
            if revision_number is None:
                revision_number = int(
                    connection.execute(
                        """
                        SELECT revision_number FROM knowledge_document_revisions
                        WHERE knowledge_document_revision_id = ?
                        """,
                        (legacy_document_revision_id,),
                    ).fetchone()[0]
                )
            connection.execute(
                """
                INSERT INTO knowledge_source_revisions(
                    knowledge_source_revision_id, knowledge_source_id, revision_number,
                    content_sha256, source_size, media_type, source_metadata_json,
                    legacy_knowledge_document_revision_id, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_revision_id,
                    source_id,
                    revision_number,
                    document.content_sha256,
                    document.source_size,
                    document.media_type,
                    json.dumps(
                        {
                            "canonical_locator": document.relative_path,
                            "corpus_roles": [role.value for role in document.corpus_roles],
                            "ingestion_policy_revision": document.ingestion_policy_revision,
                            "canonical_ir_version": document.canonical_ir_version,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    legacy_document_revision_id,
                    revision.value,
                ),
            )
        else:
            source_revision_id = str(source_revision["knowledge_source_revision_id"])
        parse = connection.execute(
            """
            SELECT knowledge_parse_revision_id FROM knowledge_parse_revisions
            WHERE knowledge_source_revision_id = ? AND parser_name = ?
              AND parser_version = ? AND parser_profile = ?
              AND canonical_ir_version = ?
            """,
            (
                source_revision_id,
                document.parser_name,
                document.parser_version,
                document.parser_profile,
                document.canonical_ir_version,
            ),
        ).fetchone()
        if parse is None:
            parse_revision_id = self._identities.new(KnowledgeParseRevisionId).value
            quality_findings = [
                {
                    "page_number": finding.page_number,
                    "reason_codes": list(finding.reason_codes),
                    "diagnostic_score": finding.diagnostic_score,
                    "selected_parser": finding.selected_parser,
                }
                for finding in document.page_parse_findings
            ]
            quality_findings.insert(
                0,
                {
                    "finding_kind": "ingestion_metadata",
                    "ingestion_policy_revision": document.ingestion_policy_revision,
                    "canonical_ir_version": document.canonical_ir_version,
                    "corpus_roles": [role.value for role in document.corpus_roles],
                },
            )
            connection.execute(
                """
                INSERT INTO knowledge_parse_revisions(
                    knowledge_parse_revision_id, knowledge_source_revision_id,
                    parser_name, parser_version, parser_profile, canonical_ir_version,
                    parse_status, quality_findings_json, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?)
                """,
                (
                    parse_revision_id,
                    source_revision_id,
                    document.parser_name,
                    document.parser_version,
                    document.parser_profile,
                    document.canonical_ir_version,
                    json.dumps(
                        quality_findings,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    revision.value,
                ),
            )
            drafts = build_canonical_nodes(document)
            node_ids: dict[str, str] = {}
            ordered_ids: list[str] = []
            for draft in drafts:
                node_id = self._identities.new(KnowledgeNodeId).value
                parent_id = (
                    None if draft.parent_local_key is None else node_ids[draft.parent_local_key]
                )
                content_hash = hashlib.sha256(draft.text.encode("utf-8")).hexdigest()
                anchor = hashlib.sha256(
                    (
                        f"{draft.node_kind.value}\0{draft.parent_local_key or ''}\0"
                        f"{draft.text.strip()}"
                    ).encode()
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO knowledge_nodes(
                        knowledge_node_id, knowledge_parse_revision_id, parent_node_id,
                        local_key, node_kind, semantic_role, ordinal, page_start, page_end,
                        source_span_start, source_span_end, content, structured_payload_json,
                        anchor_fingerprint, content_sha256, created_revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        parse_revision_id,
                        parent_id,
                        draft.local_key,
                        draft.node_kind.value,
                        draft.semantic_role,
                        draft.ordinal,
                        draft.page_start,
                        draft.page_end,
                        draft.source_span_start,
                        draft.source_span_end,
                        draft.text,
                        (
                            None
                            if draft.structured_payload is None
                            else json.dumps(
                                draft.structured_payload,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        ),
                        anchor,
                        content_hash,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO knowledge_nodes_fts(knowledge_node_id, content) VALUES (?, ?)",
                    (node_id, draft.text),
                )
                node_ids[draft.local_key] = node_id
                if parent_id is not None:
                    self._insert_edge(
                        connection,
                        revision,
                        parent_id,
                        node_id,
                        "contains",
                        document.canonical_ir_version,
                    )
                if ordered_ids:
                    self._insert_edge(
                        connection,
                        revision,
                        ordered_ids[-1],
                        node_id,
                        "next",
                        document.canonical_ir_version,
                    )
                    self._insert_edge(
                        connection,
                        revision,
                        node_id,
                        ordered_ids[-1],
                        "previous",
                        document.canonical_ir_version,
                    )
                ordered_ids.append(node_id)
        else:
            parse_revision_id = str(parse["knowledge_parse_revision_id"])
        state = connection.execute(
            "SELECT pointer_revision FROM knowledge_source_states WHERE knowledge_source_id = ?",
            (source_id,),
        ).fetchone()
        if state is None:
            connection.execute(
                """
                INSERT INTO knowledge_source_states(
                    knowledge_source_id, current_source_revision_id, current_parse_revision_id,
                    availability, pointer_revision, updated_revision
                ) VALUES (?, ?, ?, 'indexed', 1, ?)
                """,
                (source_id, source_revision_id, parse_revision_id, revision.value),
            )
        else:
            connection.execute(
                """
                UPDATE knowledge_source_states
                SET current_source_revision_id = ?, current_parse_revision_id = ?,
                    availability = 'indexed', pointer_revision = ?, updated_revision = ?
                WHERE knowledge_source_id = ?
                """,
                (
                    source_revision_id,
                    parse_revision_id,
                    int(state["pointer_revision"]) + 1,
                    revision.value,
                    source_id,
                ),
            )

    def _insert_edge(
        self,
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        from_node_id: str,
        to_node_id: str,
        edge_kind: str,
        producer_revision: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO knowledge_edges(
                knowledge_edge_id, from_node_id, to_node_id, edge_kind,
                producer_kind, producer_revision, confidence, source_locator,
                created_revision
            ) VALUES (?, ?, ?, ?, 'deterministic', ?, NULL, NULL, ?)
            """,
            (
                self._identities.new(KnowledgeEdgeId).value,
                from_node_id,
                to_node_id,
                edge_kind,
                producer_revision,
                revision.value,
            ),
        )

    @staticmethod
    def _role_for_path(relative_path: str) -> str:
        if relative_path.startswith("style-references/"):
            return CorpusRole.STYLE_EXEMPLAR.value
        if relative_path.startswith("stata-help/"):
            return CorpusRole.STATA_HELP.value
        return CorpusRole.LITERATURE_EVIDENCE.value

    @staticmethod
    def _chunks(
        document: ExtractedKnowledgeDocument,
    ) -> tuple[tuple[int, int | None, int | None, str], ...]:
        result: list[tuple[int, int | None, int | None, str]] = []
        ordinal = 0
        for page in document.pages:
            normalized = "\n".join(line.strip() for line in page.text.splitlines() if line.strip())
            start = 0
            while start < len(normalized):
                end = min(len(normalized), start + 2400)
                if end < len(normalized):
                    boundary = max(
                        normalized.rfind("\n", start, end),
                        normalized.rfind("。", start, end),
                    )
                    if boundary > start + 800:
                        end = boundary + 1
                content = normalized[start:end].strip()
                if content:
                    ordinal += 1
                    result.append((ordinal, page.page_number, page.page_number, content))
                if end >= len(normalized):
                    break
                start = max(start + 1, end - 200)
        if not result:
            raise ValueError("literature document produced no chunks")
        return tuple(result[:2048])
