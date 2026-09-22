"""Authoritative read models for the default browser research views."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from stata_research_agent.application.queries import (
    AnalysisOutputArtifactItem,
    AnalysisOutputIndexItem,
    AnalysisOutputIndexSnapshot,
    ConversationDetailSnapshot,
    ConversationTimelineItem,
    DocumentIndexSnapshot,
    DocumentSlotItem,
    JournalEntryItem,
    JournalEntryPage,
    ResultIndexItem,
    ResultIndexSnapshot,
)
from stata_research_agent.domain.identifiers import ConversationId, ResearchPathId, TurnId
from stata_research_agent.domain.revisions import WorkspaceRevision


class JournalCursorError(ValueError):
    """Base error for an opaque Journal page cursor."""


class JournalCursorInvalidError(JournalCursorError):
    pass


class JournalCursorQueryMismatchError(JournalCursorError):
    pass


@dataclass(frozen=True, slots=True)
class JournalFilters:
    turn_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    event_type: str | None = None
    actor_type: str | None = None
    object_type: str | None = None
    object_id: str | None = None

    def canonical(self) -> dict[str, str | None]:
        return {
            "actor_type": self.actor_type,
            "attempt_id": self.attempt_id,
            "event_type": self.event_type,
            "object_id": self.object_id,
            "object_type": self.object_type,
            "operation_id": self.operation_id,
            "turn_id": self.turn_id,
        }


@dataclass(frozen=True, slots=True)
class _JournalCursor:
    workspace_id: str
    as_of_revision: int
    boundary_revision: int
    boundary_ordinal: int
    page_size: int
    sort_direction: str
    filters: JournalFilters


class JournalCursorCodec:
    """Process-scoped integrity protection for opaque Journal cursors."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("Journal cursor secret must contain at least 32 bytes")
        self._secret = secret

    def encode(self, cursor: _JournalCursor) -> str:
        payload = {
            "v": 1,
            "workspace_id": cursor.workspace_id,
            "as_of_revision": cursor.as_of_revision,
            "boundary_revision": cursor.boundary_revision,
            "boundary_ordinal": cursor.boundary_ordinal,
            "page_size": cursor.page_size,
            "sort_direction": cursor.sort_direction,
            "filters": cursor.filters.canonical(),
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(body + signature).decode().rstrip("=")
        return f"jpc1_{token}"

    def decode(self, token: str) -> _JournalCursor:
        if not token.startswith("jpc1_"):
            raise JournalCursorInvalidError("Journal cursor namespace is invalid")
        try:
            encoded = token.removeprefix("jpc1_")
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            canonical = base64.urlsafe_b64encode(raw).decode().rstrip("=")
            if not hmac.compare_digest(encoded, canonical):
                raise ValueError
            if len(raw) <= 32:
                raise ValueError
            body, supplied_signature = raw[:-32], raw[-32:]
            expected_signature = hmac.new(self._secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError
            payload = json.loads(body)
            if payload["v"] != 1:
                raise ValueError
            filters = JournalFilters(**payload["filters"])
            return _JournalCursor(
                workspace_id=str(payload["workspace_id"]),
                as_of_revision=int(payload["as_of_revision"]),
                boundary_revision=int(payload["boundary_revision"]),
                boundary_ordinal=int(payload["boundary_ordinal"]),
                page_size=int(payload["page_size"]),
                sort_direction=str(payload["sort_direction"]),
                filters=filters,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise JournalCursorInvalidError("Journal cursor is malformed") from error


class SqliteResearchViewQuery:
    def __init__(self, connection: sqlite3.Connection, cursor_codec: JournalCursorCodec) -> None:
        self._connection = connection
        self._cursor_codec = cursor_codec

    def conversation(self, conversation_id: ConversationId) -> ConversationDetailSnapshot:
        self._connection.execute("BEGIN")
        try:
            revision = self._authoritative_revision()
            if (
                self._connection.execute(
                    "SELECT 1 FROM conversations WHERE conversation_id = ?",
                    (conversation_id.value,),
                ).fetchone()
                is None
            ):
                raise ValueError("Conversation does not exist")
            timeline_rows = self._connection.execute(
                """
                SELECT 'message' AS item_kind, m.message_id AS item_id,
                       NULL AS turn_id, m.role, m.content,
                       m.ordinal * 2 AS timeline_ordinal, m.created_revision
                FROM messages AS m
                WHERE m.conversation_id = ?
                UNION ALL
                SELECT 'assistant_output', output.assistant_output_id,
                       step.turn_id, 'assistant',
                       COALESCE(json_extract(output.output_json, '$.text'), output.output_json),
                       step.step_ordinal * 2 + 1, output.created_revision
                FROM assistant_outputs AS output
                JOIN model_invocations AS invocation
                  ON invocation.model_invocation_id = output.model_invocation_id
                JOIN steps AS step ON step.step_id = invocation.step_id
                JOIN turns AS turn ON turn.turn_id = step.turn_id
                WHERE turn.conversation_id = ?
                ORDER BY created_revision, timeline_ordinal, item_id
                """,
                (conversation_id.value, conversation_id.value),
            ).fetchall()
            timeline = tuple(
                ConversationTimelineItem(
                    item_kind=str(row["item_kind"]),
                    item_id=str(row["item_id"]),
                    turn_id=None if row["turn_id"] is None else TurnId(str(row["turn_id"])),
                    role=str(row["role"]),
                    content=str(row["content"]),
                    ordinal=int(row["timeline_ordinal"]),
                    created_revision=WorkspaceRevision(int(row["created_revision"])),
                )
                for row in timeline_rows
            )
            from stata_research_agent.persistence.control_query import SqliteWorkspaceQuery

            turn_rows = self._connection.execute(
                """
                SELECT turn_id, conversation_id, execution_mode, status,
                       turn_revision, enqueue_ordinal
                FROM turns WHERE conversation_id = ? ORDER BY enqueue_ordinal
                """,
                (conversation_id.value,),
            ).fetchall()
            turns = tuple(SqliteWorkspaceQuery._turn_summary(row) for row in turn_rows)
            self._connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return ConversationDetailSnapshot(
            WorkspaceRevision(revision), conversation_id, timeline, turns
        )

    def results(self, research_path_id: ResearchPathId) -> ResultIndexSnapshot:
        self._connection.execute("BEGIN")
        try:
            revision = self._authoritative_revision()
            self._require_path(research_path_id)
            rows = self._connection.execute(
                """
                SELECT slot.result_slot_id, slot.canonical_key, slot.display_name,
                       result.result_id, result.result_kind, result.producing_stata_run_id,
                       result.created_by_turn_id, result.created_revision,
                       adoption.pointer_revision, adoption.commit_revision,
                       COUNT(DISTINCT element.result_element_id) AS element_count,
                       COUNT(DISTINCT evidence.evidence_record_id) AS evidence_count
                FROM result_slots AS slot
                JOIN path_result_adoptions AS adoption
                  ON adoption.result_slot_id = slot.result_slot_id
                JOIN results AS result ON result.result_id = adoption.target_result_id
                LEFT JOIN result_elements AS element ON element.result_id = result.result_id
                LEFT JOIN evidence_statistical_sources AS evidence
                  ON evidence.result_element_id = element.result_element_id
                WHERE slot.research_path_id = ?
                GROUP BY slot.result_slot_id, slot.canonical_key, slot.display_name,
                         result.result_id, result.result_kind, result.producing_stata_run_id,
                         result.created_by_turn_id, result.created_revision,
                         adoption.pointer_revision, adoption.commit_revision
                ORDER BY slot.canonical_key
                """,
                (research_path_id.value,),
            ).fetchall()
            items = tuple(
                ResultIndexItem(
                    result_slot_id=str(row["result_slot_id"]),
                    slot_key=str(row["canonical_key"]),
                    slot_display_name=str(row["display_name"]),
                    result_id=str(row["result_id"]),
                    result_kind=str(row["result_kind"]),
                    producing_stata_run_id=str(row["producing_stata_run_id"]),
                    created_by_turn_id=TurnId(str(row["created_by_turn_id"])),
                    created_revision=WorkspaceRevision(int(row["created_revision"])),
                    pointer_revision=int(row["pointer_revision"]),
                    adopted_revision=WorkspaceRevision(int(row["commit_revision"])),
                    element_count=int(row["element_count"]),
                    evidence_count=int(row["evidence_count"]),
                    result_element_ids=tuple(
                        str(element[0])
                        for element in self._connection.execute(
                            """
                            SELECT result_element_id FROM result_elements
                            WHERE result_id = ? ORDER BY semantic_key, result_element_id
                            """,
                            (str(row["result_id"]),),
                        ).fetchall()
                    ),
                )
                for row in rows
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return ResultIndexSnapshot(WorkspaceRevision(revision), research_path_id, items)

    def documents(self, research_path_id: ResearchPathId) -> DocumentIndexSnapshot:
        self._connection.execute("BEGIN")
        try:
            revision = self._authoritative_revision()
            self._require_path(research_path_id)
            rows = self._connection.execute(
                """
                SELECT slot.document_slot_id, slot.canonical_key,
                       adoption.pointer_revision, revision.document_id,
                       revision.document_revision_id, revision.origin_kind,
                       revision.docx_artifact_id, gate.verdict,
                       adoption.commit_revision
                FROM document_slots AS slot
                LEFT JOIN path_document_adoptions AS adoption
                  ON adoption.document_slot_id = slot.document_slot_id
                LEFT JOIN document_revisions AS revision
                  ON revision.document_revision_id = adoption.target_document_revision_id
                LEFT JOIN delivery_gate_reports AS gate
                  ON gate.delivery_gate_report_id = adoption.delivery_gate_report_id
                WHERE slot.research_path_id = ?
                ORDER BY slot.canonical_key
                """,
                (research_path_id.value,),
            ).fetchall()
            items = tuple(
                DocumentSlotItem(
                    document_slot_id=str(row["document_slot_id"]),
                    slot_key=str(row["canonical_key"]),
                    pointer_revision=None
                    if row["pointer_revision"] is None
                    else int(row["pointer_revision"]),
                    document_id=None if row["document_id"] is None else str(row["document_id"]),
                    document_revision_id=None
                    if row["document_revision_id"] is None
                    else str(row["document_revision_id"]),
                    origin_kind=None if row["origin_kind"] is None else str(row["origin_kind"]),
                    docx_artifact_id=None
                    if row["docx_artifact_id"] is None
                    else str(row["docx_artifact_id"]),
                    delivery_gate_verdict=None if row["verdict"] is None else str(row["verdict"]),
                    adopted_revision=None
                    if row["commit_revision"] is None
                    else WorkspaceRevision(int(row["commit_revision"])),
                )
                for row in rows
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return DocumentIndexSnapshot(WorkspaceRevision(revision), research_path_id, items)

    def analysis_outputs(self) -> AnalysisOutputIndexSnapshot:
        self._connection.execute("BEGIN")
        try:
            revision = self._authoritative_revision()
            rows = self._connection.execute(
                """
                SELECT output.analysis_output_id, output.output_fingerprint,
                       output.runtime_kind, output.method_summary,
                       output.created_by_turn_id, output.created_revision,
                       classification.output_kind,
                       adoption.analysis_output_adoption_id,
                       evidence.evidence_record_id
                FROM analysis_outputs AS output
                JOIN analysis_output_classifications AS classification
                  USING (analysis_output_id)
                LEFT JOIN analysis_output_adoptions AS adoption USING (analysis_output_id)
                LEFT JOIN analysis_evidence_records AS evidence USING (analysis_output_id)
                ORDER BY output.created_revision DESC, output.analysis_output_id
                """
            ).fetchall()
            items = tuple(
                AnalysisOutputIndexItem(
                    analysis_output_id=str(row["analysis_output_id"]),
                    output_kind=str(row["output_kind"]),
                    output_fingerprint=str(row["output_fingerprint"]),
                    runtime_kind=str(row["runtime_kind"]),
                    method_summary=str(row["method_summary"]),
                    created_by_turn_id=TurnId(str(row["created_by_turn_id"])),
                    created_revision=WorkspaceRevision(int(row["created_revision"])),
                    document_eligible=str(row["output_kind"]) != "unknown",
                    adoption_id=(
                        None
                        if row["analysis_output_adoption_id"] is None
                        else str(row["analysis_output_adoption_id"])
                    ),
                    evidence_record_id=(
                        None
                        if row["evidence_record_id"] is None
                        else str(row["evidence_record_id"])
                    ),
                    artifacts=tuple(
                        AnalysisOutputArtifactItem(
                            artifact_id=str(artifact["artifact_id"]),
                            role=str(artifact["role"]),
                            artifact_kind=str(artifact["artifact_kind"]),
                            media_type=str(artifact["media_type"]),
                            size_bytes=int(artifact["size_bytes"]),
                            content_sha256=str(artifact["content_hash"]),
                            verification_receipt_id=(
                                None
                                if artifact["verification_receipt_id"] is None
                                else str(artifact["verification_receipt_id"])
                            ),
                        )
                        for artifact in self._connection.execute(
                            """
                            SELECT binding.artifact_id, binding.role,
                                   artifact.artifact_kind, artifact.media_type,
                                   artifact.size_bytes, artifact.content_hash,
                                   (
                                     SELECT receipt.verification_receipt_id
                                     FROM artifact_verification_receipts AS receipt
                                     WHERE receipt.artifact_id = artifact.artifact_id
                                       AND receipt.verdict = 'verified'
                                     ORDER BY receipt.workspace_revision DESC LIMIT 1
                                   ) AS verification_receipt_id
                            FROM analysis_output_artifacts AS binding
                            JOIN artifacts AS artifact USING (artifact_id)
                            WHERE binding.analysis_output_id = ?
                            ORDER BY binding.ordinal
                            """,
                            (str(row["analysis_output_id"]),),
                        ).fetchall()
                    ),
                )
                for row in rows
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return AnalysisOutputIndexSnapshot(WorkspaceRevision(revision), items)

    def journal_page(
        self,
        *,
        workspace_id: str,
        filters: JournalFilters,
        sort_direction: str = "desc",
        page_size: int = 50,
        as_of_workspace_revision: int | None = None,
        cursor: str | None = None,
    ) -> JournalEntryPage:
        if sort_direction not in {"asc", "desc"} or not 1 <= page_size <= 200:
            raise ValueError("Journal pagination policy is invalid")
        decoded: _JournalCursor | None = None
        if cursor is not None:
            decoded = self._cursor_codec.decode(cursor)
            if (
                decoded.workspace_id != workspace_id
                or decoded.filters != filters
                or decoded.sort_direction != sort_direction
                or decoded.page_size != page_size
                or (
                    as_of_workspace_revision is not None
                    and decoded.as_of_revision != as_of_workspace_revision
                )
            ):
                raise JournalCursorQueryMismatchError("Cursor query does not match")

        self._connection.execute("BEGIN")
        try:
            authoritative = self._authoritative_revision()
            snapshot = (
                decoded.as_of_revision
                if decoded is not None
                else authoritative
                if as_of_workspace_revision is None
                else min(as_of_workspace_revision, authoritative)
            )
            where, parameters = self._journal_predicates(filters)
            where.insert(0, "workspace_revision <= ?")
            parameters.insert(0, snapshot)
            if decoded is not None:
                operator = ">" if sort_direction == "asc" else "<"
                where.append(
                    f"(workspace_revision {operator} ? OR "
                    f"(workspace_revision = ? AND ordinal {operator} ?))"
                )
                parameters.extend(
                    [decoded.boundary_revision, decoded.boundary_revision, decoded.boundary_ordinal]
                )
            order = "ASC" if sort_direction == "asc" else "DESC"
            rows = self._connection.execute(
                f"""
                SELECT journal_entry_id, workspace_revision, ordinal, event_type,
                       object_type, object_id, payload_json
                FROM journal_entries
                WHERE {" AND ".join(where)}
                ORDER BY workspace_revision {order}, ordinal {order}
                LIMIT ?
                """,
                (*parameters, page_size + 1),
            ).fetchall()
            has_more = len(rows) > page_size
            visible_rows = rows[:page_size]
            items = tuple(self._journal_item(row) for row in visible_rows)
            next_cursor = None
            if has_more and visible_rows:
                boundary = visible_rows[-1]
                next_cursor = self._cursor_codec.encode(
                    _JournalCursor(
                        workspace_id,
                        snapshot,
                        int(boundary["workspace_revision"]),
                        int(boundary["ordinal"]),
                        page_size,
                        sort_direction,
                        filters,
                    )
                )
            newer_where, newer_parameters = self._journal_predicates(filters)
            newer_where.insert(0, "workspace_revision > ?")
            newer_parameters.insert(0, snapshot)
            newer_matching = self._connection.execute(
                f"SELECT EXISTS(SELECT 1 FROM journal_entries WHERE {' AND '.join(newer_where)})",
                newer_parameters,
            ).fetchone()[0]
            self._connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return JournalEntryPage(
            authoritative_revision=WorkspaceRevision(authoritative),
            as_of_workspace_revision=WorkspaceRevision(snapshot),
            items=items,
            next_cursor=next_cursor,
            page_size=page_size,
            sort_direction=sort_direction,
            workspace_advanced=authoritative > snapshot,
            newer_matching_entries_available=bool(newer_matching),
        )

    def _authoritative_revision(self) -> int:
        return int(
            self._connection.execute(
                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
            ).fetchone()[0]
        )

    def _require_path(self, research_path_id: ResearchPathId) -> None:
        if (
            self._connection.execute(
                "SELECT 1 FROM research_paths WHERE research_path_id = ?",
                (research_path_id.value,),
            ).fetchone()
            is None
        ):
            raise ValueError("Research path does not exist")

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.execute("ROLLBACK")

    @staticmethod
    def _journal_predicates(filters: JournalFilters) -> tuple[list[str], list[Any]]:
        where = ["1 = 1"]
        parameters: list[Any] = []
        direct = {
            "event_type": filters.event_type,
            "object_type": filters.object_type,
            "object_id": filters.object_id,
        }
        for column, value in direct.items():
            if value is not None:
                where.append(f"{column} = ?")
                parameters.append(value)
        payload = {
            "turn_id": filters.turn_id,
            "operation_id": filters.operation_id,
            "attempt_id": filters.attempt_id,
            "actor_type": filters.actor_type,
        }
        for key, value in payload.items():
            if value is not None:
                where.append(f"json_extract(payload_json, '$.{key}') = ?")
                parameters.append(value)
        return where, parameters

    @staticmethod
    def _journal_item(row: sqlite3.Row) -> JournalEntryItem:
        event_type = str(row["event_type"])
        object_type = str(row["object_type"])
        object_id = str(row["object_id"])
        return JournalEntryItem(
            journal_entry_id=str(row["journal_entry_id"]),
            workspace_revision=WorkspaceRevision(int(row["workspace_revision"])),
            ordinal=int(row["ordinal"]),
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            summary=f"{event_type} · {object_type} {object_id}",
            payload_json=str(row["payload_json"]),
        )
