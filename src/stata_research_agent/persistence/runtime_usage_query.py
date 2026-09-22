"""Read-only Turn usage aggregation over authoritative Workspace facts."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from stata_research_agent.application.runtime_usage import (
    CountBudgetUsage,
    ProviderAttemptUsage,
    ProviderPricingCatalog,
    RuntimeBudgetUsage,
    ToolOperationUsage,
    TurnUsageSnapshot,
    UsageQuality,
    estimate_provider_cost,
)
from stata_research_agent.domain.identifiers import TurnId


def _duration_seconds(started_at: str, terminal_at: str | None) -> float | None:
    if terminal_at is None:
        return None
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    terminal = datetime.fromisoformat(terminal_at.replace("Z", "+00:00"))
    return max(0.0, (terminal - started).total_seconds())


def _quality(value: object) -> UsageQuality:
    if value == "exact":
        return "exact"
    if value == "estimated":
        return "estimated"
    return "unknown"


class SqliteRuntimeUsageQuery:
    def __init__(
        self, connection: sqlite3.Connection, pricing_catalog: ProviderPricingCatalog
    ) -> None:
        self._connection = connection
        self._pricing = pricing_catalog

    def turn(self, turn_id: TurnId) -> TurnUsageSnapshot:
        turn = self._connection.execute(
            "SELECT status FROM turns WHERE turn_id = ?", (turn_id.value,)
        ).fetchone()
        if turn is None:
            raise ValueError("Turn does not exist")
        authoritative_revision = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
            ).fetchone()[0]
        )
        rows = self._connection.execute(
            """
            SELECT step.step_id, step.step_ordinal, invocation.model_invocation_id,
                   attempt.provider_attempt_id, attempt.attempt_ordinal, attempt.state,
                   attempt.usage_kind, attempt.input_tokens, attempt.output_tokens,
                   attempt.cached_input_tokens, attempt.uncached_input_tokens,
                   attempt.error_code, outbound.provider_profile,
                   policy.provider_kind, policy.model_name,
                   created.committed_at AS created_at,
                   terminal.committed_at AS terminal_at
            FROM steps AS step
            JOIN context_manifests AS manifest ON manifest.step_id = step.step_id
            JOIN model_policy_snapshots AS policy
              ON policy.model_policy_snapshot_id = manifest.model_policy_snapshot_id
            JOIN model_invocations AS invocation ON invocation.step_id = step.step_id
            JOIN provider_attempts AS attempt
              ON attempt.model_invocation_id = invocation.model_invocation_id
            JOIN outbound_material_records AS outbound
              ON outbound.provider_attempt_id = attempt.provider_attempt_id
            JOIN workspace_commits AS created
              ON created.workspace_revision = attempt.created_revision
            LEFT JOIN workspace_commits AS terminal
              ON terminal.workspace_revision = attempt.terminal_revision
            WHERE step.turn_id = ?
            ORDER BY step.step_ordinal, attempt.attempt_ordinal
            """,
            (turn_id.value,),
        ).fetchall()
        first_profiles: dict[str, str] = {}
        attempts: list[ProviderAttemptUsage] = []
        for row in rows:
            invocation_id = str(row["model_invocation_id"])
            profile = str(row["provider_profile"])
            first_profile = first_profiles.setdefault(invocation_id, profile)
            attempts.append(
                ProviderAttemptUsage(
                    provider_attempt_id=str(row["provider_attempt_id"]),
                    model_invocation_id=invocation_id,
                    step_id=str(row["step_id"]),
                    step_ordinal=int(row["step_ordinal"]),
                    attempt_ordinal=int(row["attempt_ordinal"]),
                    provider_profile=profile,
                    provider_kind=str(row["provider_kind"]),
                    model_name=str(row["model_name"]),
                    state=str(row["state"]),
                    usage_quality=_quality(row["usage_kind"]),
                    input_tokens=None
                    if row["input_tokens"] is None
                    else int(row["input_tokens"]),
                    output_tokens=None
                    if row["output_tokens"] is None
                    else int(row["output_tokens"]),
                    cached_input_tokens=None
                    if row["cached_input_tokens"] is None
                    else int(row["cached_input_tokens"]),
                    uncached_input_tokens=None
                    if row["uncached_input_tokens"] is None
                    else int(row["uncached_input_tokens"]),
                    is_retry=int(row["attempt_ordinal"]) > 1,
                    is_fallback=profile != first_profile,
                    observed_duration_seconds=_duration_seconds(
                        str(row["created_at"]),
                        None if row["terminal_at"] is None else str(row["terminal_at"]),
                    ),
                    error_code=None if row["error_code"] is None else str(row["error_code"]),
                )
            )

        operation_rows = self._connection.execute(
            """
            SELECT operation.operation_id, operation.tool_call_id,
                   tool.requested_tool_name AS tool_name,
                   operation.operation_kind, operation.status,
                   COUNT(operation_attempt.operation_attempt_id) AS attempt_count,
                   created.committed_at AS created_at,
                   terminal.committed_at AS terminal_at
            FROM operations AS operation
            JOIN workspace_commits AS created
              ON created.workspace_revision = operation.created_revision
            LEFT JOIN workspace_commits AS terminal
              ON terminal.workspace_revision = operation.terminal_revision
            LEFT JOIN tool_calls AS tool ON tool.tool_call_id = operation.tool_call_id
            LEFT JOIN operation_attempts AS operation_attempt
              ON operation_attempt.operation_id = operation.operation_id
            WHERE operation.requested_by_turn_id = ?
            GROUP BY operation.operation_id
            ORDER BY operation.created_revision, operation.operation_id
            """,
            (turn_id.value,),
        ).fetchall()
        operations = tuple(
            ToolOperationUsage(
                operation_id=str(row["operation_id"]),
                tool_call_id=None if row["tool_call_id"] is None else str(row["tool_call_id"]),
                tool_name=None if row["tool_name"] is None else str(row["tool_name"]),
                operation_kind=str(row["operation_kind"]),
                status=str(row["status"]),
                attempt_count=int(row["attempt_count"]),
                observed_duration_seconds=_duration_seconds(
                    str(row["created_at"]),
                    None if row["terminal_at"] is None else str(row["terminal_at"]),
                ),
            )
            for row in operation_rows
        )
        count_budget = self._count_budget(turn_id)
        runtime_budget = self._runtime_budget(turn_id)
        attempts_tuple = tuple(attempts)
        unknown = sum(
            attempt.usage_quality == "unknown"
            or attempt.input_tokens is None
            or attempt.output_tokens is None
            for attempt in attempts_tuple
        )
        if unknown:
            token_quality: UsageQuality = "unknown"
        elif any(attempt.usage_quality == "estimated" for attempt in attempts_tuple):
            token_quality = "estimated"
        else:
            token_quality = "exact"
        return TurnUsageSnapshot(
            authoritative_revision=authoritative_revision,
            turn_id=turn_id.value,
            turn_status=str(turn["status"]),
            step_count=int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM steps WHERE turn_id = ?", (turn_id.value,)
                ).fetchone()[0]
            ),
            provider_attempts=attempts_tuple,
            tool_operations=operations,
            input_tokens_observed=sum(attempt.input_tokens or 0 for attempt in attempts_tuple),
            output_tokens_observed=sum(attempt.output_tokens or 0 for attempt in attempts_tuple),
            cached_input_tokens_observed=sum(
                attempt.cached_input_tokens or 0 for attempt in attempts_tuple
            ),
            uncached_input_tokens_observed=sum(
                attempt.uncached_input_tokens
                if attempt.uncached_input_tokens is not None
                else max(0, (attempt.input_tokens or 0) - (attempt.cached_input_tokens or 0))
                for attempt in attempts_tuple
            ),
            unknown_usage_attempt_count=unknown,
            token_quality=token_quality,
            retry_count=sum(attempt.is_retry for attempt in attempts_tuple),
            fallback_count=sum(attempt.is_fallback for attempt in attempts_tuple),
            provider_duration_observed_seconds=sum(
                attempt.observed_duration_seconds or 0.0 for attempt in attempts_tuple
            ),
            tool_duration_observed_seconds=sum(
                operation.observed_duration_seconds or 0.0 for operation in operations
            ),
            count_budget=count_budget,
            runtime_budget=runtime_budget,
            monetary_estimate=estimate_provider_cost(attempts_tuple, self._pricing),
        )

    def _count_budget(self, turn_id: TurnId) -> CountBudgetUsage | None:
        row = self._connection.execute(
            """
            SELECT policy.policy_revision, policy.max_steps,
                   policy.max_tool_admissions, policy.max_provider_attempts_per_turn,
                   account.used_steps, account.used_tool_admissions,
                   account.used_provider_attempts
            FROM turn_budget_accounts AS account
            JOIN budget_policy_snapshots AS policy
              ON policy.budget_policy_snapshot_id = account.budget_policy_snapshot_id
            WHERE account.turn_id = ?
            """,
            (turn_id.value,),
        ).fetchone()
        if row is None:
            return None
        return CountBudgetUsage(
            policy_revision=str(row["policy_revision"]),
            max_steps=int(row["max_steps"]),
            used_steps=int(row["used_steps"]),
            remaining_steps=max(0, int(row["max_steps"]) - int(row["used_steps"])),
            max_tool_admissions=int(row["max_tool_admissions"]),
            used_tool_admissions=int(row["used_tool_admissions"]),
            remaining_tool_admissions=max(
                0, int(row["max_tool_admissions"]) - int(row["used_tool_admissions"])
            ),
            max_provider_attempts=int(row["max_provider_attempts_per_turn"]),
            used_provider_attempts=int(row["used_provider_attempts"]),
            remaining_provider_attempts=max(
                0,
                int(row["max_provider_attempts_per_turn"])
                - int(row["used_provider_attempts"]),
            ),
        )

    def _runtime_budget(self, turn_id: TurnId) -> RuntimeBudgetUsage | None:
        row = self._connection.execute(
            """
            SELECT policy_revision, max_wall_clock_seconds,
                   consumed_wall_clock_seconds, segment_count
            FROM turn_runtime_budgets WHERE turn_id = ?
            """,
            (turn_id.value,),
        ).fetchone()
        if row is None:
            return None
        maximum = float(row["max_wall_clock_seconds"])
        consumed = float(row["consumed_wall_clock_seconds"])
        return RuntimeBudgetUsage(
            policy_revision=str(row["policy_revision"]),
            max_wall_clock_seconds=maximum,
            consumed_wall_clock_seconds=consumed,
            remaining_wall_clock_seconds=max(0.0, maximum - consumed),
            segment_count=int(row["segment_count"]),
        )
