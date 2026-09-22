"""SQLite UoW for exact artifacts produced by Python/Shell Tool Attempts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from stata_research_agent.application.ports.artifact_data import ManagedPayload
from stata_research_agent.application.produced_artifact import (
    CapturedProducedArtifact,
    CapturedProducedArtifacts,
    CaptureProducedArtifactsCommand,
    ProducedArtifactIdentity,
)
from stata_research_agent.domain.artifact_data import ArtifactKind
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    ArtifactVerificationReceiptId,
    OperationAttemptId,
    OperationId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import AtomicCommitService, JournalDraft, MutationPayload, OutboxDraft


class SqliteProducedArtifactRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._commits = AtomicCommitService(connection)

    def record(
        self,
        command: CaptureProducedArtifactsCommand,
        *,
        payloads: tuple[ManagedPayload, ...],
        identities: tuple[ProducedArtifactIdentity, ...],
    ) -> CapturedProducedArtifacts:
        if len(command.candidates) != len(payloads) or len(payloads) != len(identities):
            raise ValueError("produced Artifact capture cardinality changed")
        request = {
            "operation_id": command.operation_id.value,
            "attempt_id": command.attempt_id.value,
            "artifacts": [
                {
                    "artifact_id": identity.artifact_id.value,
                    "artifact_kind": candidate.artifact_kind.value,
                    "media_type": candidate.media_type,
                    "role": candidate.role,
                    "relative_name": candidate.relative_name,
                    "size_bytes": payload.size_bytes,
                    "sha256": payload.sha256,
                    "managed_handle": payload.managed_handle,
                }
                for candidate, payload, identity in zip(
                    command.candidates, payloads, identities, strict=True
                )
            ],
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            producer = connection.execute(
                """
                SELECT operation.operation_kind, operation.status AS operation_status,
                       attempt.status AS attempt_status
                FROM operations AS operation
                JOIN operation_attempts AS attempt USING (operation_id)
                WHERE operation.operation_id = ? AND attempt.operation_attempt_id = ?
                """,
                (command.operation_id.value, command.attempt_id.value),
            ).fetchone()
            if producer is None:
                raise ValueError("produced Artifact owner Attempt does not exist")
            if str(producer["operation_kind"]) not in {"python.execute", "shell.execute"}:
                raise ValueError("produced Artifact owner is not Python/Shell")
            if (
                str(producer["operation_status"]) != "handoff_committed"
                or str(producer["attempt_status"]) != "handoff_committed"
            ):
                raise ValueError("produced Artifact owner is not awaiting finalization")

            now = datetime.now(UTC).isoformat()
            artifacts: list[dict[str, object]] = []
            for candidate, payload, identity in zip(
                command.candidates, payloads, identities, strict=True
            ):
                connection.execute(
                    """
                    INSERT INTO artifacts VALUES (
                        ?, ?, ?, ?, 'sha256', ?, ?, 'internal_artifact', ?
                    )
                    """,
                    (
                        identity.artifact_id.value,
                        candidate.artifact_kind.value,
                        candidate.media_type,
                        payload.size_bytes,
                        payload.sha256,
                        command.attempt_id.value,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO artifact_state_history VALUES (
                        ?, ?, 'available', 'sandbox_capture_verified', ?, ?, ?, ?
                    )
                    """,
                    (
                        identity.state_observation_id.value,
                        identity.artifact_id.value,
                        payload.size_bytes,
                        payload.sha256,
                        now,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO artifact_states VALUES (?, ?, 'available', ?, ?)",
                    (
                        identity.artifact_id.value,
                        identity.state_observation_id.value,
                        now,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO artifact_location_history
                    VALUES (?, ?, 1, 'installed', ?, ?)
                    """,
                    (
                        identity.location_id.value,
                        identity.artifact_id.value,
                        payload.managed_handle,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO artifact_locations VALUES (?, ?, 1, ?, ?)",
                    (
                        identity.artifact_id.value,
                        identity.location_id.value,
                        payload.managed_handle,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO artifact_verification_receipts VALUES (
                        ?, ?, ?, 'initial_capture', 'full_sha256',
                        ?, ?, ?, ?, ?, 1, ?, ?, 'verified', 'sandbox_capture_verified'
                    )
                    """,
                    (
                        identity.verification_receipt_id.value,
                        identity.artifact_id.value,
                        revision.value,
                        payload.size_bytes,
                        payload.size_bytes,
                        payload.sha256,
                        payload.sha256,
                        identity.location_id.value,
                        now,
                        revision.value,
                    ),
                )
                artifacts.append(
                    {
                        "artifact_id": identity.artifact_id.value,
                        "artifact_kind": candidate.artifact_kind.value,
                        "media_type": candidate.media_type,
                        "role": candidate.role,
                        "relative_name": candidate.relative_name,
                        "content_sha256": payload.sha256,
                        "size_bytes": payload.size_bytes,
                        "managed_handle": payload.managed_handle,
                        "verification_receipt_id": identity.verification_receipt_id.value,
                    }
                )
            response = {
                "operation_id": command.operation_id.value,
                "attempt_id": command.attempt_id.value,
                "artifacts": artifacts,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "sandbox.artifacts_captured",
                        "operation_attempt",
                        command.attempt_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("artifact.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="sandbox.artifacts.capture",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return CapturedProducedArtifacts(
            OperationId(str(response["operation_id"])),
            OperationAttemptId(str(response["attempt_id"])),
            tuple(
                CapturedProducedArtifact(
                    ArtifactId(str(item["artifact_id"])),
                    ArtifactKind(str(item["artifact_kind"])),
                    str(item["media_type"]),
                    str(item["role"]),
                    str(item["relative_name"]),
                    str(item["content_sha256"]),
                    int(item["size_bytes"]),
                    str(item["managed_handle"]),
                    ArtifactVerificationReceiptId(str(item["verification_receipt_id"])),
                )
                for item in response["artifacts"]
            ),
            receipt.commit_revision,
            receipt.replayed,
        )
