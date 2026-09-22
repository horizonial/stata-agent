"""SQLite repository for the registered one-model esttab RTF pipeline."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from stata_research_agent.application.stata_operation import StataOperationOutcome
from stata_research_agent.application.table_export import (
    CompletedTableArtifact,
    EsttabTableOutcome,
    ExportEsttabTableCommand,
    PreparedTableExport,
    RegisterEsttabProfileCommand,
    TableVerificationIdentity,
)
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    CommandId,
    ResultId,
    TableCoverageManifestId,
    TableExportInputManifestId,
    TableExportManifestId,
    TableRenderReceiptId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.table_export import (
    TableElement,
    VerifiedEsttabTable,
    cell_schema_for_result_profile,
    generic_cell_schema,
)

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)

EXPORT_PROFILE_ID = "stata.esttab.rtf.regression-table"
REGHDFE_EXPORT_PROFILE_ID = "stata.esttab.rtf.reghdfe-table"
IVREGRESS_EXPORT_PROFILE_ID = "stata.esttab.rtf.ivregress-2sls-table"
LOGIT_EXPORT_PROFILE_ID = "stata.esttab.rtf.logit-table"
GENERIC_EXPORT_PROFILE_ID = "stata.esttab.rtf.generic-result-table"
EXPORT_PROFILE_VERSION = 1
COMMAND_TEMPLATE = (
    "ereturn post <frozen_result_b> <frozen_result_V>, "
    "obs(<frozen_result_N>) dof(<frozen_result_df_r>)\n"
    "ereturn scalar <fit_statistic> = <frozen_result_fit>\n"
    "estimates store <system_alias>\n"
    "esttab <system_alias> using <staging_path>, replace rtf "
    "b(%18.3f) se(%18.3f) stats(N r2, fmt(0 3) "
    'labels("Observations" "R-squared")) '
    "star(* 0.05 ** 0.01 *** 0.001) title(<title>)\n"
    "estimates drop <system_alias>"
)
LOGIT_COMMAND_TEMPLATE = COMMAND_TEMPLATE.replace(
    'stats(N r2, fmt(0 3) labels("Observations" "R-squared"))',
    'stats(N r2_p, fmt(0 3) labels("Observations" "Pseudo R-squared"))',
)


class SqliteTableExportRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def register_profile(self, command: RegisterEsttabProfileCommand) -> None:
        request = {
            "export_profile_ids": [
                EXPORT_PROFILE_ID,
                LOGIT_EXPORT_PROFILE_ID,
                REGHDFE_EXPORT_PROFILE_ID,
                IVREGRESS_EXPORT_PROFILE_ID,
                GENERIC_EXPORT_PROFILE_ID,
            ],
            "profile_version": EXPORT_PROFILE_VERSION,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            active_profiles = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT result_profile_id FROM result_profiles
                    WHERE profile_version = 1 AND activation_state = 'active'
                      AND result_profile_id IN (
                          'linear_model.regress.v1', 'linear_model.reghdfe.v1',
                          'linear_model.ivregress_2sls.v1', 'binary_model.logit.v1',
                          'stata.generic-result.v1'
                      )
                    """
                ).fetchall()
            }
            if not active_profiles:
                raise ValueError("no bound statistical Result Profile is active")
            for export_profile_id, result_profile_id in (
                (EXPORT_PROFILE_ID, "linear_model.regress.v1"),
                (LOGIT_EXPORT_PROFILE_ID, "binary_model.logit.v1"),
                (REGHDFE_EXPORT_PROFILE_ID, "linear_model.reghdfe.v1"),
                (IVREGRESS_EXPORT_PROFILE_ID, "linear_model.ivregress_2sls.v1"),
                (GENERIC_EXPORT_PROFILE_ID, "stata.generic-result.v1"),
            ):
                if result_profile_id not in active_profiles:
                    continue
                profile_schema = (
                    generic_cell_schema(("mpg", "weight", "_cons"), "r2", "R-squared")
                    if result_profile_id == "stata.generic-result.v1"
                    else cell_schema_for_result_profile(result_profile_id)
                )
                schema = [
                    {
                        "cell_slot": slot,
                        "semantic_key": key,
                        "decimal_places": places,
                    }
                    for slot, key, places, _ in profile_schema
                ]
                connection.execute(
                    """
                    INSERT OR IGNORE INTO table_export_profiles VALUES (
                        ?, 1, ?, 1, ?, ?, 'active', ?
                    )
                    """,
                    (
                        export_profile_id,
                        result_profile_id,
                        hashlib.sha256(
                            (
                                LOGIT_COMMAND_TEMPLATE
                                if result_profile_id == "binary_model.logit.v1"
                                else COMMAND_TEMPLATE
                            ).encode("utf-8")
                        ).hexdigest(),
                        json.dumps(schema, separators=(",", ":")),
                        revision.value,
                    ),
                )
            return MutationPayload(
                request,
                (
                    JournalDraft(
                        "table_export_profile.activated",
                        "table_export_profile",
                        (
                            f"{EXPORT_PROFILE_ID}+{REGHDFE_EXPORT_PROFILE_ID}+"
                            f"{IVREGRESS_EXPORT_PROFILE_ID}+{LOGIT_EXPORT_PROFILE_ID}@1"
                        ),
                        request,
                    ),
                ),
                (OutboxDraft("table_export_profile.changed", request),),
            )

        self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="table_export_profile.activate_builtin",
            request=request,
            mutation=mutate,
        )

    def find_reusable_export(
        self, command: ExportEsttabTableCommand
    ) -> EsttabTableOutcome | None:
        """Reuse a verified table within the same Turn after document-only failure.

        The Turn restriction deliberately avoids treating a last-observed Artifact state as
        indefinitely fresh. Cross-Turn reuse belongs behind the Artifact verification policy.
        """

        format_parameters = canonical_json(
            {
                "coefficient_decimals": 3,
                "standard_error_decimals": 3,
                "N_decimals": 0,
                "r2_decimals": 3,
                "significance_thresholds": [0.05, 0.01, 0.001],
                "title": command.title,
                "coefficient_terms": list(command.coefficient_terms),
                "fit_statistic": command.fit_statistic,
                "fit_label": command.fit_label,
                "render_source": "immutable_result_elements",
            }
        )
        row = self._connection.execute(
            """
            SELECT input.table_export_input_manifest_id,
                   export.table_export_manifest_id,
                   render.table_render_receipt_id,
                   coverage.table_coverage_manifest_id,
                   export.table_artifact_id,
                   shape.controlled_cell_count,
                   export.created_revision
            FROM result_slots AS slot
            JOIN path_result_adoptions AS adoption
              ON adoption.result_slot_id = slot.result_slot_id
            JOIN table_export_input_manifests AS input
              ON input.result_slot_id = slot.result_slot_id
             AND input.source_result_id = adoption.target_result_id
            JOIN table_export_manifests AS export
              ON export.table_export_input_manifest_id = input.table_export_input_manifest_id
             AND export.estimation_state_gate = 'pass'
            JOIN table_render_receipts AS render
              ON render.table_export_manifest_id = export.table_export_manifest_id
             AND render.cell_evidence_gate = 'pass'
            JOIN table_numeric_coverage_manifests AS coverage
              ON coverage.table_render_receipt_id = render.table_render_receipt_id
             AND coverage.coverage_status = 'complete'
            JOIN table_render_shape_manifests AS shape
              ON shape.table_render_receipt_id = render.table_render_receipt_id
            JOIN artifact_states AS state
              ON state.artifact_id = export.table_artifact_id
             AND state.availability = 'available'
            WHERE slot.research_path_id = ? AND slot.canonical_key = ?
              AND slot.lifecycle = 'active'
              AND input.requested_by_turn_id = ?
              AND input.format_parameters_json = ?
            ORDER BY export.created_revision DESC
            LIMIT 1
            """,
            (
                command.research_path_id.value,
                command.result_slot_key,
                command.requested_by_turn_id.value,
                format_parameters,
            ),
        ).fetchone()
        if row is None:
            return None
        return EsttabTableOutcome(
            TableExportInputManifestId(str(row["table_export_input_manifest_id"])),
            TableExportManifestId(str(row["table_export_manifest_id"])),
            TableRenderReceiptId(str(row["table_render_receipt_id"])),
            TableCoverageManifestId(str(row["table_coverage_manifest_id"])),
            ArtifactId(str(row["table_artifact_id"])),
            "pass",
            "pass",
            int(row["controlled_cell_count"]),
            WorkspaceRevision(int(row["created_revision"])),
            True,
        )

    def prepare_export(
        self,
        command: ExportEsttabTableCommand,
        input_manifest_id: TableExportInputManifestId,
        stored_estimate_alias: str,
        output_relative_path: str,
    ) -> PreparedTableExport:
        request = {
            "research_path_id": command.research_path_id.value,
            "result_slot_key": command.result_slot_key,
            "requested_by_turn_id": command.requested_by_turn_id.value,
            "title": command.title,
            "coefficient_terms": list(command.coefficient_terms),
            "fit_statistic": command.fit_statistic,
            "fit_label": command.fit_label,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_turn_path(connection, command)
            facts = self._source_facts(connection, command)
            export_profile_id = self._export_profile_id(connection, str(facts["result_profile_id"]))
            result_profile_id = str(facts["result_profile_id"])
            elements = self._table_elements(
                connection,
                str(facts["result_id"]),
                result_profile_id,
                command.coefficient_terms,
                command.fit_statistic,
            )
            resolved_cell_schema = (
                generic_cell_schema(
                    command.coefficient_terms,
                    command.fit_statistic,
                    command.fit_label,
                )
                if result_profile_id == "stata.generic-result.v1"
                else cell_schema_for_result_profile(result_profile_id)
            )
            format_parameters = {
                "coefficient_decimals": 3,
                "standard_error_decimals": 3,
                "N_decimals": 0,
                "r2_decimals": 3,
                "significance_thresholds": [0.05, 0.01, 0.001],
                "title": command.title,
                "coefficient_terms": list(command.coefficient_terms),
                "fit_statistic": command.fit_statistic,
                "fit_label": command.fit_label,
                "render_source": "immutable_result_elements",
            }
            expected_shape = {
                "models": 1,
                "controlled_cells": len(elements),
                "semantic_keys": [element.semantic_key for element in elements],
                "resolved_cell_schema": [
                    {
                        "cell_slot": slot,
                        "semantic_key": semantic_key,
                        "decimal_places": decimal_places,
                    }
                    for slot, semantic_key, decimal_places, _anchor in resolved_cell_schema
                ],
            }
            connection.execute(
                """
                INSERT INTO table_export_input_manifests(
                    table_export_input_manifest_id, export_profile_id, profile_version,
                    research_path_id, result_slot_id, source_result_id,
                    source_stata_run_id, source_capture_snapshot_id,
                    source_result_candidate_id, session_id, session_generation,
                    data_state_token, source_exec_seq, stored_estimate_alias,
                    format_parameters_json, expected_shape_json, output_relative_path,
                    requested_by_turn_id, created_revision
                ) VALUES (
                    :manifest, :profile, 1, :path, :slot, :result, :run, :snapshot,
                    :candidate, :session, :generation, :data_state, :exec_seq, :alias,
                    :format, :shape, :output_path, :turn, :revision
                )
                """,
                {
                    "manifest": input_manifest_id.value,
                    "profile": export_profile_id,
                    "path": command.research_path_id.value,
                    "slot": str(facts["result_slot_id"]),
                    "result": str(facts["result_id"]),
                    "run": str(facts["stata_run_id"]),
                    "snapshot": str(facts["result_capture_snapshot_id"]),
                    "candidate": str(facts["result_candidate_id"]),
                    "session": str(facts["session_id"]),
                    "generation": int(facts["session_generation"]),
                    "data_state": str(facts["data_state_token"]),
                    "exec_seq": int(facts["source_exec_seq"]),
                    "alias": stored_estimate_alias,
                    "format": canonical_json(format_parameters),
                    "shape": canonical_json(expected_shape),
                    "output_path": output_relative_path,
                    "turn": command.requested_by_turn_id.value,
                    "revision": revision.value,
                },
            )
            response = {
                "input_manifest_id": input_manifest_id.value,
                "export_profile_id": export_profile_id,
                "result_profile_id": result_profile_id,
                **{key: facts[key] for key in facts.keys()},
                "stored_estimate_alias": stored_estimate_alias,
                "output_relative_path": output_relative_path,
                "coefficient_terms": list(command.coefficient_terms),
                "fit_statistic": command.fit_statistic,
                "fit_label": command.fit_label,
                "elements": [
                    {
                        "result_element_id": element.result_element_id,
                        "semantic_key": element.semantic_key,
                        "binary64_bits": element.binary64_bits,
                    }
                    for element in elements
                ],
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "table_export.input_frozen",
                        "table_export_input_manifest",
                        input_manifest_id.value,
                        {"source_result_id": str(facts["result_id"])},
                    ),
                ),
                (OutboxDraft("table_export.input_frozen", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="table_export.prepare",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return PreparedTableExport(
            TableExportInputManifestId(str(response["input_manifest_id"])),
            str(response["export_profile_id"]),
            str(response["result_profile_id"]),
            ResultId(str(response["result_id"])),
            str(response["result_slot_id"]),
            str(response["stata_run_id"]),
            str(response["session_id"]),
            int(response["session_generation"]),
            str(response["data_state_token"]),
            int(response["source_exec_seq"]),
            str(response["stored_estimate_alias"]),
            str(response["output_relative_path"]),
            tuple(
                TableElement(
                    str(item["result_element_id"]),
                    str(item["semantic_key"]),
                    str(item["binary64_bits"]),
                )
                for item in response["elements"]
            ),
            tuple(str(item) for item in response["coefficient_terms"]),
            str(response["fit_statistic"]),
            str(response["fit_label"]),
            (
                float(response["residual_degrees_of_freedom"])
                if response["residual_degrees_of_freedom"] is not None
                else None
            ),
            (
                str(response["dependent_variable"])
                if response["dependent_variable"] is not None
                else None
            ),
            receipt.commit_revision,
            receipt.replayed,
        )

    def load_completed_artifact(
        self,
        prepared: PreparedTableExport,
        operation: StataOperationOutcome,
    ) -> CompletedTableArtifact:
        row = self._connection.execute(
            """
            SELECT artifact.artifact_id, artifact.size_bytes, artifact.content_hash,
                   location.managed_handle, manifest.receipt_json
            FROM operations AS operation
            JOIN operation_attempts AS attempt
              ON attempt.operation_id = operation.operation_id
            JOIN completion_manifests AS manifest
              ON manifest.operation_attempt_id = attempt.operation_attempt_id
            JOIN completion_manifest_artifacts AS output
              ON output.completion_manifest_id = manifest.completion_manifest_id
             AND output.output_slot = 'table.esttab.regression'
            JOIN artifact_candidate_sources AS source
              ON source.artifact_candidate_id = output.artifact_candidate_id
            JOIN artifacts AS artifact ON artifact.artifact_id = source.artifact_id
            JOIN artifact_states AS state ON state.artifact_id = artifact.artifact_id
             AND state.availability = 'available'
            JOIN artifact_locations AS location ON location.artifact_id = artifact.artifact_id
            WHERE operation.operation_id = ? AND operation.status = 'completed'
              AND attempt.operation_attempt_id = ?
            """,
            (operation.operation_id.value, operation.attempt_id.value),
        ).fetchone()
        if row is None:
            raise ValueError("completed Table Export has no available Table Artifact")
        receipt = json.loads(str(row["receipt_json"]))
        return CompletedTableArtifact(
            ArtifactId(str(row["artifact_id"])),
            str(row["managed_handle"]),
            int(row["size_bytes"]),
            str(row["content_hash"]),
            int(receipt["session_generation"]),
            str(receipt["data_signature"]),
            int(receipt["exec_seq"]),
        )

    def commit_verification(
        self,
        command: ExportEsttabTableCommand,
        prepared: PreparedTableExport,
        operation: StataOperationOutcome,
        completed: CompletedTableArtifact,
        verified: VerifiedEsttabTable,
        identities: TableVerificationIdentity,
        finalization_command_id: CommandId,
    ) -> EsttabTableOutcome:
        if len(verified.cells) != len(prepared.elements):
            raise ValueError("verified Table shape differs from its frozen input manifest")
        if tuple(cell.result_element_id for cell in verified.cells) != tuple(
            element.result_element_id for element in prepared.elements
        ):
            raise ValueError("verified Table cells differ from the frozen Result Elements")
        request = {
            "input_manifest_id": prepared.input_manifest_id.value,
            "operation_id": operation.operation_id.value,
            "artifact_id": completed.artifact_id.value,
            "artifact_sha256": completed.sha256,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            self._assert_turn_path(connection, command)
            current = self._source_facts(connection, command)
            if str(current["result_id"]) != prepared.result_id.value:
                raise ValueError("Result adoption changed before Table Export finalization")
            artifact = connection.execute(
                """
                SELECT artifact.content_hash, artifact.size_bytes, state.availability
                FROM artifacts AS artifact
                JOIN artifact_states AS state ON state.artifact_id = artifact.artifact_id
                WHERE artifact.artifact_id = ?
                """,
                (completed.artifact_id.value,),
            ).fetchone()
            if artifact is None or tuple(artifact) != (
                completed.sha256,
                completed.size_bytes,
                "available",
            ):
                raise ValueError("Table Artifact changed before verification commit")
            connection.execute(
                """
                INSERT INTO table_export_manifests VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 'pass', ?, ?
                )
                """,
                (
                    identities.export_manifest_id.value,
                    prepared.input_manifest_id.value,
                    operation.operation_id.value,
                    operation.attempt_id.value,
                    completed.artifact_id.value,
                    completed.actual_session_generation,
                    completed.actual_data_state_token,
                    completed.actual_exec_seq,
                    canonical_json(
                        {
                            "render_source": "immutable_result_elements",
                            "source_result_id": prepared.result_id.value,
                            "source_stata_run_id": prepared.stata_run_id,
                            "live_estimation_state_required": False,
                            "rendered_cells_match_frozen_elements": True,
                            "export_execution_receipt_recorded": True,
                        }
                    ),
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO table_render_receipts VALUES (?, ?, ?, 'pass', ?, 8, ?)",
                (
                    identities.render_receipt_id.value,
                    identities.export_manifest_id.value,
                    prepared.result_id.value,
                    verified.visible_table_sha256,
                    revision.value,
                ),
            )
            for cell, identity in zip(verified.cells, identities.cells, strict=True):
                existing = connection.execute(
                    """
                    SELECT evidence_record_id FROM evidence_statistical_sources
                    WHERE result_element_id = ?
                    """,
                    (cell.result_element_id,),
                ).fetchone()
                reused = existing is not None
                if existing is None:
                    evidence_id = identity.evidence_record_candidate_id.value
                    connection.execute(
                        "INSERT INTO evidence_records VALUES (?, 'statistical_element', ?)",
                        (evidence_id, revision.value),
                    )
                    connection.execute(
                        "INSERT INTO evidence_statistical_sources VALUES (?, ?)",
                        (evidence_id, cell.result_element_id),
                    )
                else:
                    evidence_id = str(existing["evidence_record_id"])
                connection.execute(
                    "INSERT INTO evidence_issuance_receipts VALUES (?, ?, ?, 'issued', ?, ?, ?)",
                    (
                        identity.evidence_issuance_receipt_id.value,
                        evidence_id,
                        cell.result_element_id,
                        int(reused),
                        command.requested_by_turn_id.value,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO table_cell_evidence_uses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity.table_cell_use_id.value,
                        identities.render_receipt_id.value,
                        cell.semantic_cell_slot,
                        evidence_id,
                        cell.result_element_id,
                        cell.rendered_text,
                        cell.rtf_byte_start,
                        cell.rtf_byte_end,
                        revision.value,
                    ),
                )
            connection.execute(
                """
                INSERT INTO table_render_shape_manifests VALUES (?, ?, 4, ?)
                """,
                (
                    identities.render_receipt_id.value,
                    len(verified.cells),
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO table_numeric_coverage_manifests
                VALUES (?, ?, 'complete', 8, 4, 1, ?)
                """,
                (
                    identities.coverage_manifest_id.value,
                    identities.render_receipt_id.value,
                    revision.value,
                ),
            )
            response = {
                "input_manifest_id": prepared.input_manifest_id.value,
                "export_manifest_id": identities.export_manifest_id.value,
                "render_receipt_id": identities.render_receipt_id.value,
                "coverage_manifest_id": identities.coverage_manifest_id.value,
                "table_artifact_id": completed.artifact_id.value,
                "estimation_state_gate": "pass",
                "cell_evidence_gate": "pass",
                "cell_count": len(verified.cells),
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "table_export.verified",
                        "table_export_manifest",
                        identities.export_manifest_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("table_export.verified", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=finalization_command_id,
            command_type="table_export.verify",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return EsttabTableOutcome(
            TableExportInputManifestId(str(response["input_manifest_id"])),
            TableExportManifestId(str(response["export_manifest_id"])),
            TableRenderReceiptId(str(response["render_receipt_id"])),
            TableCoverageManifestId(str(response["coverage_manifest_id"])),
            ArtifactId(str(response["table_artifact_id"])),
            str(response["estimation_state_gate"]),
            str(response["cell_evidence_gate"]),
            int(response["cell_count"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _assert_turn_path(
        connection: sqlite3.Connection, command: ExportEsttabTableCommand
    ) -> None:
        exists = connection.execute(
            """
            SELECT 1 FROM turns WHERE turn_id = ? AND research_path_id = ?
              AND status IN ('running', 'waiting')
            """,
            (command.requested_by_turn_id.value, command.research_path_id.value),
        ).fetchone()
        if exists is None:
            raise ValueError("Table Export Turn is not active on this Research Path")

    @staticmethod
    def _source_facts(
        connection: sqlite3.Connection, command: ExportEsttabTableCommand
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT slot.result_slot_id, adoption.target_result_id AS result_id,
                   result.producing_stata_run_id AS stata_run_id,
                   contract.result_profile_id,
                   run.session_id, run.session_generation, run.data_state_token,
                   snapshot.result_capture_snapshot_id, candidate.result_candidate_id,
                   json_extract(manifest.receipt_json, '$.exec_seq') AS source_exec_seq,
                   json_extract(snapshot.snapshot_json, '$.depvar') AS dependent_variable,
                   (
                       SELECT json_extract(element.value, '$.value')
                       FROM json_each(
                           snapshot.snapshot_json, '$.result_catalog.elements'
                       ) AS element
                       WHERE json_extract(element.value, '$.source_key') = 'scalar.df_r'
                       LIMIT 1
                   ) AS residual_degrees_of_freedom
            FROM result_slots AS slot
            JOIN path_result_adoptions AS adoption
              ON adoption.result_slot_id = slot.result_slot_id
            JOIN results AS result ON result.result_id = adoption.target_result_id
            JOIN result_candidates AS candidate
              ON candidate.result_candidate_id = result.result_candidate_id
            JOIN result_qualification_reports AS report
              ON report.result_qualification_report_id =
                 result.originating_qualification_report_id
            JOIN result_contracts AS contract
              ON contract.result_contract_id = report.result_contract_id
            JOIN result_capture_snapshots AS snapshot
              ON snapshot.result_capture_snapshot_id = candidate.result_capture_snapshot_id
            JOIN stata_runs AS run ON run.stata_run_id = result.producing_stata_run_id
            JOIN completion_manifests AS manifest
              ON manifest.completion_manifest_id = run.completion_manifest_id
            WHERE slot.research_path_id = ? AND slot.canonical_key = ?
              AND slot.lifecycle = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM result_required_data_dependencies AS dependency
                  WHERE dependency.result_id = result.result_id
                    AND NOT EXISTS (
                        SELECT 1 FROM path_data_slots AS data_slot
                        JOIN path_data_adoptions AS data_adoption
                          ON data_adoption.path_data_slot_id = data_slot.path_data_slot_id
                        WHERE data_slot.research_path_id = slot.research_path_id
                          AND data_slot.canonical_key = dependency.input_data_slot_key
                          AND data_adoption.target_data_version_id = dependency.data_version_id
                    )
              )
            """,
            (command.research_path_id.value, command.result_slot_key),
        ).fetchone()
        if row is None or row["source_exec_seq"] is None:
            raise ValueError("current Result is not eligible for formal Table Export")
        if not isinstance(row, sqlite3.Row):
            raise TypeError("SQLite row factory contract is not active")
        return row

    @staticmethod
    def _export_profile_id(connection: sqlite3.Connection, result_profile_id: str) -> str:
        row = connection.execute(
            """
            SELECT export_profile_id FROM table_export_profiles
            WHERE bound_result_profile_id = ?
              AND bound_result_profile_version = 1
              AND profile_version = 1 AND activation_state = 'active'
            """,
            (result_profile_id,),
        ).fetchone()
        if row is None:
            raise ValueError("registered esttab Table Export Profile is not active")
        return str(row[0])

    @staticmethod
    def _table_elements(
        connection: sqlite3.Connection,
        result_id: str,
        result_profile_id: str,
        coefficient_terms: tuple[str, ...],
        fit_statistic: str,
    ) -> tuple[TableElement, ...]:
        required = tuple(
            item[1]
            for item in (
                generic_cell_schema(coefficient_terms, fit_statistic, "unused")
                if result_profile_id == "stata.generic-result.v1"
                else cell_schema_for_result_profile(result_profile_id)
            )
        )
        placeholders = ",".join("?" for _ in required)
        rows = connection.execute(
            f"""
            SELECT result_element_id, semantic_key, canonical_binary64_bits
            FROM result_elements
            WHERE result_id = ? AND value_kind = 'finite'
              AND estimate_status = 'estimated'
              AND semantic_key IN ({placeholders})
            """,
            (result_id, *required),
        ).fetchall()
        by_key = {str(row["semantic_key"]): row for row in rows}
        if set(by_key) != set(required):
            raise ValueError("Result is missing one or more registered table cells")
        return tuple(
            TableElement(
                str(by_key[key]["result_element_id"]),
                key,
                str(by_key[key]["canonical_binary64_bits"]),
            )
            for key in required
        )
