"""SQLite authority for asynchronous Memory extraction and deterministic consolidation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from urllib.parse import urlsplit

from stata_research_agent.application.memory import MemoryLifecycle
from stata_research_agent.application.memory_curator import (
    MemoryCandidateProposal,
    MemoryExtractionOutput,
    MemoryMaintenanceJob,
    MemoryMaintenanceOutcome,
    MemoryMaintenanceWindow,
    MemorySourceMessage,
    SkillEvolutionProposal,
)
from stata_research_agent.application.model_gateway import ProviderResponse
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.skill_evolution_policy import (
    ELIGIBLE_MEMORY_KINDS,
    validate_and_render_skill,
)
from stata_research_agent.domain.identifiers import (
    CommandId,
    ConversationId,
    MemoryCandidateId,
    MemoryEpisodeId,
    MemoryItemId,
    MemoryMaintenanceJobId,
    MemoryProviderAttemptId,
    MemoryRetentionHistoryId,
    MemoryRevisionId,
    MemoryRevisionSourceId,
    MemoryStateHistoryId,
    MessageId,
    SkillEvolutionCandidateId,
    SkillEvolutionStateHistoryId,
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


class SqliteMemoryMaintenanceRepository:
    """Persist Curator lifecycle without granting its model direct write authority."""

    _ACTIVE_EXACT_KINDS = {
        "research_decision",
        "research_constraint",
        "feedback",
        "unresolved_question",
    }
    _PATH_SCOPED_KINDS = {
        "research_decision",
        "research_constraint",
        "unresolved_question",
        "reference_pointer",
    }

    def __init__(self, connection: sqlite3.Connection, identities: IdentityGenerator) -> None:
        self._connection = connection
        self._identities = identities
        self._commits = AtomicCommitService(connection)

    def recover_interrupted(self) -> int:
        rows = self._connection.execute(
            """
            SELECT job.memory_maintenance_job_id, attempt.memory_provider_attempt_id,
                   attempt.status
            FROM memory_maintenance_jobs AS job
            JOIN memory_provider_attempts AS attempt
              ON attempt.memory_maintenance_job_id = job.memory_maintenance_job_id
             AND attempt.attempt_ordinal = job.attempt_count
            WHERE job.status = 'model_running'
              AND attempt.status IN ('prepared', 'dispatch_started')
            ORDER BY job.created_revision
            """
        ).fetchall()
        for row in rows:
            dispatch_started = str(row["status"]) == "dispatch_started"
            self.fail(
                command_id=self._identities.new(CommandId),
                job_id=MemoryMaintenanceJobId(str(row["memory_maintenance_job_id"])),
                attempt_id=MemoryProviderAttemptId(str(row["memory_provider_attempt_id"])),
                error_code=(
                    "memory_provider_outcome_unknown_after_restart"
                    if dispatch_started
                    else "memory_provider_not_dispatched_after_restart"
                ),
                delivery_unknown=dispatch_started,
            )
        return len(rows)

    def discover_window(self) -> MemoryMaintenanceWindow | None:
        existing = self._connection.execute(
            """
            SELECT conversation_id, source_start_revision, source_end_revision
            FROM memory_maintenance_jobs
            WHERE status = 'pending' OR (status = 'failed' AND attempt_count < 3)
            ORDER BY created_revision, memory_maintenance_job_id
            LIMIT 1
            """
        ).fetchone()
        if existing is not None:
            return self._load_window(
                ConversationId(str(existing["conversation_id"])),
                int(existing["source_start_revision"]),
                int(existing["source_end_revision"]),
            )

        row = self._connection.execute(
            """
            WITH processed AS (
                SELECT conversation_id, MAX(source_end_revision) AS source_end_revision
                FROM (
                    SELECT conversation_id, source_end_revision FROM memory_episodes
                    UNION ALL
                    SELECT conversation_id, source_end_revision
                    FROM memory_maintenance_jobs WHERE status = 'completed'
                ) GROUP BY conversation_id
            ), eligible AS (
                SELECT conversation.conversation_id,
                       COALESCE(processed.source_end_revision, 0) AS prior_end,
                       MAX(message.created_revision) AS next_end
                FROM conversations AS conversation
                LEFT JOIN conversation_memory_policies AS policy USING (conversation_id)
                LEFT JOIN processed USING (conversation_id)
                JOIN messages AS message USING (conversation_id)
                JOIN turns AS turn ON turn.triggering_message_id = message.message_id
                WHERE COALESCE(policy.contribute_memory, 1) = 1
                  AND turn.status IN ('succeeded', 'partial', 'paused', 'failed')
                  AND message.created_revision > COALESCE(processed.source_end_revision, 0)
                  AND NOT EXISTS (
                    SELECT 1 FROM memory_maintenance_jobs AS blocked
                    WHERE blocked.conversation_id = conversation.conversation_id
                      AND blocked.source_end_revision = message.created_revision
                      AND (
                        blocked.status = 'delivery_unknown'
                        OR (blocked.status = 'failed' AND blocked.attempt_count >= 3)
                      )
                  )
                GROUP BY conversation.conversation_id
            )
            SELECT conversation_id, prior_end + 1 AS source_start_revision,
                   next_end AS source_end_revision
            FROM eligible
            ORDER BY source_end_revision, conversation_id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return self._load_window(
            ConversationId(str(row["conversation_id"])),
            int(row["source_start_revision"]),
            int(row["source_end_revision"]),
        )

    def enqueue(
        self,
        *,
        command_id: CommandId,
        job_id: MemoryMaintenanceJobId,
        window: MemoryMaintenanceWindow,
        curator_revision: str,
    ) -> MemoryMaintenanceJob:
        existing = self._connection.execute(
            """
            SELECT memory_maintenance_job_id, attempt_count
            FROM memory_maintenance_jobs
            WHERE conversation_id = ? AND source_end_revision = ? AND curator_revision = ?
            """,
            (window.conversation_id.value, window.source_end_revision, curator_revision),
        ).fetchone()
        if existing is not None:
            return MemoryMaintenanceJob(
                MemoryMaintenanceJobId(str(existing["memory_maintenance_job_id"])),
                window,
                curator_revision,
                int(existing["attempt_count"]),
            )

        request = {
            "conversation_id": window.conversation_id.value,
            "source_start_revision": window.source_start_revision,
            "source_end_revision": window.source_end_revision,
            "curator_revision": curator_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            connection.execute(
                """
                INSERT INTO memory_maintenance_jobs(
                    memory_maintenance_job_id, conversation_id, source_start_revision,
                    source_end_revision, job_kind, curator_revision, status,
                    attempt_count, error_code, created_revision, updated_revision
                ) VALUES (?, ?, ?, ?, 'extract_and_consolidate', ?, 'pending', 0, NULL, ?, ?)
                """,
                (
                    job_id.value,
                    window.conversation_id.value,
                    window.source_start_revision,
                    window.source_end_revision,
                    curator_revision,
                    revision.value,
                    revision.value,
                ),
            )
            response = {"memory_maintenance_job_id": job_id.value, "status": "pending"}
            return MutationPayload(
                response,
                (JournalDraft("memory.maintenance_enqueued", "memory_job", job_id.value, request),),
                (OutboxDraft("memory.maintenance_changed", response),),
            )

        self._commits.commit_mutation(
            command_id=command_id,
            command_type="memory.maintenance.enqueue",
            request=request,
            mutation=mutate,
        )
        return MemoryMaintenanceJob(job_id, window, curator_revision, 0)

    def prepare_attempt(
        self,
        *,
        command_id: CommandId,
        job_id: MemoryMaintenanceJobId,
        attempt_id: MemoryProviderAttemptId,
        provider_profile_id: str,
        credential_version_id: str,
        endpoint_origin: str,
        model_name: str,
        request_json: str,
    ) -> int:
        parsed = json.loads(request_json)
        if not isinstance(parsed, dict):
            raise ValueError("Memory provider request must be a JSON object")
        request_hash = _sha256(request_json)

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT status, attempt_count FROM memory_maintenance_jobs
                WHERE memory_maintenance_job_id = ?
                """,
                (job_id.value,),
            ).fetchone()
            if row is None or str(row["status"]) not in {"pending", "failed"}:
                raise ValueError("Memory maintenance job is not dispatchable")
            attempt_ordinal = int(row["attempt_count"]) + 1
            connection.execute(
                """
                INSERT INTO memory_provider_attempts(
                    memory_provider_attempt_id, memory_maintenance_job_id, attempt_ordinal,
                    status, provider_profile_id, credential_version_id, endpoint_origin,
                    model_name, request_json, request_sha256, response_json,
                    response_sha256, input_tokens, output_tokens, error_code,
                    prepared_revision, updated_revision
                ) VALUES (?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, NULL, NULL,
                          NULL, NULL, NULL, ?, ?)
                """,
                (
                    attempt_id.value,
                    job_id.value,
                    attempt_ordinal,
                    provider_profile_id,
                    credential_version_id,
                    endpoint_origin,
                    model_name,
                    request_json,
                    request_hash,
                    revision.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                UPDATE memory_maintenance_jobs
                SET status = 'model_running', attempt_count = ?, error_code = NULL,
                    updated_revision = ?
                WHERE memory_maintenance_job_id = ?
                """,
                (attempt_ordinal, revision.value, job_id.value),
            )
            response = {
                "memory_maintenance_job_id": job_id.value,
                "memory_provider_attempt_id": attempt_id.value,
                "attempt_ordinal": attempt_ordinal,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.provider_prepared", "memory_attempt", attempt_id.value, response
                    ),
                ),
                (),
            )

        receipt = self._commits.commit_mutation(
            command_id=command_id,
            command_type="memory.provider.prepare",
            request={
                "memory_maintenance_job_id": job_id.value,
                "memory_provider_attempt_id": attempt_id.value,
                "request_sha256": request_hash,
            },
            mutation=mutate,
        )
        return int(receipt.response["attempt_ordinal"])

    def mark_dispatch_started(
        self,
        *,
        command_id: CommandId,
        job_id: MemoryMaintenanceJobId,
        attempt_id: MemoryProviderAttemptId,
    ) -> None:
        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            cursor = connection.execute(
                """
                UPDATE memory_provider_attempts
                SET status = 'dispatch_started', updated_revision = ?
                WHERE memory_provider_attempt_id = ? AND memory_maintenance_job_id = ?
                  AND status = 'prepared'
                """,
                (revision.value, attempt_id.value, job_id.value),
            )
            if cursor.rowcount != 1:
                raise ValueError("Memory provider attempt is not prepared")
            response = {
                "memory_maintenance_job_id": job_id.value,
                "memory_provider_attempt_id": attempt_id.value,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.provider_dispatch_started",
                        "memory_attempt",
                        attempt_id.value,
                        response,
                    ),
                ),
                (),
            )

        self._commits.commit_mutation(
            command_id=command_id,
            command_type="memory.provider.dispatch",
            request={"attempt_id": attempt_id.value},
            mutation=mutate,
        )

    def finalize(
        self,
        *,
        command_id: CommandId,
        job: MemoryMaintenanceJob,
        attempt_id: MemoryProviderAttemptId,
        response: ProviderResponse,
        extraction: MemoryExtractionOutput,
    ) -> MemoryMaintenanceOutcome:
        response_json = canonical_json(dict(response.output))
        response_hash = _sha256(response_json)

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            attempt = connection.execute(
                """
                SELECT status FROM memory_provider_attempts
                WHERE memory_provider_attempt_id = ? AND memory_maintenance_job_id = ?
                """,
                (attempt_id.value, job.job_id.value),
            ).fetchone()
            if attempt is None or str(attempt["status"]) != "dispatch_started":
                raise ValueError("Memory provider attempt is not finalizable")

            source_messages = {message.message_id.value: message for message in job.window.messages}
            counts = {"created_active": 0, "created_proposed": 0, "deduplicated": 0, "rejected": 0}
            for proposal in extraction.candidates:
                candidate_id = self._identities.new(MemoryCandidateId)
                source = source_messages.get(proposal.source_message_id.value)
                if source is None:
                    counts["rejected"] += 1
                    continue
                if proposal.supporting_quote not in source.content:
                    counts["rejected"] += 1
                    self._insert_candidate(
                        connection,
                        revision,
                        candidate_id.value,
                        job.job_id.value,
                        attempt_id.value,
                        source,
                        proposal,
                        "rejected",
                        "supporting_quote_not_exact",
                        None,
                        None,
                    )
                    continue
                path_scoped = (
                    proposal.kind.value in self._PATH_SCOPED_KINDS
                    and source.research_path_id is not None
                )
                scope_kind = "research_path" if path_scoped else "workspace"
                scope_id = (
                    str(source.research_path_id)
                    if path_scoped
                    else str(
                        connection.execute(
                            "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
                        ).fetchone()[0]
                    )
                )
                normalized = " ".join(proposal.content.lower().split())
                duplicate = connection.execute(
                    """
                    SELECT 1
                    FROM memory_current_states AS state
                    JOIN memory_items AS item USING (memory_item_id)
                    JOIN memory_revisions AS memory
                      ON memory.memory_revision_id = state.current_revision_id
                    WHERE state.lifecycle IN ('active', 'proposed')
                      AND item.scope_kind = ? AND item.scope_object_id = ?
                      AND lower(trim(memory.content)) = ?
                    LIMIT 1
                    """,
                    (scope_kind, scope_id, normalized),
                ).fetchone()
                if duplicate is not None:
                    counts["deduplicated"] += 1
                    self._insert_candidate(
                        connection,
                        revision,
                        candidate_id.value,
                        job.job_id.value,
                        attempt_id.value,
                        source,
                        proposal,
                        "deduplicated",
                        "equivalent_current_memory",
                        None,
                        None,
                    )
                    continue

                conflict = connection.execute(
                    """
                    SELECT 1
                    FROM memory_items AS item
                    JOIN memory_current_states AS state USING (memory_item_id)
                    JOIN memory_revisions AS memory
                      ON memory.memory_revision_id = state.current_revision_id
                    WHERE state.lifecycle IN ('active', 'proposed')
                      AND item.memory_kind = ?
                      AND item.scope_kind = ?
                      AND item.scope_object_id = ?
                      AND lower(trim(memory.title)) = lower(trim(?))
                    LIMIT 1
                    """,
                    (proposal.kind.value, scope_kind, scope_id, proposal.title),
                ).fetchone()
                exact_active = (
                    proposal.suggested_lifecycle is MemoryLifecycle.ACTIVE
                    and proposal.kind.value in self._ACTIVE_EXACT_KINDS
                    and conflict is None
                )
                lifecycle = "active" if exact_active else "proposed"
                content = proposal.supporting_quote if exact_active else proposal.content
                origin = "explicit_user" if exact_active else "inferred"
                item_id = self._identities.new(MemoryItemId)
                memory_revision_id = self._identities.new(MemoryRevisionId)
                connection.execute(
                    "INSERT INTO memory_items VALUES (?, ?, ?, ?, ?)",
                    (
                        item_id.value,
                        scope_kind,
                        scope_id,
                        proposal.kind.value,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO memory_revisions VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_revision_id.value,
                        item_id.value,
                        proposal.title,
                        content,
                        _sha256(content),
                        origin,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO memory_revision_sources
                    VALUES (?, ?, 'message', ?, ?, 'user_statement', ?)
                    """,
                    (
                        self._identities.new(MemoryRevisionSourceId).value,
                        memory_revision_id.value,
                        source.message_id.value,
                        str(source.created_revision),
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO memory_current_states VALUES (?, ?, ?, 1, ?)",
                    (item_id.value, memory_revision_id.value, lifecycle, revision.value),
                )
                access_tier = "hot" if lifecycle == "active" else "warm"
                pinned = int(
                    proposal.kind.value
                    in {"research_decision", "research_constraint", "unresolved_question"}
                )
                connection.execute(
                    """
                    INSERT INTO memory_retention_states(
                        memory_item_id, access_tier, pinned, retention_revision,
                        last_reinforced_revision, superseded_by_memory_item_id,
                        superseded_revision, policy_revision, updated_revision
                    ) VALUES (?, ?, ?, 1, ?, NULL, NULL, 'memory-retention/v1', ?)
                    """,
                    (
                        item_id.value,
                        access_tier,
                        pinned,
                        revision.value if lifecycle == "active" else None,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO memory_retention_history(
                        memory_retention_history_id, memory_item_id, access_tier, pinned,
                        retention_revision, superseded_by_memory_item_id, reason_code,
                        policy_revision, created_revision
                    ) VALUES (?, ?, ?, ?, 1, NULL, 'memory.curator_created',
                              'memory-retention/v1', ?)
                    """,
                    (
                        self._identities.new(MemoryRetentionHistoryId).value,
                        item_id.value,
                        access_tier,
                        pinned,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO memory_state_history
                    VALUES (?, ?, ?, ?, 'memory.curator_created', ?)
                    """,
                    (
                        self._identities.new(MemoryStateHistoryId).value,
                        item_id.value,
                        memory_revision_id.value,
                        lifecycle,
                        revision.value,
                    ),
                )
                disposition = f"created_{lifecycle}"
                counts[disposition] += 1
                self._insert_candidate(
                    connection,
                    revision,
                    candidate_id.value,
                    job.job_id.value,
                    attempt_id.value,
                    source,
                    proposal,
                    disposition,
                    "exact_user_quote"
                    if exact_active
                    else "conflicts_with_current_memory"
                    if conflict is not None
                    else "requires_user_activation",
                    item_id.value,
                    memory_revision_id.value,
                )

            episode_id = self._identities.new(MemoryEpisodeId)
            connection.execute(
                """
                INSERT INTO memory_episodes(
                    memory_episode_id, conversation_id, source_start_revision,
                    source_end_revision, summary, summary_sha256, extractor_kind,
                    extractor_revision, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, 'model_curator', ?, ?)
                """,
                (
                    episode_id.value,
                    job.window.conversation_id.value,
                    job.window.source_start_revision,
                    job.window.source_end_revision,
                    extraction.episode_summary,
                    _sha256(extraction.episode_summary),
                    job.curator_revision,
                    revision.value,
                ),
            )
            connection.execute(
                """
                UPDATE memory_provider_attempts
                SET status = 'completed', response_json = ?, response_sha256 = ?,
                    input_tokens = ?, output_tokens = ?, error_code = NULL,
                    updated_revision = ?
                WHERE memory_provider_attempt_id = ?
                """,
                (
                    response_json,
                    response_hash,
                    response.input_tokens,
                    response.output_tokens,
                    revision.value,
                    attempt_id.value,
                ),
            )
            connection.execute(
                """
                UPDATE memory_maintenance_jobs
                SET status = 'completed', error_code = NULL, updated_revision = ?
                WHERE memory_maintenance_job_id = ?
                """,
                (revision.value, job.job_id.value),
            )
            self._rebuild_summary_rows(connection, revision.value)
            skill_journals: list[JournalDraft] = []
            skill_count = 0
            for skill_proposal in extraction.skill_candidates:
                created = self._insert_skill_candidate(
                    connection,
                    revision,
                    job.job_id.value,
                    attempt_id.value,
                    skill_proposal,
                )
                if created is not None:
                    skill_count += 1
                    skill_journals.append(
                        JournalDraft(
                            "skill.evolution_proposed",
                            "skill_evolution_candidate",
                            created,
                            {
                                "skill_evolution_candidate_id": created,
                                "source_memory_item_count": len(
                                    set(skill_proposal.source_memory_item_ids)
                                ),
                            },
                        )
                    )
            payload = {
                "memory_maintenance_job_id": job.job_id.value,
                "memory_episode_id": episode_id.value,
                "skill_candidates_proposed": skill_count,
                **counts,
            }
            return MutationPayload(
                payload,
                (
                    JournalDraft(
                        "memory.maintenance_completed", "memory_job", job.job_id.value, payload
                    ),
                    *skill_journals,
                ),
                (OutboxDraft("memory.changed", payload),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command_id,
            command_type="memory.maintenance.finalize",
            request={
                "memory_maintenance_job_id": job.job_id.value,
                "memory_provider_attempt_id": attempt_id.value,
                "response_sha256": response_hash,
            },
            mutation=mutate,
        )
        values = receipt.response
        return MemoryMaintenanceOutcome(
            job.job_id,
            "completed",
            int(values["created_active"]),
            int(values["created_proposed"]),
            int(values["deduplicated"]),
            int(values["rejected"]),
            int(values["skill_candidates_proposed"]),
        )

    def fail(
        self,
        *,
        command_id: CommandId,
        job_id: MemoryMaintenanceJobId,
        attempt_id: MemoryProviderAttemptId,
        error_code: str,
        delivery_unknown: bool,
    ) -> None:
        terminal = "delivery_unknown" if delivery_unknown else "failed"

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            cursor = connection.execute(
                """
                UPDATE memory_provider_attempts
                SET status = ?, error_code = ?, updated_revision = ?
                WHERE memory_provider_attempt_id = ?
                  AND status IN ('prepared', 'dispatch_started')
                """,
                (terminal, error_code, revision.value, attempt_id.value),
            )
            if cursor.rowcount != 1:
                raise ValueError("Memory provider attempt is already terminal")
            connection.execute(
                """
                UPDATE memory_maintenance_jobs
                SET status = ?, error_code = ?, updated_revision = ?
                WHERE memory_maintenance_job_id = ?
                """,
                (terminal, error_code, revision.value, job_id.value),
            )
            payload = {
                "memory_maintenance_job_id": job_id.value,
                "memory_provider_attempt_id": attempt_id.value,
                "status": terminal,
                "error_code": error_code,
            }
            return MutationPayload(
                payload,
                (JournalDraft("memory.maintenance_failed", "memory_job", job_id.value, payload),),
                (OutboxDraft("memory.maintenance_changed", payload),),
            )

        self._commits.commit_mutation(
            command_id=command_id,
            command_type="memory.maintenance.fail",
            request={
                "memory_maintenance_job_id": job_id.value,
                "attempt_id": attempt_id.value,
                "error_code": error_code,
                "delivery_unknown": delivery_unknown,
            },
            mutation=mutate,
        )

    def rebuild_summaries(self) -> None:
        # Projection-only repair: no authoritative revision is created.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            source_revision = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                ).fetchone()[0]
            )
            self._rebuild_summary_rows(self._connection, source_revision)
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def apply_retention_policy(
        self,
        *,
        command_id: CommandId,
        observed_at: str,
        hot_days: int = 30,
        warm_days: int = 90,
        cold_days: int = 365,
        policy_revision: str = "memory-retention/v1",
    ) -> int:
        """Move only unpinned Memory toward archive; never delete history or payloads."""

        if not observed_at.strip() or not (0 < hot_days < warm_days < cold_days):
            raise ValueError("invalid Memory retention policy")
        rows = self._connection.execute(
            """
            WITH last_use AS (
                SELECT memory_item_id, MAX(created_revision) AS last_used_revision
                FROM memory_context_uses GROUP BY memory_item_id
            )
            SELECT item.memory_item_id, retention.access_tier,
                   retention.retention_revision,
                   state.lifecycle,
                   julianday(?) - julianday(commit_row.committed_at) AS inactive_days
            FROM memory_items AS item
            JOIN memory_current_states AS state USING (memory_item_id)
            JOIN memory_retention_states AS retention USING (memory_item_id)
            LEFT JOIN last_use USING (memory_item_id)
            JOIN workspace_commits AS commit_row
              ON commit_row.workspace_revision = MAX(
                    item.created_revision,
                    state.updated_revision,
                    COALESCE(last_use.last_used_revision, 0),
                    COALESCE(retention.last_reinforced_revision, 0)
                 )
            WHERE retention.pinned = 0
              AND retention.superseded_by_memory_item_id IS NULL
              AND retention.access_tier != 'archived'
            """,
            (observed_at,),
        ).fetchall()
        transitions: list[tuple[sqlite3.Row, str]] = []
        for row in rows:
            age = float(row["inactive_days"])
            tier = str(row["access_tier"])
            target = (
                "warm"
                if tier == "hot" and age >= hot_days
                else "cold"
                if tier == "warm" and age >= warm_days
                else "archived"
                if tier == "cold" and age >= cold_days
                else None
            )
            if target is not None:
                transitions.append((row, target))
        if not transitions:
            return 0

        request = {
            "observed_at": observed_at,
            "hot_days": hot_days,
            "warm_days": warm_days,
            "cold_days": cold_days,
            "policy_revision": policy_revision,
            "memory_item_ids": [str(row["memory_item_id"]) for row, _ in transitions],
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            journal: list[JournalDraft] = []
            for row, target in transitions:
                item_id = str(row["memory_item_id"])
                next_revision = int(row["retention_revision"]) + 1
                cursor = connection.execute(
                    """
                    UPDATE memory_retention_states
                    SET access_tier = ?, retention_revision = ?,
                        policy_revision = ?, updated_revision = ?
                    WHERE memory_item_id = ? AND retention_revision = ?
                      AND pinned = 0 AND superseded_by_memory_item_id IS NULL
                    """,
                    (
                        target,
                        next_revision,
                        policy_revision,
                        revision.value,
                        item_id,
                        int(row["retention_revision"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("Memory retention changed during maintenance")
                connection.execute(
                    """
                    INSERT INTO memory_retention_history(
                        memory_retention_history_id, memory_item_id, access_tier, pinned,
                        retention_revision, superseded_by_memory_item_id, reason_code,
                        policy_revision, created_revision
                    ) VALUES (?, ?, ?, 0, ?, NULL, 'inactive_decay', ?, ?)
                    """,
                    (
                        self._identities.new(MemoryRetentionHistoryId).value,
                        item_id,
                        target,
                        next_revision,
                        policy_revision,
                        revision.value,
                    ),
                )
                journal.append(
                    JournalDraft(
                        "memory.access_tier_changed",
                        "memory_item",
                        item_id,
                        {
                            "memory_item_id": item_id,
                            "access_tier": target,
                            "retention_revision": next_revision,
                            "reason": "inactive_decay",
                            "policy_revision": policy_revision,
                        },
                    )
                )
            self._rebuild_summary_rows(connection, revision.value)
            response = {
                "transition_count": len(transitions),
                "policy_revision": policy_revision,
            }
            return MutationPayload(
                response,
                tuple(journal),
                (OutboxDraft("memory.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command_id,
            command_type="memory.retention_maintenance",
            request=request,
            mutation=mutate,
        )
        return int(receipt.response["transition_count"])

    def _load_window(
        self, conversation_id: ConversationId, start: int, end: int
    ) -> MemoryMaintenanceWindow:
        messages = tuple(
            MemorySourceMessage(
                MessageId(str(row["message_id"])),
                int(row["created_revision"]),
                str(row["content"]),
                str(row["research_path_id"]),
            )
            for row in self._connection.execute(
                """
                SELECT message.message_id, message.created_revision, message.content,
                       turn.research_path_id
                FROM messages AS message
                JOIN turns AS turn ON turn.triggering_message_id = message.message_id
                WHERE message.conversation_id = ?
                  AND message.created_revision BETWEEN ? AND ?
                ORDER BY message.ordinal LIMIT 64
                """,
                (conversation_id.value, start, end),
            ).fetchall()
        )
        effective_end = messages[-1].created_revision if messages else end
        assistant_texts: list[str] = []
        rows = self._connection.execute(
            """
            SELECT output.output_json
            FROM assistant_outputs AS output
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            JOIN turns AS turn USING (turn_id)
            WHERE turn.conversation_id = ?
              AND output.created_revision BETWEEN ? AND ?
            ORDER BY output.created_revision LIMIT 64
            """,
            (conversation_id.value, start, effective_end),
        ).fetchall()
        for row in rows:
            payload = json.loads(str(row["output_json"]))
            text = payload.get("text") if isinstance(payload, dict) else None
            if isinstance(text, str) and text.strip():
                assistant_texts.append(text[:4000])
        index = tuple(
            (
                f"{row['memory_item_id']} | {row['memory_kind']} | "
                f"{row['title']} | {row['content_excerpt']}"
            )
            for row in self._connection.execute(
                """
                SELECT item.memory_item_id, item.memory_kind, revision.title,
                       substr(revision.content, 1, 500) AS content_excerpt
                FROM memory_items AS item
                JOIN memory_current_states AS state USING (memory_item_id)
                JOIN memory_retention_states AS retention USING (memory_item_id)
                JOIN memory_revisions AS revision
                  ON revision.memory_revision_id = state.current_revision_id
                WHERE state.lifecycle = 'active'
                  AND retention.access_tier != 'archived'
                  AND retention.superseded_by_memory_item_id IS NULL
                ORDER BY revision.created_revision DESC LIMIT 64
                """
            ).fetchall()
        )
        return MemoryMaintenanceWindow(
            conversation_id, start, effective_end, messages, tuple(assistant_texts), index
        )

    def _insert_skill_candidate(
        self,
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        job_id: str,
        attempt_id: str,
        proposal: SkillEvolutionProposal,
    ) -> str | None:
        source_ids = tuple(dict.fromkeys(proposal.source_memory_item_ids))
        if len(source_ids) < 2:
            return None
        placeholders = ",".join("?" for _ in source_ids)
        source_rows = connection.execute(
            f"""
            SELECT item.memory_item_id, item.memory_kind, state.current_revision_id,
                   state.lifecycle
            FROM memory_items AS item
            JOIN memory_current_states AS state USING (memory_item_id)
            WHERE item.memory_item_id IN ({placeholders})
            """,
            source_ids,
        ).fetchall()
        by_id = {str(row["memory_item_id"]): row for row in source_rows}
        if len(by_id) != len(source_ids):
            return None
        if any(
            str(by_id[source_id]["lifecycle"]) != "active"
            or str(by_id[source_id]["memory_kind"]) not in ELIGIBLE_MEMORY_KINDS
            for source_id in source_ids
        ):
            return None
        distinct_origins = int(
            connection.execute(
                f"""
                SELECT count(*) FROM (
                    SELECT DISTINCT source.source_object_type,
                                    source.source_object_id,
                                    source.source_object_revision
                    FROM memory_revision_sources AS source
                    WHERE source.memory_revision_id IN (
                        SELECT state.current_revision_id
                        FROM memory_current_states AS state
                        WHERE state.memory_item_id IN ({placeholders})
                    )
                      AND source.source_role IN (
                          'user_statement', 'user_confirmation', 'external_reference'
                      )
                )
                """,
                source_ids,
            ).fetchone()[0]
        )
        if distinct_origins < 2:
            return None
        latest = connection.execute(
            """
            SELECT proposed_version FROM skill_evolution_candidates
            WHERE skill_name = ? ORDER BY created_revision DESC LIMIT 1
            """,
            (proposal.skill_name.strip(),),
        ).fetchone()
        version = self._next_skill_version(
            None if latest is None else str(latest["proposed_version"])
        )
        validated = validate_and_render_skill(
            proposal,
            proposed_version=version,
            source_kinds=tuple(str(by_id[item]["memory_kind"]) for item in source_ids),
        )
        if "invalid_skill_name" in validated.validation_findings:
            return None
        duplicate = connection.execute(
            """
            SELECT skill_evolution_candidate_id FROM skill_evolution_candidates
            WHERE skill_name = ? AND skill_sha256 = ?
            """,
            (validated.skill_name, validated.skill_sha256),
        ).fetchone()
        if duplicate is not None:
            return None
        candidate_id = self._identities.new(SkillEvolutionCandidateId).value
        connection.execute(
            """
            INSERT INTO skill_evolution_candidates(
                skill_evolution_candidate_id, source_memory_maintenance_job_id,
                source_memory_provider_attempt_id, skill_name, proposed_version,
                description, instruction_body, skill_markdown, skill_sha256,
                rationale, policy_revision, validation_status,
                validation_findings_json, created_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                job_id,
                attempt_id,
                validated.skill_name,
                validated.proposed_version,
                validated.description,
                validated.instruction_body,
                validated.skill_markdown,
                validated.skill_sha256,
                validated.rationale,
                validated.policy_revision,
                validated.validation_status,
                canonical_json(list(validated.validation_findings)),
                revision.value,
            ),
        )
        for source_id in source_ids:
            connection.execute(
                """
                INSERT INTO skill_evolution_candidate_sources
                VALUES (?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    source_id,
                    str(by_id[source_id]["current_revision_id"]),
                    revision.value,
                ),
            )
        connection.execute(
            "INSERT INTO skill_evolution_current_states VALUES (?, 'proposed', 1, ?)",
            (candidate_id, revision.value),
        )
        connection.execute(
            """
            INSERT INTO skill_evolution_state_history
            VALUES (?, ?, 'proposed', 'memory_curator_proposed', ?)
            """,
            (
                self._identities.new(SkillEvolutionStateHistoryId).value,
                candidate_id,
                revision.value,
            ),
        )
        return candidate_id

    @staticmethod
    def _next_skill_version(latest: str | None) -> str:
        if latest is None:
            return "1.0.0"
        parts = latest.split(".")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
        return "1.0.0"

    @staticmethod
    def endpoint_origin(endpoint: str) -> str:
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("invalid provider endpoint")
        return f"{parts.scheme}://{parts.netloc}"

    @staticmethod
    def _insert_candidate(
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        candidate_id: str,
        job_id: str,
        attempt_id: str,
        source: MemorySourceMessage,
        proposal: MemoryCandidateProposal,
        disposition: str,
        reason: str,
        item_id: str | None,
        memory_revision_id: str | None,
    ) -> None:
        # Kept structural to avoid making persistence depend on model-specific DTO mutation.
        connection.execute(
            """
            INSERT INTO memory_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                job_id,
                attempt_id,
                source.message_id.value,
                source.created_revision,
                proposal.supporting_quote,
                proposal.kind.value,
                proposal.title,
                proposal.content,
                proposal.suggested_lifecycle.value,
                disposition,
                reason,
                item_id,
                memory_revision_id,
                revision.value,
            ),
        )

    @staticmethod
    def _rebuild_summary_rows(connection: sqlite3.Connection, source_revision: int) -> None:
        scopes = connection.execute(
            """
            SELECT DISTINCT item.scope_kind, item.scope_object_id
            FROM memory_items AS item
            """
        ).fetchall()
        for scope in scopes:
            rows = connection.execute(
                """
                SELECT item.memory_item_id, item.memory_kind,
                       state.current_revision_id, revision.title,
                       revision.created_revision
                FROM memory_items AS item
                JOIN memory_current_states AS state USING (memory_item_id)
                JOIN memory_retention_states AS retention USING (memory_item_id)
                JOIN memory_revisions AS revision
                  ON revision.memory_revision_id = state.current_revision_id
                WHERE state.lifecycle = 'active' AND item.scope_kind = ?
                  AND item.scope_object_id = ?
                  AND retention.access_tier = 'hot'
                  AND retention.superseded_by_memory_item_id IS NULL
                ORDER BY
                  CASE item.memory_kind
                    WHEN 'research_constraint' THEN 0
                    WHEN 'research_decision' THEN 1
                    WHEN 'unresolved_question' THEN 2
                    ELSE 3
                  END,
                  revision.created_revision DESC
                LIMIT 64
                """,
                (str(scope["scope_kind"]), str(scope["scope_object_id"])),
            ).fetchall()
            lines = [
                "Project Memory reference catalog (identities only; open exact files for content):"
            ]
            for row in rows:
                line = (
                    f"- [{row['memory_kind']}] {row['title']} "
                    f"({row['memory_item_id']} @ {row['current_revision_id']})"
                )
                encoded = "\n".join([*lines, line]).encode("utf-8")
                if len(encoded) > 10_240:
                    break
                lines.append(line)
            summary = "\n".join(lines)
            prior = connection.execute(
                """
                SELECT projection_revision FROM memory_summary_projections
                WHERE scope_kind = ? AND scope_object_id = ?
                """,
                (str(scope["scope_kind"]), str(scope["scope_object_id"])),
            ).fetchone()
            projection_revision = 1 if prior is None else int(prior[0]) + 1
            connection.execute(
                """
                INSERT INTO memory_summary_projections VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_kind, scope_object_id) DO UPDATE SET
                    summary_text = excluded.summary_text,
                    summary_sha256 = excluded.summary_sha256,
                    source_revision = excluded.source_revision,
                    projection_revision = excluded.projection_revision
                """,
                (
                    str(scope["scope_kind"]),
                    str(scope["scope_object_id"]),
                    summary,
                    _sha256(summary),
                    source_revision,
                    projection_revision,
                ),
            )
