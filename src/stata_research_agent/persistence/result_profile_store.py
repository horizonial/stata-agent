"""SQLite Run/Snapshot/Candidate/Qualification/Result vertical for regress."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from stata_research_agent.application.result_profile import (
    PromoteStataResultCommand,
    QualifyRegressResultCommand,
    RegisterGenericStataResultProfileCommand,
    RegisterRegressProfileCommand,
    RegressOperationFacts,
    ResultPromotionIdentity,
    ResultQualificationOutcome,
)
from stata_research_agent.domain.identifiers import (
    ResultCandidateId,
    ResultId,
    ResultQualificationReportId,
    RunId,
)
from stata_research_agent.domain.result_profile import (
    QualificationVerdict,
    RegisteredResultProfile,
    RegressProfileEvaluation,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


class SqliteResultProfileRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def register_profile(
        self,
        command: RegisterRegressProfileCommand | RegisterGenericStataResultProfileCommand,
        profile: RegisteredResultProfile,
    ) -> None:
        existing = self._connection.execute(
            """
            SELECT result_kind, command_family, snapshot_schema_version,
                   extractor_implementation_hash, activation_state
            FROM result_profiles
            WHERE result_profile_id = ? AND profile_version = ?
            """,
            (profile.profile_id, profile.version),
        ).fetchone()
        if existing is not None:
            expected = (
                "statistical",
                profile.command_family,
                profile.snapshot_schema_version,
                profile.extractor_implementation_hash,
                "active",
            )
            if tuple(existing) != expected:
                raise ValueError(
                    "registered Result Profile identity conflicts with builtin contract"
                )
            return
        request = {
            "result_profile_id": profile.profile_id,
            "profile_version": profile.version,
            "extractor_implementation_hash": profile.extractor_implementation_hash,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            connection.execute(
                """
                INSERT INTO result_profiles VALUES (
                    ?, ?, 'statistical', ?, ?, ?, 'active', ?
                )
                """,
                (
                    profile.profile_id,
                    profile.version,
                    profile.command_family,
                    profile.snapshot_schema_version,
                    profile.extractor_implementation_hash,
                    revision.value,
                ),
            )
            return MutationPayload(
                response=request,
                journal=(
                    JournalDraft(
                        "result_profile.activated",
                        "result_profile",
                        f"{profile.profile_id}@{profile.version}",
                        request,
                    ),
                ),
                outbox=(OutboxDraft("result_profile.changed", request),),
            )

        self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="result_profile.activate_builtin",
            request=request,
            mutation=mutate,
        )

    def load_operation(self, operation_id: str) -> RegressOperationFacts:
        row = self._connection.execute(
            """
            SELECT o.operation_id, a.operation_attempt_id,
                   m.completion_manifest_id, m.execution_status,
                   m.structured_result_status, m.structured_result_json,
                   m.receipt_json, m.session_id, m.session_generation,
                   b.input_data_version_id, b.input_data_slot_key,
                   b.input_verification_receipt_id, b.executable_source_id,
                   b.source_data_state_operation_id,
                   b.expected_data_state_token, b.expected_session_generation,
                   b.execution_purpose,
                   source_binding.execution_purpose AS source_execution_purpose,
                   json_extract(source_manifest.receipt_json, '$.exec_seq')
                       AS source_exec_seq,
                   EXISTS (
                       SELECT 1
                       FROM stata_runs AS source_run
                       JOIN results AS source_result
                         ON source_result.producing_stata_run_id = source_run.stata_run_id
                       WHERE source_run.operation_id = b.source_data_state_operation_id
                   ) AS source_has_formal_result,
                   s.command_text, s.command_sha256,
                   CASE WHEN av.verdict = 'verified'
                              AND av.verification_purpose = 'formal_run_input'
                              AND ast.availability = 'available'
                        THEN 1 ELSE 0 END AS input_is_currently_verified
            FROM operations AS o
            JOIN operation_attempts AS a ON a.operation_id = o.operation_id
            JOIN completion_manifests AS m
              ON m.operation_attempt_id = a.operation_attempt_id
            JOIN stata_operation_input_bindings AS b
              ON b.operation_attempt_id = a.operation_attempt_id
            JOIN executable_sources AS s
              ON s.executable_source_id = b.executable_source_id
            LEFT JOIN data_versions AS d
              ON d.data_version_id = b.input_data_version_id
            LEFT JOIN artifact_verification_receipts AS av
              ON av.verification_receipt_id = b.input_verification_receipt_id
             AND av.artifact_id = d.canonical_artifact_id
            LEFT JOIN artifact_states AS ast
              ON ast.artifact_id = d.canonical_artifact_id
            LEFT JOIN operations AS source_operation
              ON source_operation.operation_id = b.source_data_state_operation_id
            LEFT JOIN operation_attempts AS source_attempt
              ON source_attempt.operation_id = source_operation.operation_id
            LEFT JOIN completion_manifests AS source_manifest
              ON source_manifest.operation_attempt_id = source_attempt.operation_attempt_id
            LEFT JOIN stata_operation_input_bindings AS source_binding
              ON source_binding.operation_attempt_id = source_attempt.operation_attempt_id
            WHERE o.operation_id = ? AND o.status = 'completed'
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("formal Result requires a completed Stata Operation")
        structured_raw = row["structured_result_json"]
        structured = json.loads(str(structured_raw)) if structured_raw is not None else None
        receipt = json.loads(str(row["receipt_json"]))
        return RegressOperationFacts(
            operation_id=str(row["operation_id"]),
            attempt_id=str(row["operation_attempt_id"]),
            manifest_id=str(row["completion_manifest_id"]),
            execution_status=str(row["execution_status"]),
            structured_result_status=str(row["structured_result_status"]),
            structured=structured,
            receipt=receipt,
            input_data_version_id=(
                str(row["input_data_version_id"])
                if row["input_data_version_id"] is not None
                else None
            ),
            input_data_slot_key=(
                str(row["input_data_slot_key"]) if row["input_data_slot_key"] is not None else None
            ),
            input_verification_receipt_id=(
                str(row["input_verification_receipt_id"])
                if row["input_verification_receipt_id"] is not None
                else None
            ),
            input_is_currently_verified=bool(row["input_is_currently_verified"]),
            source_data_state_operation_id=(
                str(row["source_data_state_operation_id"])
                if row["source_data_state_operation_id"] is not None
                else None
            ),
            expected_data_state_token=(
                str(row["expected_data_state_token"])
                if row["expected_data_state_token"] is not None
                else None
            ),
            expected_session_generation=(
                int(row["expected_session_generation"])
                if row["expected_session_generation"] is not None
                else None
            ),
            executable_source_id=str(row["executable_source_id"]),
            command_text=str(row["command_text"]),
            command_sha256=str(row["command_sha256"]),
            session_id=str(row["session_id"]),
            session_generation=int(row["session_generation"]),
            execution_purpose=str(row["execution_purpose"]),
            source_execution_purpose=(
                str(row["source_execution_purpose"])
                if row["source_execution_purpose"] is not None
                else None
            ),
            source_has_formal_result=bool(row["source_has_formal_result"]),
            source_exec_seq=(
                int(row["source_exec_seq"])
                if row["source_exec_seq"] is not None
                else None
            ),
        )

    def commit_qualification(
        self,
        command: QualifyRegressResultCommand | PromoteStataResultCommand,
        profile: RegisteredResultProfile,
        facts: RegressOperationFacts,
        evaluation: RegressProfileEvaluation,
        identities: ResultPromotionIdentity,
    ) -> ResultQualificationOutcome:
        if isinstance(command, PromoteStataResultCommand):
            intended = {
                "source_contract": "stata.result-catalog/v1",
                "selected_source_keys": list(command.selected_source_keys),
                "result_summary": command.result_summary,
                "input_data_slot_key": facts.input_data_slot_key,
            }
        else:
            intended = {
                "command_family": profile.command_family,
                "dependent_variable": command.expected_dependent_variable,
                "target_terms": list(command.expected_terms),
                "input_data_slot_key": facts.input_data_slot_key,
                "absorbed_effects": list(command.expected_absorbed_effects),
                "cluster_variables": list(command.expected_cluster_variables),
                "vce": command.expected_vce,
                "endogenous_variables": list(command.expected_endogenous_variables),
                "included_exogenous_variables": list(command.expected_included_exogenous_variables),
                "excluded_instruments": list(command.expected_excluded_instruments),
            }
        request = {
            "operation_id": command.operation_id.value,
            "result_profile_id": profile.profile_id,
            "profile_version": profile.version,
            "intended_specification": intended,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            active = connection.execute(
                """
                SELECT 1 FROM result_profiles
                WHERE result_profile_id = ? AND profile_version = ?
                  AND activation_state = 'active'
                  AND extractor_implementation_hash = ?
                """,
                (
                    profile.profile_id,
                    profile.version,
                    profile.extractor_implementation_hash,
                ),
            ).fetchone()
            if active is None:
                raise ValueError("Result Profile is not registered and active")
            if facts.input_data_version_id is None or facts.input_data_slot_key is None:
                raise ValueError("formal Result requires a frozen Data Version input")
            if facts.execution_status != "succeeded":
                raise ValueError("formal Result requires successful Stata execution")
            plan_binding = self._validated_plan_binding(connection, command, facts)

            runtime_environment = facts.receipt.get("runtime_environment")
            if not isinstance(runtime_environment, Mapping):
                raise ValueError("Stata runtime environment snapshot is missing")
            environment_payload = {
                "runtime_environment": dict(runtime_environment),
                "profile_environment": (
                    dict(facts.structured["profile_environment"])
                    if facts.structured is not None
                    and isinstance(facts.structured.get("profile_environment"), Mapping)
                    else None
                ),
                "executor_instance_id": facts.receipt.get("executor_instance_id"),
                "receipt_schema_version": facts.receipt.get("schema_version"),
            }
            environment_json = canonical_json(environment_payload)
            connection.execute(
                "INSERT INTO environment_snapshots VALUES (?, 'stata', ?, ?, ?)",
                (
                    identities.environment_snapshot_id.value,
                    environment_json,
                    hashlib.sha256(environment_json.encode("utf-8")).hexdigest(),
                    revision.value,
                ),
            )
            data_state_token = facts.receipt.get("data_signature")
            if not isinstance(data_state_token, str) or not data_state_token:
                raise ValueError("formal Result requires a Stata data-state token")
            connection.execute(
                """
                INSERT INTO stata_runs VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', ?
                )
                """,
                (
                    identities.run_id.value,
                    facts.operation_id,
                    facts.attempt_id,
                    facts.manifest_id,
                    facts.executable_source_id,
                    facts.input_data_version_id,
                    facts.input_data_slot_key,
                    identities.environment_snapshot_id.value,
                    facts.session_id,
                    facts.session_generation,
                    data_state_token,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO research_command_instances VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    identities.command_instance_id.value,
                    identities.run_id.value,
                    facts.executable_source_id,
                    facts.command_text,
                    facts.command_sha256,
                    revision.value,
                ),
            )
            if plan_binding is not None:
                plan_revision_id, plan_node_id, expected_command_sha256 = plan_binding
                connection.execute(
                    """
                    INSERT INTO stata_run_plan_bindings
                    VALUES (?, ?, ?, 'matches', ?, ?, ?)
                    """,
                    (
                        identities.run_id.value,
                        plan_revision_id,
                        plan_node_id,
                        expected_command_sha256,
                        facts.command_sha256,
                        revision.value,
                    ),
                )
            connection.execute(
                "INSERT INTO result_contracts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identities.contract_id.value,
                    command.research_path_id.value,
                    command.created_by_turn_id.value,
                    profile.profile_id,
                    profile.version,
                    canonical_json(intended),
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO result_capture_points VALUES (?, ?, 1, 'formal_intent', ?)",
                (
                    identities.capture_point_id.value,
                    identities.command_instance_id.value,
                    revision.value,
                ),
            )
            snapshot = dict(facts.structured or {})
            snapshot_json = canonical_json(snapshot)
            connection.execute(
                "INSERT INTO result_capture_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identities.snapshot_id.value,
                    identities.capture_point_id.value,
                    profile.snapshot_schema_version,
                    snapshot_json,
                    hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
                    revision.value,
                ),
            )
            if evaluation.sample is not None:
                sample = evaluation.sample
                connection.execute(
                    """
                    INSERT INTO estimation_sample_manifests VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        identities.sample_manifest_id.value,
                        identities.snapshot_id.value,
                        facts.input_data_version_id,
                        str(sample["row_domain"]),
                        int(sample["row_count"]),
                        int(sample["included_count"]),
                        str(sample["encoding"]),
                        str(sample["mask_hex"]),
                        str(sample["mask_sha256"]),
                        revision.value,
                    ),
                )
            connection.execute(
                """
                INSERT INTO result_candidates VALUES (
                    ?, ?, ?, ?, 'formal_intent', ?, ?
                )
                """,
                (
                    identities.candidate_id.value,
                    identities.snapshot_id.value,
                    profile.profile_id,
                    profile.version,
                    command.created_by_turn_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO result_qualification_reports VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identities.qualification_report_id.value,
                    identities.candidate_id.value,
                    identities.contract_id.value,
                    evaluation.verdict.value,
                    json.dumps(list(evaluation.findings), separators=(",", ":")),
                    revision.value,
                ),
            )

            result_id: str | None = None
            if evaluation.verdict is QualificationVerdict.QUALIFIED:
                result_id = identities.result_id.value
                connection.execute(
                    "INSERT INTO results VALUES (?, ?, 'statistical', ?, ?, ?, ?)",
                    (
                        result_id,
                        identities.candidate_id.value,
                        identities.qualification_report_id.value,
                        identities.run_id.value,
                        command.created_by_turn_id.value,
                        revision.value,
                    ),
                )
                self._insert_elements(
                    connection,
                    revision,
                    result_id,
                    identities,
                    evaluation,
                )
                connection.execute(
                    "INSERT INTO result_required_data_dependencies VALUES (?, ?, ?, ?, ?)",
                    (
                        result_id,
                        facts.input_data_slot_key,
                        facts.input_data_version_id,
                        "analysis_dataset",
                        identities.run_id.value,
                    ),
                )

            response = {
                "run_id": identities.run_id.value,
                "candidate_id": identities.candidate_id.value,
                "qualification_report_id": identities.qualification_report_id.value,
                "verdict": evaluation.verdict.value,
                "findings": list(evaluation.findings),
                "result_id": result_id,
                "element_count": (
                    len(evaluation.elements)
                    if evaluation.verdict is QualificationVerdict.QUALIFIED
                    else 0
                ),
            }
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft(
                        "stata_run.recorded",
                        "stata_run",
                        identities.run_id.value,
                        {"operation_id": facts.operation_id},
                    ),
                    JournalDraft(
                        "result.qualification_recorded",
                        "result_candidate",
                        identities.candidate_id.value,
                        {
                            "verdict": evaluation.verdict.value,
                            "result_id": result_id,
                        },
                    ),
                ),
                outbox=(OutboxDraft("result.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type=(
                "result.stata.promote"
                if isinstance(command, PromoteStataResultCommand)
                else "result.regress.qualify"
            ),
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        result_id = response.get("result_id")
        return ResultQualificationOutcome(
            RunId(str(response["run_id"])),
            ResultCandidateId(str(response["candidate_id"])),
            ResultQualificationReportId(str(response["qualification_report_id"])),
            QualificationVerdict(str(response["verdict"])),
            tuple(str(item) for item in response["findings"]),
            ResultId(str(result_id)) if result_id is not None else None,
            int(response["element_count"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _validated_plan_binding(
        connection: sqlite3.Connection,
        command: QualifyRegressResultCommand | PromoteStataResultCommand,
        facts: RegressOperationFacts,
    ) -> tuple[str, str, str] | None:
        if command.plan_revision_id is None:
            return None
        assert command.plan_node_id is not None
        row = connection.execute(
            """
            SELECT 1
            FROM plan_revision_nodes AS node
            JOIN plan_revisions AS revision
              ON revision.plan_revision_id = node.plan_revision_id
            WHERE node.plan_revision_id = ? AND node.plan_node_id = ?
              AND EXISTS (
                  SELECT 1 FROM path_plan_adoption_history AS history
                  WHERE history.research_path_id = ?
                    AND history.target_plan_revision_id = node.plan_revision_id
              )
            """,
            (
                command.plan_revision_id.value,
                command.plan_node_id.value,
                command.research_path_id.value,
            ),
        ).fetchone()
        if row is None:
            raise ValueError(
                "formal Result Plan binding was never adopted by this Research Path"
            )
        # The Plan is semantic and intentionally does not whitelist commands.  Exact code
        # remains authoritative in ExecutableSource; the legacy hash pair records the
        # reproducible command identity until the storage column names are migrated.
        expected_hash = facts.command_sha256
        return (
            command.plan_revision_id.value,
            command.plan_node_id.value,
            expected_hash,
        )

    @staticmethod
    def _insert_elements(
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        result_id: str,
        identities: ResultPromotionIdentity,
        evaluation: RegressProfileEvaluation,
    ) -> None:
        for element, identity in zip(evaluation.elements, identities.elements, strict=True):
            primitive_ids: list[str] = []
            for primitive, primitive_id in zip(
                element.primitive_locators,
                identity.primitive_locator_ids,
                strict=True,
            ):
                primitive_ids.append(primitive_id.value)
                connection.execute(
                    """
                    INSERT INTO result_source_locators(
                        result_source_locator_id, result_capture_snapshot_id,
                        storage_locator_family, locator_json, created_revision
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        primitive_id.value,
                        identities.snapshot_id.value,
                        SqliteResultProfileRepository._locator_type(primitive),
                        canonical_json(dict(primitive)),
                        revision.value,
                    ),
                )
            locator = dict(element.locator)
            if identity.derivation_receipt_id is not None:
                locator["derivation_receipt_id"] = identity.derivation_receipt_id.value
            connection.execute(
                """
                INSERT INTO result_source_locators(
                    result_source_locator_id, result_capture_snapshot_id,
                    storage_locator_family, locator_json, created_revision
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    identity.locator_id.value,
                    identities.snapshot_id.value,
                    SqliteResultProfileRepository._locator_type(locator),
                    canonical_json(locator),
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO result_elements VALUES (
                    ?, ?, ?, ?, 'finite', ?, ?, NULL, 'native', 'level',
                    'estimated', ?, ?, ?
                )
                """,
                (
                    identity.element_id.value,
                    result_id,
                    element.semantic_key,
                    element.statistic_kind,
                    element.binary64_bits,
                    element.decimal_text,
                    element.authority,
                    identity.locator_id.value,
                    revision.value,
                ),
            )
            if identity.derivation_receipt_id is not None:
                connection.execute(
                    """
                    INSERT INTO trusted_derivation_receipts VALUES (
                        ?, ?, 1, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        identity.derivation_receipt_id.value,
                        element.derivation_profile_id,
                        json.dumps(primitive_ids, separators=(",", ":")),
                        canonical_json(dict(element.derivation_parameters or {})),
                        identity.element_id.value,
                        identities.environment_snapshot_id.value,
                        revision.value,
                    ),
                )

    @staticmethod
    def _locator_type(locator: Mapping[str, Any]) -> str:
        raw = str(locator.get("locator_type", "")).lower()
        mapping = {
            "e_scalar": "e_scalar",
            # The physical V0.1 enum predates method-neutral r-class promotion.  Preserve the
            # canonical R_SCALAR namespace in locator_json while using the legacy scalar bucket.
            "r_scalar": "e_scalar",
            "e_macro": "e_macro",
            "e_matrix_cell": "e_matrix_cell",
            # The V0.1 schema originally named the physical matrix-cell
            # discriminator after Stata's e() namespace.  Preserve that
            # storage enum for migration compatibility; locator_json retains
            # the exact R_MATRIX_CELL namespace and public lineage reads the
            # logical type from that canonical locator.
            "r_matrix_cell": "e_matrix_cell",
            "trusted_stata_derivation_receipt": "trusted_stata_derivation_receipt",
        }
        try:
            return mapping[raw]
        except KeyError as error:
            raise ValueError(f"unsupported Result source locator: {raw}") from error
