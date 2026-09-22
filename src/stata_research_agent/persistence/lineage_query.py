"""One-snapshot SQLite query for every supported Evidence lineage entry."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from stata_research_agent.application.lineage import (
    DataStateStep,
    LineageEntryKind,
    LineageSelector,
    TypedJournalLocator,
    UnifiedEvidenceLineage,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class _ResolvedEntry:
    evidence_record_id: str
    formal_result_block_id: str | None = None
    presentation_use_id: str | None = None
    rendered_text: str | None = None
    document_revision_id: str | None = None
    table_cell_evidence_use_id: str | None = None
    semantic_cell_slot: str | None = None


class SqliteEvidenceLineageQuery:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def load(self, selector: LineageSelector) -> UnifiedEvidenceLineage:
        owns_snapshot = not self._connection.in_transaction
        if owns_snapshot:
            self._connection.execute("BEGIN DEFERRED")
        try:
            revision = WorkspaceRevision(
                int(
                    self._connection.execute(
                        "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                    ).fetchone()[0]
                )
            )
            resolved = self._resolve(selector)
            source = self._load_source(resolved.evidence_record_id)
            data_state_steps = self._load_data_state_chain(str(source["operation_id"]))
            journal = self._load_journal(
                str(source["result_candidate_id"]), int(source["result_created_revision"])
            )
            chain = {
                key: source[key]
                for key in (
                    "evidence_record_id",
                    "result_element_id",
                    "result_source_locator_id",
                    "result_id",
                    "result_candidate_id",
                    "result_capture_snapshot_id",
                    "stata_run_id",
                    "research_command_instance_id",
                    "command_sha256",
                    "executable_source_id",
                    "operation_id",
                    "operation_attempt_id",
                    "completion_manifest_id",
                    "data_version_id",
                    "data_artifact_id",
                    "environment_snapshot_id",
                )
            }
            chain["data_state_steps"] = [
                {
                    "operation_id": step.operation_id,
                    "execution_purpose": step.execution_purpose,
                    "command_sha256": step.command_sha256,
                    "completion_manifest_id": step.completion_manifest_id,
                    "data_state_token": step.data_state_token,
                    "session_generation": step.session_generation,
                }
                for step in data_state_steps
            ]
            fingerprint = hashlib.sha256(
                json.dumps(chain, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            locator_json = str(source["locator_json"])
            logical_locator_type = str(source["locator_type"])
            parsed_locator = json.loads(locator_json)
            if isinstance(parsed_locator, dict) and isinstance(
                parsed_locator.get("locator_type"), str
            ):
                logical_locator_type = str(parsed_locator["locator_type"]).lower()
            return UnifiedEvidenceLineage(
                revision,
                selector,
                fingerprint,
                str(source["evidence_record_id"]),
                str(source["result_element_id"]),
                str(source["semantic_key"]),
                str(source["canonical_binary64_bits"]),
                str(source["canonical_decimal_text"]),
                str(source["result_source_locator_id"]),
                logical_locator_type,
                locator_json,
                str(source["result_id"]),
                str(source["result_candidate_id"]),
                str(source["result_capture_snapshot_id"]),
                str(source["stata_run_id"]),
                str(source["research_command_instance_id"]),
                str(source["command_text"]),
                str(source["command_sha256"]),
                str(source["executable_source_id"]),
                str(source["operation_id"]),
                str(source["operation_attempt_id"]),
                str(source["completion_manifest_id"]),
                str(source["data_version_id"]),
                str(source["data_artifact_id"]),
                str(source["input_data_slot_key"]),
                str(source["environment_snapshot_id"]),
                data_state_steps,
                journal,
                resolved.formal_result_block_id,
                resolved.presentation_use_id,
                resolved.rendered_text,
                resolved.document_revision_id,
                resolved.table_cell_evidence_use_id,
                resolved.semantic_cell_slot,
            )
        finally:
            if owns_snapshot:
                self._connection.rollback()

    def _resolve(self, selector: LineageSelector) -> _ResolvedEntry:
        if selector.entry_kind is LineageEntryKind.EVIDENCE_RECORD:
            row = self._connection.execute(
                "SELECT evidence_record_id FROM evidence_records WHERE evidence_record_id = ?",
                (selector.entry_id,),
            ).fetchone()
            if row is None:
                raise ValueError("EvidenceRecord does not exist")
            return _ResolvedEntry(str(row["evidence_record_id"]))
        if selector.entry_kind is LineageEntryKind.RESULT_ELEMENT:
            row = self._connection.execute(
                """
                SELECT evidence_record_id FROM evidence_statistical_sources
                WHERE result_element_id = ?
                """,
                (selector.entry_id,),
            ).fetchone()
            if row is None:
                raise ValueError("ResultElement has no issued formal EvidenceRecord")
            return _ResolvedEntry(str(row["evidence_record_id"]))
        if selector.entry_kind is LineageEntryKind.MESSAGE_OCCURRENCE:
            try:
                byte_start = int(str(selector.entry_subkey))
            except ValueError as error:
                raise ValueError("message occurrence subkey must be a UTF-8 byte offset") from error
            row = self._connection.execute(
                """
                SELECT use.evidence_record_id, use.evidence_presentation_use_id,
                       receipt.rendered_text
                FROM numeric_occurrences AS occurrence
                JOIN evidence_presentation_uses AS use
                  ON use.evidence_presentation_use_id = occurrence.evidence_presentation_use_id
                JOIN evidence_render_bindings AS binding
                  ON binding.evidence_presentation_use_id = use.evidence_presentation_use_id
                JOIN evidence_render_receipts AS receipt
                  ON receipt.evidence_render_receipt_id = binding.evidence_render_receipt_id
                WHERE occurrence.formal_result_block_id = ? AND occurrence.byte_start = ?
                """,
                (selector.entry_id, byte_start),
            ).fetchone()
            if row is None:
                raise ValueError("formal message has no numeric occurrence at that byte offset")
            return _ResolvedEntry(
                str(row["evidence_record_id"]),
                formal_result_block_id=selector.entry_id,
                presentation_use_id=str(row["evidence_presentation_use_id"]),
                rendered_text=str(row["rendered_text"]),
            )
        row = self._connection.execute(
            """
            SELECT cell.evidence_record_id, cell.table_cell_evidence_use_id,
                   cell.semantic_cell_slot, cell.rendered_text
            FROM document_revisions AS revision
            JOIN document_manifests AS manifest
              ON manifest.document_manifest_id = revision.document_manifest_id
            JOIN table_cell_evidence_uses AS cell
              ON cell.table_render_receipt_id = manifest.table_render_receipt_id
            WHERE revision.document_revision_id = ? AND cell.semantic_cell_slot = ?
            """,
            (selector.entry_id, selector.entry_subkey),
        ).fetchone()
        if row is None:
            raise ValueError("DocumentRevision has no verified Evidence cell at that semantic slot")
        return _ResolvedEntry(
            str(row["evidence_record_id"]),
            rendered_text=str(row["rendered_text"]),
            document_revision_id=selector.entry_id,
            table_cell_evidence_use_id=str(row["table_cell_evidence_use_id"]),
            semantic_cell_slot=str(row["semantic_cell_slot"]),
        )

    def _load_source(self, evidence_record_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT evidence.evidence_record_id,
                   element.result_element_id, element.semantic_key,
                   element.canonical_binary64_bits, element.canonical_decimal_text,
                   element.result_source_locator_id, locator.locator_type,
                   locator.locator_json, result.result_id, result.result_candidate_id,
                   result.created_revision AS result_created_revision,
                   candidate.result_capture_snapshot_id, run.stata_run_id,
                   command.research_command_instance_id, command.command_text,
                   command.command_sha256, run.executable_source_id,
                   run.operation_id, run.operation_attempt_id,
                   run.completion_manifest_id, run.input_data_version_id AS data_version_id,
                   data.canonical_artifact_id AS data_artifact_id,
                   run.input_data_slot_key, run.environment_snapshot_id
            FROM evidence_records AS evidence
            JOIN evidence_statistical_sources AS source
              ON source.evidence_record_id = evidence.evidence_record_id
            JOIN result_elements AS element
              ON element.result_element_id = source.result_element_id
            JOIN result_source_locators AS locator
              ON locator.result_source_locator_id = element.result_source_locator_id
            JOIN results AS result ON result.result_id = element.result_id
            JOIN result_candidates AS candidate
              ON candidate.result_candidate_id = result.result_candidate_id
            JOIN stata_runs AS run ON run.stata_run_id = result.producing_stata_run_id
            JOIN research_command_instances AS command
              ON command.stata_run_id = run.stata_run_id AND command.execution_ordinal = 1
            JOIN data_versions AS data ON data.data_version_id = run.input_data_version_id
            WHERE evidence.evidence_record_id = ?
            """,
            (evidence_record_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Evidence source chain is incomplete")
        if not isinstance(row, sqlite3.Row):
            raise TypeError("SQLite row factory contract is not active")
        return row

    def _load_data_state_chain(self, operation_id: str) -> tuple[DataStateStep, ...]:
        reverse_steps: list[DataStateStep] = []
        seen: set[str] = set()
        current: str | None = operation_id
        while current is not None:
            if current in seen or len(seen) >= 256:
                raise ValueError("Stata data-state predecessor chain is cyclic or too deep")
            seen.add(current)
            row = self._connection.execute(
                """
                SELECT operation.operation_id, binding.execution_purpose,
                       binding.source_data_state_operation_id,
                       source.command_text, source.command_sha256,
                       manifest.completion_manifest_id,
                       json_extract(manifest.receipt_json, '$.data_signature')
                           AS data_state_token,
                       manifest.session_generation
                FROM operations AS operation
                JOIN operation_attempts AS attempt USING (operation_id)
                JOIN stata_operation_input_bindings AS binding
                  ON binding.operation_attempt_id = attempt.operation_attempt_id
                JOIN executable_sources AS source
                  ON source.executable_source_id = binding.executable_source_id
                JOIN completion_manifests AS manifest
                  ON manifest.operation_attempt_id = attempt.operation_attempt_id
                WHERE operation.operation_id = ? AND operation.status = 'completed'
                  AND manifest.execution_status = 'succeeded'
                """,
                (current,),
            ).fetchone()
            if row is None or row["data_state_token"] is None:
                raise ValueError("Stata data-state predecessor chain is incomplete")
            reverse_steps.append(
                DataStateStep(
                    operation_id=str(row["operation_id"]),
                    execution_purpose=str(row["execution_purpose"]),
                    command_text=str(row["command_text"]),
                    command_sha256=str(row["command_sha256"]),
                    completion_manifest_id=str(row["completion_manifest_id"]),
                    data_state_token=str(row["data_state_token"]),
                    session_generation=int(row["session_generation"]),
                )
            )
            predecessor = row["source_data_state_operation_id"]
            current = str(predecessor) if predecessor is not None else None
        reverse_steps.reverse()
        if not reverse_steps or reverse_steps[0].execution_purpose != "data_load":
            raise ValueError("Stata data-state chain does not begin with a formal Data Load")
        return tuple(reverse_steps)

    def _load_journal(self, result_candidate_id: str, created_revision: int) -> TypedJournalLocator:
        row = self._connection.execute(
            """
            SELECT journal_entry_id, workspace_revision, ordinal, event_type,
                   object_type, object_id
            FROM journal_entries
            WHERE workspace_revision = ?
              AND event_type = 'result.qualification_recorded'
              AND object_type = 'result_candidate' AND object_id = ?
            """,
            (created_revision, result_candidate_id),
        ).fetchone()
        if row is None:
            raise ValueError("Result source Journal locator is missing")
        return TypedJournalLocator(
            str(row["journal_entry_id"]),
            WorkspaceRevision(int(row["workspace_revision"])),
            int(row["ordinal"]),
            str(row["event_type"]),
            str(row["object_type"]),
            str(row["object_id"]),
        )
