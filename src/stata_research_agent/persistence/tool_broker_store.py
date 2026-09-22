"""SQLite implementation of Tool Contract registration, planning, and JIT Admission."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from stata_research_agent.application.tool_broker import (
    AdmitToolCallCommand,
    CreateDispatchPlanCommand,
    DispatchPlanIdentity,
    DispatchPlanOutcome,
    ExecutorExceptionOutcome,
    PreparedToolCall,
    RecordExecutorExceptionCommand,
    RegisteredToolContract,
    RegisterToolContractCommand,
    ResourceClaimTemplate,
    ScheduledToolCall,
    ToolAdmissionIdentity,
    ToolAdmissionOutcome,
    ToolContractDefinition,
)
from stata_research_agent.domain.identifiers import ToolCallId, ToolContractId
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


class SqliteToolBrokerRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def register_contract(
        self,
        command: RegisterToolContractCommand,
        tool_contract_id: ToolContractId,
        contract_sha256: str,
    ) -> RegisteredToolContract:
        definition = command.definition
        input_schema = canonical_json(definition.input_schema)
        output_schema = canonical_json(definition.output_schema)
        policy = self._policy_json(definition)
        request = {
            "tool_name": definition.tool_name,
            "tool_version": definition.tool_version,
            "contract_sha256": contract_sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            turn = connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?",
                (command.registered_by_turn_id.value,),
            ).fetchone()
            if turn is None or str(turn["status"]) not in {"running", "waiting"}:
                raise ValueError("Tool Contract registration requires an active Turn")
            connection.execute(
                """
                INSERT INTO tool_contracts VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    tool_contract_id.value,
                    definition.tool_name,
                    definition.tool_version,
                    definition.display_name,
                    definition.operation_kind,
                    input_schema,
                    _sha256(input_schema),
                    output_schema,
                    _sha256(output_schema),
                    definition.execution_owner,
                    definition.effect_class,
                    definition.concurrency_class,
                    definition.replay_class,
                    definition.confirmation_policy,
                    definition.pause_behavior,
                    policy,
                    contract_sha256,
                    command.registered_by_turn_id.value,
                    revision.value,
                ),
            )
            response = {
                "tool_contract_id": tool_contract_id.value,
                "tool_name": definition.tool_name,
                "tool_version": definition.tool_version,
                "contract_sha256": contract_sha256,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "tool_contract.registered",
                        "tool_contract",
                        tool_contract_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("tool_contract.registered", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="tool_contract.register",
            request=request,
            mutation=mutate,
        )
        return RegisteredToolContract(
            ToolContractId(str(receipt.response["tool_contract_id"])),
            definition,
            str(receipt.response["contract_sha256"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def load_contract(self, tool_name: str) -> RegisteredToolContract | None:
        row = self._connection.execute(
            """
            SELECT * FROM tool_contracts WHERE tool_name = ?
            ORDER BY created_revision DESC LIMIT 1
            """,
            (tool_name,),
        ).fetchone()
        if row is None:
            return None
        policy = json.loads(str(row["policy_json"]))
        definition = ToolContractDefinition(
            str(row["tool_name"]),
            str(row["tool_version"]),
            str(row["display_name"]),
            str(row["operation_kind"]),
            json.loads(str(row["input_schema_json"])),
            json.loads(str(row["output_schema_json"])),
            str(row["execution_owner"]),
            str(row["effect_class"]),
            str(row["concurrency_class"]),
            str(row["replay_class"]),
            str(row["confirmation_policy"]),
            str(row["pause_behavior"]),
            float(policy["default_timeout_seconds"]),
            float(policy["max_timeout_seconds"]),
            int(policy["max_output_bytes"]),
            tuple(
                ResourceClaimTemplate(
                    str(claim["key_template"]),
                    str(claim["access_mode"]),
                    None
                    if claim.get("identity_argument") is None
                    else str(claim["identity_argument"]),
                )
                for claim in policy["resource_claim_templates"]
            ),
            str(policy.get("execution_isolation", "none")),
        )
        return RegisteredToolContract(
            ToolContractId(str(row["tool_contract_id"])),
            definition,
            str(row["contract_sha256"]),
            WorkspaceRevision(int(row["created_revision"])),
            False,
        )

    def commit_dispatch_plan(
        self,
        command: CreateDispatchPlanCommand,
        identity: DispatchPlanIdentity,
        prepared_calls: tuple[PreparedToolCall, ...],
        dependency_snapshot_json: str,
        dependency_snapshot_sha256: str,
    ) -> DispatchPlanOutcome:
        request = {
            "assistant_output_id": command.assistant_output_id.value,
            "tool_catalog_revision": command.tool_catalog_revision,
            "dependency_snapshot_sha256": dependency_snapshot_sha256,
            "calls": [
                {
                    "ordinal": ordinal,
                    "tool_name": call.requested_tool_name,
                    "provider_tool_call_id": call.provider_tool_call_id,
                    "raw_sha256": _sha256(call.raw_arguments_text),
                }
                for ordinal, call in enumerate(command.calls, start=1)
            ],
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            if (
                connection.execute(
                    "SELECT 1 FROM assistant_outputs WHERE assistant_output_id = ?",
                    (command.assistant_output_id.value,),
                ).fetchone()
                is None
            ):
                raise ValueError("Assistant Output does not exist")
            plan_revision = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(plan_revision), 0) + 1 FROM tool_dispatch_plans
                    WHERE assistant_output_id = ?
                    """,
                    (command.assistant_output_id.value,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO tool_dispatch_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identity.dispatch_plan_id.value,
                    command.assistant_output_id.value,
                    plan_revision,
                    command.tool_catalog_revision,
                    revision.value - 1,
                    dependency_snapshot_json,
                    dependency_snapshot_sha256,
                    revision.value,
                ),
            )
            scheduled: list[str] = []
            scheduled_entries: list[dict[str, object]] = []
            rejected: list[str] = []
            journal: list[JournalDraft] = []
            for call in prepared_calls:
                connection.execute(
                    "INSERT INTO raw_tool_argument_snapshots VALUES (?, ?, ?, ?)",
                    (
                        call.raw_snapshot_id.value,
                        call.proposal.raw_arguments_text,
                        _sha256(call.proposal.raw_arguments_text),
                        revision.value,
                    ),
                )
                if call.canonical_snapshot_id is not None:
                    connection.execute(
                        """
                        INSERT INTO canonical_tool_argument_snapshots VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            call.canonical_snapshot_id.value,
                            call.canonical_arguments_json,
                            call.arguments_sha256,
                            call.normalization_diff_json,
                            revision.value,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        call.tool_call_id.value,
                        command.assistant_output_id.value,
                        call.call_ordinal,
                        call.proposal.provider_tool_call_id,
                        call.proposal.requested_tool_name,
                        None if call.contract is None else call.contract.tool_contract_id.value,
                        call.raw_snapshot_id.value,
                        None
                        if call.canonical_snapshot_id is None
                        else call.canonical_snapshot_id.value,
                        call.arguments_sha256,
                        call.status,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO tool_call_status_history VALUES (?, 1, ?, ?, ?)",
                    (call.tool_call_id.value, call.status, call.reason_code, revision.value),
                )
                if call.status == "scheduled":
                    scheduled.append(call.tool_call_id.value)
                    if call.entry_id is None or call.execution_batch_ordinal is None:
                        raise ValueError("scheduled Call lacks a Dispatch Plan entry")
                    connection.execute(
                        "INSERT INTO tool_dispatch_plan_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            call.entry_id.value,
                            identity.dispatch_plan_id.value,
                            call.tool_call_id.value,
                            call.call_ordinal,
                            call.execution_batch_ordinal,
                            int(call.barrier_before),
                            int(call.barrier_after),
                            "contract_serialization" if call.barrier_before else None,
                        ),
                    )
                    scheduled_entries.append(
                        {
                            "tool_call_id": call.tool_call_id.value,
                            "call_ordinal": call.call_ordinal,
                            "execution_batch_ordinal": call.execution_batch_ordinal,
                            "barrier_before": call.barrier_before,
                            "barrier_after": call.barrier_after,
                        }
                    )
                    connection.executemany(
                        "INSERT INTO tool_resource_claims VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (
                                claim_id.value,
                                call.entry_id.value,
                                ordinal,
                                claim.resource_key,
                                claim.access_mode,
                                claim.identity_revision,
                            )
                            for ordinal, (claim_id, claim) in enumerate(
                                call.resource_claims, start=1
                            )
                        ],
                    )
                else:
                    rejected.append(call.tool_call_id.value)
                    if call.rejected_result_id is None:
                        raise ValueError("rejected Call lacks a canonical Tool Result identity")
                    result_body = canonical_json(
                        {"kind": "rejected", "reason_code": call.reason_code}
                    )
                    connection.execute(
                        """
                        INSERT INTO canonical_tool_results VALUES (
                            ?, ?, 'rejected', '1', ?, NULL, '[]', '[]', 0, NULL, ?, ?
                        )
                        """,
                        (
                            call.rejected_result_id.value,
                            call.tool_call_id.value,
                            call.reason_code,
                            _sha256(result_body),
                            revision.value,
                        ),
                    )
                journal.append(
                    JournalDraft(
                        "tool.proposed" if call.status == "scheduled" else "tool.rejected",
                        "tool_call",
                        call.tool_call_id.value,
                        {
                            "assistant_output_id": command.assistant_output_id.value,
                            "call_ordinal": call.call_ordinal,
                            "requested_tool_name": call.proposal.requested_tool_name,
                            "status": call.status,
                            "reason_code": call.reason_code,
                        },
                    )
                )
            current = connection.execute(
                """
                SELECT pointer_revision FROM assistant_dispatch_plan_adoptions
                WHERE assistant_output_id = ?
                """,
                (command.assistant_output_id.value,),
            ).fetchone()
            pointer = 1 if current is None else int(current["pointer_revision"]) + 1
            connection.execute(
                """
                INSERT INTO assistant_dispatch_plan_adoptions VALUES (?, ?, ?, ?)
                ON CONFLICT(assistant_output_id) DO UPDATE SET
                    dispatch_plan_id = excluded.dispatch_plan_id,
                    pointer_revision = excluded.pointer_revision,
                    commit_revision = excluded.commit_revision
                """,
                (
                    command.assistant_output_id.value,
                    identity.dispatch_plan_id.value,
                    pointer,
                    revision.value,
                ),
            )
            batch_count = max(
                (call.execution_batch_ordinal or 0 for call in prepared_calls), default=0
            )
            response = {
                "dispatch_plan_id": identity.dispatch_plan_id.value,
                "plan_revision": plan_revision,
                "scheduled_call_ids": scheduled,
                "scheduled_calls": scheduled_entries,
                "rejected_call_ids": rejected,
                "batch_count": batch_count,
            }
            journal.append(
                JournalDraft(
                    "tool.dispatch_plan_committed",
                    "tool_dispatch_plan",
                    identity.dispatch_plan_id.value,
                    response,
                )
            )
            return MutationPayload(
                response,
                tuple(journal),
                (OutboxDraft("tool.dispatch_plan_committed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="tool.dispatch_plan.create",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        scheduled_response = response.get("scheduled_calls")
        if not isinstance(scheduled_response, list):
            scheduled_response = [
                {
                    "tool_call_id": str(row["tool_call_id"]),
                    "call_ordinal": int(row["call_ordinal"]),
                    "execution_batch_ordinal": int(row["execution_batch_ordinal"]),
                    "barrier_before": bool(row["barrier_before"]),
                    "barrier_after": bool(row["barrier_after"]),
                }
                for row in self._connection.execute(
                    """
                    SELECT call.tool_call_id, call.call_ordinal,
                           entry.execution_batch_ordinal,
                           entry.barrier_before, entry.barrier_after
                    FROM tool_dispatch_plan_entries AS entry
                    JOIN tool_calls AS call USING (tool_call_id)
                    WHERE entry.dispatch_plan_id = ?
                    ORDER BY call.call_ordinal
                    """,
                    (str(response["dispatch_plan_id"]),),
                ).fetchall()
            ]
        return DispatchPlanOutcome(
            identity.dispatch_plan_id
            if not receipt.replayed
            else type(identity.dispatch_plan_id)(str(response["dispatch_plan_id"])),
            int(response["plan_revision"]),
            tuple(ToolCallId(str(value)) for value in response["scheduled_call_ids"]),
            tuple(
                ScheduledToolCall(
                    ToolCallId(str(item["tool_call_id"])),
                    int(item["call_ordinal"]),
                    int(item["execution_batch_ordinal"]),
                    bool(item["barrier_before"]),
                    bool(item["barrier_after"]),
                )
                for item in scheduled_response
            ),
            tuple(ToolCallId(str(value)) for value in response["rejected_call_ids"]),
            int(response["batch_count"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def admit(
        self,
        command: AdmitToolCallCommand,
        identity: ToolAdmissionIdentity,
        current_dependency_snapshot_json: str,
        current_dependency_snapshot_sha256: str,
    ) -> ToolAdmissionOutcome:
        request = {
            "tool_call_id": command.tool_call_id.value,
            "expected_turn_revision": command.expected_turn_revision,
            "admission_policy_revision": command.admission_policy_revision,
            "allowed_effect_classes": list(command.allowed_effect_classes),
            "dependency_snapshot_sha256": current_dependency_snapshot_sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT call.proposal_status, contract.tool_contract_id,
                       contract.operation_kind, contract.execution_owner,
                       contract.effect_class, contract.confirmation_policy,
                       plan.dispatch_plan_id, plan.dependency_snapshot_sha256,
                       manifest.permission_snapshot_id, manifest.context_manifest_id,
                       manifest.completion_contract_revision_id,
                       completion.normalization_required,
                       turn.turn_id, turn.turn_revision, turn.status AS turn_status,
                       budget.used_tool_admissions, policy.max_tool_admissions,
                       policy.max_same_failure_fingerprint,
                       call.requested_tool_name, call.arguments_hash
                FROM tool_calls AS call
                JOIN tool_contracts AS contract
                  ON contract.tool_contract_id = call.resolved_tool_contract_id
                JOIN tool_dispatch_plan_entries AS entry ON entry.tool_call_id = call.tool_call_id
                JOIN tool_dispatch_plans AS plan ON plan.dispatch_plan_id = entry.dispatch_plan_id
                JOIN assistant_dispatch_plan_adoptions AS adoption
                  ON adoption.assistant_output_id = call.assistant_output_id
                 AND adoption.dispatch_plan_id = plan.dispatch_plan_id
                JOIN assistant_outputs AS output
                  ON output.assistant_output_id = call.assistant_output_id
                JOIN model_invocations AS invocation
                  ON invocation.model_invocation_id = output.model_invocation_id
                JOIN steps AS step ON step.step_id = invocation.step_id
                JOIN turns AS turn ON turn.turn_id = step.turn_id
                JOIN context_manifests AS manifest ON manifest.step_id = step.step_id
                JOIN completion_contract_revisions AS completion
                  ON completion.completion_contract_revision_id =
                     manifest.completion_contract_revision_id
                JOIN turn_budget_accounts AS budget ON budget.turn_id = turn.turn_id
                JOIN budget_policy_snapshots AS policy
                  ON policy.budget_policy_snapshot_id = budget.budget_policy_snapshot_id
                WHERE call.tool_call_id = ?
                """,
                (command.tool_call_id.value,),
            ).fetchone()
            if row is None:
                raise ValueError("Tool Call is missing or its Dispatch Plan was superseded")
            if str(row["turn_status"]) != "running":
                raise ValueError("WAITING/PAUSED/non-running Turn blocks new Tool Admission")
            if str(row["proposal_status"]) != "scheduled":
                raise ValueError("Tool Call is not scheduled for Admission")
            if (
                connection.execute(
                    """
                SELECT 1 FROM pause_intents
                WHERE turn_id = ? AND status IN ('requested', 'converging')
                """,
                    (str(row["turn_id"]),),
                ).fetchone()
                is not None
            ):
                raise ValueError("user pause intent blocks new Tool Admission")
            if int(row["turn_revision"]) != command.expected_turn_revision:
                raise ValueError("Turn revision changed before JIT Admission")
            if str(row["execution_owner"]) == "provider_managed":
                raise ValueError("provider-managed Tool cannot use local Operation Admission")
            if str(row["effect_class"]) not in command.allowed_effect_classes:
                raise ValueError("current permission policy denies this Tool effect class")
            if str(row["confirmation_policy"]) != "never":
                raise ValueError("Tool Call requires a confirmation decision before Admission")
            if int(row["normalization_required"]) == 1 and str(row["effect_class"]) not in {
                "pure_read",
                "external_read",
            }:
                raise ValueError("intake-only Completion Contract blocks side effects")
            if str(row["dependency_snapshot_sha256"]) != current_dependency_snapshot_sha256:
                raise ValueError("Dispatch Plan dependencies changed before Admission")
            if int(row["used_tool_admissions"]) >= int(row["max_tool_admissions"]):
                raise ValueError("Turn Tool Admission budget is exhausted")
            repeated_failures = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM canonical_tool_results AS result
                    JOIN tool_calls AS previous USING (tool_call_id)
                    JOIN assistant_outputs AS output
                      ON output.assistant_output_id = previous.assistant_output_id
                    JOIN model_invocations AS invocation
                      ON invocation.model_invocation_id = output.model_invocation_id
                    JOIN steps AS step ON step.step_id = invocation.step_id
                    JOIN tool_dispatch_plan_entries AS previous_entry
                      ON previous_entry.tool_call_id = previous.tool_call_id
                    JOIN tool_dispatch_plans AS previous_plan
                      ON previous_plan.dispatch_plan_id = previous_entry.dispatch_plan_id
                    WHERE step.turn_id = ?
                      AND previous.requested_tool_name = ?
                      AND previous.arguments_hash = ?
                      AND previous_plan.dependency_snapshot_sha256 = ?
                      AND result.result_kind = 'error'
                    """,
                    (
                        str(row["turn_id"]),
                        str(row["requested_tool_name"]),
                        str(row["arguments_hash"]),
                        current_dependency_snapshot_sha256,
                    ),
                ).fetchone()[0]
            )
            if repeated_failures >= int(row["max_same_failure_fingerprint"]):
                raise ValueError(
                    "Tool no-progress guard blocks another identical failed call"
                )
            repeated_empty_successes = 0
            if str(row["requested_tool_name"]) == "stata.execute":
                repeated_empty_successes = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM canonical_tool_results AS result
                        JOIN tool_calls AS previous USING (tool_call_id)
                        JOIN assistant_outputs AS output
                          ON output.assistant_output_id = previous.assistant_output_id
                        JOIN model_invocations AS invocation
                          ON invocation.model_invocation_id = output.model_invocation_id
                        JOIN steps AS step ON step.step_id = invocation.step_id
                        JOIN tool_dispatch_plan_entries AS previous_entry
                          ON previous_entry.tool_call_id = previous.tool_call_id
                        JOIN tool_dispatch_plans AS previous_plan
                          ON previous_plan.dispatch_plan_id = previous_entry.dispatch_plan_id
                        WHERE step.turn_id = ?
                          AND previous.requested_tool_name = ?
                          AND previous.arguments_hash = ?
                          AND previous_plan.dependency_snapshot_sha256 = ?
                          AND result.result_kind = 'success'
                          AND result.artifact_references_json = '[]'
                          AND json_extract(result.structured_payload_json, '$.structured') IS NULL
                          AND json_extract(
                                result.structured_payload_json,
                                '$.raw_output_excerpt'
                              ) = '(task finished with empty output)'
                        """,
                        (
                            str(row["turn_id"]),
                            str(row["requested_tool_name"]),
                            str(row["arguments_hash"]),
                            current_dependency_snapshot_sha256,
                        ),
                    ).fetchone()[0]
                )
            if repeated_empty_successes >= int(row["max_same_failure_fingerprint"]):
                raise ValueError(
                    "Tool no-progress guard blocks another identical empty-success Stata call"
                )
            claims = connection.execute(
                """
                SELECT claim.resource_claim_id, claim.resource_key,
                       claim.access_mode, claim.identity_revision
                FROM tool_resource_claims AS claim
                JOIN tool_dispatch_plan_entries AS entry
                  ON entry.dispatch_plan_entry_id = claim.dispatch_plan_entry_id
                WHERE entry.tool_call_id = ? AND entry.dispatch_plan_id = ?
                ORDER BY claim.claim_ordinal
                """,
                (command.tool_call_id.value, str(row["dispatch_plan_id"])),
            ).fetchall()
            current_dependencies = json.loads(current_dependency_snapshot_json)
            resource_identities = current_dependencies.get("resource_identities", {})
            if not isinstance(resource_identities, dict):
                raise ValueError("dependency snapshot lacks resource identities")
            for claim in claims:
                expected_identity = str(claim["identity_revision"])
                if (
                    expected_identity != "stable"
                    and str(resource_identities.get(str(claim["resource_key"]), ""))
                    != expected_identity
                ):
                    raise ValueError("resource identity changed before Admission")
                conflicts = connection.execute(
                    """
                    SELECT 1 FROM tool_resource_leases
                    WHERE lease_status = 'active' AND resource_key = ?
                      AND (access_mode != 'read' OR ? != 'read')
                    LIMIT 1
                    """,
                    (str(claim["resource_key"]), str(claim["access_mode"])),
                ).fetchone()
                if conflicts is not None:
                    raise ValueError("required Tool resource is currently leased")
            claims_canonical = canonical_json(
                {
                    "claims": [
                        {
                            "resource_key": str(claim["resource_key"]),
                            "access_mode": str(claim["access_mode"]),
                            "identity_revision": str(claim["identity_revision"]),
                        }
                        for claim in claims
                    ]
                }
            )
            connection.execute(
                """
                UPDATE turn_budget_accounts
                SET used_tool_admissions = used_tool_admissions + 1,
                    account_revision = account_revision + 1,
                    updated_revision = ?
                WHERE turn_id = ?
                """,
                (revision.value, str(row["turn_id"])),
            )
            connection.execute(
                """
                INSERT INTO turn_budget_usage_history
                VALUES (?, ?, 'tool_admission', 1, NULL, ?)
                """,
                (identity.budget_usage_id.value, str(row["turn_id"]), revision.value),
            )
            connection.execute(
                """
                INSERT INTO operations VALUES (?, ?, ?, ?, 'admitted', NULL, ?, NULL)
                """,
                (
                    identity.operation_id.value,
                    str(row["operation_kind"]),
                    str(row["turn_id"]),
                    command.tool_call_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO tool_admissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.admission_id.value,
                    command.tool_call_id.value,
                    str(row["dispatch_plan_id"]),
                    identity.operation_id.value,
                    command.expected_turn_revision,
                    str(row["permission_snapshot_id"]),
                    str(row["context_manifest_id"]),
                    str(row["completion_contract_revision_id"]),
                    current_dependency_snapshot_sha256,
                    _sha256(claims_canonical),
                    command.admission_policy_revision,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO operation_tool_call_links VALUES (?, ?, NULL, 1, ?)",
                (identity.operation_id.value, command.tool_call_id.value, revision.value),
            )
            connection.executemany(
                """
                INSERT INTO tool_resource_leases VALUES (?, ?, ?, ?, 'active', ?, NULL)
                """,
                [
                    (
                        identity.admission_id.value,
                        str(claim["resource_claim_id"]),
                        str(claim["resource_key"]),
                        str(claim["access_mode"]),
                        revision.value,
                    )
                    for claim in claims
                ],
            )
            status_ordinal = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(status_ordinal), 0) + 1
                    FROM tool_call_status_history WHERE tool_call_id = ?
                    """,
                    (command.tool_call_id.value,),
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE tool_calls SET proposal_status = 'admitted' WHERE tool_call_id = ?",
                (command.tool_call_id.value,),
            )
            connection.execute(
                "INSERT INTO tool_call_status_history VALUES (?, ?, 'admitted', 'jit_passed', ?)",
                (command.tool_call_id.value, status_ordinal, revision.value),
            )
            response = {
                "tool_admission_id": identity.admission_id.value,
                "operation_id": identity.operation_id.value,
                "tool_call_id": command.tool_call_id.value,
                "status": "admitted",
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "tool.admitted",
                        "tool_call",
                        command.tool_call_id.value,
                        {
                            **response,
                            "dispatch_plan_id": str(row["dispatch_plan_id"]),
                            "tool_contract_id": str(row["tool_contract_id"]),
                        },
                    ),
                ),
                (OutboxDraft("tool.admitted", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="tool.call.admit",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return ToolAdmissionOutcome(
            type(identity.admission_id)(str(response["tool_admission_id"])),
            type(identity.operation_id)(str(response["operation_id"])),
            ToolCallId(str(response["tool_call_id"])),
            str(response["status"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def record_executor_exception(
        self, command: RecordExecutorExceptionCommand
    ) -> ExecutorExceptionOutcome:
        """Close an admitted execution that escaped without a canonical Tool Result.

        Before handoff this is a definite failure with no external side effect.  After
        handoff the external outcome is unknown, so the safety net preserves that stronger
        recovery classification instead of pretending the Tool merely failed.
        """

        request = {
            "turn_id": command.turn_id.value,
            "tool_call_id": command.tool_call_id.value,
            "operation_id": command.operation_id.value,
            "error_kind": command.error_kind,
            "error_detail": command.error_detail,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT status FROM operations
                WHERE operation_id = ? AND requested_by_turn_id = ? AND tool_call_id = ?
                """,
                (
                    command.operation_id.value,
                    command.turn_id.value,
                    command.tool_call_id.value,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("executor exception targets another admitted Operation")
            previous = str(row["status"])
            if previous == "admitted":
                terminal_status = "failed"
                outcome = "definitely_not_started"
                event_type = "tool.failed"
            elif previous == "handoff_committed":
                terminal_status = "outcome_unknown"
                outcome = "outcome_unknown"
                event_type = "tool.interrupted"
                cursor = connection.execute(
                    """
                    UPDATE operation_attempts
                    SET status = 'outcome_unknown', terminal_revision = ?
                    WHERE operation_id = ? AND status = 'handoff_committed'
                    """,
                    (revision.value, command.operation_id.value),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Tool handoff has no active Operation Attempt")
            else:
                raise ValueError("executor exception target is already terminal")
            connection.execute(
                "UPDATE operations SET status = ?, terminal_revision = ? WHERE operation_id = ?",
                (terminal_status, revision.value, command.operation_id.value),
            )
            payload = {
                "error_kind": command.error_kind,
                "failure_phase": "executor_exception",
                "execution_outcome": outcome,
            }
            if command.error_detail:
                payload["error_detail"] = command.error_detail
            payload_json = canonical_json(payload)
            connection.execute(
                """
                INSERT INTO canonical_tool_results VALUES (
                    ?, ?, 'error', '1', ?, ?, '[]', ?, 0, NULL, ?, ?
                )
                """,
                (
                    f"toolresult_{command.operation_id.value[3:]}",
                    command.tool_call_id.value,
                    "Tool executor failed before returning a canonical result",
                    payload_json,
                    canonical_json([command.operation_id.value]),
                    _sha256(payload_json),
                    revision.value,
                ),
            )
            connection.execute(
                "UPDATE tool_calls SET proposal_status = 'resolved' WHERE tool_call_id = ?",
                (command.tool_call_id.value,),
            )
            status_ordinal = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(status_ordinal), 0) + 1
                    FROM tool_call_status_history WHERE tool_call_id = ?
                    """,
                    (command.tool_call_id.value,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO tool_call_status_history VALUES (?, ?, 'resolved', ?, ?)",
                (
                    command.tool_call_id.value,
                    status_ordinal,
                    terminal_status,
                    revision.value,
                ),
            )
            connection.execute(
                """
                UPDATE tool_resource_leases
                SET lease_status = 'released', released_revision = ?
                WHERE tool_admission_id = (
                    SELECT tool_admission_id FROM tool_admissions WHERE operation_id = ?
                ) AND lease_status = 'active'
                """,
                (revision.value, command.operation_id.value),
            )
            response = {
                **request,
                "status": terminal_status,
                "execution_outcome": outcome,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        event_type,
                        "operation",
                        command.operation_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("operation.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="tool.executor_exception.record",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return ExecutorExceptionOutcome(
            type(command.operation_id)(str(response["operation_id"])),
            ToolCallId(str(response["tool_call_id"])),
            str(response["status"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _policy_json(definition: ToolContractDefinition) -> str:
        return str(
            canonical_json(
                {
                    "default_timeout_seconds": definition.default_timeout_seconds,
                    "max_timeout_seconds": definition.max_timeout_seconds,
                    "max_output_bytes": definition.max_output_bytes,
                    "resource_claim_templates": [
                        {
                            "key_template": claim.key_template,
                            "access_mode": claim.access_mode,
                            "identity_argument": claim.identity_argument,
                        }
                        for claim in definition.resource_claim_templates
                    ],
                    "execution_isolation": definition.execution_isolation,
                }
            )
        )
