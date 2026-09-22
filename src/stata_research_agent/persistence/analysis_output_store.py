"""SQLite Analysis Output classification, adoption, and eligibility UoWs."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, cast

from stata_research_agent.application.analysis_output import (
    AdoptAnalysisOutputCommand,
    AdoptedAnalysisOutput,
    AnalysisAdoptionIdentity,
    AnalysisOutputIdentity,
    ClassifiedAnalysisOutput,
    ClassifyAnalysisOutputCommand,
)
from stata_research_agent.domain.analysis_output import AnalysisOutputKind
from stata_research_agent.domain.identifiers import (
    AnalysisDocumentEligibilityId,
    AnalysisOutputAdoptionId,
    AnalysisOutputClassificationId,
    AnalysisOutputId,
    EvidenceRecordId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


class SqliteAnalysisOutputRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def classify(
        self,
        command: ClassifyAnalysisOutputCommand,
        identity: AnalysisOutputIdentity,
    ) -> ClassifiedAnalysisOutput:
        request = {
            "requested_by_turn_id": command.requested_by_turn_id.value,
            "operation_id": command.operation_id.value,
            "attempt_id": command.attempt_id.value,
            "executable_artifact_id": command.executable_artifact_id.value,
            "input_artifact_ids": [item.value for item in command.input_artifact_ids],
            "outputs": [
                {"artifact_id": item.artifact_id.value, "role": item.role}
                for item in command.outputs
            ],
            "elements": [
                {
                    "stable_key": item.stable_key,
                    "value": item.value,
                    "rendered_text": item.rendered_text,
                }
                for item in command.elements
            ],
            "output_kind": command.output_kind.value,
            "method_summary": command.method_summary,
            "environment": dict(command.environment),
            "classification_basis": dict(command.classification_basis),
            "classifier_id": command.classifier_id,
            "classifier_version": command.classifier_version,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            operation = connection.execute(
                """
                SELECT o.operation_kind, o.status AS operation_status,
                       o.requested_by_turn_id, a.status AS attempt_status
                FROM operations AS o
                JOIN operation_attempts AS a ON a.operation_id = o.operation_id
                WHERE o.operation_id = ? AND a.operation_attempt_id = ?
                """,
                (command.operation_id.value, command.attempt_id.value),
            ).fetchone()
            if operation is None:
                raise ValueError("Analysis Output producer Operation/Attempt does not exist")
            expected_runtime = {
                "python.execute": "python",
                "shell.execute": "shell",
            }.get(str(operation["operation_kind"]))
            if expected_runtime is None:
                raise ValueError("Analysis Output must be produced by Python or Shell")
            if (
                str(operation["operation_status"]) != "completed"
                or str(operation["attempt_status"]) != "completed"
            ):
                raise ValueError("Analysis Output producer must be durably completed")
            if str(operation["requested_by_turn_id"]) != command.requested_by_turn_id.value:
                raise ValueError("Analysis Output producer belongs to a different Turn")

            executable = self._artifact(connection, command.executable_artifact_id.value)
            if str(executable["artifact_kind"]) != "code":
                raise ValueError("Analysis Output executable source must be a code Artifact")
            input_rows = [
                self._artifact(connection, artifact_id.value)
                for artifact_id in command.input_artifact_ids
            ]
            output_rows = [
                self._artifact(connection, binding.artifact_id.value) for binding in command.outputs
            ]
            for row in output_rows:
                if str(row["producer_attempt_id"]) != command.attempt_id.value:
                    raise ValueError(
                        "Analysis Output Artifact is not owned by the producer Attempt"
                    )

            environment_json = canonical_json(dict(command.environment))
            environment_sha256 = hashlib.sha256(environment_json.encode("utf-8")).hexdigest()
            element_payloads = []
            for item in command.elements:
                value_json = canonical_json(item.value)
                element_payloads.append(
                    {
                        "stable_key": item.stable_key,
                        "value_json": value_json,
                        "rendered_text": item.rendered_text,
                        "value_sha256": hashlib.sha256(value_json.encode("utf-8")).hexdigest(),
                    }
                )
            fingerprint_payload: dict[str, Any] = {
                "schema_version": "analysis-output/v1",
                "operation_id": command.operation_id.value,
                "attempt_id": command.attempt_id.value,
                "runtime_kind": expected_runtime,
                "executable": {
                    "artifact_id": command.executable_artifact_id.value,
                    "sha256": str(executable["content_hash"]),
                },
                "inputs": [
                    {
                        "artifact_id": str(row["artifact_id"]),
                        "sha256": str(row["content_hash"]),
                    }
                    for row in input_rows
                ],
                "outputs": [
                    {
                        "artifact_id": binding.artifact_id.value,
                        "sha256": str(row["content_hash"]),
                        "role": binding.role,
                    }
                    for binding, row in zip(command.outputs, output_rows, strict=True)
                ],
                "elements": element_payloads,
                "environment_sha256": environment_sha256,
            }
            output_fingerprint = hashlib.sha256(
                canonical_json(fingerprint_payload).encode("utf-8")
            ).hexdigest()

            connection.execute(
                "INSERT INTO analysis_environment_snapshots VALUES (?, ?, ?, ?, ?)",
                (
                    identity.environment_snapshot_id.value,
                    expected_runtime,
                    environment_json,
                    environment_sha256,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO analysis_outputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.output_id.value,
                    command.operation_id.value,
                    command.attempt_id.value,
                    expected_runtime,
                    command.executable_artifact_id.value,
                    str(executable["content_hash"]),
                    identity.environment_snapshot_id.value,
                    output_fingerprint,
                    command.method_summary,
                    command.requested_by_turn_id.value,
                    revision.value,
                ),
            )
            for ordinal, artifact_id in enumerate(command.input_artifact_ids, start=1):
                connection.execute(
                    "INSERT INTO analysis_output_inputs VALUES (?, ?, ?)",
                    (identity.output_id.value, artifact_id.value, ordinal),
                )
            for ordinal, binding in enumerate(command.outputs, start=1):
                connection.execute(
                    "INSERT INTO analysis_output_artifacts VALUES (?, ?, ?, ?)",
                    (
                        identity.output_id.value,
                        binding.artifact_id.value,
                        ordinal,
                        binding.role,
                    ),
                )
            for ordinal, element_payload in enumerate(element_payloads, start=1):
                connection.execute(
                    "INSERT INTO analysis_output_elements VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        identity.output_id.value,
                        element_payload["stable_key"],
                        ordinal,
                        element_payload["value_json"],
                        element_payload["rendered_text"],
                        element_payload["value_sha256"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO analysis_output_classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.classification_id.value,
                    identity.output_id.value,
                    command.output_kind.value,
                    command.classifier_id,
                    command.classifier_version,
                    canonical_json(dict(command.classification_basis)),
                    output_fingerprint,
                    revision.value,
                ),
            )
            response = {
                "analysis_output_id": identity.output_id.value,
                "classification_id": identity.classification_id.value,
                "output_kind": command.output_kind.value,
                "output_fingerprint": output_fingerprint,
                "document_eligible": command.output_kind.document_eligible,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "analysis_output.classified",
                        "analysis_output",
                        identity.output_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("analysis_output.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="analysis_output.classify",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return ClassifiedAnalysisOutput(
            AnalysisOutputId(str(response["analysis_output_id"])),
            AnalysisOutputClassificationId(str(response["classification_id"])),
            AnalysisOutputKind(str(response["output_kind"])),
            str(response["output_fingerprint"]),
            bool(response["document_eligible"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def adopt(
        self,
        command: AdoptAnalysisOutputCommand,
        identity: AnalysisAdoptionIdentity,
    ) -> AdoptedAnalysisOutput:
        request = {
            "analysis_output_id": command.analysis_output_id.value,
            "adopted_by_turn_id": command.adopted_by_turn_id.value,
            "expected_output_fingerprint": command.expected_output_fingerprint,
            "preview_artifact_id": command.preview_artifact_id.value,
            "verification_receipt_ids": [item.value for item in command.verification_receipt_ids],
            "confirmation_summary": command.confirmation_summary,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            output = connection.execute(
                """
                SELECT output.output_fingerprint, output.created_by_turn_id,
                       classification.output_kind,
                       classification.analysis_output_classification_id,
                       turn.triggering_message_id, turn.status AS turn_status
                FROM analysis_outputs AS output
                JOIN analysis_output_classifications AS classification
                  ON classification.analysis_output_id = output.analysis_output_id
                JOIN turns AS turn ON turn.turn_id = ?
                WHERE output.analysis_output_id = ?
                """,
                (command.adopted_by_turn_id.value, command.analysis_output_id.value),
            ).fetchone()
            if output is None:
                raise ValueError("Analysis Output or adoption Turn does not exist")
            if str(output["turn_status"]) not in {"running", "waiting"}:
                raise ValueError("Analysis Output adoption requires an active user Turn")
            if str(output["created_by_turn_id"]) == command.adopted_by_turn_id.value:
                raise ValueError("Analysis Output adoption requires a later explicit user Turn")
            output_kind = AnalysisOutputKind(str(output["output_kind"]))
            if not output_kind.document_eligible:
                raise ValueError("UNKNOWN Analysis Output cannot be formally adopted")
            fingerprint = str(output["output_fingerprint"])
            if fingerprint != command.expected_output_fingerprint:
                raise ValueError("Analysis Output fingerprint changed before adoption")

            artifact_rows = connection.execute(
                """
                SELECT binding.artifact_id, binding.role,
                       artifact.created_revision, state.availability
                FROM analysis_output_artifacts AS binding
                JOIN artifacts AS artifact USING (artifact_id)
                JOIN artifact_states AS state USING (artifact_id)
                WHERE binding.analysis_output_id = ?
                ORDER BY binding.ordinal
                """,
                (command.analysis_output_id.value,),
            ).fetchall()
            output_artifacts = {str(row["artifact_id"]): row for row in artifact_rows}
            preview = output_artifacts.get(command.preview_artifact_id.value)
            if preview is None or str(preview["role"]) not in {"primary", "preview"}:
                raise ValueError("user confirmation must bind a primary/preview output Artifact")
            receipt_ids = [item.value for item in command.verification_receipt_ids]
            if len(receipt_ids) != len(output_artifacts) or len(set(receipt_ids)) != len(
                receipt_ids
            ):
                raise ValueError("every Analysis Output Artifact needs one exact receipt")
            verified_artifacts: set[str] = set()
            for receipt_id in receipt_ids:
                verified = connection.execute(
                    """
                    SELECT receipt.artifact_id, receipt.artifact_identity_revision,
                           receipt.verification_purpose, receipt.verdict,
                           state.availability, artifact.created_revision
                    FROM artifact_verification_receipts AS receipt
                    JOIN artifacts AS artifact USING (artifact_id)
                    JOIN artifact_states AS state USING (artifact_id)
                    WHERE receipt.verification_receipt_id = ?
                    """,
                    (receipt_id,),
                ).fetchone()
                if (
                    verified is None
                    or str(verified["artifact_id"]) not in output_artifacts
                    or str(verified["verification_purpose"]) != "document_delivery"
                    or str(verified["verdict"]) != "verified"
                    or str(verified["availability"]) != "available"
                    or int(verified["artifact_identity_revision"])
                    != int(verified["created_revision"])
                ):
                    raise ValueError(
                        "Analysis Output Artifact lacks a current document-delivery receipt"
                    )
                verified_artifacts.add(str(verified["artifact_id"]))
            if verified_artifacts != set(output_artifacts):
                raise ValueError("Verification Receipts do not cover exact output Artifacts")

            classification_id = str(output["analysis_output_classification_id"])
            connection.execute(
                """
                INSERT INTO analysis_output_adoptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.adoption_id.value,
                    command.analysis_output_id.value,
                    classification_id,
                    fingerprint,
                    command.preview_artifact_id.value,
                    command.confirmation_summary,
                    command.adopted_by_turn_id.value,
                    str(output["triggering_message_id"]),
                    command.command_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO analysis_evidence_records
                VALUES (?, 'adopted_analysis_output', ?, ?, ?)
                """,
                (
                    identity.evidence_record_id.value,
                    command.analysis_output_id.value,
                    identity.adoption_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO analysis_document_eligibility_receipts
                VALUES (?, ?, ?, ?, ?, 'eligible', 'complete', ?)
                """,
                (
                    identity.eligibility_id.value,
                    command.analysis_output_id.value,
                    identity.adoption_id.value,
                    identity.evidence_record_id.value,
                    fingerprint,
                    revision.value,
                ),
            )
            response = {
                "analysis_output_id": command.analysis_output_id.value,
                "analysis_output_adoption_id": identity.adoption_id.value,
                "evidence_record_id": identity.evidence_record_id.value,
                "analysis_document_eligibility_id": identity.eligibility_id.value,
                "output_fingerprint": fingerprint,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "analysis_output.adopted",
                        "analysis_output_adoption",
                        identity.adoption_id.value,
                        response,
                    ),
                    JournalDraft(
                        "evidence.issued",
                        "analysis_evidence_record",
                        identity.evidence_record_id.value,
                        {"evidence_kind": "adopted_analysis_output"},
                    ),
                    JournalDraft(
                        "analysis_output.document_eligible",
                        "analysis_document_eligibility",
                        identity.eligibility_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("analysis_output.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="analysis_output.adopt",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return AdoptedAnalysisOutput(
            AnalysisOutputId(str(response["analysis_output_id"])),
            AnalysisOutputAdoptionId(str(response["analysis_output_adoption_id"])),
            EvidenceRecordId(str(response["evidence_record_id"])),
            AnalysisDocumentEligibilityId(str(response["analysis_document_eligibility_id"])),
            str(response["output_fingerprint"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _artifact(connection: sqlite3.Connection, artifact_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT artifact.artifact_id, artifact.artifact_kind,
                   artifact.content_hash, artifact.producer_attempt_id,
                   artifact.created_revision, state.availability
            FROM artifacts AS artifact
            JOIN artifact_states AS state USING (artifact_id)
            WHERE artifact.artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if row is None or str(row["availability"]) != "available":
            raise ValueError(f"Analysis Output Artifact is unavailable: {artifact_id}")
        return cast(sqlite3.Row, row)
