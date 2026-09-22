"""Global SQLite control ledger for versioned Provider credential references."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from stata_research_agent.application.model_configuration import (
    WorkspaceModelConfiguration,
)
from stata_research_agent.application.provider_credentials import (
    CredentialLifecycleError,
    CredentialUnavailableError,
    ProviderCredentialAllocation,
    ProviderCredentialReceipt,
    ProviderProfileSummary,
)


class GlobalCredentialDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_profiles (
                provider_profile_id TEXT PRIMARY KEY,
                provider_kind TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                account_label TEXT,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'staging', 'enabled', 'disabled', 'credential_unavailable', 'deleted'
                    )
                ),
                active_credential_version_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS provider_credential_versions (
                credential_version_id TEXT PRIMARY KEY,
                provider_profile_id TEXT NOT NULL REFERENCES provider_profiles(provider_profile_id),
                credential_ref TEXT NOT NULL UNIQUE,
                target_name TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'allocated', 'adopted', 'retired', 'orphan_cleaned',
                        'delete_pending', 'deleted'
                    )
                ),
                created_at TEXT NOT NULL,
                adopted_at TEXT,
                retired_at TEXT
            ) STRICT;

            CREATE TRIGGER IF NOT EXISTS provider_active_credential_fk_insert
            BEFORE INSERT ON provider_profiles
            WHEN NEW.active_credential_version_id IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'active credential cannot be set during profile insert');
            END;

            CREATE TABLE IF NOT EXISTS credential_lifecycle_journal (
                credential_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_profile_id TEXT NOT NULL,
                credential_version_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS workspace_model_configurations (
                workspace_id TEXT PRIMARY KEY CHECK (workspace_id GLOB 'ws_*'),
                provider_profile_id TEXT NOT NULL
                    REFERENCES provider_profiles(provider_profile_id) ON DELETE RESTRICT,
                model_name TEXT NOT NULL CHECK (length(model_name) > 0),
                reasoning_effort TEXT NOT NULL CHECK (
                    reasoning_effort IN ('none', 'low', 'medium', 'high')
                ),
                configuration_revision INTEGER NOT NULL CHECK (configuration_revision >= 1),
                updated_at TEXT NOT NULL
            ) STRICT;
            """
        )
        configuration_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(workspace_model_configurations)"
            ).fetchall()
        }
        if "permission_mode" not in configuration_columns:
            connection.execute(
                "ALTER TABLE workspace_model_configurations "
                "ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'workspace_only' "
                "CHECK (permission_mode IN ('workspace_only', 'full_access'))"
            )
        if "context_window_tokens" not in configuration_columns:
            connection.execute(
                "ALTER TABLE workspace_model_configurations "
                "ADD COLUMN context_window_tokens INTEGER NOT NULL DEFAULT 128000 "
                "CHECK (context_window_tokens > 0)"
            )
        if "max_output_tokens" not in configuration_columns:
            connection.execute(
                "ALTER TABLE workspace_model_configurations "
                "ADD COLUMN max_output_tokens INTEGER NOT NULL DEFAULT 8192 "
                "CHECK (max_output_tokens > 0)"
            )
        if "reserved_runtime_tokens" not in configuration_columns:
            connection.execute(
                "ALTER TABLE workspace_model_configurations "
                "ADD COLUMN reserved_runtime_tokens INTEGER NOT NULL DEFAULT 4096 "
                "CHECK (reserved_runtime_tokens >= 0)"
            )
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise CredentialLifecycleError("global credential database foreign keys disabled")
        return connection


class SqliteProviderCredentialRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def list_profiles(self) -> tuple[ProviderProfileSummary, ...]:
        rows = self._connection.execute(
            """
            SELECT provider_profile_id, provider_kind, endpoint, account_label,
                   status, active_credential_version_id
            FROM provider_profiles
            WHERE status != 'deleted'
            ORDER BY provider_kind, COALESCE(account_label, ''), provider_profile_id
            """
        ).fetchall()
        return tuple(
            ProviderProfileSummary(
                str(row["provider_profile_id"]),
                str(row["provider_kind"]),
                str(row["endpoint"]),
                None if row["account_label"] is None else str(row["account_label"]),
                str(row["status"]),
                (
                    None
                    if row["active_credential_version_id"] is None
                    else str(row["active_credential_version_id"])
                ),
            )
            for row in rows
        )

    def allocate_profile(
        self,
        *,
        provider_profile_id: str,
        provider_kind: str,
        endpoint: str,
        account_label: str | None,
        credential_version_id: str,
        credential_ref: str,
        target_name: str,
    ) -> ProviderCredentialAllocation:
        if not provider_kind.strip() or not endpoint.startswith("https://"):
            raise CredentialLifecycleError("provider profile is invalid")
        now = self._now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO provider_profiles(
                    provider_profile_id, provider_kind, endpoint, account_label,
                    status, active_credential_version_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'staging', NULL, ?, ?)
                """,
                (provider_profile_id, provider_kind, endpoint, account_label, now, now),
            )
            self._insert_version(
                provider_profile_id,
                credential_version_id,
                credential_ref,
                target_name,
                now,
            )
        return ProviderCredentialAllocation(
            provider_profile_id,
            provider_kind,
            endpoint,
            credential_version_id,
            credential_ref,
            target_name,
        )

    def allocate_rotation(
        self,
        *,
        provider_profile_id: str,
        credential_version_id: str,
        credential_ref: str,
    ) -> ProviderCredentialAllocation:
        row = self._connection.execute(
            """
            SELECT provider_kind, endpoint, status FROM provider_profiles
            WHERE provider_profile_id = ?
            """,
            (provider_profile_id,),
        ).fetchone()
        if row is None or str(row["status"]) == "disabled":
            raise CredentialLifecycleError("provider profile is unavailable")
        target_name = self._target_name(
            str(row["provider_kind"]), provider_profile_id, credential_version_id
        )
        now = self._now()
        with self._connection:
            self._insert_version(
                provider_profile_id,
                credential_version_id,
                credential_ref,
                target_name,
                now,
            )
        return ProviderCredentialAllocation(
            provider_profile_id,
            str(row["provider_kind"]),
            str(row["endpoint"]),
            credential_version_id,
            credential_ref,
            target_name,
        )

    def adopt(self, credential_version_id: str) -> ProviderCredentialReceipt:
        now = self._now()
        with self._connection:
            row = self._connection.execute(
                """
                SELECT version.provider_profile_id, version.state,
                       profile.active_credential_version_id
                FROM provider_credential_versions AS version
                JOIN provider_profiles AS profile USING (provider_profile_id)
                WHERE version.credential_version_id = ?
                """,
                (credential_version_id,),
            ).fetchone()
            if row is None or str(row["state"]) != "allocated":
                raise CredentialLifecycleError("credential version is not adoptable")
            profile_id = str(row["provider_profile_id"])
            previous = row["active_credential_version_id"]
            if previous is not None:
                self._connection.execute(
                    """
                    UPDATE provider_credential_versions
                    SET state = 'retired', retired_at = ?
                    WHERE credential_version_id = ? AND state = 'adopted'
                    """,
                    (now, str(previous)),
                )
            self._connection.execute(
                """
                UPDATE provider_credential_versions
                SET state = 'adopted', adopted_at = ?
                WHERE credential_version_id = ?
                """,
                (now, credential_version_id),
            )
            self._connection.execute(
                """
                UPDATE provider_profiles
                SET active_credential_version_id = ?, status = 'enabled', updated_at = ?
                WHERE provider_profile_id = ?
                """,
                (credential_version_id, now, profile_id),
            )
            self._journal(profile_id, credential_version_id, "credential.adopted", now)
        return ProviderCredentialReceipt(profile_id, credential_version_id, "adopted")

    def resolve_active(self, credential_ref: str) -> ProviderCredentialAllocation:
        row = self._connection.execute(
            """
            SELECT profile.provider_profile_id, profile.provider_kind, profile.endpoint,
                   profile.status, profile.active_credential_version_id,
                   version.credential_version_id, version.credential_ref,
                   version.target_name, version.state
            FROM provider_credential_versions AS version
            JOIN provider_profiles AS profile USING (provider_profile_id)
            WHERE version.credential_ref = ?
            """,
            (credential_ref,),
        ).fetchone()
        if (
            row is None
            or str(row["status"]) != "enabled"
            or str(row["state"]) != "adopted"
            or str(row["active_credential_version_id"]) != str(row["credential_version_id"])
        ):
            raise CredentialUnavailableError("credential reference is not active")
        return self._allocation(row)

    def select_workspace_model(
        self,
        *,
        workspace_id: str,
        provider_profile_id: str,
        model_name: str,
        reasoning_effort: str,
        permission_mode: str,
        context_window_tokens: int,
        max_output_tokens: int,
        reserved_runtime_tokens: int,
    ) -> WorkspaceModelConfiguration:
        now = self._now()
        with self._connection:
            profile = self._connection.execute(
                """
                SELECT status FROM provider_profiles WHERE provider_profile_id = ?
                """,
                (provider_profile_id,),
            ).fetchone()
            if profile is None or str(profile["status"]) != "enabled":
                raise CredentialUnavailableError("Provider profile is not enabled")
            existing = self._connection.execute(
                """
                SELECT configuration_revision FROM workspace_model_configurations
                WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchone()
            revision = 1 if existing is None else int(existing[0]) + 1
            self._connection.execute(
                """
                INSERT INTO workspace_model_configurations(
                    workspace_id, provider_profile_id, model_name, reasoning_effort,
                    configuration_revision, updated_at, permission_mode,
                    context_window_tokens, max_output_tokens, reserved_runtime_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    provider_profile_id = excluded.provider_profile_id,
                    model_name = excluded.model_name,
                    reasoning_effort = excluded.reasoning_effort,
                    permission_mode = excluded.permission_mode,
                    context_window_tokens = excluded.context_window_tokens,
                    max_output_tokens = excluded.max_output_tokens,
                    reserved_runtime_tokens = excluded.reserved_runtime_tokens,
                    configuration_revision = excluded.configuration_revision,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    provider_profile_id,
                    model_name,
                    reasoning_effort,
                    revision,
                    now,
                    permission_mode,
                    context_window_tokens,
                    max_output_tokens,
                    reserved_runtime_tokens,
                ),
            )
        return self.resolve_workspace_model(workspace_id)

    def resolve_workspace_model(self, workspace_id: str) -> WorkspaceModelConfiguration:
        row = self._connection.execute(
            """
            SELECT configuration.workspace_id, configuration.provider_profile_id,
                   profile.provider_kind, profile.endpoint, version.credential_ref,
                   configuration.model_name, configuration.reasoning_effort,
                   configuration.permission_mode,
                   configuration.configuration_revision,
                   configuration.context_window_tokens,
                   configuration.max_output_tokens,
                   configuration.reserved_runtime_tokens
            FROM workspace_model_configurations AS configuration
            JOIN provider_profiles AS profile USING (provider_profile_id)
            JOIN provider_credential_versions AS version
              ON version.credential_version_id = profile.active_credential_version_id
            WHERE configuration.workspace_id = ?
              AND profile.status = 'enabled' AND version.state = 'adopted'
            """,
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise CredentialUnavailableError("Workspace model configuration is unavailable")
        return WorkspaceModelConfiguration(
            str(row["workspace_id"]),
            str(row["provider_profile_id"]),
            str(row["provider_kind"]),
            str(row["endpoint"]),
            str(row["credential_ref"]),
            str(row["model_name"]),
            str(row["reasoning_effort"]),
            str(row["permission_mode"]),
            int(row["configuration_revision"]),
            int(row["context_window_tokens"]),
            int(row["max_output_tokens"]),
            int(row["reserved_runtime_tokens"]),
        )

    def mark_unavailable(self, provider_profile_id: str) -> None:
        now = self._now()
        with self._connection:
            self._connection.execute(
                """
                UPDATE provider_profiles
                SET status = 'credential_unavailable', updated_at = ?
                WHERE provider_profile_id = ? AND status != 'disabled'
                """,
                (now, provider_profile_id),
            )

    def recovery_candidates(
        self,
    ) -> tuple[tuple[ProviderCredentialAllocation, str], ...]:
        rows = self._connection.execute(
            """
            SELECT profile.provider_profile_id, profile.provider_kind, profile.endpoint,
                   version.credential_version_id, version.credential_ref,
                   version.target_name, version.state
            FROM provider_credential_versions AS version
            JOIN provider_profiles AS profile USING (provider_profile_id)
            WHERE version.state IN ('allocated', 'adopted', 'delete_pending')
            ORDER BY version.created_at, version.credential_version_id
            """
        ).fetchall()
        return tuple((self._allocation(row), str(row["state"])) for row in rows)

    def mark_orphan_cleaned(self, credential_version_id: str) -> None:
        now = self._now()
        with self._connection:
            row = self._connection.execute(
                """
                SELECT provider_profile_id FROM provider_credential_versions
                WHERE credential_version_id = ? AND state = 'allocated'
                """,
                (credential_version_id,),
            ).fetchone()
            if row is None:
                return
            profile_id = str(row["provider_profile_id"])
            self._connection.execute(
                """
                UPDATE provider_credential_versions SET state = 'orphan_cleaned'
                WHERE credential_version_id = ?
                """,
                (credential_version_id,),
            )
            active = self._connection.execute(
                """
                SELECT active_credential_version_id FROM provider_profiles
                WHERE provider_profile_id = ?
                """,
                (profile_id,),
            ).fetchone()[0]
            if active is None:
                self._connection.execute(
                    """
                    UPDATE provider_profiles SET status = 'credential_unavailable', updated_at = ?
                    WHERE provider_profile_id = ?
                    """,
                    (now, profile_id),
                )
            self._journal(profile_id, credential_version_id, "credential.orphan_cleaned", now)

    def stage_profile_delete(
        self, provider_profile_id: str
    ) -> tuple[ProviderCredentialAllocation, ...]:
        now = self._now()
        with self._connection:
            profile = self._connection.execute(
                """
                SELECT provider_kind, endpoint, status FROM provider_profiles
                WHERE provider_profile_id = ?
                """,
                (provider_profile_id,),
            ).fetchone()
            if profile is None:
                raise CredentialLifecycleError("provider profile does not exist")
            rows = self._connection.execute(
                """
                SELECT ? AS provider_profile_id, ? AS provider_kind, ? AS endpoint,
                       credential_version_id, credential_ref, target_name
                FROM provider_credential_versions
                WHERE provider_profile_id = ?
                  AND state NOT IN ('orphan_cleaned', 'deleted')
                ORDER BY created_at, credential_version_id
                """,
                (
                    provider_profile_id,
                    str(profile["provider_kind"]),
                    str(profile["endpoint"]),
                    provider_profile_id,
                ),
            ).fetchall()
            self._connection.execute(
                """
                UPDATE provider_profiles
                SET status = 'disabled', active_credential_version_id = NULL, updated_at = ?
                WHERE provider_profile_id = ?
                """,
                (now, provider_profile_id),
            )
            self._connection.execute(
                """
                UPDATE provider_credential_versions SET state = 'delete_pending'
                WHERE provider_profile_id = ?
                  AND state NOT IN ('orphan_cleaned', 'deleted')
                """,
                (provider_profile_id,),
            )
            for row in rows:
                self._journal(
                    provider_profile_id,
                    str(row["credential_version_id"]),
                    "credential.delete_staged",
                    now,
                )
        return tuple(self._allocation(row) for row in rows)

    def mark_deleted(self, credential_version_id: str) -> None:
        now = self._now()
        with self._connection:
            row = self._connection.execute(
                """
                SELECT provider_profile_id FROM provider_credential_versions
                WHERE credential_version_id = ? AND state = 'delete_pending'
                """,
                (credential_version_id,),
            ).fetchone()
            if row is None:
                return
            profile_id = str(row["provider_profile_id"])
            self._connection.execute(
                """
                UPDATE provider_credential_versions SET state = 'deleted'
                WHERE credential_version_id = ?
                """,
                (credential_version_id,),
            )
            remaining = int(
                self._connection.execute(
                    """
                    SELECT count(*) FROM provider_credential_versions
                    WHERE provider_profile_id = ? AND state = 'delete_pending'
                    """,
                    (profile_id,),
                ).fetchone()[0]
            )
            if remaining == 0:
                self._connection.execute(
                    """
                    UPDATE provider_profiles SET status = 'deleted', updated_at = ?
                    WHERE provider_profile_id = ?
                    """,
                    (now, profile_id),
                )
            self._journal(profile_id, credential_version_id, "credential.deleted", now)

    def _insert_version(
        self,
        provider_profile_id: str,
        credential_version_id: str,
        credential_ref: str,
        target_name: str,
        now: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO provider_credential_versions(
                credential_version_id, provider_profile_id, credential_ref,
                target_name, state, created_at
            ) VALUES (?, ?, ?, ?, 'allocated', ?)
            """,
            (
                credential_version_id,
                provider_profile_id,
                credential_ref,
                target_name,
                now,
            ),
        )
        self._journal(provider_profile_id, credential_version_id, "credential.allocated", now)

    def _journal(self, profile_id: str, version_id: str, event_type: str, occurred_at: str) -> None:
        self._connection.execute(
            """
            INSERT INTO credential_lifecycle_journal(
                provider_profile_id, credential_version_id, event_type, occurred_at
            ) VALUES (?, ?, ?, ?)
            """,
            (profile_id, version_id, event_type, occurred_at),
        )

    @staticmethod
    def _allocation(row: sqlite3.Row) -> ProviderCredentialAllocation:
        return ProviderCredentialAllocation(
            str(row["provider_profile_id"]),
            str(row["provider_kind"]),
            str(row["endpoint"]),
            str(row["credential_version_id"]),
            str(row["credential_ref"]),
            str(row["target_name"]),
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _target_name(
        provider_kind: str, provider_profile_id: str, credential_version_id: str
    ) -> str:
        normalized_kind = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in provider_kind.lower()
        )
        return (
            "StataResearchAgent/provider/"
            f"{normalized_kind}/{provider_profile_id}/{credential_version_id}"
        )
