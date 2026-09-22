"""Checksummed, fail-closed SQLite migration registry and runner."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from stata_research_agent.domain.identifiers import WorkspaceId

from .errors import (
    MigrationChecksumError,
    MigrationLeaseHeldError,
    SchemaCompatibilityError,
    WorkspaceIdentityError,
)

APPLICATION_ID = 0x53524131  # "SRA1"


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    initializes_workspace_identity: bool = False

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("migration version must be >= 1")
        if not self.name or not self.statements:
            raise ValueError("migration name and statements are required")
        if self.version > 26 and any(
            "workspace_migration_" in statement.lower() for statement in self.statements
        ):
            raise ValueError(
                "later migrations cannot reinterpret the stable migration control schema"
            )

    @property
    def checksum(self) -> str:
        canonical = "\n-- statement --\n".join(statement.strip() for statement in self.statements)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


MIGRATION_001 = Migration(
    version=1,
    name="bootstrap_schema_ledger",
    statements=(
        f"PRAGMA application_id = {APPLICATION_ID}",
        """
        CREATE TABLE schema_meta (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            current_version INTEGER NOT NULL CHECK (current_version >= 0),
            created_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version >= 1),
            name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL CHECK (length(checksum) = 64),
            applied_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TRIGGER schema_migrations_no_update
        BEFORE UPDATE ON schema_migrations
        BEGIN
            SELECT RAISE(ABORT, 'schema_migrations is immutable');
        END
        """,
        """
        CREATE TRIGGER schema_migrations_no_delete
        BEFORE DELETE ON schema_migrations
        BEGIN
            SELECT RAISE(ABORT, 'schema_migrations is immutable');
        END
        """,
        """
        CREATE TABLE migration_lease (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            owner_token TEXT,
            acquired_at_epoch INTEGER,
            expires_at_epoch INTEGER,
            lease_revision INTEGER NOT NULL CHECK (lease_revision >= 0),
            CHECK (
                (owner_token IS NULL AND acquired_at_epoch IS NULL AND expires_at_epoch IS NULL)
                OR
                (
                    owner_token IS NOT NULL
                    AND acquired_at_epoch IS NOT NULL
                    AND expires_at_epoch IS NOT NULL
                )
            )
        ) STRICT
        """,
        "INSERT INTO schema_meta(singleton_id, current_version, created_at) VALUES (1, 0, '')",
        """
        INSERT INTO migration_lease(
            singleton_id, owner_token, acquired_at_epoch, expires_at_epoch, lease_revision
        ) VALUES (1, NULL, NULL, NULL, 0)
        """,
    ),
)

MIGRATION_002 = Migration(
    version=2,
    name="workspace_identity",
    statements=(
        """
        CREATE TABLE workspace_identity (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            workspace_id TEXT NOT NULL UNIQUE CHECK (
                workspace_id GLOB 'ws_*' AND length(workspace_id) > 3
            ),
            created_at TEXT NOT NULL
        ) STRICT
        """,
    ),
    initializes_workspace_identity=True,
)

MIGRATION_003 = Migration(
    version=3,
    name="atomic_commit_journal_outbox_receipt",
    statements=(
        """
        CREATE TABLE workspace_commits (
            workspace_revision INTEGER PRIMARY KEY CHECK (workspace_revision >= 1),
            command_id TEXT NOT NULL UNIQUE CHECK (
                command_id GLOB 'cmd_*' AND length(command_id) > 4
            ) REFERENCES command_receipts(command_id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            committed_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE command_receipts (
            command_id TEXT PRIMARY KEY CHECK (
                command_id GLOB 'cmd_*' AND length(command_id) > 4
            ),
            command_type TEXT NOT NULL,
            request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
            outcome TEXT NOT NULL CHECK (outcome IN ('committed')),
            response_json TEXT NOT NULL CHECK (
                json_valid(response_json) AND json_type(response_json) = 'object'
            ),
            commit_revision INTEGER NOT NULL UNIQUE
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE journal_entries (
            journal_entry_id TEXT PRIMARY KEY CHECK (
                journal_entry_id GLOB 'journal_*' AND length(journal_entry_id) > 8
            ),
            workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            event_type TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            payload_json TEXT NOT NULL CHECK (
                json_valid(payload_json) AND json_type(payload_json) = 'object'
            ),
            UNIQUE(workspace_revision, ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE outbox_entries (
            outbox_entry_id TEXT PRIMARY KEY CHECK (
                outbox_entry_id GLOB 'outbox_*' AND length(outbox_entry_id) > 7
            ),
            workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            topic TEXT NOT NULL,
            payload_json TEXT NOT NULL CHECK (
                json_valid(payload_json) AND json_type(payload_json) = 'object'
            ),
            UNIQUE(workspace_revision, ordinal)
        ) STRICT
        """,
        """
        CREATE TRIGGER workspace_commits_no_update
        BEFORE UPDATE ON workspace_commits
        BEGIN SELECT RAISE(ABORT, 'workspace_commits is immutable'); END
        """,
        """
        CREATE TRIGGER workspace_commits_no_delete
        BEFORE DELETE ON workspace_commits
        BEGIN SELECT RAISE(ABORT, 'workspace_commits is immutable'); END
        """,
        """
        CREATE TRIGGER command_receipts_no_update
        BEFORE UPDATE ON command_receipts
        BEGIN SELECT RAISE(ABORT, 'command_receipts is immutable'); END
        """,
        """
        CREATE TRIGGER command_receipts_no_delete
        BEFORE DELETE ON command_receipts
        BEGIN SELECT RAISE(ABORT, 'command_receipts is immutable'); END
        """,
        """
        CREATE TRIGGER journal_entries_no_update
        BEFORE UPDATE ON journal_entries
        BEGIN SELECT RAISE(ABORT, 'journal_entries is immutable'); END
        """,
        """
        CREATE TRIGGER journal_entries_no_delete
        BEFORE DELETE ON journal_entries
        BEGIN SELECT RAISE(ABORT, 'journal_entries is immutable'); END
        """,
        """
        CREATE TRIGGER outbox_entries_no_update
        BEFORE UPDATE ON outbox_entries
        BEGIN SELECT RAISE(ABORT, 'outbox_entries is immutable'); END
        """,
        """
        CREATE TRIGGER outbox_entries_no_delete
        BEFORE DELETE ON outbox_entries
        BEGIN SELECT RAISE(ABORT, 'outbox_entries is immutable'); END
        """,
    ),
)

MIGRATION_004 = Migration(
    version=4,
    name="minimal_workspace_control_model",
    statements=(
        """
        CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY CHECK (
                conversation_id GLOB 'conv_*' AND length(conversation_id) > 5
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE research_paths (
            research_path_id TEXT PRIMARY KEY CHECK (
                research_path_id GLOB 'path_*' AND length(research_path_id) > 5
            ),
            canonical_key TEXT NOT NULL UNIQUE,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE execution_scopes (
            execution_scope_id TEXT PRIMARY KEY CHECK (
                execution_scope_id GLOB 'scope_*' AND length(execution_scope_id) > 6
            ),
            canonical_key TEXT NOT NULL UNIQUE,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE messages (
            message_id TEXT PRIMARY KEY CHECK (
                message_id GLOB 'msg_*' AND length(message_id) > 4
            ),
            conversation_id TEXT NOT NULL
                REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
            role TEXT NOT NULL CHECK (role IN ('user')),
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(conversation_id, ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE completion_contracts (
            completion_contract_id TEXT PRIMARY KEY CHECK (
                completion_contract_id GLOB 'contract_*' AND length(completion_contract_id) > 9
            ),
            originated_by_message_id TEXT NOT NULL UNIQUE
                REFERENCES messages(message_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE completion_contract_revisions (
            completion_contract_revision_id TEXT PRIMARY KEY CHECK (
                completion_contract_revision_id GLOB 'contractrev_*'
                AND length(completion_contract_revision_id) > 12
            ),
            completion_contract_id TEXT NOT NULL
                REFERENCES completion_contracts(completion_contract_id) ON DELETE RESTRICT,
            revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
            contract_kind TEXT NOT NULL CHECK (contract_kind IN ('intake')),
            normalization_required INTEGER NOT NULL CHECK (normalization_required IN (0, 1)),
            source_message_id TEXT NOT NULL
                REFERENCES messages(message_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(completion_contract_id, revision_number)
        ) STRICT
        """,
        """
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY CHECK (
                turn_id GLOB 'turn_*' AND length(turn_id) > 5
            ),
            conversation_id TEXT NOT NULL
                REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
            triggering_message_id TEXT NOT NULL UNIQUE
                REFERENCES messages(message_id) ON DELETE RESTRICT,
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            execution_scope_id TEXT NOT NULL
                REFERENCES execution_scopes(execution_scope_id) ON DELETE RESTRICT,
            completion_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            execution_mode TEXT NOT NULL CHECK (execution_mode IN ('read', 'write')),
            status TEXT NOT NULL CHECK (
                status IN (
                    'queued', 'running', 'waiting', 'succeeded',
                    'partial', 'paused', 'failed'
                )
            ),
            turn_revision INTEGER NOT NULL CHECK (turn_revision >= 1),
            enqueue_ordinal INTEGER NOT NULL UNIQUE CHECK (enqueue_ordinal >= 1),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE workspace_write_lane (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            active_write_turn_id TEXT UNIQUE
                REFERENCES turns(turn_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
            lane_revision INTEGER NOT NULL CHECK (lane_revision >= 0)
        ) STRICT
        """,
        """
        CREATE TRIGGER workspace_write_lane_valid_owner
        BEFORE UPDATE OF active_write_turn_id ON workspace_write_lane
        WHEN NEW.active_write_turn_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM turns
                WHERE turn_id = NEW.active_write_turn_id
                  AND execution_mode = 'write'
                  AND status IN ('running', 'waiting')
            ) THEN RAISE(ABORT, 'invalid workspace write-lane owner') END;
        END
        """,
        """
        CREATE TRIGGER turns_release_lane_before_terminal
        BEFORE UPDATE OF status ON turns
        WHEN NEW.status IN ('succeeded', 'partial', 'paused', 'failed')
          AND EXISTS (
              SELECT 1 FROM workspace_write_lane
              WHERE singleton_id = 1 AND active_write_turn_id = OLD.turn_id
          )
        BEGIN
            SELECT RAISE(ABORT, 'release write lane before terminal turn transition');
        END
        """,
        """
        CREATE TRIGGER turns_valid_status_transition
        BEFORE UPDATE OF status ON turns
        WHEN NOT (
            (OLD.status = 'queued' AND NEW.status = 'running')
            OR (OLD.status = 'running' AND NEW.status = 'waiting')
            OR (OLD.status = 'waiting' AND NEW.status = 'running')
            OR (OLD.status IN ('running', 'waiting')
                AND NEW.status IN ('succeeded', 'partial', 'paused', 'failed'))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid turn status transition');
        END
        """,
        """
        CREATE TRIGGER turns_identity_fields_immutable
        BEFORE UPDATE ON turns
        WHEN OLD.turn_id != NEW.turn_id
          OR OLD.conversation_id != NEW.conversation_id
          OR OLD.triggering_message_id != NEW.triggering_message_id
          OR OLD.research_path_id != NEW.research_path_id
          OR OLD.execution_scope_id != NEW.execution_scope_id
          OR OLD.execution_mode != NEW.execution_mode
          OR OLD.enqueue_ordinal != NEW.enqueue_ordinal
          OR OLD.created_revision != NEW.created_revision
        BEGIN
            SELECT RAISE(ABORT, 'turn identity fields are immutable');
        END
        """,
        """
        CREATE TRIGGER messages_no_update
        BEFORE UPDATE ON messages BEGIN SELECT RAISE(ABORT, 'messages is immutable'); END
        """,
        """
        CREATE TRIGGER messages_no_delete
        BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT, 'messages is immutable'); END
        """,
        """
        CREATE TRIGGER completion_contract_revisions_no_update
        BEFORE UPDATE ON completion_contract_revisions
        BEGIN SELECT RAISE(ABORT, 'completion contract revisions are immutable'); END
        """,
        """
        CREATE TRIGGER completion_contract_revisions_no_delete
        BEFORE DELETE ON completion_contract_revisions
        BEGIN SELECT RAISE(ABORT, 'completion contract revisions are immutable'); END
        """,
    ),
)

MIGRATION_005 = Migration(
    version=5,
    name="managed_artifact_and_data_capture",
    statements=(
        """
        CREATE TABLE operations (
            operation_id TEXT PRIMARY KEY CHECK (
                operation_id GLOB 'op_*' AND length(operation_id) > 3
            ),
            operation_kind TEXT NOT NULL CHECK (operation_kind IN (
                'artifact.capture', 'artifact.verify', 'stata.execute', 'python.execute',
                'shell.execute', 'document.render'
            )),
            requested_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            tool_call_id TEXT,
            status TEXT NOT NULL CHECK (status IN (
                'proposed', 'authorized', 'admitted', 'handoff_committed',
                'completed', 'failed', 'interrupted', 'completed_unreconciled',
                'outcome_unknown', 'integrity_violation'
            )),
            idempotency_key TEXT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            terminal_revision INTEGER
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE operation_attempts (
            operation_attempt_id TEXT PRIMARY KEY CHECK (
                operation_attempt_id GLOB 'attempt_*'
                AND length(operation_attempt_id) > 8
            ),
            operation_id TEXT NOT NULL
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            attempt_number INTEGER NOT NULL CHECK (attempt_number = 1),
            session_generation INTEGER CHECK (
                session_generation IS NULL OR session_generation >= 1
            ),
            status TEXT NOT NULL CHECK (status IN (
                'created', 'handoff_committed', 'completed', 'failed', 'interrupted',
                'completed_unreconciled', 'outcome_unknown', 'integrity_violation'
            )),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            terminal_revision INTEGER
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(operation_id, attempt_number)
        ) STRICT
        """,
        """
        CREATE TABLE file_observations (
            file_observation_id TEXT PRIMARY KEY CHECK (
                file_observation_id GLOB 'fileobs_*'
                AND length(file_observation_id) > 8
            ),
            source_locator TEXT NOT NULL CHECK (
                source_locator GLOB 'workspace://*'
                OR source_locator GLOB 'external-file://*'
            ),
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            modified_ns INTEGER NOT NULL CHECK (modified_ns >= 0),
            file_identity TEXT NOT NULL,
            observed_sha256 TEXT NOT NULL CHECK (length(observed_sha256) = 64),
            observed_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY CHECK (
                artifact_id GLOB 'artifact_*' AND length(artifact_id) > 9
            ),
            artifact_kind TEXT NOT NULL CHECK (
                artifact_kind IN ('dataset', 'code', 'log', 'table', 'document', 'diagnostic')
            ),
            media_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            content_hash_algorithm TEXT NOT NULL CHECK (content_hash_algorithm = 'sha256'),
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            producer_attempt_id TEXT NOT NULL
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            capture_source_kind TEXT NOT NULL CHECK (
                capture_source_kind IN (
                    'file_observation', 'artifact_candidate', 'internal_artifact'
                )
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE artifact_file_observation_sources (
            artifact_id TEXT PRIMARY KEY
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            file_observation_id TEXT NOT NULL UNIQUE
                REFERENCES file_observations(file_observation_id) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE artifact_state_history (
            artifact_state_observation_id TEXT PRIMARY KEY CHECK (
                artifact_state_observation_id GLOB 'artifactstate_*'
                AND length(artifact_state_observation_id) > 14
            ),
            artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            availability TEXT NOT NULL CHECK (
                availability IN ('available', 'missing', 'corrupt', 'deleted')
            ),
            reason_code TEXT NOT NULL,
            observed_size INTEGER CHECK (observed_size IS NULL OR observed_size >= 0),
            observed_hash TEXT CHECK (observed_hash IS NULL OR length(observed_hash) = 64),
            observed_at TEXT NOT NULL,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE artifact_states (
            artifact_id TEXT PRIMARY KEY
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            latest_observation_id TEXT NOT NULL UNIQUE
                REFERENCES artifact_state_history(artifact_state_observation_id)
                ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
            availability TEXT NOT NULL CHECK (
                availability IN ('available', 'missing', 'corrupt', 'deleted')
            ),
            verified_at TEXT NOT NULL,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE artifact_location_history (
            artifact_location_id TEXT PRIMARY KEY CHECK (
                artifact_location_id GLOB 'artifactloc_*'
                AND length(artifact_location_id) > 12
            ),
            artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            location_version INTEGER NOT NULL CHECK (location_version >= 1),
            event_kind TEXT NOT NULL CHECK (event_kind IN ('installed', 'relocated', 'removed')),
            managed_handle TEXT NOT NULL CHECK (
                managed_handle NOT GLOB '/*'
                AND managed_handle NOT GLOB '[A-Za-z]:*'
                AND instr(managed_handle, '..') = 0
            ),
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(artifact_id, location_version)
        ) STRICT
        """,
        """
        CREATE TABLE artifact_locations (
            artifact_id TEXT PRIMARY KEY
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            artifact_location_id TEXT NOT NULL UNIQUE
                REFERENCES artifact_location_history(artifact_location_id)
                ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
            location_version INTEGER NOT NULL CHECK (location_version >= 1),
            managed_handle TEXT NOT NULL,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE artifact_verification_receipts (
            verification_receipt_id TEXT PRIMARY KEY CHECK (
                verification_receipt_id GLOB 'artifactverify_*'
                AND length(verification_receipt_id) > 15
            ),
            artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            artifact_identity_revision INTEGER NOT NULL,
            verification_purpose TEXT NOT NULL CHECK (
                verification_purpose IN (
                    'initial_capture', 'data_version_creation', 'formal_run_input',
                    'result_qualification', 'document_delivery', 'recovery'
                )
            ),
            verification_kind TEXT NOT NULL CHECK (verification_kind = 'full_sha256'),
            expected_size INTEGER NOT NULL CHECK (expected_size >= 0),
            observed_size INTEGER CHECK (observed_size IS NULL OR observed_size >= 0),
            expected_hash TEXT NOT NULL CHECK (length(expected_hash) = 64),
            observed_hash TEXT CHECK (observed_hash IS NULL OR length(observed_hash) = 64),
            artifact_location_id TEXT NOT NULL
                REFERENCES artifact_location_history(artifact_location_id) ON DELETE RESTRICT,
            location_version INTEGER NOT NULL CHECK (location_version >= 1),
            verified_at TEXT NOT NULL,
            workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            verdict TEXT NOT NULL CHECK (verdict IN ('verified', 'failed')),
            reason_code TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE data_versions (
            data_version_id TEXT PRIMARY KEY CHECK (
                data_version_id GLOB 'data_*' AND length(data_version_id) > 5
            ),
            canonical_artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            data_version_kind TEXT NOT NULL CHECK (
                data_version_kind IN ('external_import', 'working_capture', 'internal_checkpoint')
            ),
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            schema_snapshot_json TEXT NOT NULL CHECK (
                json_valid(schema_snapshot_json) AND json_type(schema_snapshot_json) = 'object'
            ),
            observation_count INTEGER CHECK (observation_count IS NULL OR observation_count >= 0),
            variable_count INTEGER CHECK (variable_count IS NULL OR variable_count >= 0),
            stata_data_signature TEXT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE data_version_parents (
            data_version_id TEXT NOT NULL
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            parent_data_version_id TEXT NOT NULL
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            PRIMARY KEY(data_version_id, ordinal),
            UNIQUE(data_version_id, parent_data_version_id)
        ) STRICT
        """,
        """
        CREATE TABLE data_version_code_artifacts (
            data_version_id TEXT NOT NULL
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            PRIMARY KEY(data_version_id, ordinal),
            UNIQUE(data_version_id, artifact_id)
        ) STRICT
        """,
        """
        CREATE TABLE path_data_slots (
            path_data_slot_id TEXT PRIMARY KEY CHECK (
                path_data_slot_id GLOB 'dataslot_*'
                AND length(path_data_slot_id) > 9
            ),
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            canonical_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'retired')),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(research_path_id, canonical_key)
        ) STRICT
        """,
        """
        CREATE TABLE path_data_adoption_history (
            path_data_slot_id TEXT NOT NULL
                REFERENCES path_data_slots(path_data_slot_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            target_data_version_id TEXT NOT NULL
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            adopted_by_command_id TEXT NOT NULL
                REFERENCES command_receipts(command_id) ON DELETE RESTRICT
                    DEFERRABLE INITIALLY DEFERRED,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(path_data_slot_id, pointer_revision)
        ) STRICT
        """,
        """
        CREATE TABLE path_data_adoptions (
            path_data_slot_id TEXT PRIMARY KEY
                REFERENCES path_data_slots(path_data_slot_id) ON DELETE RESTRICT,
            target_data_version_id TEXT NOT NULL
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON artifacts
        BEGIN SELECT RAISE(ABORT, 'artifacts is immutable'); END
        """,
        """
        CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts
        BEGIN SELECT RAISE(ABORT, 'artifacts is immutable'); END
        """,
        """
        CREATE TRIGGER file_observations_no_update BEFORE UPDATE ON file_observations
        BEGIN SELECT RAISE(ABORT, 'file observations are immutable'); END
        """,
        """
        CREATE TRIGGER file_observations_no_delete BEFORE DELETE ON file_observations
        BEGIN SELECT RAISE(ABORT, 'file observations are immutable'); END
        """,
        """
        CREATE TRIGGER artifact_sources_no_update
        BEFORE UPDATE ON artifact_file_observation_sources
        BEGIN SELECT RAISE(ABORT, 'artifact sources are immutable'); END
        """,
        """
        CREATE TRIGGER artifact_sources_no_delete
        BEFORE DELETE ON artifact_file_observation_sources
        BEGIN SELECT RAISE(ABORT, 'artifact sources are immutable'); END
        """,
        """
        CREATE TRIGGER artifact_state_history_no_update BEFORE UPDATE ON artifact_state_history
        BEGIN SELECT RAISE(ABORT, 'artifact state history is immutable'); END
        """,
        """
        CREATE TRIGGER artifact_state_history_no_delete BEFORE DELETE ON artifact_state_history
        BEGIN SELECT RAISE(ABORT, 'artifact state history is immutable'); END
        """,
        """
        CREATE TRIGGER artifact_location_history_no_update
        BEFORE UPDATE ON artifact_location_history
        BEGIN SELECT RAISE(ABORT, 'artifact location history is immutable'); END
        """,
        """
        CREATE TRIGGER artifact_location_history_no_delete
        BEFORE DELETE ON artifact_location_history
        BEGIN SELECT RAISE(ABORT, 'artifact location history is immutable'); END
        """,
        """
        CREATE TRIGGER artifact_verification_receipts_no_update
        BEFORE UPDATE ON artifact_verification_receipts
        BEGIN SELECT RAISE(ABORT, 'artifact verification receipts are immutable'); END
        """,
        """
        CREATE TRIGGER artifact_verification_receipts_no_delete
        BEFORE DELETE ON artifact_verification_receipts
        BEGIN SELECT RAISE(ABORT, 'artifact verification receipts are immutable'); END
        """,
        """
        CREATE TRIGGER data_versions_no_update BEFORE UPDATE ON data_versions
        BEGIN SELECT RAISE(ABORT, 'data versions are immutable'); END
        """,
        """
        CREATE TRIGGER data_versions_no_delete BEFORE DELETE ON data_versions
        BEGIN SELECT RAISE(ABORT, 'data versions are immutable'); END
        """,
        """
        CREATE TRIGGER path_data_adoption_history_no_update
        BEFORE UPDATE ON path_data_adoption_history
        BEGIN SELECT RAISE(ABORT, 'path data adoption history is immutable'); END
        """,
        """
        CREATE TRIGGER path_data_adoption_history_no_delete
        BEFORE DELETE ON path_data_adoption_history
        BEGIN SELECT RAISE(ABORT, 'path data adoption history is immutable'); END
        """,
    ),
)

MIGRATION_006 = Migration(
    version=6,
    name="stata_operation_handoff_and_completion_manifest",
    statements=(
        """
        CREATE TABLE stata_operation_requests (
            operation_id TEXT PRIMARY KEY
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            request_command_id TEXT NOT NULL UNIQUE,
            operation_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            session_id TEXT NOT NULL CHECK (
                length(session_id) BETWEEN 1 AND 64
                AND session_id NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
            command_text TEXT NOT NULL CHECK (length(trim(command_text)) > 0),
            command_hash TEXT NOT NULL CHECK (length(command_hash) = 64),
            timeout_seconds REAL NOT NULL CHECK (
                timeout_seconds >= 0.1 AND timeout_seconds <= 3600
            ),
            handoff_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE completion_manifests (
            completion_manifest_id TEXT PRIMARY KEY CHECK (
                completion_manifest_id GLOB 'manifest_*'
                AND length(completion_manifest_id) > 9
            ),
            operation_id TEXT NOT NULL UNIQUE
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            operation_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            envelope_schema_version TEXT NOT NULL,
            receipt_schema_version TEXT NOT NULL,
            executor_instance_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            session_generation INTEGER NOT NULL CHECK (session_generation >= 1),
            execution_status TEXT NOT NULL CHECK (execution_status IN (
                'succeeded', 'command_failed', 'timed_out', 'crashed', 'start_failed'
            )),
            rc INTEGER NOT NULL,
            raw_output_status TEXT NOT NULL,
            structured_result_status TEXT NOT NULL,
            receipt_json TEXT NOT NULL CHECK (
                json_valid(receipt_json) AND json_type(receipt_json) = 'object'
            ),
            structured_result_json TEXT CHECK (
                structured_result_json IS NULL OR (
                    json_valid(structured_result_json)
                    AND json_type(structured_result_json) = 'object'
                )
            ),
            raw_text TEXT NOT NULL,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE UNIQUE INDEX operations_unique_idempotency_key
        ON operations(idempotency_key) WHERE idempotency_key IS NOT NULL
        """,
        """
        CREATE TRIGGER completion_manifests_no_update BEFORE UPDATE ON completion_manifests
        BEGIN SELECT RAISE(ABORT, 'completion manifests are immutable'); END
        """,
        """
        CREATE TRIGGER completion_manifests_no_delete BEFORE DELETE ON completion_manifests
        BEGIN SELECT RAISE(ABORT, 'completion manifests are immutable'); END
        """,
        """
        CREATE TRIGGER stata_operation_requests_no_update BEFORE UPDATE ON stata_operation_requests
        BEGIN SELECT RAISE(ABORT, 'Stata operation requests are immutable'); END
        """,
        """
        CREATE TRIGGER stata_operation_requests_no_delete BEFORE DELETE ON stata_operation_requests
        BEGIN SELECT RAISE(ABORT, 'Stata operation requests are immutable'); END
        """,
    ),
)

MIGRATION_007 = Migration(
    version=7,
    name="artifact_capture_plan_candidate_and_promotion",
    statements=(
        """
        CREATE TABLE artifact_capture_plans (
            artifact_capture_plan_id TEXT PRIMARY KEY CHECK (
                artifact_capture_plan_id GLOB 'captureplan_*'
                AND length(artifact_capture_plan_id) > 12
            ),
            operation_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            plan_version INTEGER NOT NULL CHECK (plan_version = 1),
            unknown_output_policy TEXT NOT NULL CHECK (
                unknown_output_policy IN ('quarantine', 'reject')
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE artifact_capture_plan_outputs (
            artifact_candidate_id TEXT PRIMARY KEY CHECK (
                artifact_candidate_id GLOB 'candidate_*'
                AND length(artifact_candidate_id) > 10
            ),
            reserved_artifact_id TEXT NOT NULL UNIQUE CHECK (
                reserved_artifact_id GLOB 'artifact_*'
            ),
            reserved_promotion_id TEXT NOT NULL UNIQUE CHECK (
                reserved_promotion_id GLOB 'promotion_*'
            ),
            reserved_state_observation_id TEXT NOT NULL UNIQUE CHECK (
                reserved_state_observation_id GLOB 'artifactstate_*'
            ),
            reserved_location_id TEXT NOT NULL UNIQUE CHECK (
                reserved_location_id GLOB 'artifactloc_*'
            ),
            artifact_capture_plan_id TEXT NOT NULL
                REFERENCES artifact_capture_plans(artifact_capture_plan_id) ON DELETE RESTRICT,
            output_slot TEXT NOT NULL,
            relative_staging_path TEXT NOT NULL CHECK (
                relative_staging_path NOT GLOB '/*'
                AND relative_staging_path NOT GLOB '[A-Za-z]:*'
                AND instr(relative_staging_path, '..') = 0
            ),
            artifact_kind TEXT NOT NULL CHECK (
                artifact_kind IN ('dataset', 'code', 'log', 'table', 'document', 'diagnostic')
            ),
            media_type TEXT NOT NULL,
            required INTEGER NOT NULL CHECK (required IN (0, 1)),
            UNIQUE(artifact_capture_plan_id, output_slot),
            UNIQUE(artifact_capture_plan_id, relative_staging_path)
        ) STRICT
        """,
        """
        CREATE TABLE completion_manifest_artifacts (
            artifact_candidate_id TEXT PRIMARY KEY
                REFERENCES artifact_capture_plan_outputs(artifact_candidate_id)
                ON DELETE RESTRICT,
            completion_manifest_id TEXT NOT NULL
                REFERENCES completion_manifests(completion_manifest_id) ON DELETE RESTRICT,
            operation_attempt_id TEXT NOT NULL
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            output_slot TEXT NOT NULL,
            relative_staging_path TEXT NOT NULL,
            expected INTEGER NOT NULL CHECK (expected IN (0, 1)),
            artifact_kind TEXT NOT NULL,
            detected_format TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            producer_locator TEXT NOT NULL,
            capture_status TEXT NOT NULL CHECK (
                capture_status IN ('captured', 'quarantined')
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(completion_manifest_id, output_slot),
            UNIQUE(operation_attempt_id, artifact_candidate_id)
        ) STRICT
        """,
        """
        CREATE TABLE artifact_candidate_sources (
            artifact_id TEXT PRIMARY KEY
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            artifact_candidate_id TEXT NOT NULL UNIQUE
                REFERENCES completion_manifest_artifacts(artifact_candidate_id)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE artifact_promotions (
            artifact_promotion_id TEXT PRIMARY KEY CHECK (
                artifact_promotion_id GLOB 'promotion_*'
                AND length(artifact_promotion_id) > 10
            ),
            operation_attempt_id TEXT NOT NULL
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            artifact_candidate_id TEXT NOT NULL UNIQUE
                REFERENCES completion_manifest_artifacts(artifact_candidate_id)
                ON DELETE RESTRICT,
            artifact_id TEXT NOT NULL UNIQUE
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            managed_payload_handle TEXT NOT NULL,
            finalization_command_id TEXT NOT NULL
                REFERENCES command_receipts(command_id) ON DELETE RESTRICT
                    DEFERRABLE INITIALLY DEFERRED,
            finalized_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(operation_attempt_id, artifact_candidate_id)
        ) STRICT
        """,
        """
        CREATE TRIGGER artifact_capture_plans_no_update BEFORE UPDATE ON artifact_capture_plans
        BEGIN SELECT RAISE(ABORT, 'artifact capture plans are immutable'); END
        """,
        """
        CREATE TRIGGER artifact_capture_plans_no_delete BEFORE DELETE ON artifact_capture_plans
        BEGIN SELECT RAISE(ABORT, 'artifact capture plans are immutable'); END
        """,
        """
        CREATE TRIGGER artifact_capture_plan_outputs_no_update
        BEFORE UPDATE ON artifact_capture_plan_outputs
        BEGIN SELECT RAISE(ABORT, 'artifact capture plan outputs are immutable'); END
        """,
        """
        CREATE TRIGGER artifact_capture_plan_outputs_no_delete
        BEFORE DELETE ON artifact_capture_plan_outputs
        BEGIN SELECT RAISE(ABORT, 'artifact capture plan outputs are immutable'); END
        """,
        """
        CREATE TRIGGER completion_manifest_artifacts_no_update
        BEFORE UPDATE ON completion_manifest_artifacts
        BEGIN SELECT RAISE(ABORT, 'completion manifest artifacts are immutable'); END
        """,
        """
        CREATE TRIGGER completion_manifest_artifacts_no_delete
        BEFORE DELETE ON completion_manifest_artifacts
        BEGIN SELECT RAISE(ABORT, 'completion manifest artifacts are immutable'); END
        """,
        """
        CREATE TRIGGER artifact_promotions_no_update BEFORE UPDATE ON artifact_promotions
        BEGIN SELECT RAISE(ABORT, 'artifact promotions are immutable'); END
        """,
        """
        CREATE TRIGGER artifact_promotions_no_delete BEFORE DELETE ON artifact_promotions
        BEGIN SELECT RAISE(ABORT, 'artifact promotions are immutable'); END
        """,
    ),
)

MIGRATION_008 = Migration(
    version=8,
    name="explicit_operation_reconciliation_authorization",
    statements=(
        """
        CREATE TABLE operation_reconciliation_authorizations (
            authorization_command_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            operation_attempt_id TEXT NOT NULL
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            authorized_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            authorized_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(authorization_command_id, operation_id, operation_attempt_id)
        ) STRICT
        """,
        """
        CREATE TRIGGER operation_reconciliation_authorizations_no_update
        BEFORE UPDATE ON operation_reconciliation_authorizations
        BEGIN SELECT RAISE(ABORT, 'reconciliation authorizations are immutable'); END
        """,
        """
        CREATE TRIGGER operation_reconciliation_authorizations_no_delete
        BEFORE DELETE ON operation_reconciliation_authorizations
        BEGIN SELECT RAISE(ABORT, 'reconciliation authorizations are immutable'); END
        """,
    ),
)

MIGRATION_009 = Migration(
    version=9,
    name="registered_stata_result_profile_vertical",
    statements=(
        """
        CREATE TABLE result_profiles (
            result_profile_id TEXT NOT NULL,
            profile_version INTEGER NOT NULL CHECK (profile_version >= 1),
            result_kind TEXT NOT NULL CHECK (result_kind IN ('statistical', 'visual')),
            command_family TEXT NOT NULL,
            snapshot_schema_version TEXT NOT NULL,
            extractor_implementation_hash TEXT NOT NULL CHECK (
                length(extractor_implementation_hash) = 64
            ),
            activation_state TEXT NOT NULL CHECK (activation_state IN ('active', 'disabled')),
            registered_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(result_profile_id, profile_version)
        ) STRICT
        """,
        """
        CREATE TABLE executable_sources (
            executable_source_id TEXT PRIMARY KEY CHECK (
                executable_source_id GLOB 'execsource_*'
            ),
            operation_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            source_kind TEXT NOT NULL CHECK (source_kind IN ('direct_command', 'do_file')),
            command_text TEXT NOT NULL,
            command_sha256 TEXT NOT NULL CHECK (length(command_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE stata_operation_input_bindings (
            operation_attempt_id TEXT PRIMARY KEY
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            executable_source_id TEXT NOT NULL UNIQUE
                REFERENCES executable_sources(executable_source_id) ON DELETE RESTRICT,
            execution_purpose TEXT NOT NULL CHECK (
                execution_purpose IN ('general', 'data_load', 'formal_estimation')
            ),
            input_data_version_id TEXT
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            input_data_slot_key TEXT,
            input_verification_receipt_id TEXT
                REFERENCES artifact_verification_receipts(verification_receipt_id)
                ON DELETE RESTRICT,
            source_data_load_operation_id TEXT
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            expected_data_state_token TEXT,
            expected_session_generation INTEGER CHECK (
                expected_session_generation IS NULL OR expected_session_generation >= 1
            ),
            bound_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            CHECK (
                (input_data_version_id IS NULL AND input_data_slot_key IS NULL
                    AND input_verification_receipt_id IS NULL
                    AND execution_purpose = 'general')
                OR
                (input_data_version_id IS NOT NULL AND input_data_slot_key IS NOT NULL
                    AND input_verification_receipt_id IS NOT NULL
                    AND execution_purpose IN ('data_load', 'formal_estimation'))
            ),
            CHECK (
                (execution_purpose != 'formal_estimation'
                    AND source_data_load_operation_id IS NULL
                    AND expected_data_state_token IS NULL
                    AND expected_session_generation IS NULL)
                OR
                (execution_purpose = 'formal_estimation'
                    AND source_data_load_operation_id IS NOT NULL
                    AND expected_data_state_token IS NOT NULL
                    AND expected_session_generation IS NOT NULL)
            )
        ) STRICT
        """,
        """
        CREATE TABLE environment_snapshots (
            environment_snapshot_id TEXT PRIMARY KEY CHECK (
                environment_snapshot_id GLOB 'envsnap_*'
            ),
            runtime_kind TEXT NOT NULL CHECK (runtime_kind = 'stata'),
            payload_json TEXT NOT NULL CHECK (
                json_valid(payload_json) AND json_type(payload_json) = 'object'
            ),
            payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE stata_runs (
            stata_run_id TEXT PRIMARY KEY CHECK (stata_run_id GLOB 'run_*'),
            operation_id TEXT NOT NULL UNIQUE
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            operation_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            completion_manifest_id TEXT NOT NULL UNIQUE
                REFERENCES completion_manifests(completion_manifest_id) ON DELETE RESTRICT,
            executable_source_id TEXT NOT NULL
                REFERENCES executable_sources(executable_source_id) ON DELETE RESTRICT,
            input_data_version_id TEXT NOT NULL
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            input_data_slot_key TEXT NOT NULL,
            environment_snapshot_id TEXT NOT NULL
                REFERENCES environment_snapshots(environment_snapshot_id) ON DELETE RESTRICT,
            session_id TEXT NOT NULL,
            session_generation INTEGER NOT NULL CHECK (session_generation >= 1),
            data_state_token TEXT NOT NULL,
            run_status TEXT NOT NULL CHECK (run_status = 'succeeded'),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE research_command_instances (
            research_command_instance_id TEXT PRIMARY KEY CHECK (
                research_command_instance_id GLOB 'researchcmd_*'
            ),
            stata_run_id TEXT NOT NULL
                REFERENCES stata_runs(stata_run_id) ON DELETE RESTRICT,
            execution_ordinal INTEGER NOT NULL CHECK (execution_ordinal >= 1),
            executable_source_id TEXT NOT NULL
                REFERENCES executable_sources(executable_source_id) ON DELETE RESTRICT,
            command_text TEXT NOT NULL,
            command_sha256 TEXT NOT NULL CHECK (length(command_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(stata_run_id, execution_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE result_contracts (
            result_contract_id TEXT PRIMARY KEY CHECK (
                result_contract_id GLOB 'resultcontract_*'
            ),
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            result_profile_id TEXT NOT NULL,
            profile_version INTEGER NOT NULL,
            intended_specification_json TEXT NOT NULL CHECK (
                json_valid(intended_specification_json)
                AND json_type(intended_specification_json) = 'object'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            FOREIGN KEY(result_profile_id, profile_version)
                REFERENCES result_profiles(result_profile_id, profile_version)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE result_capture_points (
            result_capture_point_id TEXT PRIMARY KEY CHECK (
                result_capture_point_id GLOB 'capturepoint_*'
            ),
            research_command_instance_id TEXT NOT NULL
                REFERENCES research_command_instances(research_command_instance_id)
                ON DELETE RESTRICT,
            capture_ordinal INTEGER NOT NULL CHECK (capture_ordinal >= 1),
            capture_role TEXT NOT NULL CHECK (capture_role IN ('formal_intent', 'exploratory')),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(research_command_instance_id, capture_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE result_capture_snapshots (
            result_capture_snapshot_id TEXT PRIMARY KEY CHECK (
                result_capture_snapshot_id GLOB 'snapshot_*'
            ),
            result_capture_point_id TEXT NOT NULL UNIQUE
                REFERENCES result_capture_points(result_capture_point_id) ON DELETE RESTRICT,
            schema_version TEXT NOT NULL,
            snapshot_json TEXT NOT NULL CHECK (
                json_valid(snapshot_json) AND json_type(snapshot_json) = 'object'
            ),
            snapshot_sha256 TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE estimation_sample_manifests (
            estimation_sample_manifest_id TEXT PRIMARY KEY CHECK (
                estimation_sample_manifest_id GLOB 'sample_*'
            ),
            result_capture_snapshot_id TEXT NOT NULL UNIQUE
                REFERENCES result_capture_snapshots(result_capture_snapshot_id)
                ON DELETE RESTRICT,
            data_version_id TEXT NOT NULL
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            row_domain TEXT NOT NULL,
            row_count INTEGER NOT NULL CHECK (row_count >= 0),
            included_count INTEGER NOT NULL CHECK (included_count >= 0),
            encoding TEXT NOT NULL,
            mask_hex TEXT NOT NULL,
            mask_sha256 TEXT NOT NULL CHECK (length(mask_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE result_candidates (
            result_candidate_id TEXT PRIMARY KEY CHECK (
                result_candidate_id GLOB 'resultcandidate_*'
            ),
            result_capture_snapshot_id TEXT NOT NULL
                REFERENCES result_capture_snapshots(result_capture_snapshot_id)
                ON DELETE RESTRICT,
            result_profile_id TEXT NOT NULL,
            profile_version INTEGER NOT NULL,
            capture_role TEXT NOT NULL CHECK (capture_role IN ('formal_intent', 'exploratory')),
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(result_capture_snapshot_id, result_profile_id, profile_version),
            FOREIGN KEY(result_profile_id, profile_version)
                REFERENCES result_profiles(result_profile_id, profile_version)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE result_qualification_reports (
            result_qualification_report_id TEXT PRIMARY KEY CHECK (
                result_qualification_report_id GLOB 'qualification_*'
            ),
            result_candidate_id TEXT NOT NULL
                REFERENCES result_candidates(result_candidate_id) ON DELETE RESTRICT,
            result_contract_id TEXT NOT NULL
                REFERENCES result_contracts(result_contract_id) ON DELETE RESTRICT,
            verdict TEXT NOT NULL CHECK (verdict IN ('qualified', 'rejected', 'unknown')),
            findings_json TEXT NOT NULL CHECK (
                json_valid(findings_json) AND json_type(findings_json) = 'array'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE results (
            result_id TEXT PRIMARY KEY CHECK (result_id GLOB 'result_*'),
            result_candidate_id TEXT NOT NULL UNIQUE
                REFERENCES result_candidates(result_candidate_id) ON DELETE RESTRICT,
            result_kind TEXT NOT NULL CHECK (result_kind IN ('statistical', 'visual')),
            originating_qualification_report_id TEXT NOT NULL UNIQUE
                REFERENCES result_qualification_reports(result_qualification_report_id)
                ON DELETE RESTRICT,
            producing_stata_run_id TEXT NOT NULL
                REFERENCES stata_runs(stata_run_id) ON DELETE RESTRICT,
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE result_source_locators (
            result_source_locator_id TEXT PRIMARY KEY CHECK (
                result_source_locator_id GLOB 'locator_*'
            ),
            result_capture_snapshot_id TEXT NOT NULL
                REFERENCES result_capture_snapshots(result_capture_snapshot_id)
                ON DELETE RESTRICT,
            locator_type TEXT NOT NULL CHECK (locator_type IN (
                'e_scalar', 'e_macro', 'e_matrix_cell', 'trusted_stata_derivation_receipt'
            )),
            locator_json TEXT NOT NULL CHECK (
                json_valid(locator_json) AND json_type(locator_json) = 'object'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE result_elements (
            result_element_id TEXT PRIMARY KEY CHECK (
                result_element_id GLOB 'element_*'
            ),
            result_id TEXT NOT NULL
                REFERENCES results(result_id) ON DELETE RESTRICT,
            semantic_key TEXT NOT NULL,
            statistic_kind TEXT NOT NULL,
            value_kind TEXT NOT NULL CHECK (value_kind IN (
                'finite', 'stata_missing', 'pos_inf', 'neg_inf', 'not_defined'
            )),
            canonical_binary64_bits TEXT CHECK (
                canonical_binary64_bits IS NULL OR length(canonical_binary64_bits) = 16
            ),
            canonical_decimal_text TEXT,
            stata_missing_code TEXT,
            unit TEXT NOT NULL,
            scale TEXT NOT NULL,
            estimate_status TEXT NOT NULL CHECK (estimate_status IN (
                'estimated', 'omitted', 'base', 'constrained', 'empty'
            )),
            authority TEXT NOT NULL CHECK (authority IN (
                'direct_stored', 'trusted_stata_derived'
            )),
            result_source_locator_id TEXT NOT NULL
                REFERENCES result_source_locators(result_source_locator_id)
                ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(result_id, semantic_key)
        ) STRICT
        """,
        """
        CREATE TABLE trusted_derivation_receipts (
            trusted_derivation_receipt_id TEXT PRIMARY KEY CHECK (
                trusted_derivation_receipt_id GLOB 'derivation_*'
            ),
            derivation_profile_id TEXT NOT NULL,
            derivation_version INTEGER NOT NULL CHECK (derivation_version >= 1),
            primitive_locator_ids_json TEXT NOT NULL CHECK (
                json_valid(primitive_locator_ids_json)
                AND json_type(primitive_locator_ids_json) = 'array'
            ),
            parameters_json TEXT NOT NULL CHECK (
                json_valid(parameters_json) AND json_type(parameters_json) = 'object'
            ),
            output_result_element_id TEXT NOT NULL UNIQUE
                REFERENCES result_elements(result_element_id) ON DELETE RESTRICT
                    DEFERRABLE INITIALLY DEFERRED,
            environment_snapshot_id TEXT NOT NULL
                REFERENCES environment_snapshots(environment_snapshot_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE result_required_data_dependencies (
            result_id TEXT NOT NULL
                REFERENCES results(result_id) ON DELETE RESTRICT,
            input_data_slot_key TEXT NOT NULL,
            data_version_id TEXT NOT NULL
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            input_role TEXT NOT NULL,
            stata_run_id TEXT NOT NULL
                REFERENCES stata_runs(stata_run_id) ON DELETE RESTRICT,
            PRIMARY KEY(result_id, input_data_slot_key)
        ) STRICT
        """,
        """
        CREATE TRIGGER result_profiles_no_update BEFORE UPDATE ON result_profiles
        BEGIN SELECT RAISE(ABORT, 'result profiles are immutable'); END
        """,
        """
        CREATE TRIGGER executable_sources_no_update BEFORE UPDATE ON executable_sources
        BEGIN SELECT RAISE(ABORT, 'executable sources are immutable'); END
        """,
        """
        CREATE TRIGGER stata_runs_no_update BEFORE UPDATE ON stata_runs
        BEGIN SELECT RAISE(ABORT, 'Stata runs are immutable'); END
        """,
        """
        CREATE TRIGGER result_capture_snapshots_no_update
        BEFORE UPDATE ON result_capture_snapshots
        BEGIN SELECT RAISE(ABORT, 'Result capture snapshots are immutable'); END
        """,
        """
        CREATE TRIGGER result_candidates_no_update BEFORE UPDATE ON result_candidates
        BEGIN SELECT RAISE(ABORT, 'Result candidates are immutable'); END
        """,
        """
        CREATE TRIGGER result_qualification_reports_no_update
        BEFORE UPDATE ON result_qualification_reports
        BEGIN SELECT RAISE(ABORT, 'Result qualification reports are immutable'); END
        """,
        """
        CREATE TRIGGER results_no_update BEFORE UPDATE ON results
        BEGIN SELECT RAISE(ABORT, 'Results are immutable'); END
        """,
        """
        CREATE TRIGGER result_elements_no_update BEFORE UPDATE ON result_elements
        BEGIN SELECT RAISE(ABORT, 'Result elements are immutable'); END
        """,
        """
        CREATE TRIGGER result_profiles_no_delete BEFORE DELETE ON result_profiles
        BEGIN SELECT RAISE(ABORT, 'result profiles are immutable'); END
        """,
        """
        CREATE TRIGGER executable_sources_no_delete BEFORE DELETE ON executable_sources
        BEGIN SELECT RAISE(ABORT, 'executable sources are immutable'); END
        """,
        """
        CREATE TRIGGER stata_operation_input_bindings_no_update
        BEFORE UPDATE ON stata_operation_input_bindings
        BEGIN SELECT RAISE(ABORT, 'Stata operation input bindings are immutable'); END
        """,
        """
        CREATE TRIGGER stata_operation_input_bindings_no_delete
        BEFORE DELETE ON stata_operation_input_bindings
        BEGIN SELECT RAISE(ABORT, 'Stata operation input bindings are immutable'); END
        """,
        """
        CREATE TRIGGER stata_runs_no_delete BEFORE DELETE ON stata_runs
        BEGIN SELECT RAISE(ABORT, 'Stata runs are immutable'); END
        """,
        """
        CREATE TRIGGER result_capture_snapshots_no_delete
        BEFORE DELETE ON result_capture_snapshots
        BEGIN SELECT RAISE(ABORT, 'Result capture snapshots are immutable'); END
        """,
        """
        CREATE TRIGGER result_candidates_no_delete BEFORE DELETE ON result_candidates
        BEGIN SELECT RAISE(ABORT, 'Result candidates are immutable'); END
        """,
        """
        CREATE TRIGGER result_qualification_reports_no_delete
        BEFORE DELETE ON result_qualification_reports
        BEGIN SELECT RAISE(ABORT, 'Result qualification reports are immutable'); END
        """,
        """
        CREATE TRIGGER results_no_delete BEFORE DELETE ON results
        BEGIN SELECT RAISE(ABORT, 'Results are immutable'); END
        """,
        """
        CREATE TRIGGER result_elements_no_delete BEFORE DELETE ON result_elements
        BEGIN SELECT RAISE(ABORT, 'Result elements are immutable'); END
        """,
    ),
)

MIGRATION_010 = Migration(
    version=10,
    name="evidence_render_and_numeric_coverage",
    statements=(
        """
        CREATE TABLE result_slots (
            result_slot_id TEXT PRIMARY KEY CHECK (result_slot_id GLOB 'resultslot_*'),
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            canonical_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'retired')),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(research_path_id, canonical_key)
        ) STRICT
        """,
        """
        CREATE TABLE path_result_adoption_history (
            result_slot_id TEXT NOT NULL
                REFERENCES result_slots(result_slot_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            target_result_id TEXT NOT NULL
                REFERENCES results(result_id) ON DELETE RESTRICT,
            updated_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(result_slot_id, pointer_revision)
        ) STRICT
        """,
        """
        CREATE TABLE path_result_adoptions (
            result_slot_id TEXT PRIMARY KEY
                REFERENCES result_slots(result_slot_id) ON DELETE RESTRICT,
            target_result_id TEXT NOT NULL
                REFERENCES results(result_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evidence_records (
            evidence_record_id TEXT PRIMARY KEY CHECK (
                evidence_record_id GLOB 'evidence_*'
            ),
            evidence_kind TEXT NOT NULL CHECK (evidence_kind = 'statistical_element'),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evidence_statistical_sources (
            evidence_record_id TEXT PRIMARY KEY
                REFERENCES evidence_records(evidence_record_id) ON DELETE RESTRICT,
            result_element_id TEXT NOT NULL UNIQUE
                REFERENCES result_elements(result_element_id) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evidence_issuance_receipts (
            evidence_issuance_receipt_id TEXT PRIMARY KEY CHECK (
                evidence_issuance_receipt_id GLOB 'issuance_*'
            ),
            evidence_record_id TEXT NOT NULL
                REFERENCES evidence_records(evidence_record_id) ON DELETE RESTRICT,
            result_element_id TEXT NOT NULL
                REFERENCES result_elements(result_element_id) ON DELETE RESTRICT,
            issuance_status TEXT NOT NULL CHECK (issuance_status = 'issued'),
            reused_existing_record INTEGER NOT NULL CHECK (reused_existing_record IN (0, 1)),
            requested_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE formal_result_blocks (
            formal_result_block_id TEXT PRIMARY KEY CHECK (
                formal_result_block_id GLOB 'formalblock_*'
            ),
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            result_slot_id TEXT NOT NULL
                REFERENCES result_slots(result_slot_id) ON DELETE RESTRICT,
            target_result_id TEXT NOT NULL
                REFERENCES results(result_id) ON DELETE RESTRICT,
            content TEXT NOT NULL CHECK (length(content) > 0),
            content_utf8_sha256 TEXT NOT NULL CHECK (length(content_utf8_sha256) = 64),
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE format_rule_snapshots (
            format_rule_snapshot_id TEXT PRIMARY KEY CHECK (
                format_rule_snapshot_id GLOB 'formatrule_*'
            ),
            formatter_id TEXT NOT NULL CHECK (formatter_id = 'fixed_decimal'),
            formatter_version INTEGER NOT NULL CHECK (formatter_version = 1),
            parameters_json TEXT NOT NULL CHECK (
                json_valid(parameters_json) AND json_type(parameters_json) = 'object'
            ),
            input_binary64_bits TEXT NOT NULL CHECK (length(input_binary64_bits) = 16),
            rendered_text TEXT NOT NULL,
            rendered_utf8_sha256 TEXT NOT NULL CHECK (length(rendered_utf8_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evidence_render_receipts (
            evidence_render_receipt_id TEXT PRIMARY KEY CHECK (
                evidence_render_receipt_id GLOB 'renderreceipt_*'
            ),
            evidence_record_id TEXT NOT NULL
                REFERENCES evidence_records(evidence_record_id) ON DELETE RESTRICT,
            format_rule_snapshot_id TEXT NOT NULL UNIQUE
                REFERENCES format_rule_snapshots(format_rule_snapshot_id) ON DELETE RESTRICT,
            rendered_text TEXT NOT NULL,
            rendered_utf8_sha256 TEXT NOT NULL CHECK (length(rendered_utf8_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evidence_presentation_uses (
            evidence_presentation_use_id TEXT PRIMARY KEY CHECK (
                evidence_presentation_use_id GLOB 'presentationuse_*'
            ),
            evidence_record_id TEXT NOT NULL
                REFERENCES evidence_records(evidence_record_id) ON DELETE RESTRICT,
            formal_result_block_id TEXT NOT NULL
                REFERENCES formal_result_blocks(formal_result_block_id) ON DELETE RESTRICT,
            target_byte_start INTEGER NOT NULL CHECK (target_byte_start >= 0),
            target_byte_end INTEGER NOT NULL CHECK (target_byte_end > target_byte_start),
            use_context TEXT NOT NULL CHECK (use_context = 'current_adopted_result'),
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(formal_result_block_id, target_byte_start, target_byte_end)
        ) STRICT
        """,
        """
        CREATE TABLE evidence_render_bindings (
            evidence_render_binding_id TEXT PRIMARY KEY CHECK (
                evidence_render_binding_id GLOB 'renderbinding_*'
            ),
            evidence_presentation_use_id TEXT NOT NULL UNIQUE
                REFERENCES evidence_presentation_uses(evidence_presentation_use_id)
                ON DELETE RESTRICT,
            evidence_render_receipt_id TEXT NOT NULL UNIQUE
                REFERENCES evidence_render_receipts(evidence_render_receipt_id)
                ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE numeric_coverage_manifests (
            numeric_coverage_manifest_id TEXT PRIMARY KEY CHECK (
                numeric_coverage_manifest_id GLOB 'coverage_*'
            ),
            formal_result_block_id TEXT NOT NULL UNIQUE
                REFERENCES formal_result_blocks(formal_result_block_id) ON DELETE RESTRICT,
            scope_kind TEXT NOT NULL CHECK (scope_kind = 'mandatory_formal_block'),
            coverage_status TEXT NOT NULL CHECK (coverage_status = 'complete'),
            scanner_id TEXT NOT NULL CHECK (scanner_id = 'plain_text_numeric'),
            scanner_version INTEGER NOT NULL CHECK (scanner_version = 1),
            numeric_occurrence_count INTEGER NOT NULL CHECK (numeric_occurrence_count >= 1),
            bound_occurrence_count INTEGER NOT NULL CHECK (
                bound_occurrence_count = numeric_occurrence_count
            ),
            content_utf8_sha256 TEXT NOT NULL CHECK (length(content_utf8_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE numeric_occurrences (
            numeric_occurrence_id TEXT PRIMARY KEY CHECK (
                numeric_occurrence_id GLOB 'occurrence_*'
            ),
            numeric_coverage_manifest_id TEXT NOT NULL
                REFERENCES numeric_coverage_manifests(numeric_coverage_manifest_id)
                ON DELETE RESTRICT,
            formal_result_block_id TEXT NOT NULL
                REFERENCES formal_result_blocks(formal_result_block_id) ON DELETE RESTRICT,
            char_start INTEGER NOT NULL CHECK (char_start >= 0),
            char_end INTEGER NOT NULL CHECK (char_end > char_start),
            byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
            byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
            numeric_lexeme TEXT NOT NULL,
            evidence_presentation_use_id TEXT NOT NULL UNIQUE
                REFERENCES evidence_presentation_uses(evidence_presentation_use_id)
                ON DELETE RESTRICT,
            coverage_disposition TEXT NOT NULL CHECK (coverage_disposition = 'bound'),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(formal_result_block_id, byte_start, byte_end)
        ) STRICT
        """,
        """
        CREATE TRIGGER path_result_adoption_history_no_update
        BEFORE UPDATE ON path_result_adoption_history
        BEGIN SELECT RAISE(ABORT, 'path Result adoption history is immutable'); END
        """,
        """
        CREATE TRIGGER path_result_adoption_history_no_delete
        BEFORE DELETE ON path_result_adoption_history
        BEGIN SELECT RAISE(ABORT, 'path Result adoption history is immutable'); END
        """,
        *tuple(
            statement
            for table in (
                "result_slots",
                "evidence_records",
                "evidence_statistical_sources",
                "evidence_issuance_receipts",
                "formal_result_blocks",
                "format_rule_snapshots",
                "evidence_render_receipts",
                "evidence_presentation_uses",
                "evidence_render_bindings",
                "numeric_coverage_manifests",
                "numeric_occurrences",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)

MIGRATION_011 = Migration(
    version=11,
    name="registered_esttab_table_export",
    statements=(
        """
        CREATE TABLE table_export_profiles (
            export_profile_id TEXT NOT NULL,
            profile_version INTEGER NOT NULL CHECK (profile_version = 1),
            bound_result_profile_id TEXT NOT NULL,
            bound_result_profile_version INTEGER NOT NULL,
            command_template_sha256 TEXT NOT NULL CHECK (length(command_template_sha256) = 64),
            cell_schema_json TEXT NOT NULL CHECK (
                json_valid(cell_schema_json) AND json_type(cell_schema_json) = 'array'
            ),
            activation_state TEXT NOT NULL CHECK (activation_state = 'active'),
            registered_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(export_profile_id, profile_version),
            FOREIGN KEY(bound_result_profile_id, bound_result_profile_version)
                REFERENCES result_profiles(result_profile_id, profile_version)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE table_export_input_manifests (
            table_export_input_manifest_id TEXT PRIMARY KEY CHECK (
                table_export_input_manifest_id GLOB 'tableinput_*'
            ),
            export_profile_id TEXT NOT NULL,
            profile_version INTEGER NOT NULL,
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            result_slot_id TEXT NOT NULL
                REFERENCES result_slots(result_slot_id) ON DELETE RESTRICT,
            source_result_id TEXT NOT NULL
                REFERENCES results(result_id) ON DELETE RESTRICT,
            source_stata_run_id TEXT NOT NULL
                REFERENCES stata_runs(stata_run_id) ON DELETE RESTRICT,
            source_capture_snapshot_id TEXT NOT NULL
                REFERENCES result_capture_snapshots(result_capture_snapshot_id)
                ON DELETE RESTRICT,
            source_result_candidate_id TEXT NOT NULL
                REFERENCES result_candidates(result_candidate_id) ON DELETE RESTRICT,
            session_id TEXT NOT NULL,
            session_generation INTEGER NOT NULL CHECK (session_generation >= 1),
            data_state_token TEXT NOT NULL,
            source_exec_seq INTEGER NOT NULL CHECK (source_exec_seq >= 1),
            stored_estimate_alias TEXT NOT NULL UNIQUE,
            format_parameters_json TEXT NOT NULL CHECK (
                json_valid(format_parameters_json)
                AND json_type(format_parameters_json) = 'object'
            ),
            expected_shape_json TEXT NOT NULL CHECK (
                json_valid(expected_shape_json) AND json_type(expected_shape_json) = 'object'
            ),
            output_relative_path TEXT NOT NULL,
            requested_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            FOREIGN KEY(export_profile_id, profile_version)
                REFERENCES table_export_profiles(export_profile_id, profile_version)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE table_export_manifests (
            table_export_manifest_id TEXT PRIMARY KEY CHECK (
                table_export_manifest_id GLOB 'tableexport_*'
            ),
            table_export_input_manifest_id TEXT NOT NULL UNIQUE
                REFERENCES table_export_input_manifests(table_export_input_manifest_id)
                ON DELETE RESTRICT,
            export_operation_id TEXT NOT NULL UNIQUE
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            export_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            table_artifact_id TEXT NOT NULL UNIQUE
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            actual_session_generation INTEGER NOT NULL CHECK (actual_session_generation >= 1),
            actual_data_state_token TEXT NOT NULL,
            actual_exec_seq INTEGER NOT NULL CHECK (actual_exec_seq >= 1),
            estimation_state_gate TEXT NOT NULL CHECK (estimation_state_gate = 'pass'),
            state_verification_json TEXT NOT NULL CHECK (
                json_valid(state_verification_json)
                AND json_type(state_verification_json) = 'object'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE table_render_receipts (
            table_render_receipt_id TEXT PRIMARY KEY CHECK (
                table_render_receipt_id GLOB 'tablerender_*'
            ),
            table_export_manifest_id TEXT NOT NULL UNIQUE
                REFERENCES table_export_manifests(table_export_manifest_id)
                ON DELETE RESTRICT,
            source_result_id TEXT NOT NULL
                REFERENCES results(result_id) ON DELETE RESTRICT,
            cell_evidence_gate TEXT NOT NULL CHECK (cell_evidence_gate = 'pass'),
            visible_table_sha256 TEXT NOT NULL CHECK (length(visible_table_sha256) = 64),
            controlled_cell_count INTEGER NOT NULL CHECK (controlled_cell_count = 8),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE table_cell_evidence_uses (
            table_cell_evidence_use_id TEXT PRIMARY KEY CHECK (
                table_cell_evidence_use_id GLOB 'tablecelluse_*'
            ),
            table_render_receipt_id TEXT NOT NULL
                REFERENCES table_render_receipts(table_render_receipt_id) ON DELETE RESTRICT,
            semantic_cell_slot TEXT NOT NULL,
            evidence_record_id TEXT NOT NULL
                REFERENCES evidence_records(evidence_record_id) ON DELETE RESTRICT,
            result_element_id TEXT NOT NULL
                REFERENCES result_elements(result_element_id) ON DELETE RESTRICT,
            rendered_text TEXT NOT NULL,
            rtf_byte_start INTEGER NOT NULL CHECK (rtf_byte_start >= 0),
            rtf_byte_end INTEGER NOT NULL CHECK (rtf_byte_end > rtf_byte_start),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(table_render_receipt_id, semantic_cell_slot),
            UNIQUE(table_render_receipt_id, rtf_byte_start, rtf_byte_end)
        ) STRICT
        """,
        """
        CREATE TABLE table_numeric_coverage_manifests (
            table_coverage_manifest_id TEXT PRIMARY KEY CHECK (
                table_coverage_manifest_id GLOB 'tablecoverage_*'
            ),
            table_render_receipt_id TEXT NOT NULL UNIQUE
                REFERENCES table_render_receipts(table_render_receipt_id) ON DELETE RESTRICT,
            coverage_status TEXT NOT NULL CHECK (coverage_status = 'complete'),
            controlled_cell_count INTEGER NOT NULL CHECK (controlled_cell_count = 8),
            structural_exemption_count INTEGER NOT NULL CHECK (structural_exemption_count = 4),
            profile_version INTEGER NOT NULL CHECK (profile_version = 1),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        *tuple(
            statement
            for table in (
                "table_export_profiles",
                "table_export_input_manifests",
                "table_export_manifests",
                "table_render_receipts",
                "table_cell_evidence_uses",
                "table_numeric_coverage_manifests",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)

MIGRATION_012 = Migration(
    version=12,
    name="minimal_docx_delivery",
    statements=(
        """
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY CHECK (document_id GLOB 'doc_*'),
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            canonical_key TEXT NOT NULL,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(research_path_id, canonical_key)
        ) STRICT
        """,
        """
        CREATE TABLE document_manifests (
            document_manifest_id TEXT PRIMARY KEY CHECK (
                document_manifest_id GLOB 'docmanifest_*'
            ),
            table_render_receipt_id TEXT NOT NULL
                REFERENCES table_render_receipts(table_render_receipt_id)
                ON DELETE RESTRICT,
            table_coverage_manifest_id TEXT NOT NULL
                REFERENCES table_numeric_coverage_manifests(table_coverage_manifest_id)
                ON DELETE RESTRICT,
            manifest_json TEXT NOT NULL CHECK (
                json_valid(manifest_json) AND json_type(manifest_json) = 'object'
            ),
            manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
            renderer_profile TEXT NOT NULL CHECK (renderer_profile = 'word.rtf-to-docx.v1'),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE document_revisions (
            document_revision_id TEXT PRIMARY KEY CHECK (
                document_revision_id GLOB 'docrev_*'
            ),
            document_id TEXT NOT NULL
                REFERENCES documents(document_id) ON DELETE RESTRICT,
            origin_kind TEXT NOT NULL CHECK (origin_kind = 'agent_generated'),
            primary_parent_revision_id TEXT
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            docx_artifact_id TEXT NOT NULL UNIQUE
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            manifest_artifact_id TEXT NOT NULL UNIQUE
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            document_manifest_id TEXT NOT NULL UNIQUE
                REFERENCES document_manifests(document_manifest_id) ON DELETE RESTRICT,
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            producer_operation_id TEXT NOT NULL
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE document_parse_receipts (
            document_parse_receipt_id TEXT PRIMARY KEY CHECK (
                document_parse_receipt_id GLOB 'docparse_*'
            ),
            document_revision_id TEXT NOT NULL UNIQUE
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            package_sha256 TEXT NOT NULL CHECK (length(package_sha256) = 64),
            parse_verdict TEXT NOT NULL CHECK (parse_verdict = 'pass'),
            findings_json TEXT NOT NULL CHECK (
                json_valid(findings_json) AND json_type(findings_json) = 'array'
            ),
            parser_profile TEXT NOT NULL CHECK (parser_profile = 'ooxml.safe-reader.v1'),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE delivery_gate_reports (
            delivery_gate_report_id TEXT PRIMARY KEY CHECK (
                delivery_gate_report_id GLOB 'deliverygate_*'
            ),
            document_revision_id TEXT NOT NULL UNIQUE
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'unknown')),
            frozen_dependencies_json TEXT NOT NULL CHECK (
                json_valid(frozen_dependencies_json)
                AND json_type(frozen_dependencies_json) = 'object'
            ),
            findings_json TEXT NOT NULL CHECK (
                json_valid(findings_json) AND json_type(findings_json) = 'array'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE document_slots (
            document_slot_id TEXT PRIMARY KEY CHECK (
                document_slot_id GLOB 'docslot_*'
            ),
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            canonical_key TEXT NOT NULL CHECK (
                canonical_key IN ('manuscript.main.working', 'manuscript.main.delivery')
            ),
            lifecycle TEXT NOT NULL CHECK (lifecycle = 'active'),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(research_path_id, canonical_key)
        ) STRICT
        """,
        """
        CREATE TABLE path_document_adoption_history (
            document_slot_id TEXT NOT NULL
                REFERENCES document_slots(document_slot_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            target_document_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            updated_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            delivery_gate_report_id TEXT
                REFERENCES delivery_gate_reports(delivery_gate_report_id) ON DELETE RESTRICT,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(document_slot_id, pointer_revision)
        ) STRICT
        """,
        """
        CREATE TABLE path_document_adoptions (
            document_slot_id TEXT PRIMARY KEY
                REFERENCES document_slots(document_slot_id) ON DELETE RESTRICT,
            target_document_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            delivery_gate_report_id TEXT
                REFERENCES delivery_gate_reports(delivery_gate_report_id) ON DELETE RESTRICT,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TRIGGER path_document_adoption_history_no_update
        BEFORE UPDATE ON path_document_adoption_history
        BEGIN SELECT RAISE(ABORT, 'Document adoption history is immutable'); END
        """,
        """
        CREATE TRIGGER path_document_adoption_history_no_delete
        BEFORE DELETE ON path_document_adoption_history
        BEGIN SELECT RAISE(ABORT, 'Document adoption history is immutable'); END
        """,
        *tuple(
            statement
            for table in (
                "documents",
                "document_manifests",
                "document_revisions",
                "document_parse_receipts",
                "delivery_gate_reports",
                "document_slots",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)

MIGRATION_013 = Migration(
    version=13,
    name="step_context_and_model_gateway",
    statements=(
        """
        CREATE TABLE permission_snapshots (
            permission_snapshot_id TEXT PRIMARY KEY CHECK (
                permission_snapshot_id GLOB 'permissionsnap_*'
                AND length(permission_snapshot_id) > 15
            ),
            policy_revision TEXT NOT NULL,
            permissions_json TEXT NOT NULL CHECK (
                json_valid(permissions_json) AND json_type(permissions_json) = 'object'
            ),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE model_policy_snapshots (
            model_policy_snapshot_id TEXT PRIMARY KEY CHECK (
                model_policy_snapshot_id GLOB 'modelpolicy_*'
                AND length(model_policy_snapshot_id) > 12
            ),
            policy_revision TEXT NOT NULL,
            provider_profile TEXT NOT NULL,
            provider_kind TEXT NOT NULL,
            model_name TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            credential_ref TEXT NOT NULL,
            policy_json TEXT NOT NULL CHECK (
                json_valid(policy_json) AND json_type(policy_json) = 'object'
            ),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE turn_context_baselines (
            turn_context_baseline_id TEXT PRIMARY KEY CHECK (
                turn_context_baseline_id GLOB 'contextbase_*'
                AND length(turn_context_baseline_id) > 12
            ),
            turn_id TEXT NOT NULL UNIQUE
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            completion_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            system_prompt_revision TEXT NOT NULL,
            main_skill_name TEXT NOT NULL,
            main_skill_revision TEXT NOT NULL,
            main_skill_content_sha256 TEXT NOT NULL CHECK (
                length(main_skill_content_sha256) = 64
            ),
            tool_catalog_revision TEXT NOT NULL,
            model_policy_revision TEXT NOT NULL,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE steps (
            step_id TEXT PRIMARY KEY CHECK (
                step_id GLOB 'step_*' AND length(step_id) > 5
            ),
            turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
            step_ordinal INTEGER NOT NULL CHECK (step_ordinal >= 1),
            status TEXT NOT NULL CHECK (
                status IN ('context_frozen', 'model_running', 'completed', 'failed')
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            completed_revision INTEGER
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(turn_id, step_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE context_manifests (
            context_manifest_id TEXT PRIMARY KEY CHECK (
                context_manifest_id GLOB 'contextmanifest_*'
                AND length(context_manifest_id) > 16
            ),
            step_id TEXT NOT NULL UNIQUE REFERENCES steps(step_id) ON DELETE RESTRICT,
            turn_context_baseline_id TEXT NOT NULL
                REFERENCES turn_context_baselines(turn_context_baseline_id) ON DELETE RESTRICT,
            base_workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            completion_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            permission_snapshot_id TEXT NOT NULL
                REFERENCES permission_snapshots(permission_snapshot_id) ON DELETE RESTRICT,
            model_policy_snapshot_id TEXT NOT NULL
                REFERENCES model_policy_snapshots(model_policy_snapshot_id) ON DELETE RESTRICT,
            input_token_limit INTEGER NOT NULL CHECK (input_token_limit >= 1),
            remaining_step_budget INTEGER NOT NULL CHECK (remaining_step_budget >= 0),
            remaining_tool_budget INTEGER NOT NULL CHECK (remaining_tool_budget >= 0),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE context_items (
            context_item_id TEXT PRIMARY KEY CHECK (
                context_item_id GLOB 'contextitem_*' AND length(context_item_id) > 12
            ),
            context_manifest_id TEXT NOT NULL
                REFERENCES context_manifests(context_manifest_id) ON DELETE RESTRICT,
            item_ordinal INTEGER NOT NULL CHECK (item_ordinal >= 1),
            item_kind TEXT NOT NULL,
            source_object_type TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            remote_transmission_class TEXT NOT NULL CHECK (
                remote_transmission_class IN ('remote_allowed', 'local_only', 'metadata_only')
            ),
            content_text TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            UNIQUE(context_manifest_id, item_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE context_build_decisions (
            context_build_decision_id TEXT PRIMARY KEY CHECK (
                context_build_decision_id GLOB 'contextdecision_*'
                AND length(context_build_decision_id) > 16
            ),
            context_manifest_id TEXT NOT NULL
                REFERENCES context_manifests(context_manifest_id) ON DELETE RESTRICT,
            decision_ordinal INTEGER NOT NULL CHECK (decision_ordinal >= 1),
            decision_kind TEXT NOT NULL CHECK (
                decision_kind IN ('included', 'excluded', 'truncated', 'summarized')
            ),
            source_object_type TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            detail_json TEXT NOT NULL CHECK (
                json_valid(detail_json) AND json_type(detail_json) = 'object'
            ),
            UNIQUE(context_manifest_id, decision_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE model_input_snapshots (
            model_input_snapshot_id TEXT PRIMARY KEY CHECK (
                model_input_snapshot_id GLOB 'modelinput_*'
                AND length(model_input_snapshot_id) > 11
            ),
            context_manifest_id TEXT NOT NULL UNIQUE
                REFERENCES context_manifests(context_manifest_id) ON DELETE RESTRICT,
            normalized_input_json TEXT NOT NULL CHECK (
                json_valid(normalized_input_json) AND json_type(normalized_input_json) = 'object'
            ),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE model_invocations (
            model_invocation_id TEXT PRIMARY KEY CHECK (
                model_invocation_id GLOB 'modelinv_*' AND length(model_invocation_id) > 9
            ),
            step_id TEXT NOT NULL UNIQUE REFERENCES steps(step_id) ON DELETE RESTRICT,
            model_input_snapshot_id TEXT NOT NULL UNIQUE
                REFERENCES model_input_snapshots(model_input_snapshot_id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK (
                status IN ('created', 'running', 'completed', 'failed', 'delivery_unknown')
            ),
            selected_provider_attempt_id TEXT UNIQUE
                REFERENCES provider_attempts(provider_attempt_id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            assistant_output_id TEXT UNIQUE
                REFERENCES assistant_outputs(assistant_output_id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            completed_revision INTEGER
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE provider_attempts (
            provider_attempt_id TEXT PRIMARY KEY CHECK (
                provider_attempt_id GLOB 'providerattempt_*'
                AND length(provider_attempt_id) > 16
            ),
            model_invocation_id TEXT NOT NULL
                REFERENCES model_invocations(model_invocation_id) ON DELETE RESTRICT,
            attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
            state TEXT NOT NULL CHECK (
                state IN ('created', 'blocked', 'cancelled', 'dispatch_started',
                          'dispatched', 'completed', 'failed', 'delivery_unknown')
            ),
            usage_kind TEXT CHECK (usage_kind IN ('exact', 'estimated', 'unknown')),
            input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
            output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
            error_code TEXT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            terminal_revision INTEGER
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(model_invocation_id, attempt_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE provider_request_snapshots (
            provider_request_snapshot_id TEXT PRIMARY KEY CHECK (
                provider_request_snapshot_id GLOB 'providerrequest_*'
                AND length(provider_request_snapshot_id) > 16
            ),
            provider_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES provider_attempts(provider_attempt_id) ON DELETE RESTRICT,
            request_json TEXT NOT NULL CHECK (
                json_valid(request_json) AND json_type(request_json) = 'object'
            ),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE outbound_material_records (
            outbound_material_record_id TEXT PRIMARY KEY CHECK (
                outbound_material_record_id GLOB 'outboundmaterial_*'
                AND length(outbound_material_record_id) > 17
            ),
            provider_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES provider_attempts(provider_attempt_id) ON DELETE RESTRICT,
            provider_profile TEXT NOT NULL,
            endpoint_origin TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
            payload_bytes INTEGER NOT NULL CHECK (payload_bytes >= 0),
            status TEXT NOT NULL CHECK (
                status IN ('prepared', 'dispatch_started', 'dispatched', 'completed',
                           'failed', 'delivery_unknown', 'blocked', 'cancelled')
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            terminal_revision INTEGER
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE assistant_outputs (
            assistant_output_id TEXT PRIMARY KEY CHECK (
                assistant_output_id GLOB 'assistantout_*'
                AND length(assistant_output_id) > 13
            ),
            model_invocation_id TEXT NOT NULL UNIQUE
                REFERENCES model_invocations(model_invocation_id) ON DELETE RESTRICT,
            producing_provider_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES provider_attempts(provider_attempt_id) ON DELETE RESTRICT,
            output_json TEXT NOT NULL CHECK (
                json_valid(output_json) AND json_type(output_json) = 'object'
            ),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TRIGGER assistant_output_requires_completed_attempt
        BEFORE INSERT ON assistant_outputs
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM provider_attempts
                WHERE provider_attempt_id = NEW.producing_provider_attempt_id
                  AND model_invocation_id = NEW.model_invocation_id
                  AND state = 'completed'
            ) THEN RAISE(ABORT, 'assistant output requires a completed producing attempt') END;
        END
        """,
        *tuple(
            statement
            for table in (
                "permission_snapshots",
                "model_policy_snapshots",
                "turn_context_baselines",
                "context_manifests",
                "context_items",
                "context_build_decisions",
                "model_input_snapshots",
                "provider_request_snapshots",
                "assistant_outputs",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)


MIGRATION_014 = Migration(
    version=14,
    name="tool_contract_dispatch_and_jit_admission",
    statements=(
        """
        CREATE TABLE tool_contracts (
            tool_contract_id TEXT PRIMARY KEY CHECK (
                tool_contract_id GLOB 'toolcontract_*' AND length(tool_contract_id) > 13
            ),
            tool_name TEXT NOT NULL,
            tool_version TEXT NOT NULL,
            display_name TEXT NOT NULL,
            operation_kind TEXT NOT NULL CHECK (operation_kind IN (
                'artifact.capture', 'artifact.verify', 'stata.execute', 'python.execute',
                'shell.execute', 'document.render'
            )),
            input_schema_json TEXT NOT NULL CHECK (
                json_valid(input_schema_json) AND json_type(input_schema_json) = 'object'
            ),
            input_schema_sha256 TEXT NOT NULL CHECK (length(input_schema_sha256) = 64),
            output_schema_json TEXT NOT NULL CHECK (
                json_valid(output_schema_json) AND json_type(output_schema_json) = 'object'
            ),
            output_schema_sha256 TEXT NOT NULL CHECK (length(output_schema_sha256) = 64),
            execution_owner TEXT NOT NULL CHECK (
                execution_owner IN ('local_runtime', 'remote_connector', 'provider_managed')
            ),
            effect_class TEXT NOT NULL CHECK (effect_class IN (
                'pure_read', 'external_read', 'workspace_write',
                'external_side_effect', 'write_or_unknown'
            )),
            concurrency_class TEXT NOT NULL CHECK (concurrency_class IN (
                'parallel_safe', 'serial_by_resource', 'exclusive_scope', 'interactive'
            )),
            replay_class TEXT NOT NULL CHECK (replay_class IN (
                'replay_safe', 'idempotent_with_key', 'non_replayable'
            )),
            confirmation_policy TEXT NOT NULL CHECK (confirmation_policy IN (
                'never', 'policy', 'always'
            )),
            pause_behavior TEXT NOT NULL CHECK (pause_behavior IN (
                'not_interruptible', 'cooperative_cancel', 'terminatable_with_recovery'
            )),
            policy_json TEXT NOT NULL CHECK (
                json_valid(policy_json) AND json_type(policy_json) = 'object'
            ),
            contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
            registered_by_turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(tool_name, tool_version)
        ) STRICT
        """,
        """
        CREATE TABLE raw_tool_argument_snapshots (
            raw_arguments_snapshot_id TEXT PRIMARY KEY CHECK (
                raw_arguments_snapshot_id GLOB 'argsraw_*' AND length(raw_arguments_snapshot_id) > 8
            ),
            raw_arguments_text TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE canonical_tool_argument_snapshots (
            canonical_arguments_snapshot_id TEXT PRIMARY KEY CHECK (
                canonical_arguments_snapshot_id GLOB 'argscanonical_*'
                AND length(canonical_arguments_snapshot_id) > 13
            ),
            arguments_json TEXT NOT NULL CHECK (
                json_valid(arguments_json) AND json_type(arguments_json) = 'object'
            ),
            arguments_sha256 TEXT NOT NULL CHECK (length(arguments_sha256) = 64),
            normalization_diff_json TEXT NOT NULL CHECK (
                json_valid(normalization_diff_json)
                AND json_type(normalization_diff_json) = 'object'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE tool_calls (
            tool_call_id TEXT PRIMARY KEY CHECK (
                tool_call_id GLOB 'toolcall_*' AND length(tool_call_id) > 9
            ),
            assistant_output_id TEXT NOT NULL REFERENCES assistant_outputs(assistant_output_id)
                ON DELETE RESTRICT,
            call_ordinal INTEGER NOT NULL CHECK (call_ordinal >= 1),
            provider_tool_call_id TEXT,
            requested_tool_name TEXT NOT NULL,
            resolved_tool_contract_id TEXT
                REFERENCES tool_contracts(tool_contract_id) ON DELETE RESTRICT,
            raw_arguments_snapshot_id TEXT NOT NULL UNIQUE
                REFERENCES raw_tool_argument_snapshots(raw_arguments_snapshot_id)
                ON DELETE RESTRICT,
            canonical_arguments_snapshot_id TEXT UNIQUE
                REFERENCES canonical_tool_argument_snapshots(canonical_arguments_snapshot_id)
                ON DELETE RESTRICT,
            arguments_hash TEXT CHECK (arguments_hash IS NULL OR length(arguments_hash) = 64),
            proposal_status TEXT NOT NULL CHECK (proposal_status IN (
                'proposed', 'awaiting_confirmation', 'rejected', 'scheduled',
                'admitted', 'resolved', 'blocked'
            )),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(assistant_output_id, call_ordinal),
            UNIQUE(assistant_output_id, provider_tool_call_id)
        ) STRICT
        """,
        """
        CREATE TABLE tool_call_status_history (
            tool_call_id TEXT NOT NULL REFERENCES tool_calls(tool_call_id) ON DELETE RESTRICT,
            status_ordinal INTEGER NOT NULL CHECK (status_ordinal >= 1),
            proposal_status TEXT NOT NULL CHECK (proposal_status IN (
                'proposed', 'awaiting_confirmation', 'rejected', 'scheduled',
                'admitted', 'resolved', 'blocked'
            )),
            reason_code TEXT NOT NULL,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(tool_call_id, status_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE tool_dispatch_plans (
            dispatch_plan_id TEXT PRIMARY KEY CHECK (
                dispatch_plan_id GLOB 'dispatchplan_*' AND length(dispatch_plan_id) > 13
            ),
            assistant_output_id TEXT NOT NULL REFERENCES assistant_outputs(assistant_output_id)
                ON DELETE RESTRICT,
            plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
            tool_catalog_revision TEXT NOT NULL,
            base_workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            dependency_snapshot_json TEXT NOT NULL CHECK (
                json_valid(dependency_snapshot_json)
                AND json_type(dependency_snapshot_json) = 'object'
            ),
            dependency_snapshot_sha256 TEXT NOT NULL CHECK (
                length(dependency_snapshot_sha256) = 64
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(assistant_output_id, plan_revision)
        ) STRICT
        """,
        """
        CREATE TABLE assistant_dispatch_plan_adoptions (
            assistant_output_id TEXT PRIMARY KEY REFERENCES assistant_outputs(assistant_output_id)
                ON DELETE RESTRICT,
            dispatch_plan_id TEXT NOT NULL UNIQUE
                REFERENCES tool_dispatch_plans(dispatch_plan_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE tool_dispatch_plan_entries (
            dispatch_plan_entry_id TEXT PRIMARY KEY CHECK (
                dispatch_plan_entry_id GLOB 'dispatchentry_*'
                AND length(dispatch_plan_entry_id) > 14
            ),
            dispatch_plan_id TEXT NOT NULL REFERENCES tool_dispatch_plans(dispatch_plan_id)
                ON DELETE RESTRICT,
            tool_call_id TEXT NOT NULL REFERENCES tool_calls(tool_call_id) ON DELETE RESTRICT,
            call_ordinal INTEGER NOT NULL CHECK (call_ordinal >= 1),
            execution_batch_ordinal INTEGER NOT NULL CHECK (execution_batch_ordinal >= 1),
            barrier_before INTEGER NOT NULL CHECK (barrier_before IN (0, 1)),
            barrier_after INTEGER NOT NULL CHECK (barrier_after IN (0, 1)),
            barrier_reason TEXT,
            UNIQUE(dispatch_plan_id, tool_call_id),
            UNIQUE(dispatch_plan_id, call_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE tool_resource_claims (
            resource_claim_id TEXT PRIMARY KEY CHECK (
                resource_claim_id GLOB 'resourceclaim_*' AND length(resource_claim_id) > 14
            ),
            dispatch_plan_entry_id TEXT NOT NULL
                REFERENCES tool_dispatch_plan_entries(dispatch_plan_entry_id) ON DELETE RESTRICT,
            claim_ordinal INTEGER NOT NULL CHECK (claim_ordinal >= 1),
            resource_key TEXT NOT NULL,
            access_mode TEXT NOT NULL CHECK (access_mode IN ('read', 'write', 'exclusive')),
            identity_revision TEXT NOT NULL,
            UNIQUE(dispatch_plan_entry_id, claim_ordinal),
            UNIQUE(dispatch_plan_entry_id, resource_key)
        ) STRICT
        """,
        """
        CREATE TABLE tool_admissions (
            tool_admission_id TEXT PRIMARY KEY CHECK (
                tool_admission_id GLOB 'admission_*' AND length(tool_admission_id) > 10
            ),
            tool_call_id TEXT NOT NULL UNIQUE
                REFERENCES tool_calls(tool_call_id) ON DELETE RESTRICT,
            dispatch_plan_id TEXT NOT NULL REFERENCES tool_dispatch_plans(dispatch_plan_id)
                ON DELETE RESTRICT,
            operation_id TEXT NOT NULL UNIQUE
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            admitted_turn_revision INTEGER NOT NULL CHECK (admitted_turn_revision >= 1),
            permission_snapshot_id TEXT NOT NULL
                REFERENCES permission_snapshots(permission_snapshot_id) ON DELETE RESTRICT,
            context_manifest_id TEXT NOT NULL REFERENCES context_manifests(context_manifest_id)
                ON DELETE RESTRICT,
            completion_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            dependency_snapshot_sha256 TEXT NOT NULL CHECK (
                length(dependency_snapshot_sha256) = 64
            ),
            resource_claims_sha256 TEXT NOT NULL CHECK (length(resource_claims_sha256) = 64),
            admission_policy_revision TEXT NOT NULL,
            admitted_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE operation_tool_call_links (
            operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id) ON DELETE RESTRICT,
            tool_call_id TEXT NOT NULL REFERENCES tool_calls(tool_call_id) ON DELETE RESTRICT,
            retry_of_operation_id TEXT REFERENCES operations(operation_id) ON DELETE RESTRICT,
            operation_ordinal INTEGER NOT NULL CHECK (operation_ordinal >= 1),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(tool_call_id, operation_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE tool_resource_leases (
            tool_admission_id TEXT NOT NULL REFERENCES tool_admissions(tool_admission_id)
                ON DELETE RESTRICT,
            resource_claim_id TEXT NOT NULL REFERENCES tool_resource_claims(resource_claim_id)
                ON DELETE RESTRICT,
            resource_key TEXT NOT NULL,
            access_mode TEXT NOT NULL CHECK (access_mode IN ('read', 'write', 'exclusive')),
            lease_status TEXT NOT NULL CHECK (lease_status IN ('active', 'released')),
            acquired_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            released_revision INTEGER
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(tool_admission_id, resource_claim_id)
        ) STRICT
        """,
        """
        CREATE TABLE canonical_tool_results (
            tool_result_id TEXT PRIMARY KEY CHECK (
                tool_result_id GLOB 'toolresult_*' AND length(tool_result_id) > 11
            ),
            tool_call_id TEXT NOT NULL UNIQUE
                REFERENCES tool_calls(tool_call_id) ON DELETE RESTRICT,
            result_kind TEXT NOT NULL CHECK (result_kind IN (
                'success', 'error', 'rejected', 'denied', 'cancelled', 'blocked'
            )),
            result_schema_version TEXT NOT NULL,
            summary TEXT NOT NULL,
            structured_payload_json TEXT CHECK (
                structured_payload_json IS NULL OR json_valid(structured_payload_json)
            ),
            artifact_references_json TEXT NOT NULL CHECK (
                json_valid(artifact_references_json)
                AND json_type(artifact_references_json) = 'array'
            ),
            operation_references_json TEXT NOT NULL CHECK (
                json_valid(operation_references_json)
                AND json_type(operation_references_json) = 'array'
            ),
            is_truncated INTEGER NOT NULL CHECK (is_truncated IN (0, 1)),
            full_payload_artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        *tuple(
            statement
            for table in (
                "tool_contracts",
                "raw_tool_argument_snapshots",
                "canonical_tool_argument_snapshots",
                "tool_call_status_history",
                "tool_dispatch_plans",
                "tool_dispatch_plan_entries",
                "tool_resource_claims",
                "tool_admissions",
                "operation_tool_call_links",
                "canonical_tool_results",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)


MIGRATION_015 = Migration(
    version=15,
    name="turn_budget_waiting_and_pause_control",
    statements=(
        """
        CREATE TABLE budget_policy_snapshots (
            budget_policy_snapshot_id TEXT PRIMARY KEY CHECK (
                budget_policy_snapshot_id GLOB 'budgetpolicy_*'
                AND length(budget_policy_snapshot_id) > 13
            ),
            policy_revision TEXT NOT NULL,
            max_steps INTEGER NOT NULL CHECK (max_steps >= 1),
            max_tool_admissions INTEGER NOT NULL CHECK (max_tool_admissions >= 1),
            max_provider_attempts_per_invocation INTEGER NOT NULL CHECK (
                max_provider_attempts_per_invocation >= 1
            ),
            max_provider_attempts_per_turn INTEGER NOT NULL CHECK (
                max_provider_attempts_per_turn >= 1
            ),
            max_same_failure_fingerprint INTEGER NOT NULL CHECK (
                max_same_failure_fingerprint >= 1
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE turn_budget_accounts (
            turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id) ON DELETE RESTRICT,
            budget_policy_snapshot_id TEXT NOT NULL
                REFERENCES budget_policy_snapshots(budget_policy_snapshot_id) ON DELETE RESTRICT,
            used_steps INTEGER NOT NULL CHECK (used_steps >= 0),
            used_tool_admissions INTEGER NOT NULL CHECK (used_tool_admissions >= 0),
            used_provider_attempts INTEGER NOT NULL CHECK (used_provider_attempts >= 0),
            account_revision INTEGER NOT NULL CHECK (account_revision >= 1),
            updated_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE turn_budget_usage_history (
            budget_usage_id TEXT PRIMARY KEY CHECK (
                budget_usage_id GLOB 'budgetusage_*' AND length(budget_usage_id) > 12
            ),
            turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
            usage_kind TEXT NOT NULL CHECK (
                usage_kind IN ('step', 'tool_admission', 'provider_attempt', 'failure_fingerprint')
            ),
            amount INTEGER NOT NULL CHECK (amount >= 1),
            failure_fingerprint TEXT,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE waiting_requests (
            waiting_request_id TEXT PRIMARY KEY CHECK (
                waiting_request_id GLOB 'waiting_*' AND length(waiting_request_id) > 8
            ),
            turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
            tool_call_id TEXT REFERENCES tool_calls(tool_call_id) ON DELETE RESTRICT,
            wait_reason TEXT NOT NULL CHECK (
                wait_reason IN ('user_input', 'user_confirmation', 'external_resolution')
            ),
            prompt TEXT NOT NULL CHECK (length(trim(prompt)) > 0),
            status TEXT NOT NULL CHECK (
                status IN ('open', 'answered', 'closed_by_user_pause')
            ),
            created_turn_revision INTEGER NOT NULL CHECK (created_turn_revision >= 1),
            terminal_turn_revision INTEGER CHECK (
                terminal_turn_revision IS NULL OR terminal_turn_revision >= 1
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            terminal_revision INTEGER
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE UNIQUE INDEX one_open_waiting_request_per_turn
        ON waiting_requests(turn_id) WHERE status = 'open'
        """,
        """
        CREATE TABLE waiting_answers (
            waiting_answer_id TEXT PRIMARY KEY CHECK (
                waiting_answer_id GLOB 'waitinganswer_*'
                AND length(waiting_answer_id) > 14
            ),
            waiting_request_id TEXT NOT NULL UNIQUE
                REFERENCES waiting_requests(waiting_request_id) ON DELETE RESTRICT,
            message_id TEXT NOT NULL UNIQUE REFERENCES messages(message_id) ON DELETE RESTRICT,
            answer_content_sha256 TEXT NOT NULL CHECK (length(answer_content_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE pause_intents (
            pause_intent_id TEXT PRIMARY KEY CHECK (
                pause_intent_id GLOB 'pauseintent_*' AND length(pause_intent_id) > 12
            ),
            turn_id TEXT NOT NULL UNIQUE REFERENCES turns(turn_id) ON DELETE RESTRICT,
            requested_turn_revision INTEGER NOT NULL CHECK (requested_turn_revision >= 1),
            reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('requested', 'converging', 'completed')),
            requested_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            completed_revision INTEGER
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        *tuple(
            statement
            for table in (
                "budget_policy_snapshots",
                "turn_budget_usage_history",
                "waiting_answers",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)


MIGRATION_016 = Migration(
    version=16,
    name="paused_turn_continuation_relation",
    statements=(
        """
        CREATE TABLE turn_continuations (
            turn_continuation_id TEXT PRIMARY KEY CHECK (
                turn_continuation_id GLOB 'turncontinuation_*'
                AND length(turn_continuation_id) > 17
            ),
            predecessor_turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
            successor_turn_id TEXT NOT NULL UNIQUE REFERENCES turns(turn_id) ON DELETE RESTRICT,
            relation_kind TEXT NOT NULL CHECK (
                relation_kind IN ('user_pause_continuation', 'recovery_continuation')
            ),
            contract_decision TEXT NOT NULL CHECK (
                contract_decision IN ('retain_contract_revision', 'revise_contract', 'new_contract')
            ),
            predecessor_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            successor_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(predecessor_turn_id, successor_turn_id)
        ) STRICT
        """,
        """
        CREATE TRIGGER turn_continuations_no_update BEFORE UPDATE ON turn_continuations
        BEGIN SELECT RAISE(ABORT, 'turn_continuations is immutable'); END
        """,
        """
        CREATE TRIGGER turn_continuations_no_delete BEFORE DELETE ON turn_continuations
        BEGIN SELECT RAISE(ABORT, 'turn_continuations is immutable'); END
        """,
    ),
)


MIGRATION_017 = Migration(
    version=17,
    name="runtime_evaluation_goal_coverage_and_stop_guard",
    statements=(
        """
        CREATE TABLE completion_contract_revision_profiles (
            completion_contract_revision_id TEXT PRIMARY KEY
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            goal_summary TEXT NOT NULL CHECK (length(trim(goal_summary)) > 0),
            contract_readiness TEXT NOT NULL CHECK (
                contract_readiness IN ('intake_only', 'execution_ready')
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE completion_obligations (
            completion_obligation_id TEXT PRIMARY KEY CHECK (
                completion_obligation_id GLOB 'obligation_*'
                AND length(completion_obligation_id) > 11
            ),
            completion_contract_id TEXT NOT NULL
                REFERENCES completion_contracts(completion_contract_id) ON DELETE RESTRICT,
            stable_key TEXT NOT NULL CHECK (length(trim(stable_key)) > 0),
            label TEXT NOT NULL CHECK (length(trim(label)) > 0),
            provenance TEXT NOT NULL CHECK (
                provenance IN ('user_explicit', 'plan_derived', 'user_decision',
                               'agent_normalization')
            ),
            source_object_type TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(completion_contract_id, stable_key)
        ) STRICT
        """,
        """
        CREATE TABLE revision_obligation_entries (
            completion_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            completion_obligation_id TEXT NOT NULL
                REFERENCES completion_obligations(completion_obligation_id) ON DELETE RESTRICT,
            requirement_level TEXT NOT NULL CHECK (
                requirement_level IN ('required', 'optional')
            ),
            disposition TEXT NOT NULL CHECK (
                disposition IN ('active', 'deferred', 'waived', 'replaced')
            ),
            acceptance_criterion TEXT NOT NULL CHECK (
                length(trim(acceptance_criterion)) > 0
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(completion_contract_revision_id, completion_obligation_id)
        ) STRICT
        """,
        """
        CREATE TABLE obligation_state_observations (
            obligation_observation_id TEXT PRIMARY KEY CHECK (
                obligation_observation_id GLOB 'observation_*'
                AND length(obligation_observation_id) > 12
            ),
            completion_contract_revision_id TEXT NOT NULL,
            completion_obligation_id TEXT NOT NULL,
            observed_state TEXT NOT NULL CHECK (
                observed_state IN ('unsatisfied', 'satisfied', 'waived', 'deferred')
            ),
            evidence_references_json TEXT NOT NULL CHECK (
                json_valid(evidence_references_json)
                AND json_type(evidence_references_json) = 'array'
            ),
            actor_kind TEXT NOT NULL CHECK (
                actor_kind IN ('system_fact', 'user_decision')
            ),
            rationale TEXT NOT NULL,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            FOREIGN KEY (completion_contract_revision_id, completion_obligation_id)
                REFERENCES revision_obligation_entries(
                    completion_contract_revision_id, completion_obligation_id
                ) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evaluation_policy_snapshots (
            evaluation_policy_snapshot_id TEXT PRIMARY KEY CHECK (
                evaluation_policy_snapshot_id GLOB 'evalpolicy_*'
                AND length(evaluation_policy_snapshot_id) > 11
            ),
            policy_revision TEXT NOT NULL,
            finding_registry_json TEXT NOT NULL CHECK (
                json_valid(finding_registry_json)
                AND json_type(finding_registry_json) = 'object'
            ),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evidence_scope_manifests (
            evidence_scope_manifest_id TEXT PRIMARY KEY CHECK (
                evidence_scope_manifest_id GLOB 'evalscope_*'
                AND length(evidence_scope_manifest_id) > 10
            ),
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            subject_revision TEXT NOT NULL,
            declared_dependencies_json TEXT NOT NULL CHECK (
                json_valid(declared_dependencies_json)
                AND json_type(declared_dependencies_json) = 'array'
            ),
            permitted_slices_json TEXT NOT NULL CHECK (
                json_valid(permitted_slices_json)
                AND json_type(permitted_slices_json) = 'array'
            ),
            excluded_scope_json TEXT NOT NULL CHECK (
                json_valid(excluded_scope_json)
                AND json_type(excluded_scope_json) = 'array'
            ),
            manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evaluation_requests (
            evaluation_request_id TEXT PRIMARY KEY CHECK (
                evaluation_request_id GLOB 'evalrequest_*'
                AND length(evaluation_request_id) > 12
            ),
            turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
            step_id TEXT REFERENCES steps(step_id) ON DELETE RESTRICT,
            evaluation_kind TEXT NOT NULL,
            evaluation_dimension TEXT NOT NULL CHECK (
                evaluation_dimension IN ('eligibility', 'quality', 'completion', 'loop_health')
            ),
            trigger_reason TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            subject_revision TEXT NOT NULL,
            completion_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            evidence_scope_manifest_id TEXT NOT NULL UNIQUE
                REFERENCES evidence_scope_manifests(evidence_scope_manifest_id)
                ON DELETE RESTRICT,
            evaluation_policy_snapshot_id TEXT NOT NULL
                REFERENCES evaluation_policy_snapshots(evaluation_policy_snapshot_id)
                ON DELETE RESTRICT,
            grader_kind TEXT NOT NULL CHECK (
                grader_kind IN ('rule', 'statistical', 'model', 'human_adapter')
            ),
            base_workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evaluation_reports (
            evaluation_report_id TEXT PRIMARY KEY CHECK (
                evaluation_report_id GLOB 'eval_*' AND length(evaluation_report_id) > 5
            ),
            evaluation_request_id TEXT NOT NULL UNIQUE
                REFERENCES evaluation_requests(evaluation_request_id) ON DELETE RESTRICT,
            verdict TEXT NOT NULL CHECK (
                verdict IN ('pass', 'fail', 'warn', 'unknown', 'not_applicable')
            ),
            grader_version TEXT NOT NULL,
            unknowns_json TEXT NOT NULL CHECK (
                json_valid(unknowns_json) AND json_type(unknowns_json) = 'array'
            ),
            required_evidence_json TEXT NOT NULL CHECK (
                json_valid(required_evidence_json)
                AND json_type(required_evidence_json) = 'array'
            ),
            suggested_actions_json TEXT NOT NULL CHECK (
                json_valid(suggested_actions_json)
                AND json_type(suggested_actions_json) = 'array'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE evaluation_findings (
            evaluation_finding_id TEXT PRIMARY KEY CHECK (
                evaluation_finding_id GLOB 'evalfinding_*'
                AND length(evaluation_finding_id) > 13
            ),
            evaluation_report_id TEXT NOT NULL
                REFERENCES evaluation_reports(evaluation_report_id) ON DELETE RESTRICT,
            finding_ordinal INTEGER NOT NULL CHECK (finding_ordinal >= 1),
            finding_code TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'error')),
            control_effect TEXT NOT NULL CHECK (
                control_effect IN ('none', 'block_adoption', 'block_execution',
                                   'block_termination')
            ),
            confidence TEXT CHECK (confidence IS NULL OR confidence IN ('high', 'medium', 'low')),
            message TEXT NOT NULL,
            requires_user_decision INTEGER NOT NULL CHECK (
                requires_user_decision IN (0, 1)
            ),
            UNIQUE(evaluation_report_id, finding_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE goal_coverages (
            goal_coverage_id TEXT PRIMARY KEY CHECK (
                goal_coverage_id GLOB 'goalcoverage_*'
                AND length(goal_coverage_id) > 13
            ),
            turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
            completion_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            required_total INTEGER NOT NULL CHECK (required_total >= 0),
            required_satisfied INTEGER NOT NULL CHECK (required_satisfied >= 0),
            coverage_status TEXT NOT NULL CHECK (
                coverage_status IN ('satisfied', 'incomplete')
            ),
            coverage_json TEXT NOT NULL CHECK (
                json_valid(coverage_json) AND json_type(coverage_json) = 'array'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE stop_guard_decisions (
            stop_guard_decision_id TEXT PRIMARY KEY CHECK (
                stop_guard_decision_id GLOB 'stopguard_*'
                AND length(stop_guard_decision_id) > 11
            ),
            turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
            evaluated_turn_revision INTEGER NOT NULL CHECK (evaluated_turn_revision >= 1),
            completion_contract_revision_id TEXT NOT NULL
                REFERENCES completion_contract_revisions(completion_contract_revision_id)
                ON DELETE RESTRICT,
            evaluation_report_id TEXT NOT NULL
                REFERENCES evaluation_reports(evaluation_report_id) ON DELETE RESTRICT,
            goal_coverage_id TEXT NOT NULL
                REFERENCES goal_coverages(goal_coverage_id) ON DELETE RESTRICT,
            decision TEXT NOT NULL CHECK (decision IN ('continue', 'wait', 'terminate')),
            directive TEXT CHECK (
                directive IS NULL OR directive IN ('ordinary', 'revise', 'replan')
            ),
            wait_reason TEXT CHECK (
                wait_reason IS NULL OR wait_reason IN (
                    'user_input', 'user_confirmation', 'external_resolution'
                )
            ),
            terminal_disposition TEXT CHECK (
                terminal_disposition IS NULL OR terminal_disposition IN (
                    'succeed', 'partial', 'pause', 'fail'
                )
            ),
            reason_code TEXT NOT NULL,
            blockers_json TEXT NOT NULL CHECK (
                json_valid(blockers_json) AND json_type(blockers_json) = 'array'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        *tuple(
            statement
            for table in (
                "completion_contract_revision_profiles",
                "completion_obligations",
                "revision_obligation_entries",
                "obligation_state_observations",
                "evaluation_policy_snapshots",
                "evidence_scope_manifests",
                "evaluation_requests",
                "evaluation_reports",
                "evaluation_findings",
                "goal_coverages",
                "stop_guard_decisions",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)


MIGRATION_018 = Migration(
    version=18,
    name="research_path_plan_and_slot_branching",
    statements=(
        """
        CREATE TABLE research_path_profiles (
            research_path_id TEXT PRIMARY KEY
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
            origin_kind TEXT NOT NULL CHECK (
                origin_kind IN ('workspace_main', 'path_branch')
            ),
            lifecycle TEXT NOT NULL CHECK (lifecycle = 'active'),
            created_by_turn_id TEXT
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            CHECK (
                (origin_kind = 'workspace_main' AND created_by_turn_id IS NULL)
                OR
                (origin_kind = 'path_branch' AND created_by_turn_id IS NOT NULL)
            )
        ) STRICT
        """,
        """
        INSERT INTO research_path_profiles(
            research_path_id, display_name, origin_kind, lifecycle,
            created_by_turn_id, created_revision
        )
        SELECT research_path_id, 'Main', 'workspace_main', 'active', NULL, created_revision
        FROM research_paths
        WHERE canonical_key = 'main'
        """,
        """
        CREATE TABLE research_path_parents (
            child_research_path_id TEXT PRIMARY KEY
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            parent_research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            forked_from_workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            branch_reason TEXT NOT NULL CHECK (length(trim(branch_reason)) > 0),
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            CHECK (child_research_path_id != parent_research_path_id)
        ) STRICT
        """,
        """
        CREATE TABLE path_branch_manifests (
            path_branch_manifest_id TEXT PRIMARY KEY CHECK (
                path_branch_manifest_id GLOB 'pathbranch_*'
            ),
            child_research_path_id TEXT NOT NULL UNIQUE
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            parent_research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            source_workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            copied_adoptions_json TEXT NOT NULL CHECK (
                json_valid(copied_adoptions_json)
                AND json_type(copied_adoptions_json) = 'object'
            ),
            manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE path_data_slot_origins (
            child_path_data_slot_id TEXT PRIMARY KEY
                REFERENCES path_data_slots(path_data_slot_id) ON DELETE RESTRICT,
            parent_path_data_slot_id TEXT NOT NULL
                REFERENCES path_data_slots(path_data_slot_id) ON DELETE RESTRICT,
            source_workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE path_result_slot_origins (
            child_result_slot_id TEXT PRIMARY KEY
                REFERENCES result_slots(result_slot_id) ON DELETE RESTRICT,
            parent_result_slot_id TEXT NOT NULL
                REFERENCES result_slots(result_slot_id) ON DELETE RESTRICT,
            source_workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE path_document_slot_origins (
            child_document_slot_id TEXT PRIMARY KEY
                REFERENCES document_slots(document_slot_id) ON DELETE RESTRICT,
            parent_document_slot_id TEXT NOT NULL
                REFERENCES document_slots(document_slot_id) ON DELETE RESTRICT,
            source_workspace_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE plans (
            plan_id TEXT PRIMARY KEY CHECK (plan_id GLOB 'plan_*'),
            canonical_key TEXT NOT NULL UNIQUE CHECK (length(trim(canonical_key)) > 0),
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE plan_revisions (
            plan_revision_id TEXT PRIMARY KEY CHECK (
                plan_revision_id GLOB 'planrev_*'
            ),
            plan_id TEXT NOT NULL
                REFERENCES plans(plan_id) ON DELETE RESTRICT,
            revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
            summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
            specification_json TEXT NOT NULL CHECK (
                json_valid(specification_json)
                AND json_type(specification_json) = 'object'
            ),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(plan_id, revision_number)
        ) STRICT
        """,
        """
        CREATE TABLE plan_revision_parents (
            plan_revision_id TEXT NOT NULL
                REFERENCES plan_revisions(plan_revision_id) ON DELETE RESTRICT,
            parent_plan_revision_id TEXT NOT NULL
                REFERENCES plan_revisions(plan_revision_id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            relation_kind TEXT NOT NULL CHECK (relation_kind IN ('primary', 'merge')),
            PRIMARY KEY(plan_revision_id, ordinal),
            UNIQUE(plan_revision_id, parent_plan_revision_id),
            CHECK (plan_revision_id != parent_plan_revision_id)
        ) STRICT
        """,
        """
        CREATE TABLE plan_nodes (
            plan_node_id TEXT PRIMARY KEY CHECK (plan_node_id GLOB 'plannode_*'),
            plan_id TEXT NOT NULL
                REFERENCES plans(plan_id) ON DELETE RESTRICT,
            canonical_key TEXT NOT NULL CHECK (length(trim(canonical_key)) > 0),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(plan_id, canonical_key)
        ) STRICT
        """,
        """
        CREATE TABLE plan_revision_nodes (
            plan_revision_id TEXT NOT NULL
                REFERENCES plan_revisions(plan_revision_id) ON DELETE RESTRICT,
            plan_node_id TEXT NOT NULL
                REFERENCES plan_nodes(plan_node_id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            node_kind TEXT NOT NULL CHECK (
                node_kind IN (
                    'data_preparation', 'variable_construction', 'research_design',
                    'estimation', 'robustness', 'heterogeneity', 'mechanism',
                    'visualization', 'document'
                )
            ),
            specification_json TEXT NOT NULL CHECK (
                json_valid(specification_json)
                AND json_type(specification_json) = 'object'
            ),
            PRIMARY KEY(plan_revision_id, plan_node_id),
            UNIQUE(plan_revision_id, ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE plan_node_dependencies (
            plan_revision_id TEXT NOT NULL,
            upstream_plan_node_id TEXT NOT NULL,
            downstream_plan_node_id TEXT NOT NULL,
            dependency_kind TEXT NOT NULL CHECK (
                dependency_kind IN ('data', 'control', 'evidence')
            ),
            PRIMARY KEY(
                plan_revision_id, upstream_plan_node_id,
                downstream_plan_node_id, dependency_kind
            ),
            FOREIGN KEY(plan_revision_id, upstream_plan_node_id)
                REFERENCES plan_revision_nodes(plan_revision_id, plan_node_id)
                ON DELETE RESTRICT,
            FOREIGN KEY(plan_revision_id, downstream_plan_node_id)
                REFERENCES plan_revision_nodes(plan_revision_id, plan_node_id)
                ON DELETE RESTRICT,
            CHECK (upstream_plan_node_id != downstream_plan_node_id)
        ) STRICT
        """,
        """
        CREATE TABLE path_plan_adoption_history (
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            target_plan_revision_id TEXT NOT NULL
                REFERENCES plan_revisions(plan_revision_id) ON DELETE RESTRICT,
            updated_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(research_path_id, pointer_revision)
        ) STRICT
        """,
        """
        CREATE TABLE path_plan_adoptions (
            research_path_id TEXT PRIMARY KEY
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            target_plan_revision_id TEXT NOT NULL
                REFERENCES plan_revisions(plan_revision_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE stata_run_plan_bindings (
            stata_run_id TEXT PRIMARY KEY
                REFERENCES stata_runs(stata_run_id) ON DELETE RESTRICT,
            plan_revision_id TEXT NOT NULL,
            plan_node_id TEXT NOT NULL,
            adherence_verdict TEXT NOT NULL CHECK (
                adherence_verdict IN ('matches', 'deviates')
            ),
            expected_command_sha256 TEXT NOT NULL CHECK (
                length(expected_command_sha256) = 64
            ),
            actual_command_sha256 TEXT NOT NULL CHECK (
                length(actual_command_sha256) = 64
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            FOREIGN KEY(plan_revision_id, plan_node_id)
                REFERENCES plan_revision_nodes(plan_revision_id, plan_node_id)
                ON DELETE RESTRICT
        ) STRICT
        """,
        *tuple(
            statement
            for table in (
                "research_path_profiles",
                "research_path_parents",
                "path_branch_manifests",
                "path_data_slot_origins",
                "path_result_slot_origins",
                "path_document_slot_origins",
                "plans",
                "plan_revisions",
                "plan_revision_parents",
                "plan_nodes",
                "plan_revision_nodes",
                "plan_node_dependencies",
                "path_plan_adoption_history",
                "stata_run_plan_bindings",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)


MIGRATION_019 = Migration(
    version=19,
    name="authoritative_recovery_reports",
    statements=(
        """
        CREATE TABLE recovery_reports (
            recovery_report_id TEXT PRIMARY KEY CHECK (
                recovery_report_id GLOB 'recovery_*'
                AND length(recovery_report_id) > 9
            ),
            assessment_schema_version TEXT NOT NULL CHECK (
                assessment_schema_version = 'recovery-assessment/v1'
            ),
            operation_id TEXT NOT NULL
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            operation_attempt_id TEXT
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            requested_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            classification TEXT NOT NULL CHECK (
                classification IN (
                    'definitely_not_started', 'outcome_unknown',
                    'completed_unreconciled', 'committed', 'integrity_violation'
                )
            ),
            observed_operation_status TEXT NOT NULL,
            observed_attempt_status TEXT,
            observed_turn_status TEXT NOT NULL,
            observed_lane_owner_turn_id TEXT,
            observed_lane_revision INTEGER NOT NULL CHECK (observed_lane_revision >= 0),
            observed_journal_boundary TEXT NOT NULL CHECK (
                observed_journal_boundary IN (
                    'admission_only', 'handoff_committed', 'finalization_committed'
                )
            ),
            completion_manifest_id TEXT,
            manifest_verification_json TEXT NOT NULL CHECK (
                json_valid(manifest_verification_json)
                AND json_type(manifest_verification_json) = 'object'
            ),
            artifact_verification_json TEXT NOT NULL CHECK (
                json_valid(artifact_verification_json)
                AND json_type(artifact_verification_json) = 'object'
            ),
            reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
            blocked_actions_json TEXT NOT NULL CHECK (
                json_valid(blocked_actions_json)
                AND json_type(blocked_actions_json) = 'array'
            ),
            available_user_actions_json TEXT NOT NULL CHECK (
                json_valid(available_user_actions_json)
                AND json_type(available_user_actions_json) = 'array'
            ),
            turn_paused INTEGER NOT NULL CHECK (turn_paused IN (0, 1)),
            lane_released INTEGER NOT NULL CHECK (lane_released IN (0, 1)),
            input_fingerprint TEXT NOT NULL UNIQUE CHECK (length(input_fingerprint) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE INDEX recovery_reports_operation_history
        ON recovery_reports(operation_id, created_revision)
        """,
        """
        CREATE TRIGGER recovery_reports_no_update BEFORE UPDATE ON recovery_reports
        BEGIN SELECT RAISE(ABORT, 'recovery reports are immutable'); END
        """,
        """
        CREATE TRIGGER recovery_reports_no_delete BEFORE DELETE ON recovery_reports
        BEGIN SELECT RAISE(ABORT, 'recovery reports are immutable'); END
        """,
    ),
)


MIGRATION_020 = Migration(
    version=20,
    name="analysis_output_classification_adoption_and_eligibility",
    statements=(
        """
        CREATE TABLE analysis_environment_snapshots (
            analysis_environment_snapshot_id TEXT PRIMARY KEY CHECK (
                analysis_environment_snapshot_id GLOB 'envsnap_*'
            ),
            runtime_kind TEXT NOT NULL CHECK (runtime_kind IN ('python', 'shell')),
            payload_json TEXT NOT NULL CHECK (
                json_valid(payload_json) AND json_type(payload_json) = 'object'
            ),
            payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE analysis_outputs (
            analysis_output_id TEXT PRIMARY KEY CHECK (
                analysis_output_id GLOB 'analysisout_*'
            ),
            producer_operation_id TEXT NOT NULL
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            producer_attempt_id TEXT NOT NULL
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            runtime_kind TEXT NOT NULL CHECK (runtime_kind IN ('python', 'shell')),
            executable_artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            executable_sha256 TEXT NOT NULL CHECK (length(executable_sha256) = 64),
            analysis_environment_snapshot_id TEXT NOT NULL
                REFERENCES analysis_environment_snapshots(analysis_environment_snapshot_id)
                ON DELETE RESTRICT,
            output_fingerprint TEXT NOT NULL UNIQUE CHECK (length(output_fingerprint) = 64),
            method_summary TEXT NOT NULL CHECK (length(trim(method_summary)) > 0),
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            UNIQUE(producer_operation_id, producer_attempt_id)
        ) STRICT
        """,
        """
        CREATE TABLE analysis_output_inputs (
            analysis_output_id TEXT NOT NULL
                REFERENCES analysis_outputs(analysis_output_id) ON DELETE RESTRICT,
            artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            PRIMARY KEY(analysis_output_id, ordinal),
            UNIQUE(analysis_output_id, artifact_id)
        ) STRICT
        """,
        """
        CREATE TABLE analysis_output_artifacts (
            analysis_output_id TEXT NOT NULL
                REFERENCES analysis_outputs(analysis_output_id) ON DELETE RESTRICT,
            artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            role TEXT NOT NULL CHECK (role IN ('primary', 'preview', 'supporting')),
            PRIMARY KEY(analysis_output_id, ordinal),
            UNIQUE(analysis_output_id, artifact_id)
        ) STRICT
        """,
        """
        CREATE TABLE analysis_output_elements (
            analysis_output_id TEXT NOT NULL
                REFERENCES analysis_outputs(analysis_output_id) ON DELETE RESTRICT,
            stable_key TEXT NOT NULL CHECK (length(trim(stable_key)) > 0),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            value_json TEXT NOT NULL CHECK (json_valid(value_json)),
            rendered_text TEXT NOT NULL,
            value_sha256 TEXT NOT NULL CHECK (length(value_sha256) = 64),
            PRIMARY KEY(analysis_output_id, stable_key),
            UNIQUE(analysis_output_id, ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE analysis_output_classifications (
            analysis_output_classification_id TEXT PRIMARY KEY CHECK (
                analysis_output_classification_id GLOB 'analysisclass_*'
            ),
            analysis_output_id TEXT NOT NULL UNIQUE
                REFERENCES analysis_outputs(analysis_output_id) ON DELETE RESTRICT,
            output_kind TEXT NOT NULL CHECK (
                output_kind IN (
                    'visual', 'scalar', 'table', 'test', 'custom',
                    'regression', 'unknown'
                )
            ),
            classifier_id TEXT NOT NULL,
            classifier_version TEXT NOT NULL,
            basis_json TEXT NOT NULL CHECK (
                json_valid(basis_json) AND json_type(basis_json) = 'object'
            ),
            classified_fingerprint TEXT NOT NULL CHECK (length(classified_fingerprint) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE analysis_output_adoptions (
            analysis_output_adoption_id TEXT PRIMARY KEY CHECK (
                analysis_output_adoption_id GLOB 'analysisadopt_*'
            ),
            analysis_output_id TEXT NOT NULL UNIQUE
                REFERENCES analysis_outputs(analysis_output_id) ON DELETE RESTRICT,
            analysis_output_classification_id TEXT NOT NULL UNIQUE
                REFERENCES analysis_output_classifications(analysis_output_classification_id)
                ON DELETE RESTRICT,
            adopted_fingerprint TEXT NOT NULL CHECK (length(adopted_fingerprint) = 64),
            preview_artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            confirmation_summary TEXT NOT NULL CHECK (length(trim(confirmation_summary)) > 0),
            adopted_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            initiating_message_id TEXT NOT NULL
                REFERENCES messages(message_id) ON DELETE RESTRICT,
            adoption_command_id TEXT NOT NULL UNIQUE
                REFERENCES command_receipts(command_id) ON DELETE RESTRICT
                    DEFERRABLE INITIALLY DEFERRED,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE analysis_evidence_records (
            evidence_record_id TEXT PRIMARY KEY CHECK (
                evidence_record_id GLOB 'evidence_*'
            ),
            evidence_kind TEXT NOT NULL CHECK (
                evidence_kind = 'adopted_analysis_output'
            ),
            analysis_output_id TEXT NOT NULL UNIQUE
                REFERENCES analysis_outputs(analysis_output_id) ON DELETE RESTRICT,
            analysis_output_adoption_id TEXT NOT NULL UNIQUE
                REFERENCES analysis_output_adoptions(analysis_output_adoption_id)
                ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE analysis_document_eligibility_receipts (
            analysis_document_eligibility_id TEXT PRIMARY KEY CHECK (
                analysis_document_eligibility_id GLOB 'analysiseligibility_*'
            ),
            analysis_output_id TEXT NOT NULL UNIQUE
                REFERENCES analysis_outputs(analysis_output_id) ON DELETE RESTRICT,
            analysis_output_adoption_id TEXT NOT NULL UNIQUE
                REFERENCES analysis_output_adoptions(analysis_output_adoption_id)
                ON DELETE RESTRICT,
            evidence_record_id TEXT NOT NULL UNIQUE
                REFERENCES analysis_evidence_records(evidence_record_id) ON DELETE RESTRICT,
            eligible_fingerprint TEXT NOT NULL CHECK (length(eligible_fingerprint) = 64),
            eligibility_status TEXT NOT NULL CHECK (eligibility_status = 'eligible'),
            coverage_status TEXT NOT NULL CHECK (coverage_status = 'complete'),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        *tuple(
            statement
            for table in (
                "analysis_environment_snapshots",
                "analysis_outputs",
                "analysis_output_inputs",
                "analysis_output_artifacts",
                "analysis_output_elements",
                "analysis_output_classifications",
                "analysis_output_adoptions",
                "analysis_evidence_records",
                "analysis_document_eligibility_receipts",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)


MIGRATION_021 = Migration(
    version=21,
    name="document_human_roundtrip_revision_and_diff",
    statements=(
        "DROP TRIGGER document_revisions_no_update",
        "DROP TRIGGER document_revisions_no_delete",
        "DROP TRIGGER document_parse_receipts_no_update",
        "DROP TRIGGER document_parse_receipts_no_delete",
        "DROP TRIGGER delivery_gate_reports_no_update",
        "DROP TRIGGER delivery_gate_reports_no_delete",
        "DROP TRIGGER path_document_adoption_history_no_update",
        "DROP TRIGGER path_document_adoption_history_no_delete",
        "ALTER TABLE document_parse_receipts RENAME TO document_parse_receipts_legacy",
        "ALTER TABLE delivery_gate_reports RENAME TO delivery_gate_reports_legacy",
        "ALTER TABLE path_document_adoption_history "
        "RENAME TO path_document_adoption_history_legacy",
        "ALTER TABLE path_document_adoptions RENAME TO path_document_adoptions_legacy",
        "ALTER TABLE document_revisions RENAME TO document_revisions_legacy",
        """
        CREATE TABLE document_revisions (
            document_revision_id TEXT PRIMARY KEY CHECK (
                document_revision_id GLOB 'docrev_*'
            ),
            document_id TEXT NOT NULL
                REFERENCES documents(document_id) ON DELETE RESTRICT,
            origin_kind TEXT NOT NULL CHECK (
                origin_kind IN (
                    'agent_generated', 'user_returned', 'system_merged', 'imported'
                )
            ),
            primary_parent_revision_id TEXT
                REFERENCES document_revisions(document_revision_id)
                ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
            docx_artifact_id TEXT NOT NULL UNIQUE
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            manifest_artifact_id TEXT NOT NULL UNIQUE
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            document_manifest_id TEXT NOT NULL UNIQUE
                REFERENCES document_manifests(document_manifest_id) ON DELETE RESTRICT,
            created_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            producer_operation_id TEXT NOT NULL
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        INSERT INTO document_revisions SELECT * FROM document_revisions_legacy
        """,
        """
        CREATE TABLE document_parse_receipts (
            document_parse_receipt_id TEXT PRIMARY KEY CHECK (
                document_parse_receipt_id GLOB 'docparse_*'
            ),
            document_revision_id TEXT NOT NULL UNIQUE
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            package_sha256 TEXT NOT NULL CHECK (length(package_sha256) = 64),
            parse_verdict TEXT NOT NULL CHECK (parse_verdict = 'pass'),
            findings_json TEXT NOT NULL CHECK (
                json_valid(findings_json) AND json_type(findings_json) = 'array'
            ),
            parser_profile TEXT NOT NULL CHECK (parser_profile = 'ooxml.safe-reader.v1'),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        "INSERT INTO document_parse_receipts SELECT * FROM document_parse_receipts_legacy",
        """
        CREATE TABLE delivery_gate_reports (
            delivery_gate_report_id TEXT PRIMARY KEY CHECK (
                delivery_gate_report_id GLOB 'deliverygate_*'
            ),
            document_revision_id TEXT NOT NULL UNIQUE
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'unknown')),
            frozen_dependencies_json TEXT NOT NULL CHECK (
                json_valid(frozen_dependencies_json)
                AND json_type(frozen_dependencies_json) = 'object'
            ),
            findings_json TEXT NOT NULL CHECK (
                json_valid(findings_json) AND json_type(findings_json) = 'array'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        "INSERT INTO delivery_gate_reports SELECT * FROM delivery_gate_reports_legacy",
        """
        CREATE TABLE path_document_adoption_history (
            document_slot_id TEXT NOT NULL
                REFERENCES document_slots(document_slot_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            target_document_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            updated_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            delivery_gate_report_id TEXT
                REFERENCES delivery_gate_reports(delivery_gate_report_id) ON DELETE RESTRICT,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(document_slot_id, pointer_revision)
        ) STRICT
        """,
        """
        INSERT INTO path_document_adoption_history
        SELECT * FROM path_document_adoption_history_legacy
        """,
        """
        CREATE TABLE path_document_adoptions (
            document_slot_id TEXT PRIMARY KEY
                REFERENCES document_slots(document_slot_id) ON DELETE RESTRICT,
            target_document_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_by_turn_id TEXT NOT NULL
                REFERENCES turns(turn_id) ON DELETE RESTRICT,
            delivery_gate_report_id TEXT
                REFERENCES delivery_gate_reports(delivery_gate_report_id) ON DELETE RESTRICT,
            commit_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        "INSERT INTO path_document_adoptions SELECT * FROM path_document_adoptions_legacy",
        "DROP TABLE path_document_adoptions_legacy",
        "DROP TABLE path_document_adoption_history_legacy",
        "DROP TABLE delivery_gate_reports_legacy",
        "DROP TABLE document_parse_receipts_legacy",
        "DROP TABLE document_revisions_legacy",
        """
        CREATE TABLE document_revision_view_policies (
            document_revision_view_policy_id TEXT PRIMARY KEY CHECK (
                document_revision_view_policy_id GLOB 'docviewpolicy_*'
            ),
            policy_version INTEGER NOT NULL CHECK (policy_version = 1),
            policy_json TEXT NOT NULL CHECK (
                json_valid(policy_json) AND json_type(policy_json) = 'object'
            ),
            policy_sha256 TEXT NOT NULL UNIQUE CHECK (length(policy_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE document_diffs (
            document_diff_id TEXT PRIMARY KEY CHECK (document_diff_id GLOB 'docdiff_*'),
            base_document_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            returned_document_revision_id TEXT NOT NULL UNIQUE
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            document_revision_view_policy_id TEXT NOT NULL
                REFERENCES document_revision_view_policies(document_revision_view_policy_id)
                ON DELETE RESTRICT,
            semantic_change_classification TEXT NOT NULL CHECK (
                semantic_change_classification IN (
                    'no_semantic_change', 'prose_only', 'managed_conflict',
                    'structure_conflict', 'unsupported'
                )
            ),
            findings_json TEXT NOT NULL CHECK (
                json_valid(findings_json) AND json_type(findings_json) = 'array'
            ),
            normalized_base_sha256 TEXT NOT NULL CHECK (length(normalized_base_sha256) = 64),
            normalized_returned_sha256 TEXT NOT NULL CHECK (
                length(normalized_returned_sha256) = 64
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE document_return_reports (
            document_return_report_id TEXT PRIMARY KEY CHECK (
                document_return_report_id GLOB 'docreturn_*'
            ),
            base_document_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            returned_document_revision_id TEXT NOT NULL UNIQUE
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            document_diff_id TEXT NOT NULL UNIQUE
                REFERENCES document_diffs(document_diff_id) ON DELETE RESTRICT,
            return_verdict TEXT NOT NULL CHECK (
                return_verdict IN ('accepted_working', 'delivery_eligible', 'conflict')
            ),
            marker_observations_json TEXT NOT NULL CHECK (
                json_valid(marker_observations_json)
                AND json_type(marker_observations_json) = 'array'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE document_merge_receipts (
            document_merge_receipt_id TEXT PRIMARY KEY CHECK (
                document_merge_receipt_id GLOB 'docmerge_*'
            ),
            common_base_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            human_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            agent_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            merged_revision_id TEXT NOT NULL UNIQUE
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            merge_verdict TEXT NOT NULL CHECK (merge_verdict IN ('merged', 'conflict')),
            decisions_json TEXT NOT NULL CHECK (
                json_valid(decisions_json) AND json_type(decisions_json) = 'array'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            CHECK (human_revision_id != agent_revision_id)
        ) STRICT
        """,
        """
        CREATE TABLE document_revision_merge_inputs (
            merged_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            input_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            input_role TEXT NOT NULL CHECK (input_role IN ('human', 'agent')),
            PRIMARY KEY(merged_revision_id, input_role),
            UNIQUE(merged_revision_id, input_revision_id),
            CHECK (merged_revision_id != input_revision_id)
        ) STRICT
        """,
        """
        CREATE TRIGGER document_revisions_no_update BEFORE UPDATE ON document_revisions
        BEGIN SELECT RAISE(ABORT, 'document_revisions is immutable'); END
        """,
        """
        CREATE TRIGGER document_revisions_no_delete BEFORE DELETE ON document_revisions
        BEGIN SELECT RAISE(ABORT, 'document_revisions is immutable'); END
        """,
        """
        CREATE TRIGGER document_parse_receipts_no_update BEFORE UPDATE ON document_parse_receipts
        BEGIN SELECT RAISE(ABORT, 'document_parse_receipts is immutable'); END
        """,
        """
        CREATE TRIGGER document_parse_receipts_no_delete BEFORE DELETE ON document_parse_receipts
        BEGIN SELECT RAISE(ABORT, 'document_parse_receipts is immutable'); END
        """,
        """
        CREATE TRIGGER delivery_gate_reports_no_update BEFORE UPDATE ON delivery_gate_reports
        BEGIN SELECT RAISE(ABORT, 'delivery_gate_reports is immutable'); END
        """,
        """
        CREATE TRIGGER delivery_gate_reports_no_delete BEFORE DELETE ON delivery_gate_reports
        BEGIN SELECT RAISE(ABORT, 'delivery_gate_reports is immutable'); END
        """,
        """
        CREATE TRIGGER path_document_adoption_history_no_update
        BEFORE UPDATE ON path_document_adoption_history
        BEGIN SELECT RAISE(ABORT, 'Document adoption history is immutable'); END
        """,
        """
        CREATE TRIGGER path_document_adoption_history_no_delete
        BEFORE DELETE ON path_document_adoption_history
        BEGIN SELECT RAISE(ABORT, 'Document adoption history is immutable'); END
        """,
        *tuple(
            statement
            for table in (
                "document_revision_view_policies",
                "document_diffs",
                "document_return_reports",
                "document_merge_receipts",
                "document_revision_merge_inputs",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)


MIGRATION_022 = Migration(
    version=22,
    name="raw_rejected_document_return",
    statements=(
        """
        CREATE TABLE document_raw_return_reports (
            document_return_report_id TEXT PRIMARY KEY CHECK (
                document_return_report_id GLOB 'docreturn_*'
            ),
            base_document_revision_id TEXT NOT NULL
                REFERENCES document_revisions(document_revision_id) ON DELETE RESTRICT,
            raw_docx_artifact_id TEXT NOT NULL UNIQUE
                REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
            operation_id TEXT NOT NULL UNIQUE
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            operation_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            finding_code TEXT NOT NULL CHECK (finding_code IN (
                'DOCX_PACKAGE_INVALID', 'DOCX_UNSUPPORTED_FEATURE',
                'DOCX_ACTIVE_CONTENT_BLOCKED', 'DOCX_EXTERNAL_DEPENDENCY'
            )),
            detail TEXT NOT NULL,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TRIGGER document_raw_return_reports_no_update
        BEFORE UPDATE ON document_raw_return_reports
        BEGIN SELECT RAISE(ABORT, 'document_raw_return_reports is immutable'); END
        """,
        """
        CREATE TRIGGER document_raw_return_reports_no_delete
        BEFORE DELETE ON document_raw_return_reports
        BEGIN SELECT RAISE(ABORT, 'document_raw_return_reports is immutable'); END
        """,
    ),
)


MIGRATION_023 = Migration(
    version=23,
    name="path_aware_evidence_eligibility_and_projection",
    statements=(
        """
        CREATE TABLE evidence_projection_checkpoints (
            projection_name TEXT PRIMARY KEY CHECK (
                projection_name = 'evidence_current_state'
            ),
            projection_revision INTEGER NOT NULL CHECK (projection_revision >= 0),
            rebuilt_at TEXT NOT NULL
        ) STRICT
        """,
        """
        INSERT INTO evidence_projection_checkpoints
        VALUES ('evidence_current_state', 0, 'not-built')
        """,
        """
        CREATE TABLE statistical_evidence_current_states (
            evidence_record_id TEXT PRIMARY KEY
                REFERENCES evidence_records(evidence_record_id) ON DELETE CASCADE,
            source_state TEXT NOT NULL CHECK (
                source_state IN ('available', 'source_unavailable', 'needs_review', 'unknown')
            ),
            reason_code TEXT NOT NULL,
            projection_revision INTEGER NOT NULL CHECK (projection_revision >= 0)
        ) STRICT
        """,
        """
        CREATE TABLE analysis_evidence_current_states (
            evidence_record_id TEXT PRIMARY KEY
                REFERENCES analysis_evidence_records(evidence_record_id) ON DELETE CASCADE,
            source_state TEXT NOT NULL CHECK (
                source_state IN ('available', 'source_unavailable', 'needs_review', 'unknown')
            ),
            reason_code TEXT NOT NULL,
            projection_revision INTEGER NOT NULL CHECK (projection_revision >= 0)
        ) STRICT
        """,
        """
        CREATE TABLE statistical_evidence_use_validation_receipts (
            evidence_validation_receipt_id TEXT PRIMARY KEY CHECK (
                evidence_validation_receipt_id GLOB 'evidencevalidation_*'
            ),
            evidence_record_id TEXT NOT NULL
                REFERENCES evidence_records(evidence_record_id) ON DELETE RESTRICT,
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            use_context_kind TEXT NOT NULL CHECK (use_context_kind IN (
                'current_adopted_result', 'explicit_historical_comparison',
                'audit_or_explanation'
            )),
            purpose TEXT NOT NULL CHECK (purpose IN (
                'message_authoring', 'document_delivery', 'lineage_preview'
            )),
            verdict TEXT NOT NULL CHECK (verdict IN ('eligible', 'ineligible', 'unknown')),
            reason_codes_json TEXT NOT NULL CHECK (
                json_valid(reason_codes_json) AND json_type(reason_codes_json) = 'array'
            ),
            result_id TEXT NOT NULL REFERENCES results(result_id) ON DELETE RESTRICT,
            result_slot_id TEXT REFERENCES result_slots(result_slot_id) ON DELETE RESTRICT,
            result_pointer_revision INTEGER,
            plan_revision_id TEXT REFERENCES plan_revisions(plan_revision_id) ON DELETE RESTRICT,
            plan_pointer_revision INTEGER,
            dependency_snapshot_json TEXT NOT NULL CHECK (
                json_valid(dependency_snapshot_json)
                AND json_type(dependency_snapshot_json) = 'array'
            ),
            authoritative_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            policy_version INTEGER NOT NULL CHECK (policy_version = 1),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE analysis_evidence_use_validation_receipts (
            evidence_validation_receipt_id TEXT PRIMARY KEY CHECK (
                evidence_validation_receipt_id GLOB 'evidencevalidation_*'
            ),
            evidence_record_id TEXT NOT NULL
                REFERENCES analysis_evidence_records(evidence_record_id) ON DELETE RESTRICT,
            research_path_id TEXT NOT NULL
                REFERENCES research_paths(research_path_id) ON DELETE RESTRICT,
            use_context_kind TEXT NOT NULL CHECK (use_context_kind IN (
                'current_adopted_result', 'explicit_historical_comparison',
                'audit_or_explanation'
            )),
            purpose TEXT NOT NULL CHECK (purpose IN (
                'message_authoring', 'document_delivery', 'lineage_preview'
            )),
            verdict TEXT NOT NULL CHECK (verdict IN ('eligible', 'ineligible', 'unknown')),
            reason_codes_json TEXT NOT NULL CHECK (
                json_valid(reason_codes_json) AND json_type(reason_codes_json) = 'array'
            ),
            analysis_output_id TEXT NOT NULL
                REFERENCES analysis_outputs(analysis_output_id) ON DELETE RESTRICT,
            analysis_output_adoption_id TEXT NOT NULL
                REFERENCES analysis_output_adoptions(analysis_output_adoption_id)
                ON DELETE RESTRICT,
            artifact_snapshot_json TEXT NOT NULL CHECK (
                json_valid(artifact_snapshot_json)
                AND json_type(artifact_snapshot_json) = 'array'
            ),
            authoritative_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            policy_version INTEGER NOT NULL CHECK (policy_version = 1),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        *tuple(
            statement
            for table in (
                "statistical_evidence_use_validation_receipts",
                "analysis_evidence_use_validation_receipts",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)


MIGRATION_024 = Migration(
    version=24,
    name="provider_credential_version_audit_binding",
    statements=(
        """
        ALTER TABLE outbound_material_records
        ADD COLUMN credential_version_ref TEXT
        """,
    ),
)


MIGRATION_025 = Migration(
    version=25,
    name="sandbox_execution_receipts",
    statements=(
        """
        CREATE TABLE sandbox_execution_receipts (
            operation_attempt_id TEXT PRIMARY KEY
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            operation_id TEXT NOT NULL UNIQUE
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            schema_version TEXT NOT NULL,
            isolation_tier TEXT NOT NULL CHECK (isolation_tier = 'base-container'),
            policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
            network_mode TEXT NOT NULL CHECK (network_mode IN ('block', 'allow')),
            readonly_grants_json TEXT NOT NULL CHECK (
                json_valid(readonly_grants_json)
                AND json_type(readonly_grants_json) = 'array'
            ),
            readwrite_grants_json TEXT NOT NULL CHECK (
                json_valid(readwrite_grants_json)
                AND json_type(readwrite_grants_json) = 'array'
            ),
            dacl_fallback_allowed INTEGER NOT NULL CHECK (dacl_fallback_allowed = 0),
            staging_preflight TEXT NOT NULL CHECK (staging_preflight = 'passed'),
            staging_postflight TEXT NOT NULL CHECK (staging_postflight = 'passed'),
            exit_code INTEGER NOT NULL,
            output_candidates_json TEXT NOT NULL CHECK (
                json_valid(output_candidates_json)
                AND json_type(output_candidates_json) = 'array'
            ),
            receipt_sha256 TEXT NOT NULL CHECK (length(receipt_sha256) = 64),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TRIGGER sandbox_execution_receipts_no_update
        BEFORE UPDATE ON sandbox_execution_receipts
        BEGIN SELECT RAISE(ABORT, 'sandbox execution receipts are immutable'); END
        """,
        """
        CREATE TRIGGER sandbox_execution_receipts_no_delete
        BEFORE DELETE ON sandbox_execution_receipts
        BEGIN SELECT RAISE(ABORT, 'sandbox execution receipts are immutable'); END
        """,
    ),
)


MIGRATION_026 = Migration(
    version=26,
    name="workspace_migration_control",
    statements=(
        """
        CREATE TABLE workspace_migration_attempts (
            migration_attempt_id TEXT PRIMARY KEY CHECK (
                migration_attempt_id GLOB 'migration_*'
            ),
            source_schema_version INTEGER NOT NULL CHECK (source_schema_version >= 1),
            target_schema_version INTEGER NOT NULL CHECK (
                target_schema_version > source_schema_version
            ),
            target_release_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'PREPARING','PREPARED','ADOPTING','ADOPTED',
                'CLEANUP_COMPLETE','RECOVERY_REQUIRED'
            )),
            backup_manifest_json TEXT CHECK (
                backup_manifest_json IS NULL OR json_valid(backup_manifest_json)
            ),
            candidate_manifest_json TEXT CHECK (
                candidate_manifest_json IS NULL OR json_valid(candidate_manifest_json)
            ),
            failure_code TEXT,
            attempt_revision INTEGER NOT NULL CHECK (attempt_revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE UNIQUE INDEX one_open_workspace_migration
        ON workspace_migration_attempts((1))
        WHERE state IN ('PREPARING','PREPARED','ADOPTING','RECOVERY_REQUIRED')
        """,
        """
        CREATE TABLE workspace_migration_attempt_history (
            migration_attempt_id TEXT NOT NULL
                REFERENCES workspace_migration_attempts(migration_attempt_id) ON DELETE RESTRICT,
            attempt_revision INTEGER NOT NULL,
            state TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(migration_attempt_id, attempt_revision)
        ) STRICT
        """,
        """
        CREATE TABLE workspace_migration_lock (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
            migration_attempt_id TEXT
                REFERENCES workspace_migration_attempts(migration_attempt_id) ON DELETE RESTRICT,
            acquired_at TEXT,
            CHECK (
                (migration_attempt_id IS NULL AND acquired_at IS NULL)
                OR (migration_attempt_id IS NOT NULL AND acquired_at IS NOT NULL)
            )
        ) STRICT
        """,
        "INSERT INTO workspace_migration_lock VALUES (1, NULL, NULL)",
        """
        CREATE TABLE workspace_migration_receipts (
            migration_receipt_id TEXT PRIMARY KEY CHECK (
                migration_receipt_id GLOB 'migrationreceipt_*'
            ),
            migration_attempt_id TEXT NOT NULL UNIQUE
                REFERENCES workspace_migration_attempts(migration_attempt_id) ON DELETE RESTRICT,
            source_schema_version INTEGER NOT NULL,
            target_schema_version INTEGER NOT NULL,
            target_release_id TEXT NOT NULL,
            backup_sha256 TEXT NOT NULL CHECK(length(backup_sha256) = 64),
            candidate_sha256 TEXT NOT NULL CHECK(length(candidate_sha256) = 64),
            research_identity_preserved INTEGER NOT NULL CHECK(
                research_identity_preserved = 1
            ),
            research_adoption_preserved INTEGER NOT NULL CHECK(
                research_adoption_preserved = 1
            ),
            created_at TEXT NOT NULL
        ) STRICT
        """,
        *tuple(
            statement
            for table in (
                "workspace_migration_attempt_history",
                "workspace_migration_receipts",
            )
            for statement in (
                f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
                f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END""",
            )
        ),
    ),
)


MIGRATION_027 = Migration(
    version=27,
    name="formal_stata_data_state_chain",
    statements=(
        "DROP TRIGGER stata_operation_input_bindings_no_update",
        "DROP TRIGGER stata_operation_input_bindings_no_delete",
        "ALTER TABLE stata_operation_input_bindings RENAME TO old_stata_operation_input_bindings",
        """
        CREATE TABLE stata_operation_input_bindings (
            operation_attempt_id TEXT PRIMARY KEY
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            executable_source_id TEXT NOT NULL UNIQUE
                REFERENCES executable_sources(executable_source_id) ON DELETE RESTRICT,
            execution_purpose TEXT NOT NULL CHECK (
                execution_purpose IN (
                    'general', 'data_load', 'data_step', 'formal_estimation'
                )
            ),
            input_data_version_id TEXT
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            input_data_slot_key TEXT,
            input_verification_receipt_id TEXT
                REFERENCES artifact_verification_receipts(verification_receipt_id)
                ON DELETE RESTRICT,
            source_data_state_operation_id TEXT
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            expected_data_state_token TEXT,
            expected_session_generation INTEGER CHECK (
                expected_session_generation IS NULL OR expected_session_generation >= 1
            ),
            bound_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            CHECK (
                (input_data_version_id IS NULL AND input_data_slot_key IS NULL
                    AND input_verification_receipt_id IS NULL
                    AND execution_purpose = 'general')
                OR
                (input_data_version_id IS NOT NULL AND input_data_slot_key IS NOT NULL
                    AND input_verification_receipt_id IS NOT NULL
                    AND execution_purpose IN (
                        'data_load', 'data_step', 'formal_estimation'
                    ))
            ),
            CHECK (
                (execution_purpose IN ('general', 'data_load')
                    AND source_data_state_operation_id IS NULL
                    AND expected_data_state_token IS NULL
                    AND expected_session_generation IS NULL)
                OR
                (execution_purpose IN ('data_step', 'formal_estimation')
                    AND source_data_state_operation_id IS NOT NULL
                    AND expected_data_state_token IS NOT NULL
                    AND expected_session_generation IS NOT NULL)
            )
        ) STRICT
        """,
        """
        INSERT INTO stata_operation_input_bindings (
            operation_attempt_id, executable_source_id, execution_purpose,
            input_data_version_id, input_data_slot_key,
            input_verification_receipt_id, source_data_state_operation_id,
            expected_data_state_token, expected_session_generation, bound_revision
        )
        SELECT operation_attempt_id, executable_source_id, execution_purpose,
               input_data_version_id, input_data_slot_key,
               input_verification_receipt_id, source_data_load_operation_id,
               expected_data_state_token, expected_session_generation, bound_revision
        FROM old_stata_operation_input_bindings
        """,
        "DROP TABLE old_stata_operation_input_bindings",
        """
        CREATE TRIGGER stata_operation_input_bindings_no_update
        BEFORE UPDATE ON stata_operation_input_bindings
        BEGIN SELECT RAISE(ABORT, 'Stata operation input bindings are immutable'); END
        """,
        """
        CREATE TRIGGER stata_operation_input_bindings_no_delete
        BEFORE DELETE ON stata_operation_input_bindings
        BEGIN SELECT RAISE(ABORT, 'Stata operation input bindings are immutable'); END
        """,
    ),
)


MIGRATION_028 = Migration(
    version=28,
    name="context_compiler_and_provider_cache_accounting",
    statements=(
        """
        ALTER TABLE provider_attempts ADD COLUMN cached_input_tokens INTEGER
        CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0)
        """,
        """
        ALTER TABLE provider_attempts ADD COLUMN uncached_input_tokens INTEGER
        CHECK (uncached_input_tokens IS NULL OR uncached_input_tokens >= 0)
        """,
        """
        ALTER TABLE context_manifests ADD COLUMN input_token_estimate INTEGER NOT NULL DEFAULT 0
        CHECK (input_token_estimate >= 0)
        """,
        """
        ALTER TABLE context_manifests ADD COLUMN reserved_output_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (reserved_output_tokens >= 0)
        """,
    ),
)


MIGRATION_029 = Migration(
    version=29,
    name="project_memory_and_progressive_recall",
    statements=(
        """
        CREATE TABLE memory_items (
            memory_item_id TEXT PRIMARY KEY CHECK (
                memory_item_id GLOB 'memoryitem_*' AND length(memory_item_id) > 11
            ),
            scope_kind TEXT NOT NULL CHECK (scope_kind IN ('workspace', 'research_path')),
            scope_object_id TEXT NOT NULL,
            memory_kind TEXT NOT NULL CHECK (memory_kind IN (
                'user_preference', 'research_decision', 'research_constraint', 'feedback',
                'unresolved_question', 'reference_pointer', 'project_procedure'
            )),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE memory_revisions (
            memory_revision_id TEXT PRIMARY KEY CHECK (
                memory_revision_id GLOB 'memoryrev_*' AND length(memory_revision_id) > 10
            ),
            memory_item_id TEXT NOT NULL REFERENCES memory_items(memory_item_id)
                ON DELETE RESTRICT,
            revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            origin_kind TEXT NOT NULL CHECK (
                origin_kind IN ('explicit_user', 'confirmed', 'inferred', 'imported')
            ),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(memory_item_id, revision_number)
        ) STRICT
        """,
        """
        CREATE TABLE memory_revision_sources (
            memory_revision_source_id TEXT PRIMARY KEY CHECK (
                memory_revision_source_id GLOB 'memorysource_*'
                AND length(memory_revision_source_id) > 13
            ),
            memory_revision_id TEXT NOT NULL REFERENCES memory_revisions(memory_revision_id)
                ON DELETE RESTRICT,
            source_object_type TEXT NOT NULL CHECK (length(trim(source_object_type)) > 0),
            source_object_id TEXT NOT NULL CHECK (length(trim(source_object_id)) > 0),
            source_object_revision TEXT NOT NULL CHECK (length(trim(source_object_revision)) > 0),
            source_role TEXT NOT NULL CHECK (source_role IN (
                'user_statement', 'user_confirmation', 'assistant_inference',
                'research_fact_reference', 'external_reference', 'import'
            )),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(
                memory_revision_id, source_object_type, source_object_id,
                source_object_revision, source_role
            )
        ) STRICT
        """,
        """
        CREATE TABLE memory_state_history (
            memory_state_history_id TEXT PRIMARY KEY CHECK (
                memory_state_history_id GLOB 'memorystate_*'
                AND length(memory_state_history_id) > 12
            ),
            memory_item_id TEXT NOT NULL REFERENCES memory_items(memory_item_id)
                ON DELETE RESTRICT,
            memory_revision_id TEXT NOT NULL REFERENCES memory_revisions(memory_revision_id)
                ON DELETE RESTRICT,
            lifecycle TEXT NOT NULL CHECK (lifecycle IN ('proposed', 'active', 'retracted')),
            reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE memory_current_states (
            memory_item_id TEXT PRIMARY KEY REFERENCES memory_items(memory_item_id)
                ON DELETE RESTRICT,
            current_revision_id TEXT NOT NULL REFERENCES memory_revisions(memory_revision_id)
                ON DELETE RESTRICT,
            lifecycle TEXT NOT NULL CHECK (lifecycle IN ('proposed', 'active', 'retracted')),
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE conversation_memory_policies (
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id)
                ON DELETE RESTRICT,
            use_memory INTEGER NOT NULL CHECK (use_memory IN (0, 1)),
            contribute_memory INTEGER NOT NULL CHECK (contribute_memory IN (0, 1)),
            policy_revision INTEGER NOT NULL CHECK (policy_revision >= 1),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE memory_episodes (
            memory_episode_id TEXT PRIMARY KEY CHECK (
                memory_episode_id GLOB 'memoryepisode_*'
                AND length(memory_episode_id) > 14
            ),
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                ON DELETE RESTRICT,
            source_start_revision INTEGER NOT NULL CHECK (source_start_revision >= 1),
            source_end_revision INTEGER NOT NULL CHECK (
                source_end_revision >= source_start_revision
            ),
            summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
            summary_sha256 TEXT NOT NULL CHECK (length(summary_sha256) = 64),
            extractor_kind TEXT NOT NULL CHECK (length(trim(extractor_kind)) > 0),
            extractor_revision TEXT NOT NULL CHECK (length(trim(extractor_revision)) > 0),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(conversation_id, source_end_revision, extractor_revision)
        ) STRICT
        """,
        """
        CREATE TABLE memory_context_uses (
            context_item_id TEXT PRIMARY KEY REFERENCES context_items(context_item_id)
                ON DELETE RESTRICT,
            memory_item_id TEXT NOT NULL REFERENCES memory_items(memory_item_id)
                ON DELETE RESTRICT,
            memory_revision_id TEXT NOT NULL REFERENCES memory_revisions(memory_revision_id)
                ON DELETE RESTRICT,
            usage_kind TEXT NOT NULL CHECK (usage_kind IN ('exact', 'index')),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE INDEX memory_active_scope_idx
        ON memory_items(scope_kind, scope_object_id, memory_kind, created_revision)
        """,
        """
        CREATE INDEX memory_sources_lookup_idx
        ON memory_revision_sources(source_object_type, source_object_id, source_object_revision)
        """,
        """
        CREATE INDEX memory_context_revision_idx
        ON memory_context_uses(memory_revision_id, created_revision)
        """,
        """
        CREATE TRIGGER memory_items_no_update BEFORE UPDATE ON memory_items
        BEGIN SELECT RAISE(ABORT, 'Memory items are immutable'); END
        """,
        """
        CREATE TRIGGER memory_items_no_delete BEFORE DELETE ON memory_items
        BEGIN SELECT RAISE(ABORT, 'Memory items are immutable'); END
        """,
        """
        CREATE TRIGGER memory_revisions_no_update BEFORE UPDATE ON memory_revisions
        BEGIN SELECT RAISE(ABORT, 'Memory revisions are immutable'); END
        """,
        """
        CREATE TRIGGER memory_revisions_no_delete BEFORE DELETE ON memory_revisions
        BEGIN SELECT RAISE(ABORT, 'Memory revisions are immutable'); END
        """,
        """
        CREATE TRIGGER memory_revision_sources_no_update BEFORE UPDATE ON memory_revision_sources
        BEGIN SELECT RAISE(ABORT, 'Memory revision sources are immutable'); END
        """,
        """
        CREATE TRIGGER memory_revision_sources_no_delete BEFORE DELETE ON memory_revision_sources
        BEGIN SELECT RAISE(ABORT, 'Memory revision sources are immutable'); END
        """,
        """
        CREATE TRIGGER memory_state_history_no_update BEFORE UPDATE ON memory_state_history
        BEGIN SELECT RAISE(ABORT, 'Memory state history is immutable'); END
        """,
        """
        CREATE TRIGGER memory_state_history_no_delete BEFORE DELETE ON memory_state_history
        BEGIN SELECT RAISE(ABORT, 'Memory state history is immutable'); END
        """,
        """
        CREATE TRIGGER memory_episodes_no_update BEFORE UPDATE ON memory_episodes
        BEGIN SELECT RAISE(ABORT, 'Memory episodes are immutable'); END
        """,
        """
        CREATE TRIGGER memory_episodes_no_delete BEFORE DELETE ON memory_episodes
        BEGIN SELECT RAISE(ABORT, 'Memory episodes are immutable'); END
        """,
        """
        CREATE TRIGGER memory_context_uses_no_update BEFORE UPDATE ON memory_context_uses
        BEGIN SELECT RAISE(ABORT, 'Memory Context Uses are immutable'); END
        """,
        """
        CREATE TRIGGER memory_context_uses_no_delete BEFORE DELETE ON memory_context_uses
        BEGIN SELECT RAISE(ABORT, 'Memory Context Uses are immutable'); END
        """,
    ),
)


MIGRATION_030 = Migration(
    version=30,
    name="memory_curator_recall_and_compaction_checkpoint",
    statements=(
        """
        CREATE TABLE memory_maintenance_jobs (
            memory_maintenance_job_id TEXT PRIMARY KEY CHECK (
                memory_maintenance_job_id GLOB 'memoryjob_*'
                AND length(memory_maintenance_job_id) > 10
            ),
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                ON DELETE RESTRICT,
            source_start_revision INTEGER NOT NULL CHECK (source_start_revision >= 1),
            source_end_revision INTEGER NOT NULL CHECK (
                source_end_revision >= source_start_revision
            ),
            job_kind TEXT NOT NULL CHECK (job_kind IN ('extract_and_consolidate')),
            curator_revision TEXT NOT NULL CHECK (length(trim(curator_revision)) > 0),
            status TEXT NOT NULL CHECK (status IN (
                'pending', 'model_running', 'completed', 'failed', 'delivery_unknown'
            )),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            error_code TEXT,
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(conversation_id, source_end_revision, curator_revision)
        ) STRICT
        """,
        """
        CREATE TABLE memory_provider_attempts (
            memory_provider_attempt_id TEXT PRIMARY KEY CHECK (
                memory_provider_attempt_id GLOB 'memoryattempt_*'
                AND length(memory_provider_attempt_id) > 14
            ),
            memory_maintenance_job_id TEXT NOT NULL
                REFERENCES memory_maintenance_jobs(memory_maintenance_job_id)
                ON DELETE RESTRICT,
            attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
            status TEXT NOT NULL CHECK (status IN (
                'prepared', 'dispatch_started', 'completed', 'failed', 'delivery_unknown'
            )),
            provider_profile_id TEXT NOT NULL CHECK (length(trim(provider_profile_id)) > 0),
            credential_version_id TEXT NOT NULL CHECK (length(trim(credential_version_id)) > 0),
            endpoint_origin TEXT NOT NULL CHECK (length(trim(endpoint_origin)) > 0),
            model_name TEXT NOT NULL CHECK (length(trim(model_name)) > 0),
            request_json TEXT NOT NULL CHECK (
                json_valid(request_json) AND json_type(request_json) = 'object'
            ),
            request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
            response_json TEXT CHECK (
                response_json IS NULL OR (
                    json_valid(response_json) AND json_type(response_json) = 'object'
                )
            ),
            response_sha256 TEXT CHECK (
                response_sha256 IS NULL OR length(response_sha256) = 64
            ),
            input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
            output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
            error_code TEXT,
            prepared_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(memory_maintenance_job_id, attempt_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE memory_candidates (
            memory_candidate_id TEXT PRIMARY KEY CHECK (
                memory_candidate_id GLOB 'memorycandidate_*'
                AND length(memory_candidate_id) > 16
            ),
            memory_maintenance_job_id TEXT NOT NULL
                REFERENCES memory_maintenance_jobs(memory_maintenance_job_id)
                ON DELETE RESTRICT,
            memory_provider_attempt_id TEXT NOT NULL
                REFERENCES memory_provider_attempts(memory_provider_attempt_id)
                ON DELETE RESTRICT,
            source_message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE RESTRICT,
            source_message_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision),
            supporting_quote TEXT NOT NULL CHECK (length(trim(supporting_quote)) > 0),
            memory_kind TEXT NOT NULL CHECK (memory_kind IN (
                'user_preference', 'research_decision', 'research_constraint', 'feedback',
                'unresolved_question', 'reference_pointer', 'project_procedure'
            )),
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            suggested_lifecycle TEXT NOT NULL CHECK (
                suggested_lifecycle IN ('proposed', 'active')
            ),
            disposition TEXT NOT NULL CHECK (disposition IN (
                'created_active', 'created_proposed', 'deduplicated', 'rejected'
            )),
            disposition_reason TEXT NOT NULL CHECK (length(trim(disposition_reason)) > 0),
            memory_item_id TEXT REFERENCES memory_items(memory_item_id) ON DELETE RESTRICT,
            memory_revision_id TEXT REFERENCES memory_revisions(memory_revision_id)
                ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            CHECK (
                (disposition IN ('created_active', 'created_proposed')
                    AND memory_item_id IS NOT NULL AND memory_revision_id IS NOT NULL)
                OR
                (disposition IN ('deduplicated', 'rejected')
                    AND memory_item_id IS NULL AND memory_revision_id IS NULL)
            )
        ) STRICT
        """,
        """
        CREATE TABLE memory_summary_projections (
            scope_kind TEXT NOT NULL CHECK (scope_kind IN ('workspace', 'research_path')),
            scope_object_id TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            summary_sha256 TEXT NOT NULL CHECK (length(summary_sha256) = 64),
            source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
            projection_revision INTEGER NOT NULL CHECK (projection_revision >= 1),
            PRIMARY KEY(scope_kind, scope_object_id)
        ) STRICT
        """,
        """
        CREATE TABLE memory_compaction_checkpoints (
            memory_compaction_checkpoint_id TEXT PRIMARY KEY CHECK (
                memory_compaction_checkpoint_id GLOB 'memorycheckpoint_*'
                AND length(memory_compaction_checkpoint_id) > 17
            ),
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                ON DELETE RESTRICT,
            source_start_revision INTEGER NOT NULL CHECK (source_start_revision >= 1),
            source_end_revision INTEGER NOT NULL CHECK (
                source_end_revision >= source_start_revision
            ),
            disposition TEXT NOT NULL CHECK (disposition IN ('ready', 'blocked')),
            uncovered_sources_json TEXT NOT NULL CHECK (
                json_valid(uncovered_sources_json)
                AND json_type(uncovered_sources_json) = 'array'
            ),
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(conversation_id, source_end_revision, policy_revision)
        ) STRICT
        """,
        """
        CREATE INDEX memory_jobs_pending_idx
        ON memory_maintenance_jobs(status, created_revision)
        """,
        """
        CREATE INDEX memory_candidates_source_idx
        ON memory_candidates(source_message_id, source_message_revision)
        """,
        """
        CREATE TRIGGER memory_candidates_no_update BEFORE UPDATE ON memory_candidates
        BEGIN SELECT RAISE(ABORT, 'Memory candidates are immutable'); END
        """,
        """
        CREATE TRIGGER memory_candidates_no_delete BEFORE DELETE ON memory_candidates
        BEGIN SELECT RAISE(ABORT, 'Memory candidates are immutable'); END
        """,
        """
        CREATE TRIGGER memory_compaction_checkpoints_no_update
        BEFORE UPDATE ON memory_compaction_checkpoints
        BEGIN SELECT RAISE(ABORT, 'Memory compaction checkpoints are immutable'); END
        """,
        """
        CREATE TRIGGER memory_compaction_checkpoints_no_delete
        BEFORE DELETE ON memory_compaction_checkpoints
        BEGIN SELECT RAISE(ABORT, 'Memory compaction checkpoints are immutable'); END
        """,
    ),
)


MIGRATION_031 = Migration(
    version=31,
    name="controlled_memory_to_skill_evolution",
    statements=(
        """
        CREATE TABLE skill_evolution_candidates (
            skill_evolution_candidate_id TEXT PRIMARY KEY CHECK (
                skill_evolution_candidate_id GLOB 'skillcandidate_*'
                AND length(skill_evolution_candidate_id) > 15
            ),
            source_memory_maintenance_job_id TEXT NOT NULL
                REFERENCES memory_maintenance_jobs(memory_maintenance_job_id)
                ON DELETE RESTRICT,
            source_memory_provider_attempt_id TEXT NOT NULL
                REFERENCES memory_provider_attempts(memory_provider_attempt_id)
                ON DELETE RESTRICT,
            skill_name TEXT NOT NULL CHECK (
                length(skill_name) BETWEEN 1 AND 64
                AND skill_name NOT GLOB '*[^a-z0-9-]*'
                AND skill_name NOT LIKE '-%'
                AND skill_name NOT LIKE '%-'
            ),
            proposed_version TEXT NOT NULL CHECK (length(trim(proposed_version)) > 0),
            description TEXT NOT NULL CHECK (length(trim(description)) > 0),
            instruction_body TEXT NOT NULL CHECK (length(trim(instruction_body)) > 0),
            skill_markdown TEXT NOT NULL CHECK (length(trim(skill_markdown)) > 0),
            skill_sha256 TEXT NOT NULL CHECK (length(skill_sha256) = 64),
            rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            validation_status TEXT NOT NULL CHECK (
                validation_status IN ('passed', 'blocked')
            ),
            validation_findings_json TEXT NOT NULL CHECK (
                json_valid(validation_findings_json)
                AND json_type(validation_findings_json) = 'array'
            ),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(skill_name, skill_sha256)
        ) STRICT
        """,
        """
        CREATE TABLE skill_evolution_candidate_sources (
            skill_evolution_candidate_id TEXT NOT NULL
                REFERENCES skill_evolution_candidates(skill_evolution_candidate_id)
                ON DELETE RESTRICT,
            memory_item_id TEXT NOT NULL REFERENCES memory_items(memory_item_id)
                ON DELETE RESTRICT,
            memory_revision_id TEXT NOT NULL REFERENCES memory_revisions(memory_revision_id)
                ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            PRIMARY KEY(skill_evolution_candidate_id, memory_item_id)
        ) STRICT
        """,
        """
        CREATE TABLE skill_evolution_current_states (
            skill_evolution_candidate_id TEXT PRIMARY KEY
                REFERENCES skill_evolution_candidates(skill_evolution_candidate_id)
                ON DELETE RESTRICT,
            lifecycle TEXT NOT NULL CHECK (
                lifecycle IN ('proposed', 'approved', 'activated', 'rejected', 'retired')
            ),
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE skill_evolution_state_history (
            skill_evolution_state_history_id TEXT PRIMARY KEY CHECK (
                skill_evolution_state_history_id GLOB 'skillstate_*'
                AND length(skill_evolution_state_history_id) > 11
            ),
            skill_evolution_candidate_id TEXT NOT NULL
                REFERENCES skill_evolution_candidates(skill_evolution_candidate_id)
                ON DELETE RESTRICT,
            lifecycle TEXT NOT NULL CHECK (
                lifecycle IN ('proposed', 'approved', 'activated', 'rejected', 'retired')
            ),
            reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE skill_activation_manifests (
            skill_activation_manifest_id TEXT PRIMARY KEY CHECK (
                skill_activation_manifest_id GLOB 'skillactivation_*'
                AND length(skill_activation_manifest_id) > 16
            ),
            skill_evolution_candidate_id TEXT NOT NULL UNIQUE
                REFERENCES skill_evolution_candidates(skill_evolution_candidate_id)
                ON DELETE RESTRICT,
            relative_skill_path TEXT NOT NULL CHECK (
                relative_skill_path GLOB 'skills/*/SKILL.md'
            ),
            installed_sha256 TEXT NOT NULL CHECK (length(installed_sha256) = 64),
            prior_sha256 TEXT CHECK (prior_sha256 IS NULL OR length(prior_sha256) = 64),
            activation_kind TEXT NOT NULL CHECK (
                activation_kind IN ('new', 'version_update', 'idempotent_reconcile')
            ),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE INDEX skill_evolution_lifecycle_idx
        ON skill_evolution_current_states(lifecycle, updated_revision)
        """,
        """
        CREATE TRIGGER skill_evolution_candidates_no_update
        BEFORE UPDATE ON skill_evolution_candidates
        BEGIN SELECT RAISE(ABORT, 'Skill evolution candidates are immutable'); END
        """,
        """
        CREATE TRIGGER skill_evolution_candidates_no_delete
        BEFORE DELETE ON skill_evolution_candidates
        BEGIN SELECT RAISE(ABORT, 'Skill evolution candidates are immutable'); END
        """,
        """
        CREATE TRIGGER skill_evolution_candidate_sources_no_update
        BEFORE UPDATE ON skill_evolution_candidate_sources
        BEGIN SELECT RAISE(ABORT, 'Skill evolution candidate sources are immutable'); END
        """,
        """
        CREATE TRIGGER skill_evolution_candidate_sources_no_delete
        BEFORE DELETE ON skill_evolution_candidate_sources
        BEGIN SELECT RAISE(ABORT, 'Skill evolution candidate sources are immutable'); END
        """,
        """
        CREATE TRIGGER skill_evolution_state_history_no_update
        BEFORE UPDATE ON skill_evolution_state_history
        BEGIN SELECT RAISE(ABORT, 'Skill evolution state history is immutable'); END
        """,
        """
        CREATE TRIGGER skill_evolution_state_history_no_delete
        BEFORE DELETE ON skill_evolution_state_history
        BEGIN SELECT RAISE(ABORT, 'Skill evolution state history is immutable'); END
        """,
        """
        CREATE TRIGGER skill_activation_manifests_no_update
        BEFORE UPDATE ON skill_activation_manifests
        BEGIN SELECT RAISE(ABORT, 'Skill activation manifests are immutable'); END
        """,
        """
        CREATE TRIGGER skill_activation_manifests_no_delete
        BEFORE DELETE ON skill_activation_manifests
        BEGIN SELECT RAISE(ABORT, 'Skill activation manifests are immutable'); END
        """,
    ),
)


MIGRATION_032 = Migration(
    version=32,
    name="workspace_literature_rag",
    statements=(
        """
        CREATE TABLE knowledge_documents (
            knowledge_document_id TEXT PRIMARY KEY CHECK (
                knowledge_document_id GLOB 'knowledgedoc_*'
                AND length(knowledge_document_id) > 13
            ),
            relative_path TEXT NOT NULL UNIQUE CHECK (
                length(trim(relative_path)) > 0
                AND relative_path NOT LIKE '/%'
                AND relative_path NOT LIKE '%..%'
            ),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_document_revisions (
            knowledge_document_revision_id TEXT PRIMARY KEY CHECK (
                knowledge_document_revision_id GLOB 'knowledgerev_*'
                AND length(knowledge_document_revision_id) > 13
            ),
            knowledge_document_id TEXT NOT NULL
                REFERENCES knowledge_documents(knowledge_document_id) ON DELETE RESTRICT,
            revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            source_size INTEGER NOT NULL CHECK (source_size >= 0),
            media_type TEXT NOT NULL CHECK (length(trim(media_type)) > 0),
            extracted_text_sha256 TEXT NOT NULL CHECK (length(extracted_text_sha256) = 64),
            page_count INTEGER CHECK (page_count IS NULL OR page_count >= 1),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(knowledge_document_id, revision_number),
            UNIQUE(knowledge_document_id, content_sha256)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_chunks (
            knowledge_chunk_id TEXT PRIMARY KEY CHECK (
                knowledge_chunk_id GLOB 'knowledgechunk_*'
                AND length(knowledge_chunk_id) > 15
            ),
            knowledge_document_revision_id TEXT NOT NULL
                REFERENCES knowledge_document_revisions(knowledge_document_revision_id)
                ON DELETE RESTRICT,
            chunk_ordinal INTEGER NOT NULL CHECK (chunk_ordinal >= 1),
            page_start INTEGER CHECK (page_start IS NULL OR page_start >= 1),
            page_end INTEGER CHECK (page_end IS NULL OR page_end >= page_start),
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            token_estimate INTEGER NOT NULL CHECK (token_estimate >= 1),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(knowledge_document_revision_id, chunk_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_document_states (
            knowledge_document_id TEXT PRIMARY KEY
                REFERENCES knowledge_documents(knowledge_document_id) ON DELETE RESTRICT,
            current_revision_id TEXT
                REFERENCES knowledge_document_revisions(knowledge_document_revision_id)
                ON DELETE RESTRICT,
            availability TEXT NOT NULL CHECK (
                availability IN ('indexed', 'missing', 'extraction_failed')
            ),
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            CHECK (
                (availability IN ('indexed', 'missing') AND current_revision_id IS NOT NULL)
                OR (availability = 'extraction_failed')
            )
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_index_runs (
            knowledge_index_run_id TEXT PRIMARY KEY CHECK (
                knowledge_index_run_id GLOB 'knowledgeindex_*'
                AND length(knowledge_index_run_id) > 15
            ),
            scanned_count INTEGER NOT NULL CHECK (scanned_count >= 0),
            indexed_count INTEGER NOT NULL CHECK (indexed_count >= 0),
            unchanged_count INTEGER NOT NULL CHECK (unchanged_count >= 0),
            missing_count INTEGER NOT NULL CHECK (missing_count >= 0),
            errors_json TEXT NOT NULL CHECK (
                json_valid(errors_json) AND json_type(errors_json) = 'array'
            ),
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_context_uses (
            context_item_id TEXT PRIMARY KEY REFERENCES context_items(context_item_id)
                ON DELETE RESTRICT,
            knowledge_chunk_id TEXT NOT NULL REFERENCES knowledge_chunks(knowledge_chunk_id)
                ON DELETE RESTRICT,
            knowledge_document_revision_id TEXT NOT NULL
                REFERENCES knowledge_document_revisions(knowledge_document_revision_id)
                ON DELETE RESTRICT,
            retrieval_kind TEXT NOT NULL CHECK (retrieval_kind IN ('soft_prefetch')),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE INDEX knowledge_chunks_revision_idx
        ON knowledge_chunks(knowledge_document_revision_id, chunk_ordinal)
        """,
        """
        CREATE TRIGGER knowledge_documents_no_update BEFORE UPDATE ON knowledge_documents
        BEGIN SELECT RAISE(ABORT, 'Knowledge documents are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_documents_no_delete BEFORE DELETE ON knowledge_documents
        BEGIN SELECT RAISE(ABORT, 'Knowledge documents are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_document_revisions_no_update
        BEFORE UPDATE ON knowledge_document_revisions
        BEGIN SELECT RAISE(ABORT, 'Knowledge document revisions are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_document_revisions_no_delete
        BEFORE DELETE ON knowledge_document_revisions
        BEGIN SELECT RAISE(ABORT, 'Knowledge document revisions are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_chunks_no_update BEFORE UPDATE ON knowledge_chunks
        BEGIN SELECT RAISE(ABORT, 'Knowledge chunks are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_chunks_no_delete BEFORE DELETE ON knowledge_chunks
        BEGIN SELECT RAISE(ABORT, 'Knowledge chunks are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_index_runs_no_update BEFORE UPDATE ON knowledge_index_runs
        BEGIN SELECT RAISE(ABORT, 'Knowledge index runs are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_index_runs_no_delete BEFORE DELETE ON knowledge_index_runs
        BEGIN SELECT RAISE(ABORT, 'Knowledge index runs are immutable'); END
        """,
    ),
)


MIGRATION_033 = Migration(
    version=33,
    name="turn_goal_policy",
    statements=(
        """
        CREATE TABLE turn_goal_policies (
            turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id) ON DELETE RESTRICT,
            goal_mode TEXT NOT NULL CHECK (goal_mode IN ('research_loop', 'deliver_word')),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TRIGGER turn_goal_policies_no_update BEFORE UPDATE ON turn_goal_policies
        BEGIN SELECT RAISE(ABORT, 'Turn goal policy is immutable'); END
        """,
        """
        CREATE TRIGGER turn_goal_policies_no_delete BEFORE DELETE ON turn_goal_policies
        BEGIN SELECT RAISE(ABORT, 'Turn goal policy is immutable'); END
        """,
    ),
)


MIGRATION_034 = Migration(
    version=34,
    name="canonical_knowledge_runtime",
    statements=(
        """
        CREATE TABLE knowledge_sources (
            knowledge_source_id TEXT PRIMARY KEY CHECK (
                knowledge_source_id GLOB 'knowledgesource_*'
                AND length(knowledge_source_id) > 16
            ),
            source_kind TEXT NOT NULL CHECK (
                source_kind IN ('local_file', 'web_capture', 'stata_help', 'imported')
            ),
            canonical_locator TEXT NOT NULL CHECK (length(trim(canonical_locator)) > 0),
            legacy_knowledge_document_id TEXT UNIQUE
                REFERENCES knowledge_documents(knowledge_document_id) ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(source_kind, canonical_locator)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_source_memberships (
            knowledge_source_id TEXT NOT NULL
                REFERENCES knowledge_sources(knowledge_source_id) ON DELETE RESTRICT,
            corpus_role TEXT NOT NULL CHECK (
                corpus_role IN ('literature_evidence', 'stata_help', 'style_exemplar')
            ),
            membership_source TEXT NOT NULL CHECK (
                membership_source IN ('folder', 'user_selection', 'migration', 'system')
            ),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            PRIMARY KEY(knowledge_source_id, corpus_role)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_source_revisions (
            knowledge_source_revision_id TEXT PRIMARY KEY CHECK (
                knowledge_source_revision_id GLOB 'knowledgesrcrev_*'
                AND length(knowledge_source_revision_id) > 16
            ),
            knowledge_source_id TEXT NOT NULL
                REFERENCES knowledge_sources(knowledge_source_id) ON DELETE RESTRICT,
            revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            source_size INTEGER NOT NULL CHECK (source_size >= 0),
            media_type TEXT NOT NULL CHECK (length(trim(media_type)) > 0),
            source_metadata_json TEXT NOT NULL CHECK (
                json_valid(source_metadata_json) AND json_type(source_metadata_json) = 'object'
            ),
            legacy_knowledge_document_revision_id TEXT UNIQUE
                REFERENCES knowledge_document_revisions(knowledge_document_revision_id)
                ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(knowledge_source_id, revision_number),
            UNIQUE(knowledge_source_id, content_sha256)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_parse_revisions (
            knowledge_parse_revision_id TEXT PRIMARY KEY CHECK (
                knowledge_parse_revision_id GLOB 'knowledgeparse_*'
                AND length(knowledge_parse_revision_id) > 16
            ),
            knowledge_source_revision_id TEXT NOT NULL
                REFERENCES knowledge_source_revisions(knowledge_source_revision_id)
                ON DELETE RESTRICT,
            parser_name TEXT NOT NULL CHECK (length(trim(parser_name)) > 0),
            parser_version TEXT NOT NULL CHECK (length(trim(parser_version)) > 0),
            parser_profile TEXT NOT NULL CHECK (length(trim(parser_profile)) > 0),
            canonical_ir_version TEXT NOT NULL CHECK (length(trim(canonical_ir_version)) > 0),
            parse_status TEXT NOT NULL CHECK (parse_status IN ('completed', 'partial', 'failed')),
            quality_findings_json TEXT NOT NULL CHECK (
                json_valid(quality_findings_json)
                AND json_type(quality_findings_json) = 'array'
            ),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(
                knowledge_source_revision_id, parser_name, parser_version,
                parser_profile, canonical_ir_version
            )
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_nodes (
            knowledge_node_id TEXT PRIMARY KEY CHECK (
                knowledge_node_id GLOB 'knowledgenode_*'
                AND length(knowledge_node_id) > 15
            ),
            knowledge_parse_revision_id TEXT NOT NULL
                REFERENCES knowledge_parse_revisions(knowledge_parse_revision_id)
                ON DELETE RESTRICT,
            parent_node_id TEXT REFERENCES knowledge_nodes(knowledge_node_id)
                ON DELETE RESTRICT,
            local_key TEXT NOT NULL CHECK (length(trim(local_key)) > 0),
            node_kind TEXT NOT NULL CHECK (
                node_kind IN (
                    'document', 'title', 'section', 'paragraph', 'list', 'table',
                    'table_cell', 'caption', 'footnote', 'figure', 'equation',
                    'reference', 'citation', 'code', 'help_topic', 'legacy_chunk'
                )
            ),
            semantic_role TEXT,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            page_start INTEGER CHECK (page_start IS NULL OR page_start >= 1),
            page_end INTEGER CHECK (page_end IS NULL OR page_end >= page_start),
            source_span_start INTEGER CHECK (source_span_start IS NULL OR source_span_start >= 0),
            source_span_end INTEGER CHECK (
                source_span_end IS NULL OR source_span_end >= source_span_start
            ),
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            structured_payload_json TEXT CHECK (
                structured_payload_json IS NULL OR json_valid(structured_payload_json)
            ),
            anchor_fingerprint TEXT NOT NULL CHECK (length(anchor_fingerprint) = 64),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(knowledge_parse_revision_id, local_key),
            UNIQUE(knowledge_parse_revision_id, ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_edges (
            knowledge_edge_id TEXT PRIMARY KEY CHECK (
                knowledge_edge_id GLOB 'knowledgeedge_*'
                AND length(knowledge_edge_id) > 15
            ),
            from_node_id TEXT NOT NULL REFERENCES knowledge_nodes(knowledge_node_id)
                ON DELETE RESTRICT,
            to_node_id TEXT NOT NULL REFERENCES knowledge_nodes(knowledge_node_id)
                ON DELETE RESTRICT,
            edge_kind TEXT NOT NULL CHECK (
                edge_kind IN (
                    'contains', 'next', 'previous', 'cites', 'cited_by',
                    'cross_reference', 'semantic_relation'
                )
            ),
            producer_kind TEXT NOT NULL CHECK (
                producer_kind IN ('parser', 'deterministic', 'model')
            ),
            producer_revision TEXT NOT NULL CHECK (length(trim(producer_revision)) > 0),
            confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            source_locator TEXT,
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(from_node_id, to_node_id, edge_kind, producer_revision)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_source_states (
            knowledge_source_id TEXT PRIMARY KEY
                REFERENCES knowledge_sources(knowledge_source_id) ON DELETE RESTRICT,
            current_source_revision_id TEXT
                REFERENCES knowledge_source_revisions(knowledge_source_revision_id)
                ON DELETE RESTRICT,
            current_parse_revision_id TEXT
                REFERENCES knowledge_parse_revisions(knowledge_parse_revision_id)
                ON DELETE RESTRICT,
            availability TEXT NOT NULL CHECK (
                availability IN ('indexed', 'missing', 'extraction_failed')
            ),
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            CHECK (
                (availability = 'indexed' AND current_source_revision_id IS NOT NULL
                    AND current_parse_revision_id IS NOT NULL)
                OR availability IN ('missing', 'extraction_failed')
            )
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_retrieval_sessions (
            knowledge_retrieval_session_id TEXT PRIMARY KEY CHECK (
                knowledge_retrieval_session_id GLOB 'retrievalsession_*'
                AND length(knowledge_retrieval_session_id) > 18
            ),
            turn_id TEXT REFERENCES turns(turn_id) ON DELETE RESTRICT,
            objective TEXT NOT NULL CHECK (length(trim(objective)) > 0),
            retrieval_mode TEXT NOT NULL CHECK (
                retrieval_mode IN (
                    'direct', 'hierarchical', 'multi_hop',
                    'global_synthesis', 'style', 'help'
                )
            ),
            corpus_roles_json TEXT NOT NULL CHECK (
                json_valid(corpus_roles_json) AND json_type(corpus_roles_json) = 'array'
            ),
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_retrieval_session_states (
            knowledge_retrieval_session_id TEXT PRIMARY KEY
                REFERENCES knowledge_retrieval_sessions(knowledge_retrieval_session_id)
                ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK (status IN ('open', 'completed', 'partial', 'failed')),
            next_hop_ordinal INTEGER NOT NULL CHECK (next_hop_ordinal >= 1),
            stop_reason TEXT,
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            CHECK (
                (status = 'open' AND stop_reason IS NULL)
                OR (status != 'open' AND length(trim(stop_reason)) > 0)
            )
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_retrieval_session_state_history (
            knowledge_retrieval_session_id TEXT NOT NULL
                REFERENCES knowledge_retrieval_sessions(knowledge_retrieval_session_id)
                ON DELETE RESTRICT,
            state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
            status TEXT NOT NULL CHECK (status IN ('open', 'completed', 'partial', 'failed')),
            next_hop_ordinal INTEGER NOT NULL CHECK (next_hop_ordinal >= 1),
            stop_reason TEXT,
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            PRIMARY KEY(knowledge_retrieval_session_id, state_revision),
            CHECK (
                (status = 'open' AND stop_reason IS NULL)
                OR (status != 'open' AND length(trim(stop_reason)) > 0)
            )
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_retrieval_hops (
            knowledge_retrieval_hop_id TEXT PRIMARY KEY CHECK (
                knowledge_retrieval_hop_id GLOB 'retrievalhop_*'
                AND length(knowledge_retrieval_hop_id) > 14
            ),
            knowledge_retrieval_session_id TEXT NOT NULL
                REFERENCES knowledge_retrieval_sessions(knowledge_retrieval_session_id)
                ON DELETE RESTRICT,
            parent_hop_id TEXT REFERENCES knowledge_retrieval_hops(knowledge_retrieval_hop_id)
                ON DELETE RESTRICT,
            hop_ordinal INTEGER NOT NULL CHECK (hop_ordinal >= 1),
            public_subquestion TEXT NOT NULL CHECK (length(trim(public_subquestion)) > 0),
            query TEXT NOT NULL CHECK (length(trim(query)) > 0),
            strategy TEXT NOT NULL CHECK (length(trim(strategy)) > 0),
            unresolved_items_json TEXT NOT NULL CHECK (
                json_valid(unresolved_items_json)
                AND json_type(unresolved_items_json) = 'array'
            ),
            stop_reason TEXT NOT NULL CHECK (length(trim(stop_reason)) > 0),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            UNIQUE(knowledge_retrieval_session_id, hop_ordinal)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_retrieval_selections (
            knowledge_retrieval_hop_id TEXT NOT NULL
                REFERENCES knowledge_retrieval_hops(knowledge_retrieval_hop_id)
                ON DELETE RESTRICT,
            knowledge_node_id TEXT NOT NULL REFERENCES knowledge_nodes(knowledge_node_id)
                ON DELETE RESTRICT,
            selected_ordinal INTEGER NOT NULL CHECK (selected_ordinal >= 1),
            lexical_rank INTEGER CHECK (lexical_rank IS NULL OR lexical_rank >= 1),
            dense_rank INTEGER CHECK (dense_rank IS NULL OR dense_rank >= 1),
            dense_score REAL,
            fused_score REAL NOT NULL CHECK (fused_score >= 0),
            selection_reason TEXT NOT NULL CHECK (length(trim(selection_reason)) > 0),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            PRIMARY KEY(knowledge_retrieval_hop_id, knowledge_node_id),
            UNIQUE(knowledge_retrieval_hop_id, selected_ordinal),
            CHECK (lexical_rank IS NOT NULL OR dense_rank IS NOT NULL)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_node_context_uses (
            context_item_id TEXT NOT NULL REFERENCES context_items(context_item_id)
                ON DELETE RESTRICT,
            knowledge_node_id TEXT NOT NULL REFERENCES knowledge_nodes(knowledge_node_id)
                ON DELETE RESTRICT,
            knowledge_source_revision_id TEXT NOT NULL
                REFERENCES knowledge_source_revisions(knowledge_source_revision_id)
                ON DELETE RESTRICT,
            knowledge_parse_revision_id TEXT NOT NULL
                REFERENCES knowledge_parse_revisions(knowledge_parse_revision_id)
                ON DELETE RESTRICT,
            retrieval_session_id TEXT REFERENCES knowledge_retrieval_sessions(
                knowledge_retrieval_session_id
            ) ON DELETE RESTRICT,
            retrieval_hop_id TEXT REFERENCES knowledge_retrieval_hops(
                knowledge_retrieval_hop_id
            ) ON DELETE RESTRICT,
            usage_kind TEXT NOT NULL CHECK (
                usage_kind IN ('soft_prefetch', 'explicit_retrieval', 'style_exemplar', 'help')
            ),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            PRIMARY KEY(context_item_id, knowledge_node_id)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_embedding_index_revisions (
            knowledge_embedding_index_revision_id TEXT PRIMARY KEY CHECK (
                knowledge_embedding_index_revision_id GLOB 'knowledgeembedidx_*'
                AND length(knowledge_embedding_index_revision_id) > 19
            ),
            corpus_roles_json TEXT NOT NULL CHECK (
                json_valid(corpus_roles_json) AND json_type(corpus_roles_json) = 'array'
            ),
            embedding_profile_revision TEXT NOT NULL CHECK (
                length(trim(embedding_profile_revision)) > 0
            ),
            provider_kind TEXT NOT NULL CHECK (length(trim(provider_kind)) > 0),
            model_name TEXT NOT NULL CHECK (length(trim(model_name)) > 0),
            vector_dimension INTEGER NOT NULL CHECK (vector_dimension >= 1),
            normalization TEXT NOT NULL CHECK (normalization = 'l2'),
            node_selection_policy TEXT NOT NULL CHECK (
                length(trim(node_selection_policy)) > 0
            ),
            source_set_fingerprint TEXT NOT NULL CHECK (length(source_set_fingerprint) = 64),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_node_embeddings (
            knowledge_embedding_index_revision_id TEXT NOT NULL
                REFERENCES knowledge_embedding_index_revisions(
                    knowledge_embedding_index_revision_id
                ) ON DELETE CASCADE,
            knowledge_node_id TEXT NOT NULL REFERENCES knowledge_nodes(knowledge_node_id)
                ON DELETE CASCADE,
            vector_blob BLOB NOT NULL CHECK (length(vector_blob) >= 4),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision),
            PRIMARY KEY(knowledge_embedding_index_revision_id, knowledge_node_id)
        ) STRICT
        """,
        """
        CREATE VIRTUAL TABLE knowledge_nodes_fts USING fts5(
            knowledge_node_id UNINDEXED,
            content,
            tokenize = 'unicode61 remove_diacritics 2'
        )
        """,
        """
        INSERT INTO knowledge_sources(
            knowledge_source_id, source_kind, canonical_locator,
            legacy_knowledge_document_id, created_revision
        )
        SELECT
            'knowledgesource_legacy_' || knowledge_document_id,
            'local_file', relative_path, knowledge_document_id, created_revision
        FROM knowledge_documents
        """,
        """
        INSERT INTO knowledge_source_memberships(
            knowledge_source_id, corpus_role, membership_source, created_revision
        )
        SELECT
            'knowledgesource_legacy_' || knowledge_document_id,
            'literature_evidence', 'migration', created_revision
        FROM knowledge_documents
        """,
        """
        INSERT INTO knowledge_source_revisions(
            knowledge_source_revision_id, knowledge_source_id, revision_number,
            content_sha256, source_size, media_type, source_metadata_json,
            legacy_knowledge_document_revision_id, created_revision
        )
        SELECT
            'knowledgesrcrev_legacy_' || knowledge_document_revision_id,
            'knowledgesource_legacy_' || knowledge_document_id,
            revision_number, content_sha256, source_size, media_type,
            json_object('migration', 'legacy_flat_parse'),
            knowledge_document_revision_id, created_revision
        FROM knowledge_document_revisions
        """,
        """
        INSERT INTO knowledge_parse_revisions(
            knowledge_parse_revision_id, knowledge_source_revision_id,
            parser_name, parser_version, parser_profile, canonical_ir_version,
            parse_status, quality_findings_json, created_revision
        )
        SELECT
            'knowledgeparse_legacy_' || knowledge_document_revision_id,
            'knowledgesrcrev_legacy_' || knowledge_document_revision_id,
            'legacy-flat', '1', 'legacy_flat_parse', 'canonical-ir-v1',
            'completed', json_array('MIGRATED_FIXED_CHARACTER_CHUNKS'), created_revision
        FROM knowledge_document_revisions
        """,
        """
        INSERT INTO knowledge_nodes(
            knowledge_node_id, knowledge_parse_revision_id, parent_node_id,
            local_key, node_kind, semantic_role, ordinal, page_start, page_end,
            source_span_start, source_span_end, content, structured_payload_json,
            anchor_fingerprint, content_sha256, created_revision
        )
        SELECT
            'knowledgenode_legacy_' || knowledge_chunk_id,
            'knowledgeparse_legacy_' || knowledge_document_revision_id,
            NULL, 'legacy_chunk:' || chunk_ordinal, 'legacy_chunk', NULL,
            chunk_ordinal, page_start, page_end, NULL, NULL, content, NULL,
            content_sha256, content_sha256, created_revision
        FROM knowledge_chunks
        """,
        """
        INSERT INTO knowledge_source_states(
            knowledge_source_id, current_source_revision_id, current_parse_revision_id,
            availability, pointer_revision, updated_revision
        )
        SELECT
            'knowledgesource_legacy_' || state.knowledge_document_id,
            CASE WHEN state.current_revision_id IS NULL THEN NULL
                 ELSE 'knowledgesrcrev_legacy_' || state.current_revision_id END,
            CASE WHEN state.current_revision_id IS NULL THEN NULL
                 ELSE 'knowledgeparse_legacy_' || state.current_revision_id END,
            state.availability, state.pointer_revision, state.updated_revision
        FROM knowledge_document_states AS state
        """,
        """
        INSERT INTO knowledge_nodes_fts(knowledge_node_id, content)
        SELECT knowledge_node_id, content FROM knowledge_nodes
        """,
        """
        CREATE INDEX knowledge_source_memberships_role_idx
        ON knowledge_source_memberships(corpus_role, knowledge_source_id)
        """,
        """
        CREATE INDEX knowledge_nodes_parse_kind_idx
        ON knowledge_nodes(knowledge_parse_revision_id, node_kind, ordinal)
        """,
        """
        CREATE INDEX knowledge_edges_from_idx
        ON knowledge_edges(from_node_id, edge_kind)
        """,
        """
        CREATE TRIGGER knowledge_sources_no_update BEFORE UPDATE ON knowledge_sources
        BEGIN SELECT RAISE(ABORT, 'Knowledge sources are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_sources_no_delete BEFORE DELETE ON knowledge_sources
        BEGIN SELECT RAISE(ABORT, 'Knowledge sources are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_source_memberships_no_update
        BEFORE UPDATE ON knowledge_source_memberships
        BEGIN SELECT RAISE(ABORT, 'Knowledge source memberships are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_source_memberships_no_delete
        BEFORE DELETE ON knowledge_source_memberships
        BEGIN SELECT RAISE(ABORT, 'Knowledge source memberships are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_source_revisions_no_update
        BEFORE UPDATE ON knowledge_source_revisions
        BEGIN SELECT RAISE(ABORT, 'Knowledge source revisions are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_source_revisions_no_delete
        BEFORE DELETE ON knowledge_source_revisions
        BEGIN SELECT RAISE(ABORT, 'Knowledge source revisions are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_parse_revisions_no_update
        BEFORE UPDATE ON knowledge_parse_revisions
        BEGIN SELECT RAISE(ABORT, 'Knowledge parse revisions are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_parse_revisions_no_delete
        BEFORE DELETE ON knowledge_parse_revisions
        BEGIN SELECT RAISE(ABORT, 'Knowledge parse revisions are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_nodes_no_update BEFORE UPDATE ON knowledge_nodes
        BEGIN SELECT RAISE(ABORT, 'Knowledge nodes are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_nodes_no_delete BEFORE DELETE ON knowledge_nodes
        BEGIN SELECT RAISE(ABORT, 'Knowledge nodes are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_edges_no_update BEFORE UPDATE ON knowledge_edges
        BEGIN SELECT RAISE(ABORT, 'Knowledge edges are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_edges_no_delete BEFORE DELETE ON knowledge_edges
        BEGIN SELECT RAISE(ABORT, 'Knowledge edges are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_sessions_no_update
        BEFORE UPDATE ON knowledge_retrieval_sessions
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval sessions are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_sessions_no_delete
        BEFORE DELETE ON knowledge_retrieval_sessions
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval sessions are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_session_state_history_no_update
        BEFORE UPDATE ON knowledge_retrieval_session_state_history
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval state history is immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_session_state_history_no_delete
        BEFORE DELETE ON knowledge_retrieval_session_state_history
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval state history is immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_hops_no_update
        BEFORE UPDATE ON knowledge_retrieval_hops
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval hops are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_hops_no_delete
        BEFORE DELETE ON knowledge_retrieval_hops
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval hops are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_selections_no_update
        BEFORE UPDATE ON knowledge_retrieval_selections
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval selections are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_selections_no_delete
        BEFORE DELETE ON knowledge_retrieval_selections
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval selections are immutable'); END
        """,
    ),
)


MIGRATION_035 = Migration(
    version=35,
    name="context_item_trust_boundary",
    statements=(
        """
        ALTER TABLE context_items
        ADD COLUMN trust_class TEXT NOT NULL DEFAULT 'authoritative_record'
        CHECK (
            trust_class IN (
                'user_instruction', 'authoritative_record', 'recalled_context',
                'retrieved_untrusted', 'tool_output_untrusted'
            )
        )
        """,
    ),
)


MIGRATION_036 = Migration(
    version=36,
    name="turn_runtime_budget_ledger",
    statements=(
        """
        CREATE TABLE turn_runtime_budgets (
            turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id) ON DELETE RESTRICT,
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            max_wall_clock_seconds REAL NOT NULL CHECK (max_wall_clock_seconds > 0),
            consumed_wall_clock_seconds REAL NOT NULL CHECK (
                consumed_wall_clock_seconds >= 0
                AND consumed_wall_clock_seconds <= max_wall_clock_seconds
            ),
            segment_count INTEGER NOT NULL CHECK (segment_count >= 0),
            updated_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
    ),
)


MIGRATION_037 = Migration(
    version=37,
    name="dynamic_table_render_shape",
    statements=(
        """
        CREATE TABLE table_render_shape_manifests (
            table_render_receipt_id TEXT PRIMARY KEY
                REFERENCES table_render_receipts(table_render_receipt_id)
                ON DELETE RESTRICT,
            controlled_cell_count INTEGER NOT NULL CHECK (controlled_cell_count >= 1),
            structural_exemption_count INTEGER NOT NULL CHECK (
                structural_exemption_count >= 0
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        INSERT INTO table_render_shape_manifests(
            table_render_receipt_id, controlled_cell_count,
            structural_exemption_count, created_revision
        )
        SELECT render.table_render_receipt_id, COUNT(cell.table_cell_evidence_use_id),
               4, render.created_revision
        FROM table_render_receipts AS render
        JOIN table_cell_evidence_uses AS cell
          ON cell.table_render_receipt_id = render.table_render_receipt_id
        GROUP BY render.table_render_receipt_id
        """,
        """
        CREATE TRIGGER table_render_shape_manifests_no_update
        BEFORE UPDATE ON table_render_shape_manifests
        BEGIN SELECT RAISE(ABORT, 'Table render shape manifests are immutable'); END
        """,
        """
        CREATE TRIGGER table_render_shape_manifests_no_delete
        BEFORE DELETE ON table_render_shape_manifests
        BEGIN SELECT RAISE(ABORT, 'Table render shape manifests are immutable'); END
        """,
    ),
)


MIGRATION_038 = Migration(
    version=38,
    name="formal_post_estimation_execution_role",
    statements=(
        "DROP TRIGGER stata_operation_input_bindings_no_update",
        "DROP TRIGGER stata_operation_input_bindings_no_delete",
        "ALTER TABLE stata_operation_input_bindings RENAME TO old_stata_operation_input_bindings",
        """
        CREATE TABLE stata_operation_input_bindings (
            operation_attempt_id TEXT PRIMARY KEY
                REFERENCES operation_attempts(operation_attempt_id) ON DELETE RESTRICT,
            executable_source_id TEXT NOT NULL UNIQUE
                REFERENCES executable_sources(executable_source_id) ON DELETE RESTRICT,
            execution_purpose TEXT NOT NULL CHECK (
                execution_purpose IN (
                    'general', 'data_load', 'data_step', 'formal_estimation',
                    'formal_post_estimation'
                )
            ),
            input_data_version_id TEXT
                REFERENCES data_versions(data_version_id) ON DELETE RESTRICT,
            input_data_slot_key TEXT,
            input_verification_receipt_id TEXT
                REFERENCES artifact_verification_receipts(verification_receipt_id)
                ON DELETE RESTRICT,
            source_data_state_operation_id TEXT
                REFERENCES operations(operation_id) ON DELETE RESTRICT,
            expected_data_state_token TEXT,
            expected_session_generation INTEGER CHECK (
                expected_session_generation IS NULL OR expected_session_generation >= 1
            ),
            bound_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            CHECK (
                (input_data_version_id IS NULL AND input_data_slot_key IS NULL
                    AND input_verification_receipt_id IS NULL
                    AND execution_purpose = 'general')
                OR
                (input_data_version_id IS NOT NULL AND input_data_slot_key IS NOT NULL
                    AND input_verification_receipt_id IS NOT NULL
                    AND execution_purpose IN (
                        'data_load', 'data_step', 'formal_estimation',
                        'formal_post_estimation'
                    ))
            ),
            CHECK (
                (execution_purpose IN ('general', 'data_load')
                    AND source_data_state_operation_id IS NULL
                    AND expected_data_state_token IS NULL
                    AND expected_session_generation IS NULL)
                OR
                (execution_purpose IN (
                        'data_step', 'formal_estimation', 'formal_post_estimation'
                    )
                    AND source_data_state_operation_id IS NOT NULL
                    AND expected_data_state_token IS NOT NULL
                    AND expected_session_generation IS NOT NULL)
            )
        ) STRICT
        """,
        """
        INSERT INTO stata_operation_input_bindings
        SELECT * FROM old_stata_operation_input_bindings
        """,
        "DROP TABLE old_stata_operation_input_bindings",
        """
        CREATE TRIGGER stata_operation_input_bindings_no_update
        BEFORE UPDATE ON stata_operation_input_bindings
        BEGIN SELECT RAISE(ABORT, 'Stata operation input bindings are immutable'); END
        """,
        """
        CREATE TRIGGER stata_operation_input_bindings_no_delete
        BEFORE DELETE ON stata_operation_input_bindings
        BEGIN SELECT RAISE(ABORT, 'Stata operation input bindings are immutable'); END
        """,
    ),
)


MIGRATION_039 = Migration(
    version=39,
    name="logical_result_source_locator_type",
    statements=(
        "ALTER TABLE result_source_locators RENAME COLUMN locator_type TO storage_locator_family",
        """
        ALTER TABLE result_source_locators ADD COLUMN locator_type TEXT
        GENERATED ALWAYS AS (
            lower(json_extract(locator_json, '$.locator_type'))
        ) VIRTUAL
        CHECK (locator_type IN (
            'e_scalar', 'r_scalar', 'e_macro', 'e_matrix_cell', 'r_matrix_cell',
            'trusted_stata_derivation_receipt'
        ))
        """,
    ),
)


MIGRATION_040 = Migration(
    version=40,
    name="operational_turn_outcome_feedback",
    statements=(
        """
        CREATE TABLE turn_outcome_feedback (
            turn_outcome_feedback_id TEXT PRIMARY KEY CHECK (
                turn_outcome_feedback_id GLOB 'turnfeedback_*'
                AND length(turn_outcome_feedback_id) > 13
            ),
            turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE RESTRICT,
            disposition TEXT NOT NULL CHECK (
                disposition IN ('accepted', 'needs_revision', 'rejected')
            ),
            ratings_json TEXT NOT NULL CHECK (
                json_valid(ratings_json) AND json_type(ratings_json) = 'array'
            ),
            issue_codes_json TEXT NOT NULL CHECK (
                json_valid(issue_codes_json) AND json_type(issue_codes_json) = 'array'
            ),
            comment TEXT NOT NULL CHECK (length(comment) <= 4000),
            source TEXT NOT NULL CHECK (source = 'explicit_user'),
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE INDEX turn_outcome_feedback_turn_revision
        ON turn_outcome_feedback(turn_id, created_revision DESC)
        """,
        """
        CREATE TRIGGER turn_outcome_feedback_no_update
        BEFORE UPDATE ON turn_outcome_feedback
        BEGIN SELECT RAISE(ABORT, 'Turn outcome feedback is immutable'); END
        """,
        """
        CREATE TRIGGER turn_outcome_feedback_no_delete
        BEFORE DELETE ON turn_outcome_feedback
        BEGIN SELECT RAISE(ABORT, 'Turn outcome feedback is immutable'); END
        """,
    ),
)


MIGRATION_041 = Migration(
    version=41,
    name="auditable_query_planning_and_reranking",
    statements=(
        """
        CREATE TABLE knowledge_retrieval_query_variants (
            knowledge_retrieval_hop_id TEXT NOT NULL
                REFERENCES knowledge_retrieval_hops(knowledge_retrieval_hop_id)
                ON DELETE RESTRICT,
            variant_ordinal INTEGER NOT NULL CHECK (variant_ordinal >= 1),
            variant_kind TEXT NOT NULL CHECK (
                variant_kind IN ('original', 'objective', 'keyword', 'subquestion')
            ),
            query TEXT NOT NULL CHECK (length(trim(query)) > 0),
            reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
            planner_policy_revision TEXT NOT NULL CHECK (
                length(trim(planner_policy_revision)) > 0
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(knowledge_retrieval_hop_id, variant_ordinal),
            UNIQUE(knowledge_retrieval_hop_id, query)
        ) STRICT
        """,
        """
        CREATE TABLE knowledge_retrieval_candidates (
            knowledge_retrieval_hop_id TEXT NOT NULL
                REFERENCES knowledge_retrieval_hops(knowledge_retrieval_hop_id)
                ON DELETE RESTRICT,
            knowledge_node_id TEXT NOT NULL
                REFERENCES knowledge_nodes(knowledge_node_id) ON DELETE RESTRICT,
            candidate_ordinal INTEGER NOT NULL CHECK (candidate_ordinal >= 1),
            query_variant_ordinals_json TEXT NOT NULL CHECK (
                json_valid(query_variant_ordinals_json)
                AND json_type(query_variant_ordinals_json) = 'array'
            ),
            lexical_rank INTEGER CHECK (lexical_rank IS NULL OR lexical_rank >= 1),
            dense_rank INTEGER CHECK (dense_rank IS NULL OR dense_rank >= 1),
            dense_score REAL,
            fused_score REAL NOT NULL CHECK (fused_score >= 0),
            rerank_score REAL NOT NULL CHECK (rerank_score >= 0 AND rerank_score <= 1),
            relevance_label TEXT NOT NULL CHECK (
                relevance_label IN (
                    'direct_support', 'partial_support', 'background', 'low_relevance'
                )
            ),
            rerank_reason_codes_json TEXT NOT NULL CHECK (
                json_valid(rerank_reason_codes_json)
                AND json_type(rerank_reason_codes_json) = 'array'
            ),
            reranker_policy_revision TEXT NOT NULL CHECK (
                length(trim(reranker_policy_revision)) > 0
            ),
            selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            PRIMARY KEY(knowledge_retrieval_hop_id, knowledge_node_id),
            UNIQUE(knowledge_retrieval_hop_id, candidate_ordinal),
            CHECK (lexical_rank IS NOT NULL OR dense_rank IS NOT NULL)
        ) STRICT
        """,
        """
        ALTER TABLE knowledge_retrieval_selections
        ADD COLUMN rerank_score REAL CHECK (
            rerank_score IS NULL OR (rerank_score >= 0 AND rerank_score <= 1)
        )
        """,
        """
        ALTER TABLE knowledge_retrieval_selections
        ADD COLUMN relevance_label TEXT CHECK (
            relevance_label IS NULL OR relevance_label IN (
                'direct_support', 'partial_support', 'background', 'low_relevance'
            )
        )
        """,
        """
        CREATE INDEX knowledge_retrieval_candidates_hop_rank
        ON knowledge_retrieval_candidates(
            knowledge_retrieval_hop_id, rerank_score DESC, candidate_ordinal
        )
        """,
        """
        CREATE TRIGGER knowledge_retrieval_query_variants_no_update
        BEFORE UPDATE ON knowledge_retrieval_query_variants
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval query variants are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_query_variants_no_delete
        BEFORE DELETE ON knowledge_retrieval_query_variants
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval query variants are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_candidates_no_update
        BEFORE UPDATE ON knowledge_retrieval_candidates
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval candidates are immutable'); END
        """,
        """
        CREATE TRIGGER knowledge_retrieval_candidates_no_delete
        BEFORE DELETE ON knowledge_retrieval_candidates
        BEGIN SELECT RAISE(ABORT, 'Knowledge retrieval candidates are immutable'); END
        """,
    ),
)


MIGRATION_042 = Migration(
    version=42,
    name="filesystem_memory_and_retention",
    statements=(
        """
        CREATE TABLE memory_retention_states (
            memory_item_id TEXT PRIMARY KEY REFERENCES memory_items(memory_item_id)
                ON DELETE RESTRICT,
            access_tier TEXT NOT NULL CHECK (
                access_tier IN ('hot', 'warm', 'cold', 'archived')
            ),
            pinned INTEGER NOT NULL CHECK (pinned IN (0, 1)),
            retention_revision INTEGER NOT NULL CHECK (retention_revision >= 1),
            last_reinforced_revision INTEGER REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT,
            superseded_by_memory_item_id TEXT REFERENCES memory_items(memory_item_id)
                ON DELETE RESTRICT,
            superseded_revision INTEGER REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT,
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT,
            CHECK (
                (superseded_by_memory_item_id IS NULL AND superseded_revision IS NULL)
                OR
                (superseded_by_memory_item_id IS NOT NULL AND superseded_revision IS NOT NULL)
            ),
            CHECK (superseded_by_memory_item_id IS NULL OR access_tier = 'archived')
        ) STRICT
        """,
        """
        CREATE TABLE memory_retention_history (
            memory_retention_history_id TEXT PRIMARY KEY CHECK (
                memory_retention_history_id GLOB 'memoryretention_*'
                AND length(memory_retention_history_id) > 16
            ),
            memory_item_id TEXT NOT NULL REFERENCES memory_items(memory_item_id)
                ON DELETE RESTRICT,
            access_tier TEXT NOT NULL CHECK (
                access_tier IN ('hot', 'warm', 'cold', 'archived')
            ),
            pinned INTEGER NOT NULL CHECK (pinned IN (0, 1)),
            retention_revision INTEGER NOT NULL CHECK (retention_revision >= 1),
            superseded_by_memory_item_id TEXT REFERENCES memory_items(memory_item_id)
                ON DELETE RESTRICT,
            reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE memory_payload_files (
            memory_revision_id TEXT PRIMARY KEY
                REFERENCES memory_revisions(memory_revision_id) ON DELETE RESTRICT,
            relative_path TEXT NOT NULL UNIQUE CHECK (
                length(trim(relative_path)) > 0
                AND relative_path NOT LIKE '/%'
                AND relative_path NOT LIKE '%..%'
            ),
            payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 1),
            availability TEXT NOT NULL CHECK (
                availability IN ('available', 'missing', 'corrupt')
            ),
            observed_at TEXT NOT NULL,
            source_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        INSERT INTO memory_retention_states(
            memory_item_id, access_tier, pinned, retention_revision,
            last_reinforced_revision, superseded_by_memory_item_id,
            superseded_revision, policy_revision, updated_revision
        )
        SELECT item.memory_item_id,
               CASE state.lifecycle
                 WHEN 'active' THEN 'hot'
                 WHEN 'proposed' THEN 'warm'
                 ELSE 'archived'
               END,
               CASE WHEN item.memory_kind IN (
                    'research_decision', 'research_constraint', 'unresolved_question'
               ) THEN 1 ELSE 0 END,
               1,
               CASE WHEN state.lifecycle = 'active' THEN state.updated_revision ELSE NULL END,
               NULL, NULL, 'memory-retention/v1', state.updated_revision
        FROM memory_items AS item
        JOIN memory_current_states AS state USING (memory_item_id)
        """,
        """
        INSERT INTO memory_retention_history(
            memory_retention_history_id, memory_item_id, access_tier, pinned,
            retention_revision, superseded_by_memory_item_id, reason_code,
            policy_revision, created_revision
        )
        SELECT 'memoryretention_migration_' || retention.memory_item_id,
               retention.memory_item_id, retention.access_tier, retention.pinned,
               retention.retention_revision, NULL, 'migration_backfill',
               retention.policy_revision, retention.updated_revision
        FROM memory_retention_states AS retention
        """,
        """
        CREATE INDEX memory_retention_recall_idx
        ON memory_retention_states(
            access_tier, pinned, superseded_by_memory_item_id, updated_revision
        )
        """,
        """
        CREATE TRIGGER memory_retention_history_no_update
        BEFORE UPDATE ON memory_retention_history
        BEGIN SELECT RAISE(ABORT, 'Memory retention history is immutable'); END
        """,
        """
        CREATE TRIGGER memory_retention_history_no_delete
        BEFORE DELETE ON memory_retention_history
        BEGIN SELECT RAISE(ABORT, 'Memory retention history is immutable'); END
        """,
    ),
)


MIGRATION_043 = Migration(
    version=43,
    name="versioned_skill_adoption_and_outcome_observation",
    statements=(
        """
        CREATE TABLE skill_versions (
            skill_version_id TEXT PRIMARY KEY CHECK (
                skill_version_id GLOB 'skillversion_*'
                AND length(skill_version_id) > 13
            ),
            skill_name TEXT NOT NULL CHECK (
                length(skill_name) BETWEEN 1 AND 64
                AND skill_name NOT GLOB '*[^a-z0-9-]*'
            ),
            version_label TEXT NOT NULL CHECK (length(trim(version_label)) > 0),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            skill_markdown TEXT NOT NULL CHECK (length(trim(skill_markdown)) > 0),
            source_candidate_id TEXT UNIQUE REFERENCES
                skill_evolution_candidates(skill_evolution_candidate_id) ON DELETE RESTRICT,
            predecessor_skill_version_id TEXT REFERENCES skill_versions(skill_version_id)
                ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT,
            UNIQUE(skill_name, content_sha256)
        ) STRICT
        """,
        """
        CREATE TABLE skill_adoptions (
            skill_name TEXT PRIMARY KEY,
            current_skill_version_id TEXT REFERENCES skill_versions(skill_version_id)
                ON DELETE RESTRICT,
            lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'deactivated')),
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT,
            CHECK (
                (lifecycle = 'active' AND current_skill_version_id IS NOT NULL)
                OR (lifecycle = 'deactivated' AND current_skill_version_id IS NULL)
            )
        ) STRICT
        """,
        """
        CREATE TABLE skill_adoption_history (
            skill_adoption_history_id TEXT PRIMARY KEY CHECK (
                skill_adoption_history_id GLOB 'skilladoption_*'
                AND length(skill_adoption_history_id) > 14
            ),
            skill_name TEXT NOT NULL,
            target_skill_version_id TEXT REFERENCES skill_versions(skill_version_id)
                ON DELETE RESTRICT,
            action_kind TEXT NOT NULL CHECK (
                action_kind IN ('activate', 'rollback', 'deactivate')
            ),
            reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE skill_publication_manifests (
            skill_publication_manifest_id TEXT PRIMARY KEY CHECK (
                skill_publication_manifest_id GLOB 'skillpublication_*'
                AND length(skill_publication_manifest_id) > 17
            ),
            skill_name TEXT NOT NULL,
            target_skill_version_id TEXT REFERENCES skill_versions(skill_version_id)
                ON DELETE RESTRICT,
            publication_kind TEXT NOT NULL CHECK (
                publication_kind IN ('activate', 'rollback', 'deactivate')
            ),
            relative_skill_path TEXT NOT NULL CHECK (
                relative_skill_path GLOB 'skills/*/SKILL.md'
            ),
            installed_sha256 TEXT CHECK (
                installed_sha256 IS NULL OR length(installed_sha256) = 64
            ),
            prior_sha256 TEXT CHECK (prior_sha256 IS NULL OR length(prior_sha256) = 64),
            publication_result TEXT NOT NULL CHECK (
                publication_result IN (
                    'new', 'version_update', 'idempotent_reconcile',
                    'deactivated', 'idempotent_deactivation'
                )
            ),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE skill_context_uses (
            context_item_id TEXT PRIMARY KEY REFERENCES context_items(context_item_id)
                ON DELETE RESTRICT,
            skill_name TEXT NOT NULL,
            skill_revision TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            source_kind TEXT NOT NULL,
            skill_version_id TEXT REFERENCES skill_versions(skill_version_id)
                ON DELETE RESTRICT,
            usage_kind TEXT NOT NULL CHECK (usage_kind = 'exact'),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE skill_outcome_observations (
            turn_outcome_feedback_id TEXT NOT NULL REFERENCES
                turn_outcome_feedback(turn_outcome_feedback_id) ON DELETE RESTRICT,
            context_item_id TEXT NOT NULL REFERENCES skill_context_uses(context_item_id)
                ON DELETE RESTRICT,
            relationship_kind TEXT NOT NULL CHECK (
                relationship_kind = 'co_occurrence_not_causation'
            ),
            created_revision INTEGER NOT NULL REFERENCES workspace_commits(workspace_revision)
                ON DELETE RESTRICT,
            PRIMARY KEY(turn_outcome_feedback_id, context_item_id)
        ) STRICT
        """,
        """
        INSERT INTO skill_versions(
            skill_version_id, skill_name, version_label, content_sha256,
            skill_markdown, source_candidate_id, predecessor_skill_version_id,
            created_revision
        )
        SELECT 'skillversion_migration_' || candidate.skill_evolution_candidate_id,
               candidate.skill_name, candidate.proposed_version, candidate.skill_sha256,
               candidate.skill_markdown, candidate.skill_evolution_candidate_id, NULL,
               manifest.created_revision
        FROM skill_evolution_candidates AS candidate
        JOIN skill_activation_manifests AS manifest USING (skill_evolution_candidate_id)
        """,
        """
        INSERT INTO skill_adoptions(
            skill_name, current_skill_version_id, lifecycle, pointer_revision,
            updated_revision
        )
        SELECT version.skill_name, version.skill_version_id, 'active', 1,
               version.created_revision
        FROM skill_versions AS version
        WHERE NOT EXISTS (
            SELECT 1 FROM skill_versions AS newer
            WHERE newer.skill_name = version.skill_name
              AND (
                  newer.created_revision > version.created_revision
                  OR (
                      newer.created_revision = version.created_revision
                      AND newer.skill_version_id > version.skill_version_id
                  )
              )
        )
        """,
        """
        INSERT INTO skill_adoption_history(
            skill_adoption_history_id, skill_name, target_skill_version_id,
            action_kind, reason, pointer_revision, created_revision
        )
        SELECT 'skilladoption_migration_' || adoption.current_skill_version_id,
               adoption.skill_name, adoption.current_skill_version_id,
               'activate', 'migration_backfill', adoption.pointer_revision,
               adoption.updated_revision
        FROM skill_adoptions AS adoption
        """,
        """
        CREATE INDEX skill_versions_name_idx
        ON skill_versions(skill_name, created_revision)
        """,
        """
        CREATE INDEX skill_context_version_idx
        ON skill_context_uses(skill_version_id, created_revision)
        """,
        """
        CREATE INDEX skill_outcome_feedback_idx
        ON skill_outcome_observations(turn_outcome_feedback_id, created_revision)
        """,
        """
        CREATE TRIGGER skill_versions_no_update BEFORE UPDATE ON skill_versions
        BEGIN SELECT RAISE(ABORT, 'Skill versions are immutable'); END
        """,
        """
        CREATE TRIGGER skill_versions_no_delete BEFORE DELETE ON skill_versions
        BEGIN SELECT RAISE(ABORT, 'Skill versions are immutable'); END
        """,
        """
        CREATE TRIGGER skill_adoption_history_no_update BEFORE UPDATE ON skill_adoption_history
        BEGIN SELECT RAISE(ABORT, 'Skill adoption history is immutable'); END
        """,
        """
        CREATE TRIGGER skill_adoption_history_no_delete BEFORE DELETE ON skill_adoption_history
        BEGIN SELECT RAISE(ABORT, 'Skill adoption history is immutable'); END
        """,
        """
        CREATE TRIGGER skill_publication_manifests_no_update
        BEFORE UPDATE ON skill_publication_manifests
        BEGIN SELECT RAISE(ABORT, 'Skill publication manifests are immutable'); END
        """,
        """
        CREATE TRIGGER skill_publication_manifests_no_delete
        BEFORE DELETE ON skill_publication_manifests
        BEGIN SELECT RAISE(ABORT, 'Skill publication manifests are immutable'); END
        """,
        """
        CREATE TRIGGER skill_context_uses_no_update BEFORE UPDATE ON skill_context_uses
        BEGIN SELECT RAISE(ABORT, 'Skill Context Uses are immutable'); END
        """,
        """
        CREATE TRIGGER skill_context_uses_no_delete BEFORE DELETE ON skill_context_uses
        BEGIN SELECT RAISE(ABORT, 'Skill Context Uses are immutable'); END
        """,
        """
        CREATE TRIGGER skill_outcome_observations_no_update
        BEFORE UPDATE ON skill_outcome_observations
        BEGIN SELECT RAISE(ABORT, 'Skill outcome observations are immutable'); END
        """,
        """
        CREATE TRIGGER skill_outcome_observations_no_delete
        BEFORE DELETE ON skill_outcome_observations
        BEGIN SELECT RAISE(ABORT, 'Skill outcome observations are immutable'); END
        """,
    ),
)


MIGRATION_044 = Migration(
    version=44,
    name="independent_skill_evaluation",
    statements=(
        """
        CREATE TABLE skill_evaluation_runs (
            skill_evaluation_run_id TEXT PRIMARY KEY CHECK (
                skill_evaluation_run_id GLOB 'skilleval_*'
                AND length(skill_evaluation_run_id) > 10
            ),
            skill_name TEXT NOT NULL,
            candidate_skill_version_id TEXT NOT NULL
                REFERENCES skill_versions(skill_version_id) ON DELETE RESTRICT,
            baseline_skill_version_id TEXT
                REFERENCES skill_versions(skill_version_id) ON DELETE RESTRICT,
            evaluation_kind TEXT NOT NULL CHECK (
                evaluation_kind IN (
                    'observational_single_version',
                    'observational_version_comparison',
                    'paired_replay'
                )
            ),
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            source_start_revision INTEGER NOT NULL CHECK (source_start_revision >= 1),
            source_end_revision INTEGER NOT NULL CHECK (
                source_end_revision >= source_start_revision
            ),
            evidence_json TEXT NOT NULL CHECK (
                json_valid(evidence_json) AND json_type(evidence_json) = 'object'
            ),
            status TEXT NOT NULL CHECK (
                status IN ('evaluating', 'completed', 'failed', 'delivery_unknown')
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            updated_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            CHECK (
                (evaluation_kind = 'observational_single_version'
                 AND baseline_skill_version_id IS NULL)
                OR (evaluation_kind != 'observational_single_version'
                    AND baseline_skill_version_id IS NOT NULL)
            ),
            CHECK (
                baseline_skill_version_id IS NULL
                OR baseline_skill_version_id != candidate_skill_version_id
            )
        ) STRICT
        """,
        """
        CREATE TABLE skill_evaluation_attempts (
            skill_evaluation_attempt_id TEXT PRIMARY KEY CHECK (
                skill_evaluation_attempt_id GLOB 'skillevalattempt_*'
                AND length(skill_evaluation_attempt_id) > 17
            ),
            skill_evaluation_run_id TEXT NOT NULL UNIQUE
                REFERENCES skill_evaluation_runs(skill_evaluation_run_id) ON DELETE RESTRICT,
            provider_profile_id TEXT NOT NULL,
            credential_version_id TEXT NOT NULL,
            model_name TEXT NOT NULL CHECK (length(trim(model_name)) > 0),
            request_json TEXT NOT NULL CHECK (json_valid(request_json)),
            response_json TEXT CHECK (response_json IS NULL OR json_valid(response_json)),
            usage_kind TEXT CHECK (
                usage_kind IS NULL OR usage_kind IN ('exact', 'estimated', 'unknown')
            ),
            input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
            output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
            cached_input_tokens INTEGER CHECK (
                cached_input_tokens IS NULL OR cached_input_tokens >= 0
            ),
            uncached_input_tokens INTEGER CHECK (
                uncached_input_tokens IS NULL OR uncached_input_tokens >= 0
            ),
            finish_reason TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('dispatched', 'completed', 'failed', 'delivery_unknown')
            ),
            error_code TEXT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            updated_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE skill_evaluation_reports (
            skill_evaluation_run_id TEXT PRIMARY KEY
                REFERENCES skill_evaluation_runs(skill_evaluation_run_id) ON DELETE RESTRICT,
            evaluator_kind TEXT NOT NULL CHECK (evaluator_kind = 'independent_model'),
            evaluator_revision TEXT NOT NULL CHECK (length(trim(evaluator_revision)) > 0),
            verdict TEXT NOT NULL CHECK (
                verdict IN (
                    'insufficient_evidence', 'candidate_preferred',
                    'baseline_preferred', 'mixed', 'no_material_difference'
                )
            ),
            rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
            limitations_json TEXT NOT NULL CHECK (
                json_valid(limitations_json)
                AND json_type(limitations_json) = 'array'
                AND json_array_length(limitations_json) > 0
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE skill_improvement_proposals (
            skill_improvement_proposal_id TEXT PRIMARY KEY CHECK (
                skill_improvement_proposal_id GLOB 'skillproposal_*'
                AND length(skill_improvement_proposal_id) > 14
            ),
            skill_evaluation_run_id TEXT NOT NULL
                REFERENCES skill_evaluation_reports(skill_evaluation_run_id) ON DELETE RESTRICT,
            proposal_kind TEXT NOT NULL CHECK (
                proposal_kind IN ('revise', 'merge', 'retire', 'keep_observing')
            ),
            target_skill_version_id TEXT NOT NULL
                REFERENCES skill_versions(skill_version_id) ON DELETE RESTRICT,
            merge_target_skill_name TEXT,
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
            suggested_instruction_body TEXT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            CHECK (
                (proposal_kind = 'merge' AND length(trim(merge_target_skill_name)) > 0)
                OR (proposal_kind != 'merge' AND merge_target_skill_name IS NULL)
            ),
            CHECK (
                (proposal_kind IN ('revise', 'merge')
                 AND length(trim(suggested_instruction_body)) > 0)
                OR proposal_kind NOT IN ('revise', 'merge')
            )
        ) STRICT
        """,
        """
        CREATE INDEX skill_evaluation_skill_revision_idx
        ON skill_evaluation_runs(skill_name, updated_revision DESC)
        """,
        """
        CREATE TRIGGER skill_evaluation_reports_no_update
        BEFORE UPDATE ON skill_evaluation_reports
        BEGIN SELECT RAISE(ABORT, 'Skill evaluation reports are immutable'); END
        """,
        """
        CREATE TRIGGER skill_evaluation_reports_no_delete
        BEFORE DELETE ON skill_evaluation_reports
        BEGIN SELECT RAISE(ABORT, 'Skill evaluation reports are immutable'); END
        """,
        """
        CREATE TRIGGER skill_improvement_proposals_no_update
        BEFORE UPDATE ON skill_improvement_proposals
        BEGIN SELECT RAISE(ABORT, 'Skill improvement proposals are immutable'); END
        """,
        """
        CREATE TRIGGER skill_improvement_proposals_no_delete
        BEFORE DELETE ON skill_improvement_proposals
        BEGIN SELECT RAISE(ABORT, 'Skill improvement proposals are immutable'); END
        """,
    ),
)


MIGRATION_045 = Migration(
    version=45,
    name="human_gated_skill_change_candidates",
    statements=(
        """
        CREATE TABLE skill_change_candidates (
            skill_change_candidate_id TEXT PRIMARY KEY CHECK (
                skill_change_candidate_id GLOB 'skillchange_*'
                AND length(skill_change_candidate_id) > 12
            ),
            source_proposal_id TEXT NOT NULL UNIQUE
                REFERENCES skill_improvement_proposals(skill_improvement_proposal_id)
                ON DELETE RESTRICT,
            change_kind TEXT NOT NULL CHECK (change_kind IN ('revise', 'merge')),
            skill_name TEXT NOT NULL CHECK (
                length(skill_name) BETWEEN 1 AND 64
                AND skill_name NOT GLOB '*[^a-z0-9-]*'
            ),
            base_skill_version_id TEXT NOT NULL
                REFERENCES skill_versions(skill_version_id) ON DELETE RESTRICT,
            merge_source_skill_version_id TEXT
                REFERENCES skill_versions(skill_version_id) ON DELETE RESTRICT,
            proposed_version TEXT NOT NULL CHECK (length(trim(proposed_version)) > 0),
            description TEXT NOT NULL CHECK (length(trim(description)) > 0),
            instruction_body TEXT NOT NULL CHECK (length(trim(instruction_body)) > 0),
            skill_markdown TEXT NOT NULL CHECK (length(trim(skill_markdown)) > 0),
            skill_sha256 TEXT NOT NULL CHECK (length(skill_sha256) = 64),
            rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
            policy_revision TEXT NOT NULL CHECK (length(trim(policy_revision)) > 0),
            validation_status TEXT NOT NULL CHECK (
                validation_status IN ('passed', 'blocked')
            ),
            validation_findings_json TEXT NOT NULL CHECK (
                json_valid(validation_findings_json)
                AND json_type(validation_findings_json) = 'array'
            ),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT,
            CHECK (
                (change_kind = 'merge' AND merge_source_skill_version_id IS NOT NULL)
                OR (change_kind = 'revise' AND merge_source_skill_version_id IS NULL)
            ),
            CHECK (
                merge_source_skill_version_id IS NULL
                OR merge_source_skill_version_id != base_skill_version_id
            ),
            UNIQUE(skill_name, skill_sha256)
        ) STRICT
        """,
        """
        CREATE TABLE skill_change_current_states (
            skill_change_candidate_id TEXT PRIMARY KEY
                REFERENCES skill_change_candidates(skill_change_candidate_id)
                ON DELETE RESTRICT,
            lifecycle TEXT NOT NULL CHECK (
                lifecycle IN ('proposed', 'activated', 'rejected')
            ),
            pointer_revision INTEGER NOT NULL CHECK (pointer_revision >= 1),
            updated_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE skill_change_state_history (
            skill_change_state_history_id TEXT PRIMARY KEY CHECK (
                skill_change_state_history_id GLOB 'skillchangestate_*'
                AND length(skill_change_state_history_id) > 17
            ),
            skill_change_candidate_id TEXT NOT NULL
                REFERENCES skill_change_candidates(skill_change_candidate_id)
                ON DELETE RESTRICT,
            lifecycle TEXT NOT NULL CHECK (
                lifecycle IN ('proposed', 'activated', 'rejected')
            ),
            reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE skill_version_change_sources (
            skill_version_id TEXT PRIMARY KEY
                REFERENCES skill_versions(skill_version_id) ON DELETE RESTRICT,
            skill_change_candidate_id TEXT NOT NULL UNIQUE
                REFERENCES skill_change_candidates(skill_change_candidate_id)
                ON DELETE RESTRICT,
            created_revision INTEGER NOT NULL
                REFERENCES workspace_commits(workspace_revision) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE INDEX skill_change_lifecycle_idx
        ON skill_change_current_states(lifecycle, updated_revision)
        """,
        """
        CREATE TRIGGER skill_change_candidates_no_update
        BEFORE UPDATE ON skill_change_candidates
        BEGIN SELECT RAISE(ABORT, 'Skill change candidates are immutable'); END
        """,
        """
        CREATE TRIGGER skill_change_candidates_no_delete
        BEFORE DELETE ON skill_change_candidates
        BEGIN SELECT RAISE(ABORT, 'Skill change candidates are immutable'); END
        """,
        """
        CREATE TRIGGER skill_change_state_history_no_update
        BEFORE UPDATE ON skill_change_state_history
        BEGIN SELECT RAISE(ABORT, 'Skill change state history is immutable'); END
        """,
        """
        CREATE TRIGGER skill_change_state_history_no_delete
        BEFORE DELETE ON skill_change_state_history
        BEGIN SELECT RAISE(ABORT, 'Skill change state history is immutable'); END
        """,
        """
        CREATE TRIGGER skill_version_change_sources_no_update
        BEFORE UPDATE ON skill_version_change_sources
        BEGIN SELECT RAISE(ABORT, 'Skill version change sources are immutable'); END
        """,
        """
        CREATE TRIGGER skill_version_change_sources_no_delete
        BEFORE DELETE ON skill_version_change_sources
        BEGIN SELECT RAISE(ABORT, 'Skill version change sources are immutable'); END
        """,
    ),
)


MIGRATIONS = (
    MIGRATION_001,
    MIGRATION_002,
    MIGRATION_003,
    MIGRATION_004,
    MIGRATION_005,
    MIGRATION_006,
    MIGRATION_007,
    MIGRATION_008,
    MIGRATION_009,
    MIGRATION_010,
    MIGRATION_011,
    MIGRATION_012,
    MIGRATION_013,
    MIGRATION_014,
    MIGRATION_015,
    MIGRATION_016,
    MIGRATION_017,
    MIGRATION_018,
    MIGRATION_019,
    MIGRATION_020,
    MIGRATION_021,
    MIGRATION_022,
    MIGRATION_023,
    MIGRATION_024,
    MIGRATION_025,
    MIGRATION_026,
    MIGRATION_027,
    MIGRATION_028,
    MIGRATION_029,
    MIGRATION_030,
    MIGRATION_031,
    MIGRATION_032,
    MIGRATION_033,
    MIGRATION_034,
    MIGRATION_035,
    MIGRATION_036,
    MIGRATION_037,
    MIGRATION_038,
    MIGRATION_039,
    MIGRATION_040,
    MIGRATION_041,
    MIGRATION_042,
    MIGRATION_043,
    MIGRATION_044,
    MIGRATION_045,
)


class MigrationRunner:
    def __init__(
        self,
        migrations: Iterable[Migration] = MIGRATIONS,
        *,
        now_epoch: Callable[[], int] | None = None,
        lease_seconds: int = 60,
    ) -> None:
        self._migrations = tuple(migrations)
        self._now_epoch = now_epoch or (lambda: int(time.time()))
        self._lease_seconds = lease_seconds
        versions = [migration.version for migration in self._migrations]
        if versions != list(range(1, len(versions) + 1)):
            raise ValueError("migration registry must be contiguous and one-based")

    @property
    def current_version(self) -> int:
        return len(self._migrations)

    def migrate(self, connection: sqlite3.Connection, workspace_id: WorkspaceId) -> None:
        current = self._inspect_compatibility(connection)
        if current >= 1:
            self._verify_applied_checksums(connection, current)
        if current == self.current_version:
            self._verify_workspace_identity(connection, workspace_id)
            return

        if current == 0:
            self._apply_migration(connection, self._migrations[0], workspace_id)
            current = 1

        lease_token = self._acquire_lease(connection)
        try:
            for migration in self._migrations[current:]:
                self._apply_migration(connection, migration, workspace_id, lease_token=lease_token)
        finally:
            self._release_lease(connection, lease_token)

        self._verify_applied_checksums(connection, self.current_version)
        self._verify_workspace_identity(connection, workspace_id)

    def validate(self, connection: sqlite3.Connection, workspace_id: WorkspaceId) -> None:
        current = self._inspect_compatibility(connection)
        if current != self.current_version:
            raise SchemaCompatibilityError(
                f"schema version {current} is not ready; expected {self.current_version}"
            )
        self._verify_applied_checksums(connection, current)
        self._verify_workspace_identity(connection, workspace_id)

    def migrate_in_uow(
        self,
        connection: sqlite3.Connection,
        workspace_id: WorkspaceId,
        before_commit: Callable[[sqlite3.Connection, int, int], None],
    ) -> None:
        """Apply every pending reviewed step and a caller adoption hook atomically."""
        current = self._inspect_compatibility(connection)
        if current < 1 or current >= self.current_version:
            raise SchemaCompatibilityError(
                f"staged migration requires 1 <= source < {self.current_version}; got {current}"
            )
        self._verify_applied_checksums(connection, current)
        self._verify_workspace_identity(connection, workspace_id)
        connection.execute("BEGIN EXCLUSIVE")
        try:
            for migration in self._migrations[current:]:
                applied_at = datetime.now(UTC).isoformat()
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (migration.version, migration.name, migration.checksum, applied_at),
                )
                connection.execute(
                    "UPDATE schema_meta SET current_version = ? WHERE singleton_id = 1",
                    (migration.version,),
                )
                connection.execute(f"PRAGMA user_version = {migration.version}")
            before_commit(connection, current, self.current_version)
            self._verify_applied_checksums(connection, self.current_version)
            self._verify_workspace_identity(connection, workspace_id)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _inspect_compatibility(self, connection: sqlite3.Connection) -> int:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        table_count = int(
            connection.execute(
                "SELECT count(*) FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
        if user_version > self.current_version:
            raise SchemaCompatibilityError(
                f"database schema {user_version} is newer than supported {self.current_version}"
            )
        if table_count and application_id != APPLICATION_ID:
            raise SchemaCompatibilityError(
                f"unexpected SQLite application_id {application_id}; expected {APPLICATION_ID}"
            )
        if user_version == 0:
            if table_count:
                raise SchemaCompatibilityError(
                    "non-empty database has no recognized schema version"
                )
            return 0

        row = connection.execute(
            "SELECT current_version FROM schema_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None or int(row[0]) != user_version:
            raise SchemaCompatibilityError("schema_meta and PRAGMA user_version disagree")
        return user_version

    def _verify_applied_checksums(
        self, connection: sqlite3.Connection, current_version: int
    ) -> None:
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        if len(rows) != current_version:
            raise MigrationChecksumError(
                f"expected {current_version} migration records, found {len(rows)}"
            )
        for row, expected in zip(rows, self._migrations[:current_version], strict=True):
            actual = (int(row["version"]), str(row["name"]), str(row["checksum"]))
            wanted = (expected.version, expected.name, expected.checksum)
            if actual != wanted:
                raise MigrationChecksumError(
                    f"migration {expected.version} does not match reviewed registry"
                )

    def _apply_migration(
        self,
        connection: sqlite3.Connection,
        migration: Migration,
        workspace_id: WorkspaceId,
        *,
        lease_token: str | None = None,
    ) -> None:
        applied_at = datetime.now(UTC).isoformat()
        connection.execute("BEGIN EXCLUSIVE")
        try:
            for statement in migration.statements:
                connection.execute(statement)
            if migration.initializes_workspace_identity:
                connection.execute(
                    """
                    INSERT INTO workspace_identity(singleton_id, workspace_id, created_at)
                    VALUES (1, ?, ?)
                    """,
                    (workspace_id.value, applied_at),
                )
            if migration.version == 1:
                connection.execute(
                    "UPDATE schema_meta SET created_at = ? WHERE singleton_id = 1", (applied_at,)
                )
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (migration.version, migration.name, migration.checksum, applied_at),
            )
            connection.execute(
                "UPDATE schema_meta SET current_version = ? WHERE singleton_id = 1",
                (migration.version,),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
            if lease_token is not None:
                refreshed_expiry = self._now_epoch() + self._lease_seconds
                cursor = connection.execute(
                    """
                    UPDATE migration_lease
                    SET expires_at_epoch = ?
                    WHERE singleton_id = 1 AND owner_token = ?
                    """,
                    (refreshed_expiry, lease_token),
                )
                if cursor.rowcount != 1:
                    raise MigrationLeaseHeldError(
                        "migration lease ownership changed during migration"
                    )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _acquire_lease(self, connection: sqlite3.Connection) -> str:
        owner_token = secrets.token_hex(16)
        now = self._now_epoch()
        expires = now + self._lease_seconds
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """
                UPDATE migration_lease
                SET owner_token = ?, acquired_at_epoch = ?, expires_at_epoch = ?,
                    lease_revision = lease_revision + 1
                WHERE singleton_id = 1
                  AND (owner_token IS NULL OR expires_at_epoch < ?)
                """,
                (owner_token, now, expires, now),
            )
            if cursor.rowcount != 1:
                raise MigrationLeaseHeldError("another process holds the migration lease")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return owner_token

    def _release_lease(self, connection: sqlite3.Connection, owner_token: str) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """
                UPDATE migration_lease
                SET owner_token = NULL, acquired_at_epoch = NULL, expires_at_epoch = NULL,
                    lease_revision = lease_revision + 1
                WHERE singleton_id = 1 AND owner_token = ?
                """,
                (owner_token,),
            )
            if cursor.rowcount != 1:
                raise MigrationLeaseHeldError("migration lease ownership changed unexpectedly")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _verify_workspace_identity(
        connection: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> None:
        row = connection.execute(
            "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
        ).fetchone()
        if row is None or str(row[0]) != workspace_id.value:
            actual = None if row is None else str(row[0])
            raise WorkspaceIdentityError(
                f"Workspace identity mismatch: expected={workspace_id.value!r}, actual={actual!r}"
            )
