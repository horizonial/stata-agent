"""SQLite reader for authoritative candidates used by the Step Context Compiler."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from typing import Any

from stata_research_agent.application.context_compiler import ContextSourceCandidate
from stata_research_agent.application.knowledge_retrieval import (
    CorpusRole,
    infer_retrieval_intent,
)
from stata_research_agent.application.model_gateway import ContextItemCandidate
from stata_research_agent.domain.identifiers import TurnId

from .knowledge_store import SqliteKnowledgeRepository


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _agent_tool_result_payload(payload: object) -> object:
    """Project verbose Stata receipts into lossless-for-action Agent context.

    The canonical Tool Result remains untouched in SQLite.  For the model, the selectable
    source-key/value catalog is sufficient; locator graphs, covariance provenance, and the
    duplicate top-level coefficient view remain available through Trace and lineage APIs.
    """

    if not isinstance(payload, dict):
        return payload
    structured = payload.get("structured")
    if not isinstance(structured, dict):
        return payload
    catalog = structured.get("result_catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("elements"), list):
        return payload
    elements: list[dict[str, object]] = []
    for raw in catalog["elements"]:
        if not isinstance(raw, dict):
            continue
        elements.append(
            {key: raw[key] for key in ("source_key", "statistic_kind", "value") if key in raw}
        )
    compact_structured = {
        key: structured[key]
        for key in (
            "cmd",
            "cmdline",
            "N",
            "r2",
            "r2_p",
            "depvar",
            "vce",
            "vcetype",
            "provenance",
        )
        if key in structured
    }
    compact_structured["result_catalog"] = {
        "schema_version": catalog.get("schema_version"),
        "elements": elements,
    }
    capability = structured.get("result_source_capability")
    if isinstance(capability, dict):
        compact_structured["result_source_capability"] = {
            key: capability[key]
            for key in (
                "capability_id",
                "capability_version",
                "snapshot_schema_version",
            )
            if key in capability
        }
    compact = {
        key: payload[key]
        for key in (
            "completion_manifest_id",
            "execution_status",
            "operation_attempt_id",
            "operation_id",
            "raw_output_excerpt",
        )
        if key in payload
    }
    compact["structured"] = compact_structured
    compact["context_projection"] = {
        "kind": "stata_result_catalog",
        "canonical_payload_preserved": True,
        "omitted_redundant_views": [
            "coefs",
            "scalars",
            "stored_result_source_map",
            "locator_graphs",
        ],
    }
    return compact


def _loaded_skill_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    required = ("name", "revision", "content_sha256", "source_kind", "content")
    if not all(isinstance(payload.get(key), str) and str(payload[key]).strip() for key in required):
        return None
    return {key: payload[key] for key in required}


class SqliteContextAuthorityReader:
    """Read current authority without mutating it or manufacturing research facts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def collect(self, turn_id: TurnId) -> tuple[ContextSourceCandidate, ...]:
        turn = self._connection.execute(
            """
            SELECT turn.turn_id, turn.conversation_id, turn.triggering_message_id,
                   turn.research_path_id, turn.completion_contract_revision_id,
                   turn.created_revision, turn.enqueue_ordinal, turn.turn_revision
            FROM turns AS turn WHERE turn.turn_id = ?
            """,
            (turn_id.value,),
        ).fetchone()
        if turn is None:
            raise ValueError("Turn does not exist")

        candidates: list[ContextSourceCandidate] = []
        candidates.extend(self._conversation_candidates(turn))
        candidates.extend(self._completion_contract_candidates(turn))
        candidates.extend(self._research_state_candidates(turn))
        candidates.extend(self._memory_candidates(turn))
        candidates.extend(self._knowledge_candidates(turn))
        candidates.extend(self._tool_interaction_candidates(turn))
        return tuple(candidates)

    def _conversation_candidates(self, turn: sqlite3.Row) -> Iterable[ContextSourceCandidate]:
        rows = self._connection.execute(
            """
            SELECT message.message_id, message.content, message.ordinal,
                   message.created_revision,
                   CASE WHEN request.turn_id IS NULL THEN 0 ELSE 1 END AS is_waiting_answer
            FROM messages AS message
            LEFT JOIN waiting_answers AS answer ON answer.message_id = message.message_id
            LEFT JOIN waiting_requests AS request
              ON request.waiting_request_id = answer.waiting_request_id
             AND request.turn_id = ?
            WHERE message.conversation_id = ?
              AND (
                    message.created_revision <= ?
                    OR request.turn_id = ?
                  )
            ORDER BY message.ordinal
            """,
            (
                str(turn["turn_id"]),
                str(turn["conversation_id"]),
                int(turn["created_revision"]),
                str(turn["turn_id"]),
            ),
        ).fetchall()
        total = len(rows)
        for index, row in enumerate(rows):
            triggering = str(row["message_id"]) == str(turn["triggering_message_id"])
            waiting_answer = bool(row["is_waiting_answer"])
            mandatory = triggering or waiting_answer
            age = total - index - 1
            priority = 0 if mandatory else 1 if age < 12 else 2
            yield ContextSourceCandidate(
                ContextItemCandidate(
                    "waiting_answer" if waiting_answer else "user_message",
                    "message",
                    str(row["message_id"]),
                    str(row["created_revision"]),
                    "remote_allowed",
                    str(row["content"]),
                ),
                priority,
                "waiting_answer"
                if waiting_answer
                else "triggering_message"
                if triggering
                else "conversation_history",
                mandatory=mandatory,
                summarizable=not mandatory,
                recency=int(row["ordinal"]),
            )

        outputs = self._connection.execute(
            """
            SELECT output.assistant_output_id, output.output_json, output.created_revision,
                   step.step_ordinal, step.turn_id, owner.enqueue_ordinal
            FROM assistant_outputs AS output
            JOIN model_invocations AS invocation
              ON invocation.model_invocation_id = output.model_invocation_id
            JOIN steps AS step ON step.step_id = invocation.step_id
            JOIN turns AS owner ON owner.turn_id = step.turn_id
            WHERE owner.conversation_id = ? AND owner.enqueue_ordinal <= ?
            ORDER BY output.created_revision, step.step_ordinal
            """,
            (str(turn["conversation_id"]), int(turn["enqueue_ordinal"])),
        ).fetchall()
        total_outputs = len(outputs)
        for index, row in enumerate(outputs):
            payload = json.loads(str(row["output_json"]))
            # Calls are represented below as an atomic Call/Result pair.  Keep the model's
            # prose and plan here so a later Step can continue its own reasoning trajectory.
            payload.pop("tool_calls", None)
            current_turn = str(row["turn_id"]) == str(turn["turn_id"])
            age = total_outputs - index - 1
            priority = 1 if current_turn or age < 8 else 2
            yield ContextSourceCandidate(
                ContextItemCandidate(
                    "assistant_message",
                    "assistant_output",
                    str(row["assistant_output_id"]),
                    str(row["created_revision"]),
                    "remote_allowed",
                    _json(payload),
                ),
                priority,
                "current_turn_continuity" if current_turn else "conversation_history",
                summarizable=True,
                recency=int(row["created_revision"]),
            )

    def _completion_contract_candidates(
        self, turn: sqlite3.Row
    ) -> Iterable[ContextSourceCandidate]:
        profile = self._connection.execute(
            """
            SELECT revision.completion_contract_revision_id, revision.revision_number,
                   revision.contract_kind, revision.normalization_required,
                   profile.goal_summary, profile.contract_readiness
            FROM completion_contract_revisions AS revision
            LEFT JOIN completion_contract_revision_profiles AS profile
              USING (completion_contract_revision_id)
            WHERE revision.completion_contract_revision_id = ?
            """,
            (str(turn["completion_contract_revision_id"]),),
        ).fetchone()
        obligations = self._connection.execute(
            """
            SELECT obligation.completion_obligation_id, obligation.stable_key,
                   obligation.label, obligation.provenance, entry.requirement_level,
                   entry.disposition, entry.acceptance_criterion,
                   COALESCE((
                       SELECT observation.observed_state
                       FROM obligation_state_observations AS observation
                       WHERE observation.completion_contract_revision_id =
                             entry.completion_contract_revision_id
                         AND observation.completion_obligation_id = entry.completion_obligation_id
                       ORDER BY observation.commit_revision DESC LIMIT 1
                   ), 'unsatisfied') AS observed_state
            FROM revision_obligation_entries AS entry
            JOIN completion_obligations AS obligation USING (completion_obligation_id)
            WHERE entry.completion_contract_revision_id = ?
            ORDER BY obligation.stable_key
            """,
            (str(turn["completion_contract_revision_id"]),),
        ).fetchall()
        content = {
            "completion_contract_revision_id": str(turn["completion_contract_revision_id"]),
            "revision_number": None if profile is None else int(profile["revision_number"]),
            "contract_kind": None if profile is None else str(profile["contract_kind"]),
            "normalization_required": None
            if profile is None
            else bool(profile["normalization_required"]),
            "goal_summary": None
            if profile is None or profile["goal_summary"] is None
            else str(profile["goal_summary"]),
            "contract_readiness": None
            if profile is None or profile["contract_readiness"] is None
            else str(profile["contract_readiness"]),
            "obligations": [dict(row) for row in obligations],
        }
        yield self._state_candidate(
            "completion_contract",
            str(turn["completion_contract_revision_id"]),
            str(turn["turn_revision"]),
            content,
        )

    def _research_state_candidates(self, turn: sqlite3.Row) -> Iterable[ContextSourceCandidate]:
        path_id = str(turn["research_path_id"])

        plan = self._connection.execute(
            """
            SELECT adoption.target_plan_revision_id, adoption.pointer_revision,
                   adoption.commit_revision, revision.summary, revision.specification_json
            FROM path_plan_adoptions AS adoption
            JOIN plan_revisions AS revision
              ON revision.plan_revision_id = adoption.target_plan_revision_id
            WHERE adoption.research_path_id = ?
            """,
            (path_id,),
        ).fetchone()
        if plan is not None:
            yield self._state_candidate(
                "current_plan",
                str(plan["target_plan_revision_id"]),
                str(plan["pointer_revision"]),
                {
                    "plan_revision_id": str(plan["target_plan_revision_id"]),
                    "pointer_revision": int(plan["pointer_revision"]),
                    "summary": str(plan["summary"]),
                    "specification": json.loads(str(plan["specification_json"])),
                },
            )

        data_rows = self._connection.execute(
            """
            SELECT slot.canonical_key, slot.display_name, adoption.target_data_version_id,
                   adoption.pointer_revision, version.data_version_kind,
                   version.observation_count, version.variable_count,
                   version.schema_snapshot_json, version.stata_data_signature
            FROM path_data_slots AS slot
            JOIN path_data_adoptions AS adoption USING (path_data_slot_id)
            JOIN data_versions AS version
              ON version.data_version_id = adoption.target_data_version_id
            WHERE slot.research_path_id = ? AND slot.lifecycle = 'active'
            ORDER BY slot.canonical_key
            """,
            (path_id,),
        ).fetchall()
        if data_rows:
            yield self._state_candidate(
                "current_data",
                path_id,
                ":".join(str(row["pointer_revision"]) for row in data_rows),
                {
                    "slots": [
                        {
                            **{
                                key: row[key]
                                for key in (
                                    "canonical_key",
                                    "display_name",
                                    "target_data_version_id",
                                    "pointer_revision",
                                    "data_version_kind",
                                    "observation_count",
                                    "variable_count",
                                    "stata_data_signature",
                                )
                            },
                            "schema": json.loads(str(row["schema_snapshot_json"])),
                        }
                        for row in data_rows
                    ]
                },
            )

        result_rows = self._connection.execute(
            """
            SELECT slot.result_slot_id, slot.canonical_key, slot.display_name,
                   adoption.target_result_id, adoption.pointer_revision,
                   result.result_kind, result.producing_stata_run_id
            FROM result_slots AS slot
            JOIN path_result_adoptions AS adoption USING (result_slot_id)
            JOIN results AS result ON result.result_id = adoption.target_result_id
            WHERE slot.research_path_id = ? AND slot.lifecycle = 'active'
            ORDER BY slot.canonical_key
            """,
            (path_id,),
        ).fetchall()
        if result_rows:
            results: list[dict[str, Any]] = []
            for row in result_rows:
                elements = self._connection.execute(
                    """
                    SELECT result_element_id, semantic_key, statistic_kind, value_kind,
                           canonical_decimal_text, stata_missing_code, unit, scale,
                           estimate_status, authority
                    FROM result_elements WHERE result_id = ? ORDER BY semantic_key
                    LIMIT 500
                    """,
                    (str(row["target_result_id"]),),
                ).fetchall()
                results.append({**dict(row), "elements": [dict(item) for item in elements]})
            yield self._state_candidate(
                "current_results",
                path_id,
                ":".join(str(row["pointer_revision"]) for row in result_rows),
                {"slots": results},
            )

        document_rows = self._connection.execute(
            """
            SELECT slot.canonical_key, adoption.target_document_revision_id,
                   adoption.pointer_revision, adoption.delivery_gate_report_id,
                   revision.origin_kind, revision.docx_artifact_id,
                   revision.manifest_artifact_id
            FROM document_slots AS slot
            JOIN path_document_adoptions AS adoption USING (document_slot_id)
            JOIN document_revisions AS revision
              ON revision.document_revision_id = adoption.target_document_revision_id
            WHERE slot.research_path_id = ? AND slot.lifecycle = 'active'
            ORDER BY slot.canonical_key
            """,
            (path_id,),
        ).fetchall()
        if document_rows:
            yield self._state_candidate(
                "current_documents",
                path_id,
                ":".join(str(row["pointer_revision"]) for row in document_rows),
                {"slots": [dict(row) for row in document_rows]},
            )

        analysis_rows = self._connection.execute(
            """
            SELECT adoption.analysis_output_adoption_id, adoption.analysis_output_id,
                   adoption.confirmation_summary, adoption.created_revision,
                   output.method_summary, classification.output_kind
            FROM analysis_output_adoptions AS adoption
            JOIN analysis_outputs AS output USING (analysis_output_id)
            JOIN analysis_output_classifications AS classification USING (analysis_output_id)
            ORDER BY adoption.created_revision DESC LIMIT 20
            """
        ).fetchall()
        if analysis_rows:
            yield ContextSourceCandidate(
                ContextItemCandidate(
                    "adopted_analysis_outputs",
                    "research_path",
                    path_id,
                    str(max(int(row["created_revision"]) for row in analysis_rows)),
                    "remote_allowed",
                    _json({"outputs": [dict(row) for row in analysis_rows]}),
                ),
                1,
                "current_research_state",
                summarizable=True,
            )

    def _tool_interaction_candidates(self, turn: sqlite3.Row) -> Iterable[ContextSourceCandidate]:
        rows = self._connection.execute(
            """
            SELECT call.tool_call_id, call.call_ordinal, call.requested_tool_name,
                   call.proposal_status, call.created_revision,
                   canonical.arguments_json,
                   result.tool_result_id, result.result_kind, result.summary,
                   result.structured_payload_json, result.artifact_references_json,
                   result.is_truncated, result.full_payload_artifact_id,
                   result.created_revision AS result_revision
            FROM tool_calls AS call
            JOIN assistant_outputs AS output
              ON output.assistant_output_id = call.assistant_output_id
            JOIN model_invocations AS invocation
              ON invocation.model_invocation_id = output.model_invocation_id
            JOIN steps AS step ON step.step_id = invocation.step_id
            LEFT JOIN canonical_tool_argument_snapshots AS canonical
              ON canonical.canonical_arguments_snapshot_id = call.canonical_arguments_snapshot_id
            LEFT JOIN canonical_tool_results AS result USING (tool_call_id)
            WHERE step.turn_id = ?
            ORDER BY call.created_revision, call.call_ordinal
            """,
            (str(turn["turn_id"]),),
        ).fetchall()
        total = len(rows)
        for index, row in enumerate(rows):
            age = total - index - 1
            mandatory = age < 4
            priority = 0 if mandatory else 1 if age < 16 else 2
            group = f"tool:{row['tool_call_id']}"
            recency = int(row["created_revision"])
            call_content = {
                "tool_call_id": str(row["tool_call_id"]),
                "name": str(row["requested_tool_name"]),
                "status": str(row["proposal_status"]),
                "arguments": None
                if row["arguments_json"] is None
                else json.loads(str(row["arguments_json"])),
            }
            yield ContextSourceCandidate(
                ContextItemCandidate(
                    "tool_call",
                    "tool_call",
                    str(row["tool_call_id"]),
                    str(row["created_revision"]),
                    "remote_allowed",
                    _json(call_content),
                ),
                priority,
                "recent_tool_interaction",
                mandatory=mandatory,
                summarizable=not mandatory,
                group_key=group,
                recency=recency,
            )
            if row["tool_result_id"] is not None:
                canonical_payload = (
                    None
                    if row["structured_payload_json"] is None
                    else json.loads(str(row["structured_payload_json"]))
                )
                projected_payload = _agent_tool_result_payload(canonical_payload)
                loaded_skill = None
                if (
                    str(row["requested_tool_name"]) == "research.load_skill"
                    and str(row["result_kind"]) == "success"
                ):
                    loaded_skill = _loaded_skill_payload(canonical_payload)
                    if isinstance(projected_payload, dict):
                        projected_payload = {
                            key: value
                            for key, value in projected_payload.items()
                            if key != "content"
                        }
                result_content = {
                    "tool_result_id": str(row["tool_result_id"]),
                    "kind": str(row["result_kind"]),
                    "summary": str(row["summary"]),
                    "payload": projected_payload,
                    "artifacts": json.loads(str(row["artifact_references_json"])),
                    "is_truncated": bool(row["is_truncated"]),
                    "full_payload_artifact_id": row["full_payload_artifact_id"],
                }
                yield ContextSourceCandidate(
                    ContextItemCandidate(
                        "tool_result",
                        "canonical_tool_result",
                        str(row["tool_result_id"]),
                        str(row["result_revision"]),
                        "remote_allowed",
                        _json(result_content),
                        "tool_output_untrusted",
                    ),
                    priority,
                    "recent_tool_interaction",
                    mandatory=mandatory,
                    summarizable=not mandatory,
                    group_key=group,
                    recency=recency,
                )
                if loaded_skill is not None:
                    skill_name = str(loaded_skill["name"])
                    content_sha256 = str(loaded_skill["content_sha256"])
                    version = self._connection.execute(
                        """
                        SELECT version.skill_version_id,
                               adoption.current_skill_version_id, adoption.lifecycle
                        FROM skill_versions AS version
                        LEFT JOIN skill_adoptions AS adoption USING (skill_name)
                        WHERE version.skill_name = ? AND version.content_sha256 = ?
                        """,
                        (skill_name, content_sha256),
                    ).fetchone()
                    if version is None or (
                        str(version["lifecycle"]) == "active"
                        and str(version["current_skill_version_id"])
                        == str(version["skill_version_id"])
                    ):
                        source_id = (
                            f"external:{skill_name}:{content_sha256}"
                            if version is None
                            else str(version["skill_version_id"])
                        )
                        yield ContextSourceCandidate(
                            ContextItemCandidate(
                                "specialized_skill",
                                "skill_revision",
                                source_id,
                                str(loaded_skill["revision"]),
                                "remote_allowed",
                                _json(loaded_skill),
                            ),
                            priority,
                            "explicit_skill_load",
                            mandatory=mandatory,
                            summarizable=False,
                            group_key=group,
                            recency=recency,
                        )
                if str(row["requested_tool_name"]) in {"memory.search", "memory.open"}:
                    for hit in self._memory_tool_hits(canonical_payload):
                        revision_id = str(hit["memory_revision_id"])
                        created_revision = int(str(hit["created_revision"]))
                        opened = str(row["requested_tool_name"]) == "memory.open"
                        yield ContextSourceCandidate(
                            ContextItemCandidate(
                                "memory_open" if opened else "memory_search_hit",
                                "memory_revision",
                                revision_id,
                                str(created_revision),
                                "remote_allowed",
                                _json(
                                    {
                                        "memory_item_id": str(hit["memory_item_id"]),
                                        "memory_revision_id": revision_id,
                                        "scope_kind": str(hit["scope_kind"]),
                                        "memory_kind": str(hit["memory_kind"]),
                                        "title": str(hit["title"]),
                                        "content": hit.get("content") if opened else None,
                                        "excerpt": str(hit.get("excerpt", "")),
                                        "access_tier": str(hit["access_tier"]),
                                    }
                                ),
                                "recalled_context",
                            ),
                            priority,
                            "explicit_memory_recall",
                            mandatory=mandatory,
                            summarizable=not opened,
                            group_key=group,
                            recency=created_revision,
                        )

    def _memory_candidates(self, turn: sqlite3.Row) -> Iterable[ContextSourceCandidate]:
        policy = self._connection.execute(
            """
            SELECT COALESCE(policy.use_memory, 1) AS use_memory
            FROM conversations AS conversation
            LEFT JOIN conversation_memory_policies AS policy USING (conversation_id)
            WHERE conversation.conversation_id = ?
            """,
            (str(turn["conversation_id"]),),
        ).fetchone()
        if policy is None or not bool(policy["use_memory"]):
            return

        workspace_id = str(
            self._connection.execute(
                "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
            ).fetchone()[0]
        )
        for scope_kind, scope_object_id in (
            ("workspace", workspace_id),
            ("research_path", str(turn["research_path_id"])),
        ):
            summary = self._connection.execute(
                """
                SELECT summary_text, source_revision, projection_revision
                FROM memory_summary_projections
                WHERE scope_kind = ? AND scope_object_id = ?
                """,
                (scope_kind, scope_object_id),
            ).fetchone()
            if summary is not None:
                yield ContextSourceCandidate(
                    ContextItemCandidate(
                        "project_memory_index",
                        "memory_summary_projection",
                        f"{scope_kind}:{scope_object_id}",
                        f"{summary['source_revision']}:{summary['projection_revision']}",
                        "remote_allowed",
                        str(summary["summary_text"]),
                        "recalled_context",
                    ),
                    1,
                    "project_memory_navigation_index",
                    summarizable=False,
                    recency=int(summary["source_revision"]),
                )

        trigger = self._connection.execute(
            "SELECT content FROM messages WHERE message_id = ?",
            (str(turn["triggering_message_id"]),),
        ).fetchone()
        query_tokens = self._memory_tokens("" if trigger is None else str(trigger["content"]))
        rows = self._connection.execute(
            """
            SELECT item.memory_item_id, item.scope_kind, item.scope_object_id,
                   item.memory_kind, state.current_revision_id, state.pointer_revision,
                   revision.title, revision.content, revision.origin_kind,
                   revision.created_revision, retention.pinned,
                   COALESCE(MAX(use.created_revision), 0) AS last_used_revision
            FROM memory_items AS item
            JOIN memory_current_states AS state USING (memory_item_id)
            JOIN memory_retention_states AS retention USING (memory_item_id)
            JOIN memory_revisions AS revision
              ON revision.memory_revision_id = state.current_revision_id
            LEFT JOIN memory_context_uses AS use USING (memory_item_id)
            WHERE state.lifecycle = 'active'
              AND retention.access_tier = 'hot'
              AND retention.superseded_by_memory_item_id IS NULL
              AND (
                    (item.scope_kind = 'workspace' AND item.scope_object_id = ?)
                    OR
                    (item.scope_kind = 'research_path' AND item.scope_object_id = ?)
                  )
            GROUP BY item.memory_item_id
            ORDER BY
                CASE item.scope_kind WHEN 'research_path' THEN 0 ELSE 1 END,
                CASE revision.origin_kind
                    WHEN 'explicit_user' THEN 0
                    WHEN 'confirmed' THEN 1
                    ELSE 2
                END,
                last_used_revision DESC,
                revision.created_revision DESC
            LIMIT 64
            """,
            (workspace_id, str(turn["research_path_id"])),
        ).fetchall()
        scored_rows = [
            (
                len(
                    query_tokens.intersection(
                        self._memory_tokens(f"{row['title']} {row['content']}")
                    )
                ),
                row,
            )
            for row in rows
        ]
        ranked_rows = [
            row
            for overlap, row in sorted(
                (item for item in scored_rows if item[0] > 0 or bool(item[1]["pinned"])),
                key=lambda row: (
                    0 if bool(row[1]["pinned"]) else 1,
                    -row[0],
                    0 if str(row[1]["scope_kind"]) == "research_path" else 1,
                    0
                    if str(row[1]["memory_kind"])
                    in {"research_decision", "research_constraint", "unresolved_question"}
                    else 1,
                    -max(
                        int(row[1]["created_revision"]),
                        int(row[1]["last_used_revision"]),
                    ),
                ),
            )[:4]
        ]
        for row in ranked_rows:
            sources = self._connection.execute(
                """
                SELECT source_object_type, source_object_id, source_object_revision, source_role
                FROM memory_revision_sources
                WHERE memory_revision_id = ?
                ORDER BY memory_revision_source_id
                """,
                (str(row["current_revision_id"]),),
            ).fetchall()
            high_relevance = str(row["memory_kind"]) in {
                "research_decision",
                "research_constraint",
                "unresolved_question",
            }
            content = {
                "boundary": (
                    "Recalled project context only; not Evidence, a numeric source, or proof "
                    "of current Research State. Current user input and authoritative state win."
                ),
                "memory_item_id": str(row["memory_item_id"]),
                "memory_revision_id": str(row["current_revision_id"]),
                "scope_kind": str(row["scope_kind"]),
                "scope_object_id": str(row["scope_object_id"]),
                "kind": str(row["memory_kind"]),
                "title": str(row["title"]),
                "content": str(row["content"]),
                "origin": str(row["origin_kind"]),
                "sources": [dict(source) for source in sources],
            }
            yield ContextSourceCandidate(
                ContextItemCandidate(
                    "project_memory",
                    "memory_revision",
                    str(row["current_revision_id"]),
                    str(row["created_revision"]),
                    "remote_allowed",
                    _json(content),
                    "recalled_context",
                ),
                1 if high_relevance else 2,
                "active_project_memory",
                summarizable=False,
                recency=max(int(row["created_revision"]), int(row["last_used_revision"])),
            )

    @staticmethod
    def _memory_tool_hits(payload: object) -> tuple[dict[str, object], ...]:
        if not isinstance(payload, dict):
            return ()
        raw_hits = payload.get("hits")
        if not isinstance(raw_hits, list):
            return ()
        required = {
            "memory_item_id",
            "memory_revision_id",
            "created_revision",
            "scope_kind",
            "memory_kind",
            "title",
            "access_tier",
        }
        return tuple(hit for hit in raw_hits if isinstance(hit, dict) and required.issubset(hit))

    def _knowledge_candidates(self, turn: sqlite3.Row) -> Iterable[ContextSourceCandidate]:
        trigger = self._connection.execute(
            "SELECT content, created_revision FROM messages WHERE message_id = ?",
            (str(turn["triggering_message_id"]),),
        ).fetchone()
        if trigger is None:
            return
        repository = SqliteKnowledgeRepository(self._connection)
        indexed = repository.canonical_catalog(CorpusRole.LITERATURE_EVIDENCE)
        hint = infer_retrieval_intent(str(trigger["content"]), literature_available=bool(indexed))
        yield ContextSourceCandidate(
            ContextItemCandidate(
                "retrieval_intent_hint",
                "retrieval_intent_hint",
                str(turn["triggering_message_id"]),
                hint.policy_revision,
                "remote_allowed",
                _json(
                    {
                        "boundary": (
                            "Soft context-routing hint only. The Agent remains responsible for "
                            "interpreting the request and may search or ignore suggested sources."
                        ),
                        "query": hint.query,
                        "concepts": list(hint.concepts),
                        "suggested_sources": list(hint.suggested_sources),
                        "reason_codes": list(hint.reason_codes),
                    }
                ),
                "retrieved_untrusted",
            ),
            1,
            "soft_retrieval_intent",
            summarizable=False,
            recency=int(trigger["created_revision"]),
        )
        if not indexed:
            return
        yield ContextSourceCandidate(
            ContextItemCandidate(
                "knowledge_catalog",
                "knowledge_catalog",
                "workspace-literature",
                ":".join(item[1][:12] for item in indexed),
                "remote_allowed",
                _json(
                    {
                        "boundary": "Workspace reference catalog; not statistical Evidence.",
                        "documents": [
                            {
                                "relative_path": path,
                                "document_sha256": digest,
                                "page_count": pages,
                            }
                            for path, digest, pages in indexed[:128]
                        ],
                    }
                ),
                "retrieved_untrusted",
            ),
            2,
            "workspace_literature_catalog",
            summarizable=True,
            recency=int(trigger["created_revision"]),
        )
        for hit in repository.search_canonical(
            str(trigger["content"]),
            corpus_roles=(CorpusRole.LITERATURE_EVIDENCE,),
            limit=4,
        ):
            yield ContextSourceCandidate(
                ContextItemCandidate(
                    "knowledge_node",
                    "knowledge_node",
                    hit.node_id,
                    hit.parse_revision_id,
                    "remote_allowed",
                    _json(
                        {
                            "boundary": (
                                "Retrieved Workspace reference text; not statistical Evidence. "
                                "Preserve source/parse revision, node, and locator when citing."
                            ),
                            "source_locator": hit.source_locator,
                            "knowledge_source_revision_id": hit.source_revision_id,
                            "knowledge_parse_revision_id": hit.parse_revision_id,
                            "page_start": hit.page_start,
                            "page_end": hit.page_end,
                            "section_title": hit.section_title,
                            "knowledge_node_id": hit.node_id,
                            "content": hit.content,
                        }
                    ),
                    "retrieved_untrusted",
                ),
                1,
                "soft_literature_prefetch",
                summarizable=False,
                recency=int(trigger["created_revision"]),
            )

    @staticmethod
    def _memory_tokens(value: str) -> set[str]:
        # Unicode word tokens work for identifiers and Latin text; individual Han characters
        # preserve useful overlap without requiring a language-specific tokenizer.
        words = {token.lower() for token in re.findall(r"[\w.-]{2,}", value)}
        han = set(re.findall(r"[\u4e00-\u9fff]", value))
        return words | han

    @staticmethod
    def _state_candidate(
        kind: str, object_id: str, revision: str, content: object
    ) -> ContextSourceCandidate:
        return ContextSourceCandidate(
            ContextItemCandidate(
                "research_state_reference",
                kind,
                object_id,
                revision,
                "remote_allowed",
                _json(content),
            ),
            0,
            "current_research_state",
            mandatory=True,
        )
