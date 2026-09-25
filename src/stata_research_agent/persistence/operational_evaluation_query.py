"""Read-only L1/L2/L3 metrics derived from ordinary Workspace execution facts."""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from stata_research_agent.application.operational_evaluation import (
    OperationalBreakdown,
    OperationalBreakdownItem,
    OperationalConfigurationSlice,
    OperationalLayerSnapshot,
    OperationalMetric,
    TurnOperationalEvaluationSnapshot,
    WorkspaceOperationalEvaluationSnapshot,
)
from stata_research_agent.domain.identifiers import TurnId

_POLICY = "operational-evaluation/v2"


def _ratio(
    metric_id: str,
    layer: str,
    subsystem: str,
    numerator: int,
    denominator: int,
    *,
    explanation: str,
    sources: tuple[str, ...],
    hard_minimum: float | None = None,
) -> OperationalMetric:
    if denominator == 0:
        return OperationalMetric(
            metric_id,
            layer,  # type: ignore[arg-type]
            subsystem,
            "not_applicable",
            None,
            float(numerator),
            0.0,
            "ratio",
            explanation,
            sources,
        )
    value = numerator / denominator
    status = "observed"
    if hard_minimum is not None:
        status = "pass" if value >= hard_minimum else "fail"
    return OperationalMetric(
        metric_id,
        layer,  # type: ignore[arg-type]
        subsystem,
        status,  # type: ignore[arg-type]
        value,
        float(numerator),
        float(denominator),
        "ratio",
        explanation,
        sources,
    )


def _count(
    metric_id: str,
    layer: str,
    subsystem: str,
    value: int,
    *,
    explanation: str,
    sources: tuple[str, ...],
    hard_zero: bool = False,
    warn_above_zero: bool = False,
) -> OperationalMetric:
    status = "observed"
    if hard_zero:
        status = "pass" if value == 0 else "fail"
    elif warn_above_zero:
        status = "pass" if value == 0 else "warn"
    return OperationalMetric(
        metric_id,
        layer,  # type: ignore[arg-type]
        subsystem,
        status,  # type: ignore[arg-type]
        float(value),
        float(value),
        None,
        "count",
        explanation,
        sources,
    )


def _observed(
    metric_id: str,
    layer: str,
    subsystem: str,
    value: float | None,
    *,
    unit: str,
    explanation: str,
    sources: tuple[str, ...],
) -> OperationalMetric:
    return OperationalMetric(
        metric_id,
        layer,  # type: ignore[arg-type]
        subsystem,
        "not_applicable" if value is None else "observed",
        value,
        value,
        None,
        unit,
        explanation,
        sources,
    )


def _layer(layer: str, metrics: tuple[OperationalMetric, ...]) -> OperationalLayerSnapshot:
    hard_failures = sum(metric.status == "fail" for metric in metrics)
    warnings = sum(metric.status == "warn" for metric in metrics)
    applicable = tuple(metric for metric in metrics if metric.status != "not_applicable")
    if hard_failures:
        status = "fail"
    elif warnings:
        status = "warn"
    elif any(metric.status == "pass" for metric in applicable):
        status = "pass"
    elif applicable:
        status = "observed"
    else:
        status = "not_applicable"
    return OperationalLayerSnapshot(
        layer,  # type: ignore[arg-type]
        status,  # type: ignore[arg-type]
        metrics,
        hard_failures,
        warnings,
    )


