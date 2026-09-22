"""SQLite authority for frozen Step contexts and provider dispatch facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import cast

from stata_research_agent.application.model_gateway import (
    FrozenModelInvocation,
    ProviderAttemptIdentity,
    ProviderResponse,
    StartModelStepCommand,
    StepFreezeIdentity,
)
from stata_research_agent.domain.identifiers import (
    AssistantOutputId,
    CommandId,
    ModelInputSnapshotId,
    ModelInvocationId,
    ProviderAttemptId,
    ResearchPathId,
    StepId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _successful_knowledge_tool_payload(
    result_kind: str,
    structured_payload_json: str,
) -> dict[str, object] | None:
    """Return retrieval provenance only for a successful Knowledge Tool Result.

    Error Results are ordinary model feedback and intentionally do not create
    Knowledge Node context-use rows.  Their error payload follows a different
    schema, so validating it as a successful retrieval would turn a recoverable
    bad tool argument into a fatal Step-freeze error.
    """
    if result_kind != "success":
        return None
    payload = json.loads(structured_payload_json)
    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
        raise ValueError("Knowledge Tool Result payload is malformed")
    return payload


class SqliteModelGatewayRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def freeze_step(
        self,
        command: StartModelStepCommand,
        identities: StepFreezeIdentity,
        normalized_input_json: str,
        normalized_input_sha256: str,
    ) -> FrozenModelInvocation:
        request = {
            "turn_id": command.turn_id.value,
            "expected_turn_revision": command.expected_turn_revision,
            "system_prompt_revision": command.system_prompt_revision,
            "main_skill_name": command.main_skill_name,
            "main_skill_revision": command.main_skill_revision,
            "tool_catalog_revision": command.tool_catalog_revision,
            "model_policy_revision": command.model_policy_revision,
            "normalized_input_sha256": normalized_input_sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            turn = connection.execute(
                """
                SELECT research_path_id, completion_contract_revision_id, turn_revision, status
                FROM turns WHERE turn_id = ?
                """,
                (command.turn_id.value,),
            ).fetchone()
            if turn is None:
                raise ValueError("Turn does not exist")
            if str(turn["status"]) != "running":
                raise ValueError("a model Step can start only while its Turn is running")
            if int(turn["turn_revision"]) != command.expected_turn_revision:
                raise ValueError("Turn revision changed before Context Manifest freeze")
            research_path_id = str(turn["research_path_id"])
            completion_revision_id = str(turn["completion_contract_revision_id"])
            main_skill_hash = _sha256(command.main_skill_content)

            memory_uses: list[tuple[str, str, str, str]] = []
            knowledge_uses: list[tuple[str, str, str]] = []
            knowledge_node_uses: list[tuple[str, str, str, str, str | None, str | None, str]] = []
            skill_uses: list[
                tuple[str, str, str, str, str, str | None, str]
            ] = []
            for context_item_id, item in zip(
                identities.context_item_ids, command.context_items, strict=True
            ):
                if item.source_object_type == "memory_summary_projection":
                    try:
                        scope_kind, scope_object_id = item.source_object_id.split(":", 1)
                        source_revision, projection_revision = item.source_revision.split(":", 1)
                    except ValueError as error:
                        raise ValueError("Memory summary identity is malformed") from error
                    summary = connection.execute(
                        """
                        SELECT source_revision, projection_revision
                        FROM memory_summary_projections
                        WHERE scope_kind = ? AND scope_object_id = ?
                        """,
                        (scope_kind, scope_object_id),
                    ).fetchone()
                    if (
                        summary is None
                        or str(summary["source_revision"]) != source_revision
                        or str(summary["projection_revision"]) != projection_revision
                        or (scope_kind == "research_path" and scope_object_id != research_path_id)
                    ):
                        raise ValueError(
                            "selected Memory summary is stale or out of scope; recompile context"
                        )
                    continue
                if item.source_object_type == "knowledge_chunk":
                    chunk = connection.execute(
                        """
                        SELECT chunk.knowledge_chunk_id,
                               chunk.knowledge_document_revision_id,
                               state.current_revision_id, state.availability
                        FROM knowledge_chunks AS chunk
                        JOIN knowledge_document_revisions AS revision
                          USING (knowledge_document_revision_id)
                        JOIN knowledge_document_states AS state
                          USING (knowledge_document_id)
                        WHERE chunk.knowledge_chunk_id = ?
                        """,
                        (item.source_object_id,),
                    ).fetchone()
                    if (
                        chunk is None
                        or str(chunk["availability"]) != "indexed"
                        or str(chunk["knowledge_document_revision_id"]) != item.source_revision
                        or str(chunk["current_revision_id"]) != item.source_revision
                    ):
                        raise ValueError(
                            "selected Knowledge Chunk is stale or unavailable; recompile context"
                        )
                    knowledge_uses.append(
                        (
                            context_item_id.value,
                            str(chunk["knowledge_chunk_id"]),
                            str(chunk["knowledge_document_revision_id"]),
                        )
                    )
                    continue
                if item.source_object_type == "knowledge_node":
                    node = connection.execute(
                        """
                        SELECT node.knowledge_node_id, parse.knowledge_parse_revision_id,
                               parse.knowledge_source_revision_id,
                               state.current_source_revision_id,
                               state.current_parse_revision_id, state.availability
                        FROM knowledge_nodes AS node
                        JOIN knowledge_parse_revisions AS parse
                          USING (knowledge_parse_revision_id)
                        JOIN knowledge_source_revisions AS source_revision
                          USING (knowledge_source_revision_id)
                        JOIN knowledge_source_states AS state USING (knowledge_source_id)
                        WHERE node.knowledge_node_id = ?
                        """,
                        (item.source_object_id,),
                    ).fetchone()
                    if (
                        node is None
                        or str(node["availability"]) != "indexed"
                        or str(node["knowledge_parse_revision_id"]) != item.source_revision
                        or str(node["current_parse_revision_id"]) != item.source_revision
                        or str(node["current_source_revision_id"])
                        != str(node["knowledge_source_revision_id"])
                    ):
                        raise ValueError(
                            "selected Knowledge Node is stale or unavailable; recompile context"
                        )
                    knowledge_node_uses.append(
                        (
                            context_item_id.value,
                            str(node["knowledge_node_id"]),
                            str(node["knowledge_source_revision_id"]),
                            str(node["knowledge_parse_revision_id"]),
                            None,
                            None,
                            "soft_prefetch",
                        )
                    )
                    continue
                if item.source_object_type == "skill_revision":
                    payload = json.loads(item.content)
                    if not isinstance(payload, dict):
                        raise ValueError("Skill Revision context payload is malformed")
                    skill_name = str(payload.get("name", ""))
                    skill_revision = str(payload.get("revision", ""))
                    content_sha256 = str(payload.get("content_sha256", ""))
                    source_kind = str(payload.get("source_kind", ""))
                    skill_markdown = str(payload.get("content", ""))
                    if (
                        not skill_name
                        or not skill_revision
                        or not source_kind
                        or len(content_sha256) != 64
                        or _sha256(skill_markdown) != content_sha256
                        or item.source_revision != skill_revision
                    ):
                        raise ValueError("Skill Revision context identity is malformed")
                    skill_version_id: str | None = None
                    if item.source_object_id.startswith("skillversion_"):
                        version = connection.execute(
                            """
                            SELECT version.skill_version_id, version.skill_name,
                                   version.content_sha256, adoption.lifecycle,
                                   adoption.current_skill_version_id
                            FROM skill_versions AS version
                            JOIN skill_adoptions AS adoption USING (skill_name)
                            WHERE version.skill_version_id = ?
                            """,
                            (item.source_object_id,),
                        ).fetchone()
                        if (
                            version is None
                            or str(version["skill_name"]) != skill_name
                            or str(version["content_sha256"]) != content_sha256
                            or str(version["lifecycle"]) != "active"
                            or str(version["current_skill_version_id"])
                            != item.source_object_id
                        ):
                            raise ValueError(
                                "selected evolved Skill is no longer the active version; "
                                "recompile context"
                            )
                        skill_version_id = item.source_object_id
                    elif item.source_object_id != f"external:{skill_name}:{content_sha256}":
                        raise ValueError("external Skill Revision identity is malformed")
                    skill_uses.append(
                        (
                            context_item_id.value,
                            skill_name,
                            skill_revision,
                            content_sha256,
                            source_kind,
                            skill_version_id,
                            "exact",
                        )
                    )
                    continue
                if item.source_object_type == "canonical_tool_result":
                    result = connection.execute(
                        """
                        SELECT result.structured_payload_json, call.requested_tool_name,
                               result.result_kind
                        FROM canonical_tool_results AS result
                        JOIN tool_calls AS call USING (tool_call_id)
                        WHERE result.tool_result_id = ?
                        """,
                        (item.source_object_id,),
                    ).fetchone()
                    knowledge_tools = {
                        "research.search_knowledge": "explicit_retrieval",
                        "stata.which_command": "help",
                        "stata.lookup_help": "help",
                        "stata.grep_help": "help",
                        "stata.explain_run_error": "help",
                        "writing.retrieve_style_exemplars": "style_exemplar",
                        "research.read_knowledge_nodes": "explicit_retrieval",
                        "research.expand_knowledge": "explicit_retrieval",
                        "research.build_evidence_packet": "explicit_retrieval",
                    }
                    if result is not None and str(result["requested_tool_name"]) in knowledge_tools:
                        payload = _successful_knowledge_tool_payload(
                            str(result["result_kind"]),
                            str(result["structured_payload_json"]),
                        )
                        if payload is None:
                            continue
                        session_id = payload.get("retrieval_session_id")
                        hop_id = payload.get("retrieval_hop_id")
                        for raw_hit in cast(list[object], payload["hits"]):
                            if not isinstance(raw_hit, dict):
                                raise ValueError("Knowledge Tool Result hit is malformed")
                            node_id = str(raw_hit.get("knowledge_node_id", ""))
                            source_revision_id = str(
                                raw_hit.get("knowledge_source_revision_id", "")
                            )
                            parse_revision_id = str(raw_hit.get("knowledge_parse_revision_id", ""))
                            exists = connection.execute(
                                """
                                SELECT 1 FROM knowledge_nodes AS node
                                JOIN knowledge_parse_revisions AS parse
                                  USING (knowledge_parse_revision_id)
                                WHERE node.knowledge_node_id = ?
                                  AND parse.knowledge_parse_revision_id = ?
                                  AND parse.knowledge_source_revision_id = ?
                                """,
                                (node_id, parse_revision_id, source_revision_id),
                            ).fetchone()
                            if exists is None:
                                raise ValueError(
                                    "Knowledge Tool Result references an unknown source node"
                                )
                            knowledge_node_uses.append(
                                (
                                    context_item_id.value,
                                    node_id,
                                    source_revision_id,
                                    parse_revision_id,
                                    None if session_id is None else str(session_id),
                                    None if hop_id is None else str(hop_id),
                                    knowledge_tools[str(result["requested_tool_name"])],
                                )
                            )
                    continue
                if item.source_object_type != "memory_revision":
                    continue
                memory = connection.execute(
                    """
                    SELECT state.memory_item_id, state.current_revision_id, state.lifecycle,
                           revision.created_revision, item.scope_kind, item.scope_object_id,
                           retention.access_tier,
                           retention.superseded_by_memory_item_id
                    FROM memory_current_states AS state
                    JOIN memory_revisions AS revision
                      ON revision.memory_revision_id = state.current_revision_id
                    JOIN memory_items AS item USING (memory_item_id)
                    JOIN memory_retention_states AS retention USING (memory_item_id)
                    WHERE state.current_revision_id = ?
                    """,
                    (item.source_object_id,),
                ).fetchone()
                if (
                    memory is None
                    or str(memory["lifecycle"]) != "active"
                    or (
                        str(memory["access_tier"]) == "archived" and item.item_kind != "memory_open"
                    )
                    or (
                        memory["superseded_by_memory_item_id"] is not None
                        and item.item_kind != "memory_open"
                    )
                    or str(memory["current_revision_id"]) != item.source_object_id
                    or str(memory["created_revision"]) != item.source_revision
                    or (
                        str(memory["scope_kind"]) == "research_path"
                        and str(memory["scope_object_id"]) != research_path_id
                    )
                ):
                    raise ValueError(
                        "selected Memory Revision is no longer current and active; "
                        "recompile context"
                    )
                memory_uses.append(
                    (
                        context_item_id.value,
                        str(memory["memory_item_id"]),
                        str(memory["current_revision_id"]),
                        "index" if item.item_kind == "memory_search_hit" else "exact",
                    )
                )

            budget = connection.execute(
                """
                SELECT account.used_steps, account.used_tool_admissions,
                       account.used_provider_attempts, account.account_revision,
                       policy.max_steps, policy.max_tool_admissions,
                       policy.max_provider_attempts_per_invocation,
                       policy.max_provider_attempts_per_turn,
                       policy.max_same_failure_fingerprint, policy.policy_revision
                FROM turn_budget_accounts AS account
                JOIN budget_policy_snapshots AS policy
                  ON policy.budget_policy_snapshot_id = account.budget_policy_snapshot_id
                WHERE account.turn_id = ?
                """,
                (command.turn_id.value,),
            ).fetchone()
            if budget is None:
                connection.execute(
                    "INSERT INTO budget_policy_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identities.budget_policy_snapshot_id.value,
                        command.budget_policy_revision,
                        command.remaining_step_budget,
                        command.remaining_tool_budget,
                        command.max_provider_attempts_per_invocation,
                        command.max_provider_attempts_per_turn,
                        command.max_same_failure_fingerprint,
                        revision.value,
                    ),
                )
                used_steps = 0
                used_tools = 0
                max_steps = command.remaining_step_budget
                max_tools = command.remaining_tool_budget
                connection.execute(
                    "INSERT INTO turn_budget_accounts VALUES (?, ?, 0, 0, 0, 1, ?)",
                    (
                        command.turn_id.value,
                        identities.budget_policy_snapshot_id.value,
                        revision.value,
                    ),
                )
            else:
                expected_policy = (
                    command.budget_policy_revision,
                    command.remaining_step_budget,
                    command.remaining_tool_budget,
                    command.max_provider_attempts_per_invocation,
                    command.max_provider_attempts_per_turn,
                    command.max_same_failure_fingerprint,
                )
                actual_policy = (
                    str(budget["policy_revision"]),
                    int(budget["max_steps"]),
                    int(budget["max_tool_admissions"]),
                    int(budget["max_provider_attempts_per_invocation"]),
                    int(budget["max_provider_attempts_per_turn"]),
                    int(budget["max_same_failure_fingerprint"]),
                )
                if actual_policy != expected_policy:
                    raise ValueError("Turn Budget Policy is immutable")
                used_steps = int(budget["used_steps"])
                used_tools = int(budget["used_tool_admissions"])
                max_steps = int(budget["max_steps"])
                max_tools = int(budget["max_tool_admissions"])
                if (
                    command.current_remaining_step_budget is not None
                    and command.current_remaining_step_budget != max_steps - used_steps
                ):
                    raise ValueError("Turn Step budget snapshot is stale")
                if (
                    command.current_remaining_tool_budget is not None
                    and command.current_remaining_tool_budget != max_tools - used_tools
                ):
                    raise ValueError("Turn Tool budget snapshot is stale")
            if used_steps >= max_steps:
                raise ValueError("Turn Step budget is exhausted")
            connection.execute(
                """
                UPDATE turn_budget_accounts
                SET used_steps = used_steps + 1,
                    account_revision = account_revision + 1,
                    updated_revision = ?
                WHERE turn_id = ?
                """,
                (revision.value, command.turn_id.value),
            )
            connection.execute(
                """
                INSERT INTO turn_budget_usage_history
                VALUES (?, ?, 'step', 1, NULL, ?)
                """,
                (
                    identities.step_budget_usage_id.value,
                    command.turn_id.value,
                    revision.value,
                ),
            )

            baseline = connection.execute(
                """
                SELECT turn_context_baseline_id, completion_contract_revision_id,
                       system_prompt_revision, main_skill_name, main_skill_revision,
                       main_skill_content_sha256, tool_catalog_revision, model_policy_revision
                FROM turn_context_baselines WHERE turn_id = ?
                """,
                (command.turn_id.value,),
            ).fetchone()
            baseline_values = (
                completion_revision_id,
                command.system_prompt_revision,
                command.main_skill_name,
                command.main_skill_revision,
                main_skill_hash,
                command.tool_catalog_revision,
                command.model_policy_revision,
            )
            if baseline is None:
                baseline_id = identities.baseline_id.value
                connection.execute(
                    """
                    INSERT INTO turn_context_baselines(
                        turn_context_baseline_id, turn_id,
                        completion_contract_revision_id, system_prompt_revision,
                        main_skill_name, main_skill_revision, main_skill_content_sha256,
                        tool_catalog_revision, model_policy_revision, created_revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (baseline_id, command.turn_id.value, *baseline_values, revision.value),
                )
            else:
                baseline_id = str(baseline["turn_context_baseline_id"])
                actual_values = tuple(
                    str(baseline[name])
                    for name in (
                        "completion_contract_revision_id",
                        "system_prompt_revision",
                        "main_skill_name",
                        "main_skill_revision",
                        "main_skill_content_sha256",
                        "tool_catalog_revision",
                        "model_policy_revision",
                    )
                )
                if actual_values != baseline_values:
                    raise ValueError(
                        "Turn Context Baseline is immutable for the lifetime of a Turn"
                    )

            permissions_json = canonical_json(command.permissions)
            policy_json = canonical_json(command.provider_policy)
            connection.execute(
                """
                INSERT INTO permission_snapshots VALUES (?, ?, ?, ?, ?)
                """,
                (
                    identities.permission_snapshot_id.value,
                    command.permission_policy_revision,
                    permissions_json,
                    _sha256(permissions_json),
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO model_policy_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identities.model_policy_snapshot_id.value,
                    command.model_policy_revision,
                    command.provider_profile,
                    command.provider_kind,
                    command.model_name,
                    command.endpoint,
                    command.credential_ref,
                    policy_json,
                    _sha256(policy_json),
                    revision.value,
                ),
            )
            step_ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(step_ordinal), 0) + 1 FROM steps WHERE turn_id = ?",
                    (command.turn_id.value,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO steps VALUES (?, ?, ?, 'model_running', ?, NULL)",
                (
                    identities.step_id.value,
                    command.turn_id.value,
                    step_ordinal,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO context_manifests(
                    context_manifest_id, step_id, turn_context_baseline_id,
                    base_workspace_revision, research_path_id,
                    completion_contract_revision_id, permission_snapshot_id,
                    model_policy_snapshot_id, input_token_limit,
                    remaining_step_budget, remaining_tool_budget, created_revision,
                    input_token_estimate, reserved_output_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identities.context_manifest_id.value,
                    identities.step_id.value,
                    baseline_id,
                    revision.value - 1,
                    research_path_id,
                    completion_revision_id,
                    identities.permission_snapshot_id.value,
                    identities.model_policy_snapshot_id.value,
                    command.input_token_limit,
                    max_steps - used_steps - 1,
                    max_tools - used_tools,
                    revision.value,
                    (len(normalized_input_json.encode("utf-8")) + 3) // 4,
                    (
                        int(command.provider_policy.get("max_tokens", 0))
                        if isinstance(command.provider_policy.get("max_tokens", 0), int)
                        else 0
                    ),
                ),
            )
            connection.executemany(
                """
                INSERT INTO context_items(
                    context_item_id, context_manifest_id, item_ordinal, item_kind,
                    source_object_type, source_object_id, source_revision,
                    remote_transmission_class, content_text, content_sha256, trust_class
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        identity.value,
                        identities.context_manifest_id.value,
                        ordinal,
                        item.item_kind,
                        item.source_object_type,
                        item.source_object_id,
                        item.source_revision,
                        item.remote_transmission_class,
                        item.content,
                        _sha256(item.content),
                        item.trust_class,
                    )
                    for ordinal, (identity, item) in enumerate(
                        zip(identities.context_item_ids, command.context_items, strict=True),
                        start=1,
                    )
                ],
            )
            connection.executemany(
                """
                INSERT INTO memory_context_uses(
                    context_item_id, memory_item_id, memory_revision_id,
                    usage_kind, created_revision
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [(*memory_use, revision.value) for memory_use in memory_uses],
            )
            connection.executemany(
                """
                INSERT INTO knowledge_context_uses(
                    context_item_id, knowledge_chunk_id,
                    knowledge_document_revision_id, retrieval_kind, created_revision
                ) VALUES (?, ?, ?, 'soft_prefetch', ?)
                """,
                [(*knowledge_use, revision.value) for knowledge_use in knowledge_uses],
            )
            connection.executemany(
                """
                INSERT INTO knowledge_node_context_uses(
                    context_item_id, knowledge_node_id, knowledge_source_revision_id,
                    knowledge_parse_revision_id, retrieval_session_id, retrieval_hop_id,
                    usage_kind, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(*knowledge_use, revision.value) for knowledge_use in knowledge_node_uses],
            )
            connection.executemany(
                """
                INSERT INTO skill_context_uses(
                    context_item_id, skill_name, skill_revision, content_sha256,
                    source_kind, skill_version_id, usage_kind, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(*skill_use, revision.value) for skill_use in skill_uses],
            )
            connection.executemany(
                """
                INSERT INTO context_build_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        identity.value,
                        identities.context_manifest_id.value,
                        ordinal,
                        decision.decision_kind,
                        decision.source_object_type,
                        decision.source_object_id,
                        decision.reason_code,
                        canonical_json(decision.detail),
                    )
                    for ordinal, (identity, decision) in enumerate(
                        zip(identities.decision_ids, command.build_decisions, strict=True),
                        start=1,
                    )
                ],
            )
            connection.execute(
                "INSERT INTO model_input_snapshots VALUES (?, ?, ?, ?, ?)",
                (
                    identities.input_snapshot_id.value,
                    identities.context_manifest_id.value,
                    normalized_input_json,
                    normalized_input_sha256,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO model_invocations(
                    model_invocation_id, step_id, model_input_snapshot_id, status,
                    selected_provider_attempt_id, assistant_output_id,
                    created_revision, completed_revision
                ) VALUES (?, ?, ?, 'running', NULL, NULL, ?, NULL)
                """,
                (
                    identities.invocation_id.value,
                    identities.step_id.value,
                    identities.input_snapshot_id.value,
                    revision.value,
                ),
            )
            response = {
                "step_id": identities.step_id.value,
                "model_invocation_id": identities.invocation_id.value,
                "model_input_snapshot_id": identities.input_snapshot_id.value,
                "research_path_id": research_path_id,
                "normalized_input_sha256": normalized_input_sha256,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "model.requested",
                        "model_invocation",
                        identities.invocation_id.value,
                        {
                            "step_id": identities.step_id.value,
                            "context_manifest_id": identities.context_manifest_id.value,
                            "model_input_snapshot_id": identities.input_snapshot_id.value,
                            "model_input_sha256": normalized_input_sha256,
                        },
                    ),
                ),
                (OutboxDraft("model.requested", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="model.step.freeze",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return FrozenModelInvocation(
            StepId(str(response["step_id"])),
            ModelInvocationId(str(response["model_invocation_id"])),
            ModelInputSnapshotId(str(response["model_input_snapshot_id"])),
            ResearchPathId(str(response["research_path_id"])),
            json.loads(normalized_input_json),
            str(response["normalized_input_sha256"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def prepare_attempt(
        self,
        *,
        command_id: CommandId,
        invocation_id: ModelInvocationId,
        attempt_ordinal: int,
        identity: ProviderAttemptIdentity,
        request_json: str,
        request_sha256: str,
        provider_profile: str,
        credential_version_ref: str,
        endpoint_origin: str,
        payload_bytes: int,
    ) -> WorkspaceRevision:
        request = {
            "model_invocation_id": invocation_id.value,
            "attempt_ordinal": attempt_ordinal,
            "request_sha256": request_sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT invocation.status, step.turn_id,
                       account.used_provider_attempts,
                       policy.max_provider_attempts_per_invocation,
                       policy.max_provider_attempts_per_turn,
                       policy.max_same_failure_fingerprint
                FROM model_invocations AS invocation
                JOIN steps AS step ON step.step_id = invocation.step_id
                JOIN turn_budget_accounts AS account ON account.turn_id = step.turn_id
                JOIN budget_policy_snapshots AS policy
                  ON policy.budget_policy_snapshot_id = account.budget_policy_snapshot_id
                WHERE invocation.model_invocation_id = ?
                """,
                (invocation_id.value,),
            ).fetchone()
            if row is None or str(row["status"]) != "running":
                raise ValueError("Model Invocation is not accepting another Provider Attempt")
            existing_attempts = int(
                connection.execute(
                    """
                    SELECT count(*) FROM provider_attempts WHERE model_invocation_id = ?
                    """,
                    (invocation_id.value,),
                ).fetchone()[0]
            )
            if existing_attempts >= int(row["max_provider_attempts_per_invocation"]):
                raise ValueError("Provider Attempt budget for this Invocation is exhausted")
            if int(row["used_provider_attempts"]) >= int(row["max_provider_attempts_per_turn"]):
                raise ValueError("Provider Attempt budget for this Turn is exhausted")
            repeated_failure = connection.execute(
                """
                SELECT error_code, count(*) AS failure_count
                FROM provider_attempts
                WHERE model_invocation_id = ? AND error_code IS NOT NULL
                GROUP BY error_code
                HAVING count(*) >= ?
                LIMIT 1
                """,
                (invocation_id.value, int(row["max_same_failure_fingerprint"])),
            ).fetchone()
            if repeated_failure is not None:
                raise ValueError("no-progress failure fingerprint threshold was reached")
            connection.execute(
                """
                UPDATE turn_budget_accounts
                SET used_provider_attempts = used_provider_attempts + 1,
                    account_revision = account_revision + 1,
                    updated_revision = ?
                WHERE turn_id = ?
                """,
                (revision.value, str(row["turn_id"])),
            )
            connection.execute(
                """
                INSERT INTO turn_budget_usage_history
                VALUES (?, ?, 'provider_attempt', 1, NULL, ?)
                """,
                (identity.budget_usage_id.value, str(row["turn_id"]), revision.value),
            )
            connection.execute(
                """
                INSERT INTO provider_attempts(
                    provider_attempt_id, model_invocation_id, attempt_ordinal, state,
                    usage_kind, input_tokens, output_tokens, error_code,
                    created_revision, terminal_revision,
                    cached_input_tokens, uncached_input_tokens
                ) VALUES (?, ?, ?, 'created', NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL)
                """,
                (
                    identity.attempt_id.value,
                    invocation_id.value,
                    attempt_ordinal,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO provider_request_snapshots VALUES (?, ?, ?, ?, ?)",
                (
                    identity.request_snapshot_id.value,
                    identity.attempt_id.value,
                    request_json,
                    request_sha256,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO outbound_material_records(
                    outbound_material_record_id, provider_attempt_id,
                    provider_profile, endpoint_origin, payload_sha256,
                    payload_bytes, status, created_revision, terminal_revision,
                    credential_version_ref
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, NULL, ?)
                """,
                (
                    identity.outbound_record_id.value,
                    identity.attempt_id.value,
                    provider_profile,
                    endpoint_origin,
                    request_sha256,
                    payload_bytes,
                    revision.value,
                    credential_version_ref,
                ),
            )
            payload = {
                "model_invocation_id": invocation_id.value,
                "provider_attempt_id": identity.attempt_id.value,
                "attempt_ordinal": attempt_ordinal,
                "request_snapshot_id": identity.request_snapshot_id.value,
                "outbound_material_record_id": identity.outbound_record_id.value,
            }
            return MutationPayload(
                payload,
                (
                    JournalDraft(
                        "model.provider_attempt_prepared",
                        "provider_attempt",
                        identity.attempt_id.value,
                        payload,
                    ),
                ),
                (OutboxDraft("model.provider_attempt_prepared", payload),),
            )

        return self._commits.commit_mutation(
            command_id=command_id,
            command_type="model.provider_attempt.prepare",
            request=request,
            mutation=mutate,
        ).commit_revision

    def mark_dispatch_started(
        self,
        command_id: CommandId,
        turn_id: TurnId,
        invocation_id: ModelInvocationId,
        attempt_id: ProviderAttemptId,
    ) -> WorkspaceRevision:
        request = {
            "turn_id": turn_id.value,
            "model_invocation_id": invocation_id.value,
            "provider_attempt_id": attempt_id.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT attempt.state, turn.status AS turn_status
                FROM provider_attempts AS attempt
                JOIN model_invocations AS invocation
                  ON invocation.model_invocation_id = attempt.model_invocation_id
                JOIN steps AS step ON step.step_id = invocation.step_id
                JOIN turns AS turn ON turn.turn_id = step.turn_id
                WHERE attempt.provider_attempt_id = ?
                  AND attempt.model_invocation_id = ? AND turn.turn_id = ?
                """,
                (attempt_id.value, invocation_id.value, turn_id.value),
            ).fetchone()
            if row is None or str(row["state"]) != "created":
                raise ValueError("Provider Attempt is not in CREATED")
            if str(row["turn_status"]) != "running":
                raise ValueError("Turn stopped before provider dispatch")
            connection.execute(
                """
                UPDATE provider_attempts SET state = 'dispatch_started'
                WHERE provider_attempt_id = ?
                """,
                (attempt_id.value,),
            )
            connection.execute(
                """
                UPDATE outbound_material_records SET status = 'dispatch_started'
                WHERE provider_attempt_id = ?
                """,
                (attempt_id.value,),
            )
            payload = {
                "model_invocation_id": invocation_id.value,
                "provider_attempt_id": attempt_id.value,
            }
            return MutationPayload(
                payload,
                (
                    JournalDraft(
                        "model.dispatch_started", "provider_attempt", attempt_id.value, payload
                    ),
                ),
                (OutboxDraft("model.dispatch_started", payload),),
            )

        return self._commits.commit_mutation(
            command_id=command_id,
            command_type="model.dispatch.start",
            request=request,
            mutation=mutate,
        ).commit_revision

    def complete_attempt(
        self,
        *,
        command_id: CommandId,
        invocation_id: ModelInvocationId,
        attempt_id: ProviderAttemptId,
        assistant_output_id: AssistantOutputId,
        output_json: str,
        output_sha256: str,
        response: ProviderResponse,
    ) -> WorkspaceRevision:
        request = {
            "model_invocation_id": invocation_id.value,
            "provider_attempt_id": attempt_id.value,
            "assistant_output_id": assistant_output_id.value,
            "output_sha256": output_sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT invocation.step_id, attempt.state, step.turn_id
                FROM provider_attempts AS attempt
                JOIN model_invocations AS invocation
                  ON invocation.model_invocation_id = attempt.model_invocation_id
                JOIN steps AS step ON step.step_id = invocation.step_id
                WHERE attempt.provider_attempt_id = ? AND invocation.model_invocation_id = ?
                """,
                (attempt_id.value, invocation_id.value),
            ).fetchone()
            if row is None or str(row["state"]) != "dispatch_started":
                raise ValueError("only a dispatched Provider Attempt can complete")
            step_id = str(row["step_id"])
            connection.execute(
                """
                UPDATE provider_attempts
                SET state = 'completed', usage_kind = ?, input_tokens = ?, output_tokens = ?,
                    cached_input_tokens = ?, uncached_input_tokens = ?, terminal_revision = ?
                WHERE provider_attempt_id = ?
                """,
                (
                    response.usage_kind,
                    response.input_tokens,
                    response.output_tokens,
                    response.cached_input_tokens,
                    response.uncached_input_tokens,
                    revision.value,
                    attempt_id.value,
                ),
            )
            connection.execute(
                """
                UPDATE outbound_material_records
                SET status = 'completed', terminal_revision = ?
                WHERE provider_attempt_id = ?
                """,
                (revision.value, attempt_id.value),
            )
            connection.execute(
                "INSERT INTO assistant_outputs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    assistant_output_id.value,
                    invocation_id.value,
                    attempt_id.value,
                    output_json,
                    output_sha256,
                    revision.value,
                ),
            )
            connection.execute(
                """
                UPDATE model_invocations
                SET status = 'completed', selected_provider_attempt_id = ?,
                    assistant_output_id = ?, completed_revision = ?
                WHERE model_invocation_id = ?
                """,
                (attempt_id.value, assistant_output_id.value, revision.value, invocation_id.value),
            )
            connection.execute(
                "UPDATE steps SET status = 'completed', completed_revision = ? WHERE step_id = ?",
                (revision.value, step_id),
            )
            payload = {
                "model_invocation_id": invocation_id.value,
                "provider_attempt_id": attempt_id.value,
                "assistant_output_id": assistant_output_id.value,
                "output_sha256": output_sha256,
                "usage_kind": response.usage_kind,
            }
            return MutationPayload(
                payload,
                (
                    JournalDraft(
                        "model.completed", "model_invocation", invocation_id.value, payload
                    ),
                ),
                (OutboxDraft("model.completed", payload),),
            )

        return self._commits.commit_mutation(
            command_id=command_id,
            command_type="model.provider_attempt.complete",
            request=request,
            mutation=mutate,
        ).commit_revision

    def fail_attempt(
        self,
        *,
        command_id: CommandId,
        invocation_id: ModelInvocationId,
        attempt_id: ProviderAttemptId,
        error_code: str,
        terminal_state: str,
        terminal_invocation: bool,
    ) -> WorkspaceRevision:
        if terminal_state not in {"failed", "delivery_unknown"}:
            raise ValueError("invalid Provider Attempt terminal state")
        request = {
            "model_invocation_id": invocation_id.value,
            "provider_attempt_id": attempt_id.value,
            "error_code": error_code,
            "terminal_state": terminal_state,
            "terminal_invocation": terminal_invocation,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT invocation.step_id, attempt.state, step.turn_id
                FROM provider_attempts AS attempt
                JOIN model_invocations AS invocation
                  ON invocation.model_invocation_id = attempt.model_invocation_id
                JOIN steps AS step ON step.step_id = invocation.step_id
                WHERE attempt.provider_attempt_id = ? AND invocation.model_invocation_id = ?
                """,
                (attempt_id.value, invocation_id.value),
            ).fetchone()
            if row is None or str(row["state"]) not in {"created", "dispatch_started"}:
                raise ValueError("Provider Attempt is not active")
            connection.execute(
                """
                UPDATE provider_attempts SET state = ?, error_code = ?, terminal_revision = ?
                WHERE provider_attempt_id = ?
                """,
                (terminal_state, error_code, revision.value, attempt_id.value),
            )
            connection.execute(
                """
                UPDATE outbound_material_records SET status = ?, terminal_revision = ?
                WHERE provider_attempt_id = ?
                """,
                (terminal_state, revision.value, attempt_id.value),
            )
            failure_fingerprint = _sha256(f"{invocation_id.value}:{error_code}")
            connection.execute(
                """
                INSERT INTO turn_budget_usage_history
                VALUES (?, ?, 'failure_fingerprint', 1, ?, ?)
                """,
                (
                    f"budgetusage_failure_{attempt_id.value}",
                    str(row["turn_id"]),
                    failure_fingerprint,
                    revision.value,
                ),
            )
            if terminal_invocation:
                step_status = "failed"
                connection.execute(
                    """
                    UPDATE model_invocations SET status = ?, completed_revision = ?
                    WHERE model_invocation_id = ?
                    """,
                    (terminal_state, revision.value, invocation_id.value),
                )
                connection.execute(
                    "UPDATE steps SET status = ?, completed_revision = ? WHERE step_id = ?",
                    (step_status, revision.value, str(row["step_id"])),
                )
            payload = dict(request)
            payload["terminal_revision"] = revision.value
            event = (
                "model.delivery_unknown"
                if terminal_state == "delivery_unknown"
                else "model.provider_attempt_failed"
            )
            return MutationPayload(
                payload,
                (JournalDraft(event, "provider_attempt", attempt_id.value, payload),),
                (OutboxDraft(event, payload),),
            )

        return self._commits.commit_mutation(
            command_id=command_id,
            command_type="model.provider_attempt.fail",
            request=request,
            mutation=mutate,
        ).commit_revision
