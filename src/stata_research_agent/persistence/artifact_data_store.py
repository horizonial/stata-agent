"""SQLite authoritative adapter for Artifact/Data capture and adoption."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from stata_research_agent.application.artifact_data import (
    AdoptPathDataCommand,
    CaptureDataVersionCommand,
    PathDataAdoptionResult,
    VerifyArtifactCommand,
)
from stata_research_agent.application.ports.artifact_data import ManagedPayload
from stata_research_agent.domain.artifact_data import (
    ArtifactAvailability,
    ArtifactKind,
    ArtifactVerification,
    CapturedDataVersion,
    VerificationPurpose,
    VerificationVerdict,
)
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    ArtifactLocationId,
    ArtifactStateObservationId,
    ArtifactVerificationReceiptId,
    DataVersionId,
    FileObservationId,
    OperationAttemptId,
    OperationId,
    PathDataSlotId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)
from .errors import CommandConflictError


def _request_hash(command_type: str, request: dict[str, object]) -> str:
    return hashlib.sha256(
        canonical_json({"command_type": command_type, "request": request}).encode("utf-8")
    ).hexdigest()


class SqliteArtifactDataRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    @staticmethod
    def _capture_request(
        command: CaptureDataVersionCommand, source_locator: str
    ) -> dict[str, object]:
        return {
            "source_locator": source_locator,
            "data_version_kind": command.data_version_kind.value,
            "created_by_turn_id": command.created_by_turn_id.value,
            "media_type": command.media_type,
        }

    def replay_capture(
        self, command: CaptureDataVersionCommand, *, source_locator: str
    ) -> CapturedDataVersion | None:
        row = self._connection.execute(
            """
            SELECT command_type, request_hash, response_json, commit_revision
            FROM command_receipts WHERE command_id = ?
            """,
            (command.command_id.value,),
        ).fetchone()
        if row is None:
            return None
        request = self._capture_request(command, source_locator)
        expected = _request_hash("data.capture", request)
        if str(row["command_type"]) != "data.capture" or str(row["request_hash"]) != expected:
            raise CommandConflictError(
                f"command_id {command.command_id.value} was reused with different content"
            )
        response = json.loads(str(row["response_json"]))
        return CapturedDataVersion(
            artifact_id=ArtifactId(str(response["artifact_id"])),
            data_version_id=DataVersionId(str(response["data_version_id"])),
            verification_receipt_id=ArtifactVerificationReceiptId(
                str(response["verification_receipt_id"])
            ),
            content_sha256=str(response["content_sha256"]),
            size_bytes=int(response["size_bytes"]),
            managed_handle=str(response["managed_handle"]),
            commit_revision=WorkspaceRevision(int(row["commit_revision"])),
            replayed=True,
        )

    def record_capture(
        self,
        command: CaptureDataVersionCommand,
        *,
        payload: ManagedPayload,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
        file_observation_id: FileObservationId,
        artifact_id: ArtifactId,
        state_observation_id: ArtifactStateObservationId,
        location_id: ArtifactLocationId,
        verification_receipt_id: ArtifactVerificationReceiptId,
        data_version_id: DataVersionId,
    ) -> CapturedDataVersion:
        request = self._capture_request(command, payload.source_locator)

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO operations(
                    operation_id, operation_kind, requested_by_turn_id, tool_call_id,
                    status, idempotency_key, created_revision, terminal_revision
                ) VALUES (?, 'artifact.capture', ?, NULL, 'completed', ?, ?, ?)
                """,
                (
                    operation_id.value,
                    command.created_by_turn_id.value,
                    command.command_id.value,
                    revision.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO operation_attempts(
                    operation_attempt_id, operation_id, attempt_number,
                    session_generation, status, created_revision, terminal_revision
                ) VALUES (?, ?, 1, NULL, 'completed', ?, ?)
                """,
                (attempt_id.value, operation_id.value, revision.value, revision.value),
            )
            connection.execute(
                """
                INSERT INTO file_observations VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_observation_id.value,
                    payload.source_locator,
                    payload.source_size_bytes,
                    payload.source_modified_ns,
                    payload.source_file_identity,
                    payload.sha256,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO artifacts VALUES (?, ?, ?, ?, 'sha256', ?, ?, 'file_observation', ?)
                """,
                (
                    artifact_id.value,
                    ArtifactKind.DATASET.value,
                    command.media_type,
                    payload.size_bytes,
                    payload.sha256,
                    attempt_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO artifact_file_observation_sources VALUES (?, ?)",
                (artifact_id.value, file_observation_id.value),
            )
            connection.execute(
                """
                INSERT INTO artifact_state_history
                VALUES (?, ?, 'available', 'capture_verified', ?, ?, ?, ?)
                """,
                (
                    state_observation_id.value,
                    artifact_id.value,
                    payload.size_bytes,
                    payload.sha256,
                    now,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO artifact_states VALUES (?, ?, 'available', ?, ?)",
                (
                    artifact_id.value,
                    state_observation_id.value,
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
                    location_id.value,
                    artifact_id.value,
                    payload.managed_handle,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO artifact_locations VALUES (?, ?, 1, ?, ?)",
                (
                    artifact_id.value,
                    location_id.value,
                    payload.managed_handle,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO artifact_verification_receipts VALUES (
                    ?, ?, ?, 'data_version_creation', 'full_sha256',
                    ?, ?, ?, ?, ?, 1, ?, ?, 'verified', 'capture_verified'
                )
                """,
                (
                    verification_receipt_id.value,
                    artifact_id.value,
                    revision.value,
                    payload.size_bytes,
                    payload.size_bytes,
                    payload.sha256,
                    payload.sha256,
                    location_id.value,
                    now,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO data_versions VALUES (?, ?, ?, ?, '{}', NULL, NULL, NULL, ?)
                """,
                (
                    data_version_id.value,
                    artifact_id.value,
                    command.data_version_kind.value,
                    command.created_by_turn_id.value,
                    revision.value,
                ),
            )
            response = {
                "artifact_id": artifact_id.value,
                "data_version_id": data_version_id.value,
                "verification_receipt_id": verification_receipt_id.value,
                "content_sha256": payload.sha256,
                "size_bytes": payload.size_bytes,
                "managed_handle": payload.managed_handle,
            }
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft(
                        "artifact.captured",
                        "artifact",
                        artifact_id.value,
                        {"source_locator": payload.source_locator},
                    ),
                    JournalDraft(
                        "data_version.created",
                        "data_version",
                        data_version_id.value,
                        {"canonical_artifact_id": artifact_id.value},
                    ),
                ),
                outbox=(
                    OutboxDraft(
                        "workspace.changed",
                        {
                            "artifact_id": artifact_id.value,
                            "data_version_id": data_version_id.value,
                        },
                    ),
                ),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="data.capture",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return CapturedDataVersion(
            artifact_id=ArtifactId(str(response["artifact_id"])),
            data_version_id=DataVersionId(str(response["data_version_id"])),
            verification_receipt_id=ArtifactVerificationReceiptId(
                str(response["verification_receipt_id"])
            ),
            content_sha256=str(response["content_sha256"]),
            size_bytes=int(response["size_bytes"]),
            managed_handle=str(response["managed_handle"]),
            commit_revision=receipt.commit_revision,
            replayed=receipt.replayed,
        )

    def artifact_managed_handle(self, artifact_id: ArtifactId) -> str:
        row = self._connection.execute(
            "SELECT managed_handle FROM artifact_locations WHERE artifact_id = ?",
            (artifact_id.value,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown Artifact: {artifact_id.value}")
        return str(row["managed_handle"])

    def record_verification(
        self,
        command: VerifyArtifactCommand,
        *,
        observed_size: int | None,
        observed_sha256: str | None,
        state_observation_id: ArtifactStateObservationId,
        verification_receipt_id: ArtifactVerificationReceiptId,
    ) -> ArtifactVerification:
        request = {"artifact_id": command.artifact_id.value, "purpose": command.purpose.value}

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT a.size_bytes, a.content_hash, a.created_revision,
                       l.artifact_location_id, l.location_version
                FROM artifacts a JOIN artifact_locations l USING (artifact_id)
                WHERE a.artifact_id = ?
                """,
                (command.artifact_id.value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown Artifact: {command.artifact_id.value}")
            expected_size = int(row["size_bytes"])
            expected_hash = str(row["content_hash"])
            if observed_size is None or observed_sha256 is None:
                availability = ArtifactAvailability.MISSING
                verdict = VerificationVerdict.FAILED
                reason = "managed_payload_missing"
            elif observed_size != expected_size or observed_sha256 != expected_hash:
                availability = ArtifactAvailability.CORRUPT
                verdict = VerificationVerdict.FAILED
                reason = "managed_payload_mismatch"
            else:
                availability = ArtifactAvailability.AVAILABLE
                verdict = VerificationVerdict.VERIFIED
                reason = "integrity_verified"
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO artifact_state_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    state_observation_id.value,
                    command.artifact_id.value,
                    availability.value,
                    reason,
                    observed_size,
                    observed_sha256,
                    now,
                    revision.value,
                ),
            )
            connection.execute(
                """
                UPDATE artifact_states SET latest_observation_id = ?, availability = ?,
                    verified_at = ?, commit_revision = ? WHERE artifact_id = ?
                """,
                (
                    state_observation_id.value,
                    availability.value,
                    now,
                    revision.value,
                    command.artifact_id.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO artifact_verification_receipts VALUES (
                    ?, ?, ?, ?, 'full_sha256', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    verification_receipt_id.value,
                    command.artifact_id.value,
                    int(row["created_revision"]),
                    command.purpose.value,
                    expected_size,
                    observed_size,
                    expected_hash,
                    observed_sha256,
                    str(row["artifact_location_id"]),
                    int(row["location_version"]),
                    now,
                    revision.value,
                    verdict.value,
                    reason,
                ),
            )
            response = {
                "verification_receipt_id": verification_receipt_id.value,
                "artifact_id": command.artifact_id.value,
                "purpose": command.purpose.value,
                "verdict": verdict.value,
                "availability": availability.value,
                "expected_size": expected_size,
                "observed_size": observed_size,
                "expected_sha256": expected_hash,
                "observed_sha256": observed_sha256,
            }
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft(
                        "artifact.verified",
                        "artifact",
                        command.artifact_id.value,
                        {"purpose": command.purpose.value, "verdict": verdict.value},
                    ),
                ),
                outbox=(OutboxDraft("workspace.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="artifact.verify",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return ArtifactVerification(
            receipt_id=ArtifactVerificationReceiptId(str(response["verification_receipt_id"])),
            artifact_id=ArtifactId(str(response["artifact_id"])),
            purpose=VerificationPurpose(str(response["purpose"])),
            verdict=VerificationVerdict(str(response["verdict"])),
            availability=ArtifactAvailability(str(response["availability"])),
            expected_size=int(response["expected_size"]),
            observed_size=None
            if response["observed_size"] is None
            else int(response["observed_size"]),
            expected_sha256=str(response["expected_sha256"]),
            observed_sha256=None
            if response["observed_sha256"] is None
            else str(response["observed_sha256"]),
            commit_revision=receipt.commit_revision,
        )

    def adopt_path_data(
        self,
        command: AdoptPathDataCommand,
        *,
        path_data_slot_id: PathDataSlotId,
    ) -> PathDataAdoptionResult:
        request = {
            "research_path_id": command.research_path_id.value,
            "canonical_slot_key": command.canonical_slot_key,
            "data_version_id": command.data_version_id.value,
            "expected_pointer_revision": command.expected_pointer_revision,
            "display_name": command.display_name,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            data_row = connection.execute(
                """
                SELECT s.availability FROM data_versions d
                JOIN artifact_states s ON s.artifact_id = d.canonical_artifact_id
                WHERE d.data_version_id = ?
                """,
                (command.data_version_id.value,),
            ).fetchone()
            if data_row is None:
                raise ValueError(f"unknown Data Version: {command.data_version_id.value}")
            if str(data_row["availability"]) != ArtifactAvailability.AVAILABLE.value:
                raise ValueError("Data Version canonical payload is not currently available")
            slot = connection.execute(
                """
                SELECT path_data_slot_id, lifecycle FROM path_data_slots
                WHERE research_path_id = ? AND canonical_key = ?
                """,
                (command.research_path_id.value, command.canonical_slot_key),
            ).fetchone()
            if slot is None:
                if command.expected_pointer_revision != 0:
                    raise ValueError("Path Data Slot pointer revision mismatch")
                selected_slot_id = path_data_slot_id.value
                connection.execute(
                    "INSERT INTO path_data_slots VALUES (?, ?, ?, ?, 'active', ?)",
                    (
                        selected_slot_id,
                        command.research_path_id.value,
                        command.canonical_slot_key,
                        command.display_name,
                        revision.value,
                    ),
                )
                current_pointer_revision = 0
            else:
                selected_slot_id = str(slot["path_data_slot_id"])
                if str(slot["lifecycle"]) != "active":
                    raise ValueError("Path Data Slot is retired")
                current = connection.execute(
                    "SELECT pointer_revision FROM path_data_adoptions WHERE path_data_slot_id = ?",
                    (selected_slot_id,),
                ).fetchone()
                current_pointer_revision = 0 if current is None else int(current[0])
            if current_pointer_revision != command.expected_pointer_revision:
                raise ValueError("Path Data Slot pointer revision mismatch")
            next_pointer_revision = current_pointer_revision + 1
            connection.execute(
                """
                INSERT INTO path_data_adoption_history VALUES (?, ?, ?, ?, ?)
                """,
                (
                    selected_slot_id,
                    next_pointer_revision,
                    command.data_version_id.value,
                    command.command_id.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO path_data_adoptions VALUES (?, ?, ?, ?)
                ON CONFLICT(path_data_slot_id) DO UPDATE SET
                    target_data_version_id = excluded.target_data_version_id,
                    pointer_revision = excluded.pointer_revision,
                    commit_revision = excluded.commit_revision
                """,
                (
                    selected_slot_id,
                    command.data_version_id.value,
                    next_pointer_revision,
                    revision.value,
                ),
            )
            response = {
                "path_data_slot_id": selected_slot_id,
                "data_version_id": command.data_version_id.value,
                "pointer_revision": next_pointer_revision,
            }
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft(
                        "path_data.adopted",
                        "path_data_slot",
                        selected_slot_id,
                        response,
                    ),
                ),
                outbox=(OutboxDraft("workspace.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="path_data.adopt",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return PathDataAdoptionResult(
            path_data_slot_id=PathDataSlotId(str(response["path_data_slot_id"])),
            data_version_id=DataVersionId(str(response["data_version_id"])),
            pointer_revision=int(response["pointer_revision"]),
            commit_revision=receipt.commit_revision,
            replayed=receipt.replayed,
        )
