"""Read-only deterministic audit for a versioned Founder research journey."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from stata_research_agent.application.founder_acceptance import (
    ControlledJourneyAuditRequest,
    FounderAcceptanceError,
    FounderAuditMode,
    FounderAuditStatus,
    FounderJourneyAuditReport,
    FounderJourneyAuditRequest,
    FounderObservation,
)
from stata_research_agent.application.lineage import LineageEntryKind, LineageSelector
from stata_research_agent.application.ports.founder_acceptance import (
    InstalledCandidateVerifier,
)
from stata_research_agent.persistence.lineage_query import SqliteEvidenceLineageQuery


class SqliteFounderJourneyAuditor:
    def __init__(
        self,
        connection: sqlite3.Connection,
        candidate_verifier: InstalledCandidateVerifier | None = None,
    ) -> None:
        self._connection = connection
        self._candidate_verifier = candidate_verifier

    def audit(self, request: FounderJourneyAuditRequest) -> FounderJourneyAuditReport:
        if request.scenario.journey.value != "autonomous":
            raise FounderAcceptanceError("this auditor only handles the autonomous journey")
        checks: dict[str, Callable[[], tuple[bool, str]]] = {
            "fixture.identity_match": lambda: (
                request.observed_fixture_sha256 == request.scenario.fixture.sha256,
                "observed fixture hash matches the immutable scenario fixture",
            ),
            "turn.succeeded": lambda: self._exists(
                "SELECT 1 FROM turns WHERE turn_id = ? AND status = 'succeeded'",
                (request.turn_id,),
                "Turn reached authoritative succeeded state",
            ),
            "plan.adopted": lambda: self._exists(
                "SELECT 1 FROM path_plan_adoptions WHERE research_path_id = ?",
                (request.research_path_id,),
                "Research Path has an adopted Plan Revision",
            ),
            "data.current": lambda: self._exists(
                """
                SELECT 1 FROM path_data_slots AS slot
                JOIN path_data_adoptions AS adoption
                  ON adoption.path_data_slot_id = slot.path_data_slot_id
                WHERE slot.research_path_id = ? AND slot.canonical_key = ?
                  AND slot.lifecycle = 'active'
                """,
                (request.research_path_id, request.scenario.data_slot_key),
                "scenario Data Slot has a current adopted Data Version",
            ),
            "stata.plan_match": lambda: self._current_result_chain(
                request, "binding.adherence_verdict = 'matches'"
            ),
            "result.qualified_stata": lambda: self._current_result_chain(
                request,
                "qualification.verdict = 'qualified' AND run.run_status = 'succeeded'",
            ),
            "result.current": lambda: self._current_result_chain(
                request,
                "run.input_data_version_id = data_adoption.target_data_version_id",
            ),
            "word.delivery_pass": lambda: self._delivery(
                request, "gate.verdict = 'pass' AND state.availability = 'available'"
            ),
            "word.numeric_coverage_complete": lambda: self._delivery(
                request,
                "coverage.coverage_status = 'complete' "
                "AND coverage.controlled_cell_count = 8 "
                "AND (SELECT count(*) FROM table_cell_evidence_uses AS uses "
                "WHERE uses.table_render_receipt_id = render.table_render_receipt_id) = 8",
                with_coverage=True,
            ),
            "goal.coverage_satisfied": lambda: self._exists(
                """
                SELECT 1 FROM goal_coverages
                WHERE turn_id = ? AND coverage_status = 'satisfied'
                  AND required_total = required_satisfied
                """,
                (request.turn_id,),
                "all required Completion Contract obligations are satisfied",
            ),
        }
        forbidden: dict[str, Callable[[], tuple[bool, str]]] = {
            "plan.deviation": lambda: self._detected(
                """
                SELECT 1 FROM results AS result
                JOIN stata_run_plan_bindings AS binding
                  ON binding.stata_run_id = result.producing_stata_run_id
                JOIN result_slots AS slot ON slot.research_path_id = ?
                JOIN path_result_adoptions AS adoption
                  ON adoption.result_slot_id = slot.result_slot_id
                 AND adoption.target_result_id = result.result_id
                WHERE binding.adherence_verdict = 'deviates'
                """,
                (request.research_path_id,),
                "a current formal Result deviates from its Plan binding",
            ),
            "non_stata_formal_result": lambda: self._detected(
                """
                SELECT 1 FROM result_slots AS slot
                JOIN path_result_adoptions AS adoption
                  ON adoption.result_slot_id = slot.result_slot_id
                JOIN results AS result ON result.result_id = adoption.target_result_id
                LEFT JOIN stata_runs AS run
                  ON run.stata_run_id = result.producing_stata_run_id
                WHERE slot.research_path_id = ? AND run.stata_run_id IS NULL
                """,
                (request.research_path_id,),
                "a current formal Result lacks a Stata Run source",
            ),
            "unqualified_current_result": lambda: self._detected(
                """
                SELECT 1 FROM result_slots AS slot
                JOIN path_result_adoptions AS adoption
                  ON adoption.result_slot_id = slot.result_slot_id
                JOIN results AS result ON result.result_id = adoption.target_result_id
                JOIN result_qualification_reports AS report
                  ON report.result_qualification_report_id =
                     result.originating_qualification_report_id
                WHERE slot.research_path_id = ? AND report.verdict != 'qualified'
                """,
                (request.research_path_id,),
                "a current Result was adopted without a qualified report",
            ),
            "word.delivery_gap": lambda: self._detected(
                """
                SELECT 1 FROM document_slots AS slot
                JOIN path_document_adoptions AS adoption
                  ON adoption.document_slot_id = slot.document_slot_id
                LEFT JOIN delivery_gate_reports AS gate
                  ON gate.document_revision_id = adoption.target_document_revision_id
                WHERE slot.research_path_id = ?
                  AND slot.canonical_key = 'manuscript.main.delivery'
                  AND (gate.verdict IS NULL OR gate.verdict != 'pass')
                """,
                (request.research_path_id,),
                "the current delivery manuscript has a missing or failing gate",
            ),
        }

        observations: list[FounderObservation] = []
        findings: list[str] = []
        for code in request.scenario.required_observations:
            check = checks.get(code)
            if check is None:
                raise FounderAcceptanceError(f"unknown required observation: {code}")
            satisfied, detail = check()
            observations.append(FounderObservation(code, satisfied, detail))
            if not satisfied:
                findings.append(f"REQUIRED_OBSERVATION_MISSING:{code}")
        for code in request.scenario.forbidden_observations:
            check = forbidden.get(code)
            if check is None:
                raise FounderAcceptanceError(f"unknown forbidden observation: {code}")
            detected, detail = check()
            observations.append(FounderObservation(code, not detected, detail))
            if detected:
                findings.append(f"FORBIDDEN_OBSERVATION_DETECTED:{code}")

        return self._finalize(
            request.scenario.scenario_id,
            request.scenario.version,
            request.scenario.scenario_sha256,
            request.mode,
            request.exact_candidate_sha256,
            observations,
            findings,
        )

    def audit_controlled(self, request: ControlledJourneyAuditRequest) -> FounderJourneyAuditReport:
        if request.scenario.journey.value != "controlled":
            raise FounderAcceptanceError("this auditor only handles the controlled journey")
        required: dict[str, Callable[[], tuple[bool, str]]] = {
            "fixture.identity_match": lambda: (
                request.observed_fixture_sha256 == request.scenario.fixture.sha256,
                "observed fixture hash matches the immutable scenario fixture",
            ),
            "control.waiting_answered": lambda: self._exists(
                """
                SELECT 1 FROM waiting_requests AS request
                JOIN waiting_answers AS answer
                  ON answer.waiting_request_id = request.waiting_request_id
                WHERE request.turn_id = ? AND request.status = 'answered'
                """,
                (request.predecessor_turn_id,),
                "the researcher answered the typed Waiting request",
            ),
            "control.pause_completed": lambda: self._exists(
                """
                SELECT 1 FROM pause_intents AS pause
                JOIN turns AS turn ON turn.turn_id = pause.turn_id
                WHERE pause.turn_id = ? AND pause.status = 'completed'
                  AND turn.status = 'paused'
                """,
                (request.predecessor_turn_id,),
                "safe pause converged and the predecessor remains paused",
            ),
            "control.continuation_created": lambda: self._exists(
                """
                SELECT 1 FROM turn_continuations AS continuation
                JOIN turns AS successor
                  ON successor.turn_id = continuation.successor_turn_id
                WHERE continuation.predecessor_turn_id = ?
                  AND continuation.successor_turn_id = ?
                  AND continuation.relation_kind = 'user_pause_continuation'
                  AND successor.status = 'running'
                """,
                (request.predecessor_turn_id, request.successor_turn_id),
                "a new running Turn continues the paused predecessor",
            ),
            "path.branch_created": lambda: self._exists(
                """
                SELECT 1 FROM research_path_parents AS parent
                JOIN path_branch_manifests AS manifest
                  ON manifest.child_research_path_id = parent.child_research_path_id
                WHERE parent.parent_research_path_id = ?
                  AND parent.child_research_path_id = ?
                  AND parent.created_by_turn_id = ?
                """,
                (
                    request.parent_research_path_id,
                    request.child_research_path_id,
                    request.successor_turn_id,
                ),
                "a child Research Path preserves an explicit parent relation",
            ),
            "path.branch_snapshot_preserved": lambda: (
                self._path_snapshot(request.parent_research_path_id)
                == self._path_snapshot(request.child_research_path_id),
                "the new branch initially copied Plan/Data/Result/Document adoptions",
            ),
            "lineage.numeric_reverse_lookup": lambda: self._lineage(request.evidence_record_id),
        }
        forbidden: dict[str, Callable[[], tuple[bool, str]]] = {
            "predecessor.resumed": lambda: self._detected(
                "SELECT 1 FROM turns WHERE turn_id = ? AND status != 'paused'",
                (request.predecessor_turn_id,),
                "the old paused Turn was incorrectly resumed",
            ),
            "branch.missing_parent": lambda: (
                not self._exists(
                    """
                    SELECT 1 FROM research_path_parents
                    WHERE parent_research_path_id = ? AND child_research_path_id = ?
                    """,
                    (request.parent_research_path_id, request.child_research_path_id),
                    "branch parent relation exists",
                )[0],
                "the branch lost its explicit parent relation",
            ),
            "lineage.untraceable": lambda: (
                not self._lineage(request.evidence_record_id)[0],
                "the selected number cannot be traced to Data/Command/Run/Locator",
            ),
        }
        observations: list[FounderObservation] = []
        findings: list[str] = []
        for code in request.scenario.required_observations:
            check = required.get(code)
            if check is None:
                raise FounderAcceptanceError(f"unknown required observation: {code}")
            satisfied, detail = check()
            observations.append(FounderObservation(code, satisfied, detail))
            if not satisfied:
                findings.append(f"REQUIRED_OBSERVATION_MISSING:{code}")
        for code in request.scenario.forbidden_observations:
            check = forbidden.get(code)
            if check is None:
                raise FounderAcceptanceError(f"unknown forbidden observation: {code}")
            detected, detail = check()
            observations.append(FounderObservation(code, not detected, detail))
            if detected:
                findings.append(f"FORBIDDEN_OBSERVATION_DETECTED:{code}")
        return self._finalize(
            request.scenario.scenario_id,
            request.scenario.version,
            request.scenario.scenario_sha256,
            request.mode,
            request.exact_candidate_sha256,
            observations,
            findings,
        )

    def _finalize(
        self,
        scenario_id: str,
        scenario_version: int,
        scenario_sha256: str,
        mode: FounderAuditMode,
        exact_candidate_sha256: str | None,
        observations: list[FounderObservation],
        findings: list[str],
    ) -> FounderJourneyAuditReport:
        candidate_identity_valid = exact_candidate_sha256 is not None
        if candidate_identity_valid and (
            len(exact_candidate_sha256 or "") != 64
            or any(char not in "0123456789abcdef" for char in (exact_candidate_sha256 or ""))
        ):
            findings.append("EXACT_CANDIDATE_IDENTITY_INVALID")
            candidate_identity_valid = False
        candidate_bound = bool(
            candidate_identity_valid
            and self._candidate_verifier is not None
            and self._candidate_verifier.is_verified_active_candidate(exact_candidate_sha256 or "")
        )
        if mode is FounderAuditMode.FOUNDER_ACCEPTANCE and not candidate_bound:
            findings.append("VERIFIED_ACTIVE_INSTALLED_CANDIDATE_REQUIRED")

        acceptance_eligible = not findings and candidate_bound
        if findings:
            status = FounderAuditStatus.FAILED
        elif mode is FounderAuditMode.FOUNDER_ACCEPTANCE:
            status = FounderAuditStatus.ACCEPTANCE_PASSED
        else:
            status = FounderAuditStatus.IMPLEMENTATION_PASSED
        return FounderJourneyAuditReport(
            status,
            scenario_id,
            scenario_version,
            scenario_sha256,
            exact_candidate_sha256,
            acceptance_eligible,
            tuple(observations),
            tuple(findings),
        )

    def _path_snapshot(self, research_path_id: str) -> tuple[object, ...]:
        plan = self._connection.execute(
            "SELECT target_plan_revision_id FROM path_plan_adoptions WHERE research_path_id = ?",
            (research_path_id,),
        ).fetchone()
        data = tuple(
            tuple(row)
            for row in self._connection.execute(
                """
                SELECT slot.canonical_key, adoption.target_data_version_id
                FROM path_data_slots AS slot
                JOIN path_data_adoptions AS adoption
                  ON adoption.path_data_slot_id = slot.path_data_slot_id
                WHERE slot.research_path_id = ? ORDER BY slot.canonical_key
                """,
                (research_path_id,),
            )
        )
        results = tuple(
            tuple(row)
            for row in self._connection.execute(
                """
                SELECT slot.canonical_key, adoption.target_result_id
                FROM result_slots AS slot
                JOIN path_result_adoptions AS adoption
                  ON adoption.result_slot_id = slot.result_slot_id
                WHERE slot.research_path_id = ? ORDER BY slot.canonical_key
                """,
                (research_path_id,),
            )
        )
        documents = tuple(
            tuple(row)
            for row in self._connection.execute(
                """
                SELECT slot.canonical_key, adoption.target_document_revision_id
                FROM document_slots AS slot
                JOIN path_document_adoptions AS adoption
                  ON adoption.document_slot_id = slot.document_slot_id
                WHERE slot.research_path_id = ? ORDER BY slot.canonical_key
                """,
                (research_path_id,),
            )
        )
        return (None if plan is None else str(plan[0]), data, results, documents)

    def _lineage(self, evidence_record_id: str) -> tuple[bool, str]:
        try:
            lineage = SqliteEvidenceLineageQuery(self._connection).load(
                LineageSelector(LineageEntryKind.EVIDENCE_RECORD, evidence_record_id)
            )
        except ValueError:
            return False, "Evidence lineage query did not resolve"
        complete = bool(
            lineage.command_text.strip()
            and lineage.data_version_id
            and lineage.data_artifact_id
            and lineage.stata_run_id
            and lineage.result_source_locator_id
            and lineage.locator_json
        )
        return complete, "Evidence resolves to Data, command, Stata Run, and source locator"

    def _current_result_chain(
        self, request: FounderJourneyAuditRequest, predicate: str
    ) -> tuple[bool, str]:
        return self._exists(
            f"""
            SELECT 1 FROM result_slots AS slot
            JOIN path_result_adoptions AS result_adoption
              ON result_adoption.result_slot_id = slot.result_slot_id
            JOIN results AS result
              ON result.result_id = result_adoption.target_result_id
            JOIN result_qualification_reports AS qualification
              ON qualification.result_qualification_report_id =
                 result.originating_qualification_report_id
            JOIN stata_runs AS run ON run.stata_run_id = result.producing_stata_run_id
            JOIN stata_run_plan_bindings AS binding
              ON binding.stata_run_id = run.stata_run_id
            JOIN path_plan_adoptions AS plan
              ON plan.research_path_id = slot.research_path_id
             AND plan.target_plan_revision_id = binding.plan_revision_id
            JOIN path_data_slots AS data_slot
              ON data_slot.research_path_id = slot.research_path_id
             AND data_slot.canonical_key = ?
            JOIN path_data_adoptions AS data_adoption
              ON data_adoption.path_data_slot_id = data_slot.path_data_slot_id
            WHERE slot.research_path_id = ? AND slot.canonical_key = ?
              AND {predicate}
            """,
            (
                request.scenario.data_slot_key,
                request.research_path_id,
                request.scenario.result_slot_key,
            ),
            f"current {request.scenario.result_slot_key} satisfies {predicate}",
        )

    def _delivery(
        self,
        request: FounderJourneyAuditRequest,
        predicate: str,
        *,
        with_coverage: bool = False,
    ) -> tuple[bool, str]:
        coverage_joins = ""
        if with_coverage:
            coverage_joins = """
                JOIN document_manifests AS manifest
                  ON manifest.document_manifest_id = revision.document_manifest_id
                JOIN table_render_receipts AS render
                  ON render.table_render_receipt_id = manifest.table_render_receipt_id
                JOIN table_numeric_coverage_manifests AS coverage
                  ON coverage.table_coverage_manifest_id =
                     manifest.table_coverage_manifest_id
            """
        return self._exists(
            f"""
            SELECT 1 FROM document_slots AS slot
            JOIN path_document_adoptions AS adoption
              ON adoption.document_slot_id = slot.document_slot_id
            JOIN document_revisions AS revision
              ON revision.document_revision_id = adoption.target_document_revision_id
            JOIN delivery_gate_reports AS gate
              ON gate.document_revision_id = revision.document_revision_id
            JOIN artifact_states AS state ON state.artifact_id = revision.docx_artifact_id
            {coverage_joins}
            WHERE slot.research_path_id = ?
              AND slot.canonical_key = 'manuscript.main.delivery'
              AND {predicate}
            """,
            (request.research_path_id,),
            f"current delivery manuscript satisfies {predicate}",
        )

    def _exists(self, sql: str, parameters: tuple[object, ...], detail: str) -> tuple[bool, str]:
        return self._connection.execute(sql, parameters).fetchone() is not None, detail

    def _detected(self, sql: str, parameters: tuple[object, ...], detail: str) -> tuple[bool, str]:
        return self._connection.execute(sql, parameters).fetchone() is not None, detail
