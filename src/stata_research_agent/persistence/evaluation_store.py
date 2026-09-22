"""SQLite authority for evaluator reports, goal coverage, and Stop Guard."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from stata_research_agent.application.evaluation import (
    DecideNaturalStopCommand,
    EvaluationIdentity,
    EvaluationOutcome,
    EvaluationVerdict,
    NormalizeCompletionContractCommand,
    NormalizeContractOutcome,
    NormalizedContractIdentity,
    ObligationStateOutcome,
    RecordEvaluationCommand,
    RecordObligationStateCommand,
    StopGuardIdentity,
    StopGuardOutcome,
)
from stata_research_agent.domain.identifiers import (
    CompletionContractRevisionId,
    CompletionObligationId,
    EvaluationReportId,
    EvaluationRequestId,
    GoalCoverageId,
    ObligationObservationId,
    StopGuardDecisionRecordId,
    TurnId,
    WaitingRequestId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.status import (
    ContinueDirective,
    StopGuardDecision,
    TerminalDisposition,
    WaitReason,
)

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)

FINDING_POLICY: dict[str, tuple[str, bool]] = {
    "completion_ready": ("none", False),
    "quality_concern": ("none", False),
    "plan_deviation": ("block_termination", False),
    "required_evidence_missing": ("block_termination", False),
    "research_semantic_ambiguity": ("block_termination", True),
    "loop_no_progress": ("block_execution", False),
}


class SqliteEvaluationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def normalize_contract(
        self,
        command: NormalizeCompletionContractCommand,
        identity: NormalizedContractIdentity,
    ) -> NormalizeContractOutcome:
        request = {
            "turn_id": command.turn_id.value,
            "expected_turn_revision": command.expected_turn_revision,
            "goal_summary": command.goal_summary,
            "obligations": [
                {
                    "stable_key": item.stable_key,
                    "label": item.label,
                    "provenance": item.provenance.value,
                    "source_object_type": item.source_object_type,
                    "source_object_id": item.source_object_id,
                    "requirement_level": item.requirement_level.value,
                    "acceptance_criterion": item.acceptance_criterion,
                }
                for item in command.obligations
            ],
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            turn = connection.execute(
                """
                SELECT status, turn_revision, triggering_message_id,
                       completion_contract_revision_id
                FROM turns WHERE turn_id = ?
                """,
                (command.turn_id.value,),
            ).fetchone()
            if turn is None or str(turn["status"]) != "running":
                raise ValueError("only a Running Turn can normalize its Completion Contract")
            if int(turn["turn_revision"]) != command.expected_turn_revision:
                raise ValueError("Turn revision changed before Contract normalization")
            current = connection.execute(
                """
                SELECT completion_contract_id, revision_number
                FROM completion_contract_revisions
                WHERE completion_contract_revision_id = ?
                """,
                (str(turn["completion_contract_revision_id"]),),
            ).fetchone()
            if current is None:
                raise ValueError("Turn Completion Contract revision is missing")
            contract_id = str(current["completion_contract_id"])
            revision_number = (
                int(
                    connection.execute(
                        """
                    SELECT MAX(revision_number) FROM completion_contract_revisions
                    WHERE completion_contract_id = ?
                    """,
                        (contract_id,),
                    ).fetchone()[0]
                )
                + 1
            )
            connection.execute(
                """
                INSERT INTO completion_contract_revisions
                VALUES (?, ?, ?, 'intake', 0, ?, ?)
                """,
                (
                    identity.revision_id.value,
                    contract_id,
                    revision_number,
                    str(turn["triggering_message_id"]),
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO completion_contract_revision_profiles
                VALUES (?, ?, 'execution_ready', ?)
                """,
                (identity.revision_id.value, command.goal_summary, revision.value),
            )
            for item, obligation_id in zip(
                command.obligations, identity.obligation_ids, strict=True
            ):
                connection.execute(
                    """
                    INSERT INTO completion_obligations
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        obligation_id.value,
                        contract_id,
                        item.stable_key,
                        item.label,
                        item.provenance.value,
                        item.source_object_type,
                        item.source_object_id,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO revision_obligation_entries
                    VALUES (?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        identity.revision_id.value,
                        obligation_id.value,
                        item.requirement_level.value,
                        item.acceptance_criterion,
                        revision.value,
                    ),
                )
            next_turn_revision = command.expected_turn_revision + 1
            connection.execute(
                """
                UPDATE turns
                SET completion_contract_revision_id = ?, turn_revision = ?
                WHERE turn_id = ?
                """,
                (identity.revision_id.value, next_turn_revision, command.turn_id.value),
            )
            response = {
                "turn_id": command.turn_id.value,
                "completion_contract_revision_id": identity.revision_id.value,
                "turn_revision": next_turn_revision,
                "obligation_ids": [value.value for value in identity.obligation_ids],
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "completion.contract.revised",
                        "completion_contract_revision",
                        identity.revision_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("workspace.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="completion_contract.normalize",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return NormalizeContractOutcome(
            TurnId(str(response["turn_id"])),
            CompletionContractRevisionId(str(response["completion_contract_revision_id"])),
            int(response["turn_revision"]),
            tuple(CompletionObligationId(str(value)) for value in response["obligation_ids"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def record_obligation_state(
        self,
        command: RecordObligationStateCommand,
        observation_id: ObligationObservationId,
    ) -> ObligationStateOutcome:
        request = {
            "turn_id": command.turn_id.value,
            "completion_obligation_id": command.completion_obligation_id.value,
            "observed_state": command.observed_state,
            "evidence_references": list(command.evidence_references),
            "actor_kind": command.actor_kind,
            "rationale": command.rationale,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            turn = connection.execute(
                """
                SELECT status, completion_contract_revision_id
                FROM turns WHERE turn_id = ?
                """,
                (command.turn_id.value,),
            ).fetchone()
            if turn is None or str(turn["status"]) != "running":
                raise ValueError("obligation state can only be recorded for a Running Turn")
            contract_revision_id = str(turn["completion_contract_revision_id"])
            entry = connection.execute(
                """
                SELECT 1 FROM revision_obligation_entries
                WHERE completion_contract_revision_id = ?
                  AND completion_obligation_id = ?
                """,
                (contract_revision_id, command.completion_obligation_id.value),
            ).fetchone()
            if entry is None:
                raise ValueError("obligation is not part of the Turn-bound Contract revision")
            connection.execute(
                """
                INSERT INTO obligation_state_observations
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id.value,
                    contract_revision_id,
                    command.completion_obligation_id.value,
                    command.observed_state,
                    canonical_json(list(command.evidence_references)),
                    command.actor_kind,
                    command.rationale,
                    revision.value,
                ),
            )
            response = {
                "obligation_observation_id": observation_id.value,
                "observed_state": command.observed_state,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "completion.obligation.observed",
                        "completion_obligation",
                        command.completion_obligation_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("workspace.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="completion_obligation.observe",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return ObligationStateOutcome(
            ObligationObservationId(str(response["obligation_observation_id"])),
            str(response["observed_state"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def record_evaluation(
        self, command: RecordEvaluationCommand, identity: EvaluationIdentity
    ) -> EvaluationOutcome:
        request = {
            "turn_id": command.turn_id.value,
            "expected_turn_revision": command.expected_turn_revision,
            "step_id": None if command.step_id is None else command.step_id.value,
            "evaluation_kind": command.evaluation_kind,
            "dimension": command.dimension.value,
            "trigger_reason": command.trigger_reason,
            "subject": [command.subject_type, command.subject_id, command.subject_revision],
            "dependencies": [
                [item.object_type, item.object_id, item.object_revision]
                for item in command.dependencies
            ],
            "permitted_slices": list(command.permitted_slices),
            "excluded_scope": list(command.excluded_scope),
            "grader_kind": command.grader_kind,
            "grader_version": command.grader_version,
            "verdict": command.verdict.value,
            "findings": [
                [item.finding_code, item.severity, item.message, item.confidence]
                for item in command.findings
            ],
            "unknowns": list(command.unknowns),
            "required_evidence": list(command.required_evidence),
            "suggested_actions": list(command.suggested_actions),
            "policy_revision": command.policy_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            turn = connection.execute(
                """
                SELECT status, turn_revision, completion_contract_revision_id
                FROM turns WHERE turn_id = ?
                """,
                (command.turn_id.value,),
            ).fetchone()
            if turn is None or str(turn["status"]) != "running":
                raise ValueError("Runtime Evaluation requires a Running Turn")
            if int(turn["turn_revision"]) != command.expected_turn_revision:
                raise ValueError("Turn revision changed before Runtime Evaluation")
            if command.step_id is not None:
                step = connection.execute(
                    "SELECT turn_id FROM steps WHERE step_id = ?",
                    (command.step_id.value,),
                ).fetchone()
                if step is None or str(step["turn_id"]) != command.turn_id.value:
                    raise ValueError("evaluation Step does not belong to the Turn")
            mapped_findings: list[tuple[str, bool]] = []
            for finding in command.findings:
                policy = FINDING_POLICY.get(finding.finding_code)
                if policy is None:
                    raise ValueError(f"unregistered finding_code: {finding.finding_code}")
                mapped_findings.append(policy)
            if command.verdict is EvaluationVerdict.UNKNOWN and not (
                command.unknowns or command.required_evidence
            ):
                raise ValueError("UNKNOWN evaluation must identify unknowns or required evidence")
            dependency_json = canonical_json(
                [
                    {
                        "object_type": item.object_type,
                        "object_id": item.object_id,
                        "object_revision": item.object_revision,
                    }
                    for item in command.dependencies
                ]
            )
            manifest_payload = {
                "subject": [command.subject_type, command.subject_id, command.subject_revision],
                "dependencies": [
                    [item.object_type, item.object_id, item.object_revision]
                    for item in command.dependencies
                ],
                "permitted_slices": list(command.permitted_slices),
                "excluded_scope": list(command.excluded_scope),
            }
            policy_json = canonical_json(
                {
                    key: {"control_effect": value[0], "requires_user_decision": value[1]}
                    for key, value in sorted(FINDING_POLICY.items())
                }
            )
            policy_hash = hashlib.sha256(policy_json.encode()).hexdigest()
            manifest_hash = hashlib.sha256(canonical_json(manifest_payload).encode()).hexdigest()
            connection.execute(
                "INSERT INTO evaluation_policy_snapshots VALUES (?, ?, ?, ?, ?)",
                (
                    identity.policy_snapshot_id.value,
                    command.policy_revision,
                    policy_json,
                    policy_hash,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO evidence_scope_manifests
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.evidence_scope_manifest_id.value,
                    command.subject_type,
                    command.subject_id,
                    command.subject_revision,
                    dependency_json,
                    canonical_json(list(command.permitted_slices)),
                    canonical_json(list(command.excluded_scope)),
                    manifest_hash,
                    revision.value,
                ),
            )
            contract_revision_id = str(turn["completion_contract_revision_id"])
            connection.execute(
                """
                INSERT INTO evaluation_requests
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.evaluation_request_id.value,
                    command.turn_id.value,
                    None if command.step_id is None else command.step_id.value,
                    command.evaluation_kind,
                    command.dimension.value,
                    command.trigger_reason,
                    command.subject_type,
                    command.subject_id,
                    command.subject_revision,
                    contract_revision_id,
                    identity.evidence_scope_manifest_id.value,
                    identity.policy_snapshot_id.value,
                    command.grader_kind,
                    revision.value - 1,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO evaluation_reports
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.evaluation_report_id.value,
                    identity.evaluation_request_id.value,
                    command.verdict.value,
                    command.grader_version,
                    canonical_json(list(command.unknowns)),
                    canonical_json(list(command.required_evidence)),
                    canonical_json(list(command.suggested_actions)),
                    revision.value,
                ),
            )
            for ordinal, (finding, policy, finding_id) in enumerate(
                zip(command.findings, mapped_findings, identity.finding_ids, strict=True),
                start=1,
            ):
                connection.execute(
                    """
                    INSERT INTO evaluation_findings
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finding_id.value,
                        identity.evaluation_report_id.value,
                        ordinal,
                        finding.finding_code,
                        finding.severity,
                        policy[0],
                        finding.confidence,
                        finding.message,
                        int(policy[1]),
                    ),
                )
            response = {
                "evaluation_request_id": identity.evaluation_request_id.value,
                "evaluation_report_id": identity.evaluation_report_id.value,
                "verdict": command.verdict.value,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "evaluation.recorded",
                        "evaluation_report",
                        identity.evaluation_report_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("evaluation.recorded", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="evaluation.record",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return EvaluationOutcome(
            EvaluationRequestId(str(response["evaluation_request_id"])),
            EvaluationReportId(str(response["evaluation_report_id"])),
            EvaluationVerdict(str(response["verdict"])),
            receipt.commit_revision,
            receipt.replayed,
        )

    def decide_natural_stop(
        self, command: DecideNaturalStopCommand, identity: StopGuardIdentity
    ) -> StopGuardOutcome:
        request = {
            "turn_id": command.turn_id.value,
            "expected_turn_revision": command.expected_turn_revision,
            "evaluation_report_id": command.evaluation_report_id.value,
            "requested_disposition": command.requested_disposition.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            turn = connection.execute(
                """
                SELECT status, turn_revision, execution_mode,
                       completion_contract_revision_id
                FROM turns WHERE turn_id = ?
                """,
                (command.turn_id.value,),
            ).fetchone()
            if turn is None or str(turn["status"]) != "running":
                raise ValueError("Stop Guard requires a Running Turn")
            if int(turn["turn_revision"]) != command.expected_turn_revision:
                raise ValueError("Turn revision changed before Stop Guard")
            contract_revision_id = str(turn["completion_contract_revision_id"])
            report = connection.execute(
                """
                SELECT report.verdict, report.required_evidence_json,
                       report.created_revision, request.turn_id,
                       request.completion_contract_revision_id,
                       request.evaluation_dimension
                FROM evaluation_reports AS report
                JOIN evaluation_requests AS request
                  ON request.evaluation_request_id = report.evaluation_request_id
                WHERE report.evaluation_report_id = ?
                """,
                (command.evaluation_report_id.value,),
            ).fetchone()
            if report is None:
                raise ValueError("unknown Evaluation Report")
            if str(report["turn_id"]) != command.turn_id.value:
                raise ValueError("Evaluation Report belongs to another Turn")
            if str(report["completion_contract_revision_id"]) != contract_revision_id:
                raise ValueError("Evaluation Report targets another Contract revision")
            if str(report["evaluation_dimension"]) != "completion":
                raise ValueError("Natural Stop requires a COMPLETION evaluation")
            if int(report["created_revision"]) != revision.value - 1:
                raise ValueError("Evaluation Report is not fresh for Natural Stop")

            readiness = connection.execute(
                """
                SELECT contract_readiness
                FROM completion_contract_revision_profiles
                WHERE completion_contract_revision_id = ?
                """,
                (contract_revision_id,),
            ).fetchone()
            execution_ready = (
                readiness is not None and str(readiness["contract_readiness"]) == "execution_ready"
            )
            obligation_rows = connection.execute(
                """
                WITH latest AS (
                    SELECT completion_obligation_id, observed_state,
                           ROW_NUMBER() OVER (
                               PARTITION BY completion_obligation_id
                               ORDER BY commit_revision DESC
                           ) AS row_number
                    FROM obligation_state_observations
                    WHERE completion_contract_revision_id = ?
                )
                SELECT entry.completion_obligation_id, obligation.stable_key,
                       entry.requirement_level, entry.disposition,
                       COALESCE(latest.observed_state, 'unsatisfied') AS observed_state
                FROM revision_obligation_entries AS entry
                JOIN completion_obligations AS obligation
                  ON obligation.completion_obligation_id = entry.completion_obligation_id
                LEFT JOIN latest
                  ON latest.completion_obligation_id = entry.completion_obligation_id
                 AND latest.row_number = 1
                WHERE entry.completion_contract_revision_id = ?
                ORDER BY obligation.stable_key
                """,
                (contract_revision_id, contract_revision_id),
            ).fetchall()
            coverage: list[dict[str, Any]] = []
            required_total = 0
            required_satisfied = 0
            incomplete_keys: list[str] = []
            for row in obligation_rows:
                required = str(row["requirement_level"]) == "required"
                satisfied = (
                    str(row["observed_state"]) in {"satisfied", "waived"}
                    or str(row["disposition"]) == "waived"
                )
                if required and str(row["disposition"]) not in {"replaced"}:
                    required_total += 1
                    if satisfied:
                        required_satisfied += 1
                    else:
                        incomplete_keys.append(str(row["stable_key"]))
                coverage.append(
                    {
                        "obligation_id": str(row["completion_obligation_id"]),
                        "stable_key": str(row["stable_key"]),
                        "requirement_level": str(row["requirement_level"]),
                        "disposition": str(row["disposition"]),
                        "observed_state": str(row["observed_state"]),
                        "satisfied": satisfied,
                    }
                )
            coverage_satisfied = execution_ready and not incomplete_keys
            connection.execute(
                """
                INSERT INTO goal_coverages VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.goal_coverage_id.value,
                    command.turn_id.value,
                    contract_revision_id,
                    required_total,
                    required_satisfied,
                    "satisfied" if coverage_satisfied else "incomplete",
                    canonical_json(coverage),
                    revision.value,
                ),
            )

            findings = connection.execute(
                """
                SELECT finding_code, control_effect, requires_user_decision
                FROM evaluation_findings WHERE evaluation_report_id = ?
                ORDER BY finding_ordinal
                """,
                (command.evaluation_report_id.value,),
            ).fetchall()
            termination_blocks = [
                str(row["finding_code"])
                for row in findings
                if str(row["control_effect"]) == "block_termination"
            ]
            requires_user = any(int(row["requires_user_decision"]) for row in findings)
            unresolved_calls = int(
                connection.execute(
                    """
                    SELECT count(*)
                    FROM tool_calls AS call
                    JOIN assistant_outputs AS output
                      ON output.assistant_output_id = call.assistant_output_id
                    JOIN model_invocations AS invocation
                      ON invocation.model_invocation_id = output.model_invocation_id
                    JOIN steps AS step ON step.step_id = invocation.step_id
                    WHERE step.turn_id = ?
                      AND call.proposal_status IN (
                          'proposed', 'awaiting_confirmation', 'scheduled', 'admitted'
                      )
                    """,
                    (command.turn_id.value,),
                ).fetchone()[0]
            )
            active_operations = int(
                connection.execute(
                    """
                    SELECT count(*) FROM operations
                    WHERE requested_by_turn_id = ?
                      AND status IN (
                          'proposed', 'authorized', 'admitted', 'handoff_committed', 'interrupted'
                      )
                    """,
                    (command.turn_id.value,),
                ).fetchone()[0]
            )
            unsafe_operations = int(
                connection.execute(
                    """
                    SELECT count(*) FROM operations
                    WHERE requested_by_turn_id = ?
                      AND status IN (
                          'completed_unreconciled', 'outcome_unknown', 'integrity_violation'
                      )
                    """,
                    (command.turn_id.value,),
                ).fetchone()[0]
            )
            pause_pending = (
                connection.execute(
                    """
                SELECT 1 FROM pause_intents
                WHERE turn_id = ? AND status IN ('requested', 'converging')
                """,
                    (command.turn_id.value,),
                ).fetchone()
                is not None
            )

            blockers: list[str] = []
            decision = StopGuardDecision.CONTINUE
            directive: ContinueDirective | None = ContinueDirective.ORDINARY
            wait_reason: WaitReason | None = None
            terminal: TerminalDisposition | None = None
            reason_code = "runtime_work_remaining"
            waiting_request_id: str | None = None
            next_turn_revision = command.expected_turn_revision
            turn_status = "running"

            if unsafe_operations:
                blockers.append("unsafe_operation_outcome")
                decision = StopGuardDecision.WAIT
                directive = None
                wait_reason = WaitReason.EXTERNAL_RESOLUTION
                reason_code = "operation_reconciliation_required"
            elif pause_pending:
                blockers.append("pause_intent_pending")
                decision = StopGuardDecision.CONTINUE
                reason_code = "pause_convergence_required"
            elif unresolved_calls or active_operations:
                if unresolved_calls:
                    blockers.append("unresolved_tool_calls")
                if active_operations:
                    blockers.append("active_operations")
            elif command.requested_disposition in {
                TerminalDisposition.PAUSE,
                TerminalDisposition.FAIL,
            }:
                decision = StopGuardDecision.TERMINATE
                directive = None
                terminal = command.requested_disposition
                reason_code = f"agent_requested_{command.requested_disposition.value}"
            elif requires_user:
                blockers.append("user_decision_required")
                decision = StopGuardDecision.WAIT
                directive = None
                wait_reason = WaitReason.USER_CONFIRMATION
                reason_code = "evaluation_requires_user_decision"
            elif str(report["verdict"]) == "unknown":
                blockers.append("evaluation_unknown")
                directive = ContinueDirective.REVISE
                reason_code = "collect_required_evidence"
            elif not execution_ready:
                if command.requested_disposition is TerminalDisposition.PARTIAL:
                    decision = StopGuardDecision.TERMINATE
                    directive = None
                    terminal = TerminalDisposition.PARTIAL
                    reason_code = "partial_completion_accepted"
                else:
                    blockers.append("contract_not_execution_ready")
                    directive = ContinueDirective.REPLAN
                    reason_code = "normalize_completion_contract"
            elif incomplete_keys:
                if command.requested_disposition is TerminalDisposition.PARTIAL:
                    decision = StopGuardDecision.TERMINATE
                    directive = None
                    terminal = TerminalDisposition.PARTIAL
                    reason_code = "partial_completion_accepted"
                else:
                    blockers.extend(f"obligation:{key}" for key in incomplete_keys)
                    directive = ContinueDirective.REPLAN
                    reason_code = "required_obligations_incomplete"
            elif termination_blocks:
                blockers.extend(f"finding:{code}" for code in termination_blocks)
                directive = ContinueDirective.REVISE
                reason_code = "evaluation_blocks_termination"
            else:
                decision = StopGuardDecision.TERMINATE
                directive = None
                terminal = command.requested_disposition
                reason_code = "completion_ready"

            if decision is StopGuardDecision.WAIT:
                assert wait_reason is not None
                next_turn_revision += 1
                turn_status = "waiting"
                prompt = (
                    "The Turn cannot safely finish until the unresolved execution outcome is "
                    "reconciled."
                    if wait_reason is WaitReason.EXTERNAL_RESOLUTION
                    else (
                        "The completion evaluation requires your research decision "
                        "before proceeding."
                    )
                )
                connection.execute(
                    "UPDATE turns SET status = 'waiting', turn_revision = ? WHERE turn_id = ?",
                    (next_turn_revision, command.turn_id.value),
                )
                connection.execute(
                    """
                    INSERT INTO waiting_requests
                    VALUES (?, ?, NULL, ?, ?, 'open', ?, NULL, ?, NULL)
                    """,
                    (
                        identity.waiting_request_id.value,
                        command.turn_id.value,
                        wait_reason.value,
                        prompt,
                        next_turn_revision,
                        revision.value,
                    ),
                )
                waiting_request_id = identity.waiting_request_id.value
            elif decision is StopGuardDecision.TERMINATE:
                if str(turn["execution_mode"]) == "write":
                    cursor = connection.execute(
                        """
                        UPDATE workspace_write_lane
                        SET active_write_turn_id = NULL, lane_revision = lane_revision + 1
                        WHERE singleton_id = 1 AND active_write_turn_id = ?
                        """,
                        (command.turn_id.value,),
                    )
                    if cursor.rowcount != 1:
                        raise sqlite3.IntegrityError("active write Turn does not own the lane")
                next_turn_revision += 1
                assert terminal is not None
                turn_status = {
                    TerminalDisposition.SUCCEED: "succeeded",
                    TerminalDisposition.PARTIAL: "partial",
                    TerminalDisposition.PAUSE: "paused",
                    TerminalDisposition.FAIL: "failed",
                }[terminal]
                connection.execute(
                    "UPDATE turns SET status = ?, turn_revision = ? WHERE turn_id = ?",
                    (turn_status, next_turn_revision, command.turn_id.value),
                )

            connection.execute(
                """
                INSERT INTO stop_guard_decisions
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.decision_id.value,
                    command.turn_id.value,
                    command.expected_turn_revision,
                    contract_revision_id,
                    command.evaluation_report_id.value,
                    identity.goal_coverage_id.value,
                    decision.value,
                    None if directive is None else directive.value,
                    None if wait_reason is None else wait_reason.value,
                    None if terminal is None else terminal.value,
                    reason_code,
                    canonical_json(blockers),
                    revision.value,
                ),
            )
            response = {
                "decision_id": identity.decision_id.value,
                "goal_coverage_id": identity.goal_coverage_id.value,
                "decision": decision.value,
                "directive": None if directive is None else directive.value,
                "wait_reason": None if wait_reason is None else wait_reason.value,
                "terminal_disposition": None if terminal is None else terminal.value,
                "reason_code": reason_code,
                "blockers": blockers,
                "turn_revision": next_turn_revision,
                "turn_status": turn_status,
                "waiting_request_id": waiting_request_id,
            }
            events = [
                JournalDraft(
                    "agent.decision.recorded",
                    "stop_guard_decision",
                    identity.decision_id.value,
                    response,
                )
            ]
            if decision is StopGuardDecision.WAIT:
                events.append(JournalDraft("turn.waiting", "turn", command.turn_id.value, response))
            elif decision is StopGuardDecision.TERMINATE:
                events.append(
                    JournalDraft("turn.terminated", "turn", command.turn_id.value, response)
                )
            return MutationPayload(
                response,
                tuple(events),
                (OutboxDraft("workspace.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="stop_guard.natural_stop",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        waiting_id = response["waiting_request_id"]
        return StopGuardOutcome(
            StopGuardDecisionRecordId(str(response["decision_id"])),
            GoalCoverageId(str(response["goal_coverage_id"])),
            StopGuardDecision(str(response["decision"])),
            None
            if response["directive"] is None
            else ContinueDirective(str(response["directive"])),
            None if response["wait_reason"] is None else WaitReason(str(response["wait_reason"])),
            None
            if response["terminal_disposition"] is None
            else TerminalDisposition(str(response["terminal_disposition"])),
            str(response["reason_code"]),
            tuple(str(value) for value in response["blockers"]),
            int(response["turn_revision"]),
            str(response["turn_status"]),
            None if waiting_id is None else WaitingRequestId(str(waiting_id)),
            receipt.commit_revision,
            receipt.replayed,
        )
