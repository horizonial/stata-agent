from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from stata_research_agent.application.analysis_output import (
    AdoptAnalysisOutputCommand,
    AnalysisArtifactBinding,
    AnalysisElement,
    ClassifyAnalysisOutputCommand,
)
from stata_research_agent.application.analysis_output_service import AnalysisOutputService
from stata_research_agent.application.control import (
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.evidence_eligibility import (
    EligibilityVerdict,
    EvidenceEligibilityQuery,
    EvidenceUseContext,
    EvidenceUsePurpose,
    ValidateEvidenceUseCommand,
)
from stata_research_agent.application.evidence_eligibility_service import (
    EvidenceEligibilityService,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.analysis_output import AnalysisOutputKind
from stata_research_agent.domain.identifiers import (
    ArtifactId,
    ArtifactStateObservationId,
    ArtifactVerificationReceiptId,
    CommandId,
    OperationAttemptId,
    OperationId,
    ResearchPathId,
    TurnId,
    WorkspaceId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.status import TurnStatus
from stata_research_agent.persistence.analysis_output_store import (
    SqliteAnalysisOutputRepository,
)
from stata_research_agent.persistence.atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.evidence_eligibility_store import (
    SqliteEvidenceEligibilityRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


@dataclass(frozen=True, slots=True)
class SeededAnalysisExecution:
    operation_id: OperationId
    attempt_id: OperationAttemptId
    code_artifact_id: ArtifactId
    input_artifact_id: ArtifactId
    output_artifact_id: ArtifactId
    output_receipt_id: ArtifactVerificationReceiptId


def initialized(tmp_path: Path) -> tuple[sqlite3.Connection, WorkspaceControlService, TurnId]:
    workspace = WorkspaceDatabase(tmp_path / "workspace", WorkspaceId("ws_analysis_output"))
    workspace.create()
    connection = workspace.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    control.create_workspace(
        CreateWorkspaceCommand(
            CommandId("cmd_analysis_workspace"), WorkspaceId("ws_analysis_output")
        )
    )
    turn = control.submit_message(
        SubmitMessageCommand(
            CommandId("cmd_analysis_turn"),
            "Explore the supplied data with Python and preserve the exact outputs.",
        )
    )
    return connection, control, turn.turn_id


def seed_completed_python_execution(
    connection: sqlite3.Connection,
    turn_id: TurnId,
    suffix: str,
    *,
    operation_kind: str = "python.execute",
) -> SeededAnalysisExecution:
    operation_id = OperationId(f"op_python_{suffix}")
    attempt_id = OperationAttemptId(f"attempt_python_{suffix}")
    code_id = ArtifactId(f"artifact_code_{suffix}")
    input_id = ArtifactId(f"artifact_input_{suffix}")
    output_id = ArtifactId(f"artifact_output_{suffix}")
    receipt_id = ArtifactVerificationReceiptId(f"artifactverify_output_{suffix}")
    command_id = CommandId(f"cmd_seed_python_{suffix}")

    def mutate(database: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
        database.execute(
            """
            INSERT INTO operations VALUES (?, ?, ?, NULL, 'completed', ?, ?, ?)
            """,
            (
                operation_id.value,
                operation_kind,
                turn_id.value,
                f"seed-{suffix}",
                revision.value,
                revision.value,
            ),
        )
        database.execute(
            "INSERT INTO operation_attempts VALUES (?, ?, 1, NULL, 'completed', ?, ?)",
            (attempt_id.value, operation_id.value, revision.value, revision.value),
        )
        artifacts = (
            (code_id, "code", "text/x-python", "1" * 64),
            (input_id, "dataset", "application/x-stata-dta", "2" * 64),
            (output_id, "table", "application/json", "3" * 64),
        )
        for ordinal, (artifact_id, kind, media_type, digest) in enumerate(artifacts, start=1):
            state_id = f"artifactstate_{suffix}_{ordinal}"
            location_id = f"artifactloc_{suffix}_{ordinal}"
            managed_handle = f".stata-agent/objects/{artifact_id.value}/{digest}.payload"
            database.execute(
                """
                INSERT INTO artifacts VALUES (
                    ?, ?, ?, 16, 'sha256', ?, ?, 'internal_artifact', ?
                )
                """,
                (
                    artifact_id.value,
                    kind,
                    media_type,
                    digest,
                    attempt_id.value,
                    revision.value,
                ),
            )
            database.execute(
                """
                INSERT INTO artifact_state_history VALUES (
                    ?, ?, 'available', 'seeded_exact_payload', 16, ?,
                    '2026-09-19T00:00:00Z', ?
                )
                """,
                (state_id, artifact_id.value, digest, revision.value),
            )
            database.execute(
                """
                INSERT INTO artifact_states VALUES (
                    ?, ?, 'available', '2026-09-19T00:00:00Z', ?
                )
                """,
                (artifact_id.value, state_id, revision.value),
            )
            database.execute(
                "INSERT INTO artifact_location_history VALUES (?, ?, 1, 'installed', ?, ?)",
                (location_id, artifact_id.value, managed_handle, revision.value),
            )
            database.execute(
                "INSERT INTO artifact_locations VALUES (?, ?, 1, ?, ?)",
                (artifact_id.value, location_id, managed_handle, revision.value),
            )
        database.execute(
            """
            INSERT INTO artifact_verification_receipts VALUES (
                ?, ?, ?, 'document_delivery', 'full_sha256', 16, 16,
                ?, ?, ?, 1, '2026-09-19T00:00:00Z', ?, 'verified', 'integrity_verified'
            )
            """,
            (
                receipt_id.value,
                output_id.value,
                revision.value,
                "3" * 64,
                "3" * 64,
                f"artifactloc_{suffix}_3",
                revision.value,
            ),
        )
        return MutationPayload(
            {"operation_id": operation_id.value},
            (
                JournalDraft(
                    "tool.completed",
                    "operation",
                    operation_id.value,
                    {"fixture": "completed_python_execution"},
                ),
            ),
            (OutboxDraft("operation.changed", {"operation_id": operation_id.value}),),
        )

    AtomicCommitService(connection).commit_mutation(
        command_id=command_id,
        command_type="test.python_execution.seed",
        request={"suffix": suffix},
        mutation=mutate,
    )
    return SeededAnalysisExecution(
        operation_id,
        attempt_id,
        code_id,
        input_id,
        output_id,
        receipt_id,
    )


def classify_command(
    seeded: SeededAnalysisExecution,
    turn_id: TurnId,
    *,
    command_id: str,
    output_kind: AnalysisOutputKind,
) -> ClassifyAnalysisOutputCommand:
    return ClassifyAnalysisOutputCommand(
        CommandId(command_id),
        turn_id,
        seeded.operation_id,
        seeded.attempt_id,
        seeded.code_artifact_id,
        (seeded.input_artifact_id,),
        (AnalysisArtifactBinding(seeded.output_artifact_id, "primary"),),
        (AnalysisElement("statistic", {"value": 2.5}, "2.5"),),
        output_kind,
        "Exact Python output from a completed, traceable execution.",
        {"python": "3.12", "packages": {"stdlib": "3.12"}},
        {"declared_kind": output_kind.value, "reviewable": True},
    )


def test_non_regression_output_requires_exact_user_adoption_before_eligibility(
    tmp_path: Path,
) -> None:
    connection, control, producer_turn_id = initialized(tmp_path)
    identities = UuidIdentityGenerator()
    service = AnalysisOutputService(SqliteAnalysisOutputRepository(connection), identities)
    try:
        seeded = seed_completed_python_execution(connection, producer_turn_id, "visual")
        classified = service.classify(
            classify_command(
                seeded,
                producer_turn_id,
                command_id="cmd_classify_visual",
                output_kind=AnalysisOutputKind.VISUAL,
            )
        )
        assert classified.document_eligible is True
        assert (
            connection.execute("SELECT count(*) FROM analysis_output_adoptions").fetchone()[0] == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM analysis_document_eligibility_receipts"
            ).fetchone()[0]
            == 0
        )

        control.complete_turn(
            CompleteTurnCommand(
                CommandId("cmd_complete_python_turn"),
                producer_turn_id,
                TurnStatus.SUCCEEDED,
            )
        )
        adoption_turn = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_analysis_adoption_turn"),
                "I reviewed the exact preview and adopt this non-regression output.",
            )
        )
        before_wrong = connection.execute(
            "SELECT max(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        with pytest.raises(ValueError, match="fingerprint changed"):
            service.adopt(
                AdoptAnalysisOutputCommand(
                    CommandId("cmd_adopt_wrong_fingerprint"),
                    classified.analysis_output_id,
                    adoption_turn.turn_id,
                    "0" * 64,
                    seeded.output_artifact_id,
                    (seeded.output_receipt_id,),
                    "Adopt the reviewed exact output.",
                )
            )
        assert (
            connection.execute("SELECT max(workspace_revision) FROM workspace_commits").fetchone()[
                0
            ]
            == before_wrong
        )

        adopted = service.adopt(
            AdoptAnalysisOutputCommand(
                CommandId("cmd_adopt_visual"),
                classified.analysis_output_id,
                adoption_turn.turn_id,
                classified.output_fingerprint,
                seeded.output_artifact_id,
                (seeded.output_receipt_id,),
                "Adopt the reviewed exact output.",
            )
        )
        assert adopted.output_fingerprint == classified.output_fingerprint
        evidence = connection.execute(
            """
            SELECT evidence_kind FROM analysis_evidence_records
            WHERE evidence_record_id = ?
            """,
            (adopted.evidence_record_id.value,),
        ).fetchone()
        assert evidence[0] == "adopted_analysis_output"
        eligibility = connection.execute(
            """
            SELECT eligibility_status, coverage_status
            FROM analysis_document_eligibility_receipts
            WHERE analysis_document_eligibility_id = ?
            """,
            (adopted.eligibility_id.value,),
        ).fetchone()
        assert tuple(eligibility) == ("eligible", "complete")

        evidence_eligibility = EvidenceEligibilityService(
            SqliteEvidenceEligibilityRepository(connection), identities
        )
        evidence_eligibility.rebuild_projection()
        path_id = ResearchPathId(
            str(
                connection.execute(
                    "SELECT research_path_id FROM turns WHERE turn_id = ?",
                    (adoption_turn.turn_id.value,),
                ).fetchone()[0]
            )
        )
        query = EvidenceEligibilityQuery(
            adopted.evidence_record_id,
            path_id,
            EvidenceUseContext.CURRENT_ADOPTED_RESULT,
            EvidenceUsePurpose.DOCUMENT_DELIVERY,
        )
        current = evidence_eligibility.query(query)
        assert current.verdict is EligibilityVerdict.ELIGIBLE
        assert current.source_state == "available"
        validated = evidence_eligibility.validate_for_use(
            ValidateEvidenceUseCommand(
                CommandId("cmd_validate_analysis_evidence"),
                adoption_turn.turn_id,
                query,
            )
        )
        assert validated.eligibility.verdict is EligibilityVerdict.ELIGIBLE

        missing_observation_id = ArtifactStateObservationId("artifactstate_analysis_output_missing")

        def mark_missing(
            database: sqlite3.Connection, revision: WorkspaceRevision
        ) -> MutationPayload:
            database.execute(
                """
                INSERT INTO artifact_state_history
                VALUES (?, ?, 'missing', 'test_removed', NULL, NULL,
                        '2026-09-19T01:00:00Z', ?)
                """,
                (
                    missing_observation_id.value,
                    seeded.output_artifact_id.value,
                    revision.value,
                ),
            )
            database.execute(
                """
                UPDATE artifact_states SET latest_observation_id = ?,
                    availability = 'missing', verified_at = '2026-09-19T01:00:00Z',
                    commit_revision = ? WHERE artifact_id = ?
                """,
                (
                    missing_observation_id.value,
                    revision.value,
                    seeded.output_artifact_id.value,
                ),
            )
            return MutationPayload(
                {"artifact_id": seeded.output_artifact_id.value},
                (
                    JournalDraft(
                        "artifact.observed_missing",
                        "artifact",
                        seeded.output_artifact_id.value,
                        {"reason": "test_removed"},
                    ),
                ),
                (
                    OutboxDraft(
                        "artifact.changed",
                        {"artifact_id": seeded.output_artifact_id.value},
                    ),
                ),
            )

        AtomicCommitService(connection).commit_mutation(
            command_id=CommandId("cmd_mark_analysis_output_missing"),
            command_type="test.artifact_missing",
            request={"artifact_id": seeded.output_artifact_id.value},
            mutation=mark_missing,
        )
        unavailable = evidence_eligibility.query(query)
        assert unavailable.verdict is EligibilityVerdict.INELIGIBLE
        assert "SOURCE_UNAVAILABLE" in unavailable.reason_codes
        assert unavailable.source_state == "available"
        assert unavailable.projection_lag >= 2
        assert (
            connection.execute(
                "SELECT count(*) FROM analysis_evidence_use_validation_receipts"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_python_regression_requires_exact_later_user_confirmation_before_adoption(
    tmp_path: Path,
) -> None:
    connection, control, turn_id = initialized(tmp_path)
    service = AnalysisOutputService(
        SqliteAnalysisOutputRepository(connection), UuidIdentityGenerator()
    )
    try:
        seeded = seed_completed_python_execution(connection, turn_id, "regression")
        classified = service.classify(
            classify_command(
                seeded,
                turn_id,
                command_id="cmd_classify_regression",
                output_kind=AnalysisOutputKind.REGRESSION,
            )
        )
        assert classified.document_eligible is True
        control.complete_turn(
            CompleteTurnCommand(
                CommandId("cmd_complete_regression_turn"),
                turn_id,
                TurnStatus.SUCCEEDED,
            )
        )
        adoption_turn = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_regression_adoption_turn"),
                "I reviewed this exact Python regression and ask to adopt it.",
            )
        )
        adopted = service.adopt(
            AdoptAnalysisOutputCommand(
                CommandId("cmd_adopt_python_regression"),
                classified.analysis_output_id,
                adoption_turn.turn_id,
                classified.output_fingerprint,
                seeded.output_artifact_id,
                (seeded.output_receipt_id,),
                "I confirm that I want this Python regression.",
            )
        )
        assert adopted.analysis_output_id == classified.analysis_output_id
        assert (
            connection.execute("SELECT count(*) FROM analysis_output_adoptions").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM analysis_evidence_records").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM analysis_document_eligibility_receipts"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM analysis_outputs").fetchone()[0] == 1
    finally:
        connection.close()


def test_shell_custom_output_uses_the_same_typed_classification_boundary(
    tmp_path: Path,
) -> None:
    connection, _control, turn_id = initialized(tmp_path)
    service = AnalysisOutputService(
        SqliteAnalysisOutputRepository(connection), UuidIdentityGenerator()
    )
    try:
        seeded = seed_completed_python_execution(
            connection,
            turn_id,
            "shell",
            operation_kind="shell.execute",
        )
        classified = service.classify(
            classify_command(
                seeded,
                turn_id,
                command_id="cmd_classify_shell_custom",
                output_kind=AnalysisOutputKind.CUSTOM,
            )
        )
        assert classified.document_eligible is True
        runtime_kind = connection.execute(
            """
            SELECT runtime_kind FROM analysis_outputs WHERE analysis_output_id = ?
            """,
            (classified.analysis_output_id.value,),
        ).fetchone()[0]
        assert runtime_kind == "shell"
    finally:
        connection.close()