class SqliteOperationalEvaluationQuery:
    """Compute explainable operational metrics without mutating research authority."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def turn(self, turn_id: TurnId) -> TurnOperationalEvaluationSnapshot:
        turn = self._connection.execute(
            """
            SELECT turn.turn_id, turn.status, turn.research_path_id,
                   created_commit.committed_at AS created_at,
                   (
                       SELECT terminal_commit.committed_at
                       FROM journal_entries AS journal
                       JOIN workspace_commits AS terminal_commit USING (workspace_revision)
                       WHERE journal.object_type = 'turn'
                         AND journal.object_id = turn.turn_id
                         AND journal.event_type = 'turn.terminated'
                       ORDER BY journal.workspace_revision DESC, journal.ordinal DESC
                       LIMIT 1
                   ) AS terminal_at
            FROM turns AS turn
            JOIN workspace_commits AS created_commit
              ON created_commit.workspace_revision = turn.created_revision
            WHERE turn.turn_id = ?
            """,
            (turn_id.value,),
        ).fetchone()
        if turn is None:
            raise ValueError("Turn does not exist")
        workspace_id = str(
            self._connection.execute(
                "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
            ).fetchone()[0]
        )
        revision = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
            ).fetchone()[0]
        )
        metrics = self._metrics(turn_id.value, str(turn["status"]))
        return TurnOperationalEvaluationSnapshot(
            "stata-research-agent.operational-turn-evaluation/v1",
            _POLICY,
            revision,
            workspace_id,
            turn_id.value,
            str(turn["status"]),
            str(turn["research_path_id"]),
            str(turn["created_at"]),
            None if turn["terminal_at"] is None else str(turn["terminal_at"]),
            self._configuration(turn_id.value),
            tuple(
                _layer(layer, tuple(metric for metric in metrics if metric.layer == layer))
                for layer in ("L1", "L2", "L3")
            ),
            self._breakdowns(turn_id.value),
        )

    def workspace(self) -> WorkspaceOperationalEvaluationSnapshot:
        workspace_id = str(
            self._connection.execute(
                "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
            ).fetchone()[0]
        )
        revision = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
            ).fetchone()[0]
        )
        turns = tuple(
            self.turn(TurnId(str(row[0])))
            for row in self._connection.execute(
                "SELECT turn_id FROM turns ORDER BY enqueue_ordinal"
            ).fetchall()
        )
        layers = tuple(self._aggregate_layer(layer, turns) for layer in ("L1", "L2", "L3"))
        return WorkspaceOperationalEvaluationSnapshot(
            "stata-research-agent.operational-workspace-evaluation/v1",
            _POLICY,
            revision,
            workspace_id,
            turns,
            layers,
            self._aggregate_breakdowns(turns),
        )

    def _scalar(self, statement: str, turn_id: str) -> int:
        return int(self._connection.execute(statement, (turn_id,)).fetchone()[0])

    def _table_exists(self, table_name: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            is not None
        )

    def _configuration(self, turn_id: str) -> OperationalConfigurationSlice:
        baseline = self._connection.execute(
            """
            SELECT system_prompt_revision, main_skill_name, main_skill_revision,
                   tool_catalog_revision
            FROM turn_context_baselines WHERE turn_id = ?
            """,
            (turn_id,),
        ).fetchone()
        policies = self._connection.execute(
            """
            SELECT DISTINCT policy.policy_revision, policy.provider_kind, policy.model_name
            FROM model_policy_snapshots AS policy
            JOIN context_manifests AS manifest USING (model_policy_snapshot_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ?
            ORDER BY policy.policy_revision, policy.provider_kind, policy.model_name
            """,
            (turn_id,),
        ).fetchall()
        return OperationalConfigurationSlice(
            None if baseline is None else str(baseline["system_prompt_revision"]),
            None if baseline is None else str(baseline["main_skill_name"]),
            None if baseline is None else str(baseline["main_skill_revision"]),
            None if baseline is None else str(baseline["tool_catalog_revision"]),
            tuple(sorted({str(row["policy_revision"]) for row in policies})),
            tuple(sorted({str(row["provider_kind"]) for row in policies})),
            tuple(sorted({str(row["model_name"]) for row in policies})),
        )

    def _grouped_breakdown(
        self,
        *,
        breakdown_id: str,
        layer: str,
        subsystem: str,
        statement: str,
        turn_id: str,
        explanation: str,
        sources: tuple[str, ...],
    ) -> OperationalBreakdown:
        rows = self._connection.execute(statement, (turn_id,)).fetchall()
        return OperationalBreakdown(
            breakdown_id,
            layer,  # type: ignore[arg-type]
            subsystem,
            tuple(
                OperationalBreakdownItem(str(row["category"]), int(row["amount"])) for row in rows
            ),
            explanation,
            sources,
        )

    def _breakdowns(self, turn_id: str) -> tuple[OperationalBreakdown, ...]:
        values = [
            self._grouped_breakdown(
                breakdown_id="l1.gateway.provider_attempt_outcomes",
                layer="L1",
                subsystem="gateway",
                statement="""
                    SELECT state || ':' || COALESCE(error_code, 'none') AS category,
                           COUNT(*) AS amount
                    FROM provider_attempts AS attempt
                    JOIN model_invocations AS invocation USING (model_invocation_id)
                    JOIN steps AS step USING (step_id)
                    WHERE step.turn_id = ?
                    GROUP BY state, COALESCE(error_code, 'none')
                    ORDER BY amount DESC, category
                """,
                turn_id=turn_id,
                explanation="Provider outcomes grouped by durable state and error code.",
                sources=("provider_attempts",),
            ),
            self._grouped_breakdown(
                breakdown_id="l1.tool.operation_outcomes",
                layer="L1",
                subsystem="tool",
                statement="""
                    SELECT operation_kind || ':' || status AS category, COUNT(*) AS amount
                    FROM operations WHERE requested_by_turn_id = ?
                    GROUP BY operation_kind, status ORDER BY amount DESC, category
                """,
                turn_id=turn_id,
                explanation="Tool-side effects grouped by operation kind and durable outcome.",
                sources=("operations",),
            ),
            self._grouped_breakdown(
                breakdown_id="l1.evaluator.findings",
                layer="L1",
                subsystem="evaluator",
                statement="""
                    SELECT finding.severity || ':' || finding.finding_code AS category,
                           COUNT(*) AS amount
                    FROM evaluation_findings AS finding
                    JOIN evaluation_reports AS report USING (evaluation_report_id)
                    JOIN evaluation_requests AS request USING (evaluation_request_id)
                    WHERE request.turn_id = ?
                    GROUP BY finding.severity, finding.finding_code
                    ORDER BY amount DESC, category
                """,
                turn_id=turn_id,
                explanation="Evaluator findings grouped by severity and stable finding code.",
                sources=("evaluation_findings", "evaluation_requests"),
            ),
            self._grouped_breakdown(
                breakdown_id="l2.loop.stop_guard_decisions",
                layer="L2",
                subsystem="agent_loop",
                statement="""
                    SELECT decision || ':' || reason_code AS category, COUNT(*) AS amount
                    FROM stop_guard_decisions WHERE turn_id = ?
                    GROUP BY decision, reason_code ORDER BY amount DESC, category
                """,
                turn_id=turn_id,
                explanation="Loop decisions grouped by Stop Guard action and reason.",
                sources=("stop_guard_decisions",),
            ),
            self._grouped_breakdown(
                breakdown_id="l2.loop.repeated_tool_calls",
                layer="L2",
                subsystem="agent_loop",
                statement="""
                    SELECT requested_tool_name AS category, SUM(amount - 1) AS amount FROM (
                        SELECT call.requested_tool_name, call.arguments_hash,
                               COUNT(*) AS amount
                        FROM tool_calls AS call
                        JOIN assistant_outputs AS output USING (assistant_output_id)
                        JOIN model_invocations AS invocation USING (model_invocation_id)
                        JOIN steps AS step USING (step_id)
                        WHERE step.turn_id = ? AND call.arguments_hash IS NOT NULL
                        GROUP BY call.requested_tool_name, call.arguments_hash
                        HAVING COUNT(*) > 1
                    ) GROUP BY requested_tool_name ORDER BY amount DESC, category
                """,
                turn_id=turn_id,
                explanation=(
                    "Repeated identical calls by Tool. Repetition is diagnostic and may be a "
                    "legitimate retry; it is not automatically a failure."
                ),
                sources=("tool_calls",),
            ),
        ]
        if self._table_exists("turn_outcome_feedback"):
            values.append(
                self._grouped_breakdown(
                    breakdown_id="l3.product.user_outcome_history",
                    layer="L3",
                    subsystem="user_outcome",
                    statement="""
                        SELECT disposition AS category, COUNT(*) AS amount
                        FROM turn_outcome_feedback WHERE turn_id = ?
                        GROUP BY disposition ORDER BY amount DESC, category
                    """,
                    turn_id=turn_id,
                    explanation=(
                        "Append-only explicit user outcome history; current metrics use the latest "
                        "entry only."
                    ),
                    sources=("turn_outcome_feedback",),
                )
            )
        return tuple(values)

    def _metrics(self, turn_id: str, turn_status: str) -> tuple[OperationalMetric, ...]:
        metrics: list[OperationalMetric] = []

        memory_uses = self._scalar(
            """
            SELECT COUNT(*) FROM memory_context_uses AS use
            JOIN context_items AS item ON item.context_item_id = use.context_item_id
            JOIN context_manifests AS manifest
              ON manifest.context_manifest_id = item.context_manifest_id
            JOIN steps AS step ON step.step_id = manifest.step_id
            WHERE step.turn_id = ?
            """,
            turn_id,
        )
        sourced_memory_uses = self._scalar(
            """
            SELECT COUNT(DISTINCT use.context_item_id)
            FROM memory_context_uses AS use
            JOIN memory_revision_sources AS source
              ON source.memory_revision_id = use.memory_revision_id
            JOIN context_items AS item ON item.context_item_id = use.context_item_id
            JOIN context_manifests AS manifest
              ON manifest.context_manifest_id = item.context_manifest_id
            JOIN steps AS step ON step.step_id = manifest.step_id
            WHERE step.turn_id = ?
            """,
            turn_id,
        )
        inactive_memory_uses = self._scalar(
            """
            SELECT COUNT(*) FROM memory_context_uses AS use
            JOIN context_items AS item ON item.context_item_id = use.context_item_id
            JOIN context_manifests AS manifest
              ON manifest.context_manifest_id = item.context_manifest_id
            JOIN steps AS step ON step.step_id = manifest.step_id
            WHERE step.turn_id = ? AND NOT EXISTS (
                SELECT 1 FROM memory_state_history AS state
                WHERE state.memory_item_id = use.memory_item_id
                  AND state.created_revision <= use.created_revision
                  AND state.created_revision = (
                      SELECT MAX(latest.created_revision)
                      FROM memory_state_history AS latest
                      WHERE latest.memory_item_id = use.memory_item_id
                        AND latest.created_revision <= use.created_revision
                  )
                  AND state.lifecycle = 'active'
            )
            """,
            turn_id,
        )
        memory_search_requests = self._scalar(
            """
            SELECT COUNT(*) FROM tool_calls AS call
            JOIN assistant_outputs AS output USING (assistant_output_id)
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND call.requested_tool_name = 'memory.search'
            """,
            turn_id,
        )
        memory_search_successes = self._scalar(
            """
            SELECT COUNT(*) FROM tool_calls AS call
            JOIN canonical_tool_results AS result USING (tool_call_id)
            JOIN assistant_outputs AS output USING (assistant_output_id)
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND call.requested_tool_name = 'memory.search'
              AND result.result_kind = 'success'
            """,
            turn_id,
        )
        memory_search_candidates = self._scalar(
            """
            SELECT COALESCE(SUM(json_array_length(
                       json_extract(result.structured_payload_json, '$.hits')
                   )), 0)
            FROM tool_calls AS call
            JOIN canonical_tool_results AS result USING (tool_call_id)
            JOIN assistant_outputs AS output USING (assistant_output_id)
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND call.requested_tool_name = 'memory.search'
              AND result.result_kind = 'success'
              AND json_type(result.structured_payload_json, '$.hits') = 'array'
            """,
            turn_id,
        )
        memory_open_requests = self._scalar(
            """
            SELECT COUNT(*) FROM tool_calls AS call
            JOIN assistant_outputs AS output USING (assistant_output_id)
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND call.requested_tool_name = 'memory.open'
            """,
            turn_id,
        )
        memory_open_successes = self._scalar(
            """
            SELECT COUNT(*) FROM tool_calls AS call
            JOIN canonical_tool_results AS result USING (tool_call_id)
            JOIN assistant_outputs AS output USING (assistant_output_id)
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND call.requested_tool_name = 'memory.open'
              AND result.result_kind = 'success'
            """,
            turn_id,
        )
        exact_memory_uses = self._scalar(
            """
            SELECT COUNT(*) FROM memory_context_uses AS use
            JOIN context_items AS item USING (context_item_id)
            JOIN context_manifests AS manifest USING (context_manifest_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND use.usage_kind = 'exact'
            """,
            turn_id,
        )
        metrics.extend(
            (
                _count(
                    "l1.memory.search_request_count",
                    "L1",
                    "memory",
                    memory_search_requests,
                    explanation="Explicit Project Memory search requests in this Turn.",
                    sources=("tool_calls",),
                ),
                _ratio(
                    "l1.memory.search_success_rate",
                    "L1",
                    "memory",
                    memory_search_successes,
                    memory_search_requests,
                    explanation="Successful explicit Memory searches per search request.",
                    sources=("tool_calls", "canonical_tool_results"),
                ),
                _count(
                    "l1.memory.search_candidate_count",
                    "L1",
                    "memory",
                    memory_search_candidates,
                    explanation="Total candidate Memory hits returned by successful searches.",
                    sources=("canonical_tool_results",),
                ),
                _count(
                    "l1.memory.open_request_count",
                    "L1",
                    "memory",
                    memory_open_requests,
                    explanation="Exact Memory revision open requests in this Turn.",
                    sources=("tool_calls",),
                ),
                _ratio(
                    "l1.memory.open_success_rate",
                    "L1",
                    "memory",
                    memory_open_successes,
                    memory_open_requests,
                    explanation="Successful exact Memory opens per open request.",
                    sources=("tool_calls", "canonical_tool_results"),
                ),
                _count(
                    "l1.memory.context_use_count",
                    "L1",
                    "memory",
                    memory_uses,
                    explanation="Memory revisions included in model Context during this Turn.",
                    sources=("memory_context_uses",),
                ),
                _ratio(
                    "l1.memory.exact_context_use_rate",
                    "L1",
                    "memory",
                    exact_memory_uses,
                    memory_uses,
                    explanation="Memory Context uses carrying exact revision content.",
                    sources=("memory_context_uses",),
                ),
                _ratio(
                    "l1.memory.source_link_rate",
                    "L1",
                    "memory",
                    sourced_memory_uses,
                    memory_uses,
                    explanation=(
                        "Memory injected into model context retains an authoritative source."
                    ),
                    sources=("memory_context_uses", "memory_revision_sources"),
                    hard_minimum=1.0,
                ),
                _count(
                    "l1.memory.inactive_context_use_count",
                    "L1",
                    "memory",
                    inactive_memory_uses,
                    explanation=(
                        "Retracted or not-yet-active Memory must not enter a later Context."
                    ),
                    sources=("memory_context_uses", "memory_state_history"),
                    hard_zero=True,
                ),
            )
        )

        skill_uses = self._scalar(
            """
            SELECT COUNT(*) FROM skill_context_uses AS use
            JOIN context_items AS item USING (context_item_id)
            JOIN context_manifests AS manifest USING (context_manifest_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ?
            """,
            turn_id,
        )
        exact_skill_uses = self._scalar(
            """
            SELECT COUNT(*) FROM skill_context_uses AS use
            JOIN context_items AS item USING (context_item_id)
            JOIN context_manifests AS manifest USING (context_manifest_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND use.usage_kind = 'exact'
              AND length(use.content_sha256) = 64
              AND length(trim(use.skill_revision)) > 0
            """,
            turn_id,
        )
        inactive_version_uses = self._scalar(
            """
            SELECT COUNT(*) FROM skill_context_uses AS use
            JOIN context_items AS item USING (context_item_id)
            JOIN context_manifests AS manifest USING (context_manifest_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND use.skill_version_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM skill_adoption_history AS adoption
                  WHERE adoption.skill_name = use.skill_name
                    AND adoption.created_revision <= use.created_revision
                    AND adoption.created_revision = (
                        SELECT MAX(latest.created_revision)
                        FROM skill_adoption_history AS latest
                        WHERE latest.skill_name = use.skill_name
                          AND latest.created_revision <= use.created_revision
                    )
                    AND adoption.action_kind IN ('activate', 'rollback')
                    AND adoption.target_skill_version_id = use.skill_version_id
              )
            """,
            turn_id,
        )
        skill_outcomes = self._scalar(
            """
            SELECT COUNT(DISTINCT observation.turn_outcome_feedback_id)
            FROM skill_outcome_observations AS observation
            JOIN skill_context_uses AS use USING (context_item_id)
            JOIN context_items AS item USING (context_item_id)
            JOIN context_manifests AS manifest USING (context_manifest_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ?
            """,
            turn_id,
        )
        metrics.extend(
            (
                _ratio(
                    "l1.skill.exact_identity_rate",
                    "L1",
                    "skill",
                    exact_skill_uses,
                    skill_uses,
                    explanation=(
                        "Skill Context use records an exact revision and content identity."
                    ),
                    sources=("skill_context_uses", "context_items"),
                    hard_minimum=1.0,
                ),
                _count(
                    "l1.skill.inactive_version_context_use_count",
                    "L1",
                    "skill",
                    inactive_version_uses,
                    explanation=(
                        "A versioned workspace Skill must be active at the revision where it "
                        "enters Context."
                    ),
                    sources=("skill_context_uses", "skill_adoption_history"),
                    hard_zero=True,
                ),
                _count(
                    "l1.skill.explicit_outcome_observation_count",
                    "L1",
                    "skill",
                    skill_outcomes,
                    explanation=(
                        "Explicit user outcomes linked to exact Skill use as non-causal "
                        "co-occurrence observations."
                    ),
                    sources=("skill_outcome_observations", "turn_outcome_feedback"),
                ),
            )
        )

        retrieval_sessions = self._scalar(
            "SELECT COUNT(*) FROM knowledge_retrieval_sessions WHERE turn_id = ?",
            turn_id,
        )
        completed_retrieval = self._scalar(
            """
            SELECT COUNT(*) FROM knowledge_retrieval_sessions AS session
            JOIN knowledge_retrieval_session_states AS state USING (
                knowledge_retrieval_session_id
            )
            WHERE session.turn_id = ? AND state.status = 'completed'
            """,
            turn_id,
        )
        used_retrieval = self._scalar(
            """
            SELECT COUNT(DISTINCT session.knowledge_retrieval_session_id)
            FROM knowledge_retrieval_sessions AS session
            JOIN knowledge_node_context_uses AS use
              ON use.retrieval_session_id = session.knowledge_retrieval_session_id
            WHERE session.turn_id = ?
            """,
            turn_id,
        )
        metrics.extend(
            (
                _ratio(
                    "l1.rag.session_completion_rate",
                    "L1",
                    "rag",
                    completed_retrieval,
                    retrieval_sessions,
                    explanation="Real retrieval sessions that reached a durable completed state.",
                    sources=(
                        "knowledge_retrieval_sessions",
                        "knowledge_retrieval_session_states",
                    ),
                ),
                _ratio(
                    "l1.rag.context_utilization_rate",
                    "L1",
                    "rag",
                    used_retrieval,
                    retrieval_sessions,
                    explanation="Retrieval sessions whose selected evidence entered model context.",
                    sources=("knowledge_retrieval_sessions", "knowledge_node_context_uses"),
                ),
            )
        )

        manifests = self._scalar(
            """
            SELECT COUNT(*) FROM context_manifests AS manifest
            JOIN steps AS step ON step.step_id = manifest.step_id
            WHERE step.turn_id = ?
            """,
            turn_id,
        )
        context_items = self._scalar(
            """
            SELECT COUNT(*) FROM context_items AS item
            JOIN context_manifests AS manifest
              ON manifest.context_manifest_id = item.context_manifest_id
            JOIN steps AS step ON step.step_id = manifest.step_id
            WHERE step.turn_id = ?
            """,
            turn_id,
        )
        remote_local_only = self._scalar(
            """
            SELECT COUNT(*) FROM context_items AS item
            JOIN context_manifests AS manifest
              ON manifest.context_manifest_id = item.context_manifest_id
            JOIN steps AS step ON step.step_id = manifest.step_id
            JOIN model_policy_snapshots AS policy
              ON policy.model_policy_snapshot_id = manifest.model_policy_snapshot_id
            WHERE step.turn_id = ?
              AND item.remote_transmission_class = 'local_only'
              AND policy.provider_kind != 'local'
            """,
            turn_id,
        )
        metrics.extend(
            (
                _count(
                    "l1.context.manifest_count",
                    "L1",
                    "context",
                    manifests,
                    explanation="Frozen Context manifests created by real Steps.",
                    sources=("context_manifests", "steps"),
                ),
                _ratio(
                    "l1.context.mean_items_per_manifest",
                    "L1",
                    "context",
                    context_items,
                    manifests,
                    explanation="Observed Context assembly size; this is not a quality score.",
                    sources=("context_items", "context_manifests"),
                ),
                _count(
                    "l1.context.remote_local_only_violation_count",
                    "L1",
                    "context",
                    remote_local_only,
                    explanation="Local-only Context items must not be assembled for remote models.",
                    sources=("context_items", "model_policy_snapshots"),
                    hard_zero=True,
                ),
            )
        )

        plan_revisions = self._scalar(
            "SELECT COUNT(*) FROM plan_revisions WHERE created_by_turn_id = ?",
            turn_id,
        )
        formal_runs = self._scalar(
            """
            SELECT COUNT(*) FROM stata_runs AS run
            JOIN operations AS operation ON operation.operation_id = run.operation_id
            WHERE operation.requested_by_turn_id = ?
            """,
            turn_id,
        )
        aligned_runs = self._scalar(
            """
            SELECT COUNT(*) FROM stata_runs AS run
            JOIN operations AS operation ON operation.operation_id = run.operation_id
            JOIN stata_run_plan_bindings AS binding ON binding.stata_run_id = run.stata_run_id
            WHERE operation.requested_by_turn_id = ? AND binding.adherence_verdict = 'matches'
            """,
            turn_id,
        )
        metrics.extend(
            (
                _count(
                    "l1.plan.revision_count",
                    "L1",
                    "plan",
                    plan_revisions,
                    explanation="Semantic Plan revisions created during this Turn.",
                    sources=("plan_revisions",),
                ),
                _ratio(
                    "l1.plan.formal_run_alignment_rate",
                    "L1",
                    "plan",
                    aligned_runs,
                    formal_runs,
                    explanation="Formal Stata Runs that match their frozen Plan node binding.",
                    sources=("stata_runs", "stata_run_plan_bindings"),
                    hard_minimum=1.0,
                ),
            )
        )

        tool_calls = self._scalar(
            """
            SELECT COUNT(*) FROM tool_calls AS call
            JOIN assistant_outputs AS output USING (assistant_output_id)
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ?
            """,
            turn_id,
        )
        rejected_calls = self._scalar(
            """
            SELECT COUNT(*) FROM tool_calls AS call
            JOIN assistant_outputs AS output USING (assistant_output_id)
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND call.proposal_status = 'rejected'
            """,
            turn_id,
        )
        operations = self._scalar(
            "SELECT COUNT(*) FROM operations WHERE requested_by_turn_id = ?",
            turn_id,
        )
        completed_operations = self._scalar(
            """
            SELECT COUNT(*) FROM operations
            WHERE requested_by_turn_id = ? AND status = 'completed'
            """,
            turn_id,
        )
        operations_without_admission = self._scalar(
            """
            SELECT COUNT(*) FROM operations AS operation
            LEFT JOIN tool_admissions AS admission
              ON admission.operation_id = operation.operation_id
            WHERE operation.requested_by_turn_id = ?
              AND operation.tool_call_id IS NOT NULL
              AND admission.operation_id IS NULL
            """,
            turn_id,
        )
        duplicate_calls = self._scalar(
            """
            SELECT COALESCE(SUM(grouped.amount - 1), 0) FROM (
                SELECT call.requested_tool_name, call.arguments_hash, COUNT(*) AS amount
                FROM tool_calls AS call
                JOIN assistant_outputs AS output USING (assistant_output_id)
                JOIN model_invocations AS invocation USING (model_invocation_id)
                JOIN steps AS step USING (step_id)
                WHERE step.turn_id = ? AND call.arguments_hash IS NOT NULL
                GROUP BY call.requested_tool_name, call.arguments_hash
                HAVING COUNT(*) > 1
            ) AS grouped
            """,
            turn_id,
        )
        metrics.extend(
            (
                _ratio(
                    "l1.tool.proposal_rejection_rate",
                    "L1",
                    "tool",
                    rejected_calls,
                    tool_calls,
                    explanation="Observed Tool proposals rejected before execution.",
                    sources=("tool_calls",),
                ),
                _ratio(
                    "l1.tool.operation_completion_rate",
                    "L1",
                    "tool",
                    completed_operations,
                    operations,
                    explanation="Operations that reached a durable completed state.",
                    sources=("operations",),
                ),
                _count(
                    "l1.tool.operation_without_admission_count",
                    "L1",
                    "tool",
                    operations_without_admission,
                    explanation="Tool-originated Operations may not bypass JIT Admission.",
                    sources=("operations", "tool_admissions"),
                    hard_zero=True,
                ),
                _ratio(
                    "l1.tool.duplicate_action_rate",
                    "L1",
                    "tool",
                    duplicate_calls,
                    tool_calls,
                    explanation="Repeated identical Tool name and canonical argument hashes.",
                    sources=("tool_calls",),
                ),
            )
        )

        evaluation_reports = self._scalar(
            """
            SELECT COUNT(*) FROM evaluation_reports AS report
            JOIN evaluation_requests AS request USING (evaluation_request_id)
            WHERE request.turn_id = ?
            """,
            turn_id,
        )
        successful_without_valid_stop = self._scalar(
            """
            SELECT COUNT(*) FROM turns AS turn
            WHERE turn.turn_id = ? AND turn.status = 'succeeded'
              AND EXISTS (
                  SELECT 1 FROM steps AS step WHERE step.turn_id = turn.turn_id
              )
              AND NOT EXISTS (
                SELECT 1 FROM stop_guard_decisions AS decision
                JOIN goal_coverages AS coverage USING (goal_coverage_id)
                WHERE decision.turn_id = turn.turn_id
                  AND decision.decision = 'terminate'
                  AND decision.terminal_disposition = 'succeed'
                  AND coverage.coverage_status = 'satisfied'
            )
            """,
            turn_id,
        )
        terminate_without_coverage = self._scalar(
            """
            SELECT COUNT(*) FROM stop_guard_decisions AS decision
            JOIN goal_coverages AS coverage USING (goal_coverage_id)
            WHERE decision.turn_id = ? AND decision.decision = 'terminate'
              AND decision.terminal_disposition = 'succeed'
              AND coverage.coverage_status != 'satisfied'
            """,
            turn_id,
        )
        metrics.extend(
            (
                _count(
                    "l1.evaluator.report_count",
                    "L1",
                    "evaluator",
                    evaluation_reports,
                    explanation="Runtime Evaluator reports emitted for the real Turn.",
                    sources=("evaluation_requests", "evaluation_reports"),
                ),
                _count(
                    "l1.evaluator.success_without_valid_stop_count",
                    "L1",
                    "evaluator",
                    successful_without_valid_stop,
                    explanation="Succeeded Turns require a terminating Stop Guard with coverage.",
                    sources=("turns", "stop_guard_decisions", "goal_coverages"),
                    hard_zero=True,
                ),
                _count(
                    "l1.evaluator.terminate_without_coverage_count",
                    "L1",
                    "evaluator",
                    terminate_without_coverage,
                    explanation="Stop Guard may not declare success with incomplete obligations.",
                    sources=("stop_guard_decisions", "goal_coverages"),
                    hard_zero=True,
                ),
            )
        )

        invocations = self._scalar(
            """
            SELECT COUNT(*) FROM model_invocations AS invocation
            JOIN steps AS step USING (step_id) WHERE step.turn_id = ?
            """,
            turn_id,
        )
        completed_invocations = self._scalar(
            """
            SELECT COUNT(*) FROM model_invocations AS invocation
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND invocation.status = 'completed'
            """,
            turn_id,
        )
        attempts = self._scalar(
            """
            SELECT COUNT(*) FROM provider_attempts AS attempt
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id) WHERE step.turn_id = ?
            """,
            turn_id,
        )
        retries = self._scalar(
            """
            SELECT COUNT(*) FROM provider_attempts AS attempt
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND attempt.attempt_ordinal > 1
            """,
            turn_id,
        )
        unknown_usage = self._scalar(
            """
            SELECT COUNT(*) FROM provider_attempts AS attempt
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND (
                attempt.usage_kind IS NULL OR attempt.usage_kind = 'unknown'
                OR attempt.input_tokens IS NULL OR attempt.output_tokens IS NULL
            )
            """,
            turn_id,
        )
        delivery_unknown = self._scalar(
            """
            SELECT COUNT(*) FROM provider_attempts AS attempt
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            WHERE step.turn_id = ? AND attempt.state = 'delivery_unknown'
            """,
            turn_id,
        )
        metrics.extend(
            (
                _ratio(
                    "l1.gateway.invocation_completion_rate",
                    "L1",
                    "gateway",
                    completed_invocations,
                    invocations,
                    explanation="Logical Model Invocations completed with an Assistant Output.",
                    sources=("model_invocations", "assistant_outputs"),
                ),
                _ratio(
                    "l1.gateway.retry_rate",
                    "L1",
                    "gateway",
                    retries,
                    attempts,
                    explanation="Provider Attempts beyond the first Attempt per Invocation.",
                    sources=("provider_attempts",),
                ),
                _ratio(
                    "l1.gateway.usage_accounting_completeness",
                    "L1",
                    "gateway",
                    attempts - unknown_usage,
                    attempts,
                    explanation="Provider Attempts with usable token accounting.",
                    sources=("provider_attempts",),
                ),
                _count(
                    "l1.gateway.delivery_unknown_count",
                    "L1",
                    "gateway",
                    delivery_unknown,
                    explanation="Provider dispatches whose delivery outcome is unresolved.",
                    sources=("provider_attempts",),
                    warn_above_zero=True,
                ),
            )
        )

        steps = self._scalar("SELECT COUNT(*) FROM steps WHERE turn_id = ?", turn_id)
        completed_steps = self._scalar(
            "SELECT COUNT(*) FROM steps WHERE turn_id = ? AND status = 'completed'",
            turn_id,
        )
        waiting_barrier_violations = self._scalar(
            """
            SELECT COUNT(*) FROM waiting_requests AS waiting
            WHERE waiting.turn_id = ? AND (
                EXISTS (
                    SELECT 1 FROM steps AS step
                    WHERE step.turn_id = waiting.turn_id
                      AND step.created_revision > waiting.created_revision
                      AND (
                          waiting.terminal_revision IS NULL
                          OR step.created_revision < waiting.terminal_revision
                      )
                ) OR EXISTS (
                    SELECT 1 FROM tool_admissions AS admission
                    JOIN tool_calls AS call USING (tool_call_id)
                    JOIN assistant_outputs AS output USING (assistant_output_id)
                    JOIN model_invocations AS invocation USING (model_invocation_id)
                    JOIN steps AS step USING (step_id)
                    WHERE step.turn_id = waiting.turn_id
                      AND admission.admitted_revision > waiting.created_revision
                      AND (
                          waiting.terminal_revision IS NULL
                          OR admission.admitted_revision < waiting.terminal_revision
                      )
                )
            )
            """,
            turn_id,
        )
        failed_operations = self._scalar(
            """
            SELECT COUNT(*) FROM operations
            WHERE requested_by_turn_id = ? AND status = 'failed'
            """,
            turn_id,
        )
        recovered_failures = self._scalar(
            """
            SELECT COUNT(*) FROM operations AS failed
            WHERE failed.requested_by_turn_id = ? AND failed.status = 'failed'
              AND EXISTS (
                  SELECT 1 FROM steps AS step
                  WHERE step.turn_id = failed.requested_by_turn_id
                    AND step.created_revision > failed.terminal_revision
                    AND step.status = 'completed'
              )
            """,
            turn_id,
        )
        metrics.extend(
            (
                _ratio(
                    "l2.loop.step_completion_rate",
                    "L2",
                    "agent_loop",
                    completed_steps,
                    steps,
                    explanation="Steps that completed after Context, model and Tool processing.",
                    sources=("steps",),
                ),
                _ratio(
                    "l2.loop.failure_to_progress_rate",
                    "L2",
                    "agent_loop",
                    recovered_failures,
                    failed_operations,
                    explanation=(
                        "Failed Operations followed by a later completed Step; this is a progress "
                        "proxy, not a research-quality judgment."
                    ),
                    sources=("operations", "steps"),
                ),
                _count(
                    "l2.loop.waiting_barrier_violation_count",
                    "L2",
                    "agent_loop",
                    waiting_barrier_violations,
                    explanation="Waiting forbids new Model Steps and Tool Admissions.",
                    sources=("waiting_requests", "steps", "tool_admissions"),
                    hard_zero=True,
                ),
                _ratio(
                    "l2.loop.duplicate_action_rate",
                    "L2",
                    "agent_loop",
                    duplicate_calls,
                    tool_calls,
                    explanation="Repeated identical actions across the Turn trajectory.",
                    sources=("tool_calls",),
                ),
                _count(
                    "l2.loop.success_contract_violation_count",
                    "L2",
                    "agent_loop",
                    successful_without_valid_stop,
                    explanation="A successful Loop must finish through evidence-aware Stop Guard.",
                    sources=("turns", "stop_guard_decisions", "goal_coverages"),
                    hard_zero=True,
                ),
            )
        )

        usage = self._connection.execute(
            """
            SELECT SUM(COALESCE(attempt.input_tokens, 0)) AS input_tokens,
                   SUM(COALESCE(attempt.output_tokens, 0)) AS output_tokens,
                   SUM(COALESCE(attempt.cached_input_tokens, 0)) AS cached_input_tokens,
                   SUM(
                       CASE WHEN terminal.committed_at IS NULL THEN 0.0
                            ELSE MAX(
                                0.0,
                                (julianday(terminal.committed_at)
                                 - julianday(created.committed_at)) * 86400.0
                            ) END
                   ) AS provider_seconds
            FROM provider_attempts AS attempt
            JOIN model_invocations AS invocation USING (model_invocation_id)
            JOIN steps AS step USING (step_id)
            JOIN workspace_commits AS created
              ON created.workspace_revision = attempt.created_revision
            LEFT JOIN workspace_commits AS terminal
              ON terminal.workspace_revision = attempt.terminal_revision
            WHERE step.turn_id = ?
            """,
            (turn_id,),
        ).fetchone()
        tool_seconds = float(
            self._connection.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN terminal.committed_at IS NULL THEN 0.0
                         ELSE MAX(
                             0.0,
                             (julianday(terminal.committed_at)
                              - julianday(created.committed_at)) * 86400.0
                         ) END
                ), 0.0)
                FROM operations AS operation
                JOIN workspace_commits AS created
                  ON created.workspace_revision = operation.created_revision
                LEFT JOIN workspace_commits AS terminal
                  ON terminal.workspace_revision = operation.terminal_revision
                WHERE operation.requested_by_turn_id = ?
                """,
                (turn_id,),
            ).fetchone()[0]
        )
        input_tokens = None if attempts == 0 else float(usage["input_tokens"] or 0)
        output_tokens = None if attempts == 0 else float(usage["output_tokens"] or 0)
        provider_seconds = None if attempts == 0 else float(usage["provider_seconds"] or 0)
        cached_tokens = 0 if attempts == 0 else int(usage["cached_input_tokens"] or 0)
        metrics.extend(
            (
                _observed(
                    "l2.efficiency.input_tokens_observed",
                    "L2",
                    "efficiency",
                    input_tokens,
                    unit="tokens",
                    explanation="Observed provider input tokens consumed by the real Turn.",
                    sources=("provider_attempts",),
                ),
                _observed(
                    "l2.efficiency.output_tokens_observed",
                    "L2",
                    "efficiency",
                    output_tokens,
                    unit="tokens",
                    explanation="Observed provider output tokens consumed by the real Turn.",
                    sources=("provider_attempts",),
                ),
                _ratio(
                    "l2.efficiency.cached_input_token_rate",
                    "L2",
                    "efficiency",
                    cached_tokens,
                    int(input_tokens or 0),
                    explanation=(
                        "Observed cached input tokens divided by all observed input tokens."
                    ),
                    sources=("provider_attempts",),
                ),
                _observed(
                    "l2.efficiency.provider_duration_observed_seconds",
                    "L2",
                    "efficiency",
                    provider_seconds,
                    unit="seconds",
                    explanation=(
                        "Sum of observed Provider Attempt durations; overlapping attempts are "
                        "not wall-clock duration."
                    ),
                    sources=("provider_attempts", "workspace_commits"),
                ),
                _observed(
                    "l2.efficiency.tool_duration_observed_seconds",
                    "L2",
                    "efficiency",
                    None if operations == 0 else tool_seconds,
                    unit="seconds",
                    explanation=(
                        "Sum of observed Operation durations; overlapping Operations are not "
                        "wall-clock duration."
                    ),
                    sources=("operations", "workspace_commits"),
                ),
            )
        )

        results = self._scalar(
            "SELECT COUNT(*) FROM results WHERE created_by_turn_id = ?",
            turn_id,
        )
        stata_results = self._scalar(
            """
            SELECT COUNT(*) FROM results AS result
            JOIN stata_runs AS run ON run.stata_run_id = result.producing_stata_run_id
            WHERE result.created_by_turn_id = ? AND run.run_status = 'succeeded'
            """,
            turn_id,
        )
        documents = self._scalar(
            "SELECT COUNT(*) FROM document_revisions WHERE created_by_turn_id = ?",
            turn_id,
        )
        delivered_documents = self._scalar(
            """
            SELECT COUNT(*) FROM document_revisions AS document
            JOIN delivery_gate_reports AS gate
              ON gate.document_revision_id = document.document_revision_id
            WHERE document.created_by_turn_id = ? AND gate.verdict = 'pass'
            """,
            turn_id,
        )
        evidence_uses = self._scalar(
            """
            SELECT COUNT(*)
            FROM statistical_evidence_use_validation_receipts AS receipt
            JOIN document_revisions AS document
              ON document.created_revision = receipt.created_revision
            WHERE document.created_by_turn_id = ?
              AND receipt.purpose = 'document_delivery'
              AND receipt.verdict = 'eligible'
            """,
            turn_id,
        )
        unresolved_operations = self._scalar(
            """
            SELECT COUNT(*) FROM operations WHERE requested_by_turn_id = ?
              AND status IN (
                  'interrupted', 'completed_unreconciled', 'outcome_unknown',
                  'integrity_violation'
              )
            """,
            turn_id,
        )
        replay_ready_runs = self._scalar(
            """
            SELECT COUNT(*) FROM stata_runs AS run
            JOIN operations AS operation ON operation.operation_id = run.operation_id
            JOIN executable_sources AS source
              ON source.executable_source_id = run.executable_source_id
            JOIN data_versions AS data
              ON data.data_version_id = run.input_data_version_id
            JOIN environment_snapshots AS environment
              ON environment.environment_snapshot_id = run.environment_snapshot_id
            WHERE operation.requested_by_turn_id = ?
            """,
            turn_id,
        )
        outcome_feedback = None
        if self._table_exists("turn_outcome_feedback"):
            outcome_feedback = self._connection.execute(
                """
                SELECT disposition, issue_codes_json
                FROM turn_outcome_feedback
                WHERE turn_id = ?
                ORDER BY created_revision DESC
                LIMIT 1
                """,
                (turn_id,),
            ).fetchone()
        has_outcome_feedback = int(outcome_feedback is not None)
        accepted_outcome = int(
            outcome_feedback is not None and str(outcome_feedback["disposition"]) == "accepted"
        )
        revision_requested = int(
            outcome_feedback is not None
            and str(outcome_feedback["disposition"]) in {"needs_revision", "rejected"}
        )
        metrics.extend(
            (
                _ratio(
                    "l3.product.stata_result_provenance_rate",
                    "L3",
                    "data_fidelity",
                    stata_results,
                    results,
                    explanation="Formal Results backed by a succeeded Stata Run.",
                    sources=("results", "stata_runs"),
                    hard_minimum=1.0,
                ),
                _ratio(
                    "l3.product.word_delivery_gate_pass_rate",
                    "L3",
                    "word_delivery",
                    delivered_documents,
                    documents,
                    explanation="Generated document revisions accepted by the Delivery Gate.",
                    sources=("document_revisions", "delivery_gate_reports"),
                    hard_minimum=1.0,
                ),
                _count(
                    "l3.product.document_evidence_receipt_count",
                    "L3",
                    "data_fidelity",
                    evidence_uses,
                    explanation="Eligible statistical evidence receipts used for delivery.",
                    sources=("statistical_evidence_use_validation_receipts",),
                ),
                _ratio(
                    "l3.product.replay_ready_stata_run_rate",
                    "L3",
                    "reproducibility",
                    replay_ready_runs,
                    formal_runs,
                    explanation=(
                        "Stata Runs retaining executable source, input Data Version and "
                        "Environment; "
                        "this measures replay readiness, not an executed clean rerun."
                    ),
                    sources=(
                        "stata_runs",
                        "executable_sources",
                        "data_versions",
                        "environment_snapshots",
                    ),
                    hard_minimum=1.0,
                ),
                _count(
                    "l3.product.unresolved_operation_count",
                    "L3",
                    "recoverability",
                    unresolved_operations,
                    explanation="Operations still requiring recovery or reconciliation.",
                    sources=("operations",),
                    warn_above_zero=True,
                ),
                _count(
                    "l3.product.waiting_control_violation_count",
                    "L3",
                    "researcher_control",
                    waiting_barrier_violations,
                    explanation="Researcher decision boundaries violated during real use.",
                    sources=("waiting_requests", "steps", "tool_admissions"),
                    hard_zero=True,
                ),
                _count(
                    "l3.product.turn_success_count",
                    "L3",
                    "task_completion",
                    int(turn_status == "succeeded"),
                    explanation="Observed terminal success; quality remains a separate judgment.",
                    sources=("turns",),
                ),
                _ratio(
                    "l3.product.explicit_user_acceptance_rate",
                    "L3",
                    "user_outcome",
                    accepted_outcome,
                    has_outcome_feedback,
                    explanation=(
                        "Latest explicit user disposition for completed real-use Turns. "
                        "This is a usefulness signal, not an objective research-quality score."
                    ),
                    sources=("turn_outcome_feedback",),
                ),
                _count(
                    "l3.product.explicit_revision_request_count",
                    "L3",
                    "user_outcome",
                    revision_requested,
                    explanation=(
                        "Turns whose latest explicit user disposition requested revision or "
                        "rejected the outcome."
                    ),
                    sources=("turn_outcome_feedback",),
                ),
            )
        )
        return tuple(metrics)

    @staticmethod
    def _aggregate_layer(
        layer: str, turns: tuple[TurnOperationalEvaluationSnapshot, ...]
    ) -> OperationalLayerSnapshot:
        grouped: dict[str, list[OperationalMetric]] = defaultdict(list)
        for turn in turns:
            source = next(item for item in turn.layers if item.layer == layer)
            for metric in source.metrics:
                grouped[metric.metric_id].append(metric)
        aggregated: list[OperationalMetric] = []
        status_order = {
            "fail": 5,
            "warn": 4,
            "unknown": 3,
            "pass": 2,
            "observed": 1,
            "not_applicable": 0,
        }
        for metric_id, values in sorted(grouped.items()):
            exemplar = values[0]
            applicable = [item for item in values if item.status != "not_applicable"]
            status = max(values, key=lambda item: status_order[item.status]).status
            numerator = sum(item.numerator or 0.0 for item in applicable) if applicable else 0.0
            denominators = [item.denominator for item in applicable]
            denominator_values = [item for item in denominators if item is not None]
            if applicable and len(denominator_values) == len(applicable):
                denominator: float | None = sum(denominator_values)
                value = numerator / denominator if denominator else None
            elif applicable:
                denominator = None
                value = sum(item.value or 0.0 for item in applicable)
            else:
                denominator = 0.0
                value = None
            aggregated.append(
                OperationalMetric(
                    metric_id,
                    exemplar.layer,
                    exemplar.subsystem,
                    status,
                    value,
                    numerator,
                    denominator,
                    exemplar.unit,
                    exemplar.explanation,
                    tuple(sorted({table for item in values for table in item.source_tables})),
                )
            )
        return _layer(layer, tuple(aggregated))

    @staticmethod
    def _aggregate_breakdowns(
        turns: tuple[TurnOperationalEvaluationSnapshot, ...],
    ) -> tuple[OperationalBreakdown, ...]:
        grouped: dict[str, list[OperationalBreakdown]] = defaultdict(list)
        for turn in turns:
            for breakdown in turn.breakdowns:
                grouped[breakdown.breakdown_id].append(breakdown)
        result: list[OperationalBreakdown] = []
        for breakdown_id, values in sorted(grouped.items()):
            counts: dict[str, int] = defaultdict(int)
            for value in values:
                for item in value.items:
                    counts[item.key] += item.count
            exemplar = values[0]
            result.append(
                OperationalBreakdown(
                    breakdown_id,
                    exemplar.layer,
                    exemplar.subsystem,
                    tuple(
                        OperationalBreakdownItem(key, count)
                        for key, count in sorted(
                            counts.items(), key=lambda item: (-item[1], item[0])
                        )
                    ),
                    exemplar.explanation,
                    tuple(sorted({table for value in values for table in value.source_tables})),
                )
            )
        return tuple(result)
