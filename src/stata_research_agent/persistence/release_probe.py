"""Read-only global control database check for candidate activation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from stata_research_agent.application.release_activation import VerifiedRelease


class ReadOnlyControlDatabaseProbe:
    def __init__(self, database_path: Path) -> None:
        self._path = database_path.resolve()

    def __call__(self, release: VerifiedRelease) -> tuple[bool, str]:
        if not self._path.is_file():
            return True, "control_database_not_created"
        before = self._fingerprint()
        uri = f"file:{quote(self._path.as_posix())}?mode=ro&immutable=1"
        try:
            connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=1)
            try:
                connection.execute("PRAGMA query_only = ON")
                integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            finally:
                connection.close()
        except sqlite3.Error:
            return False, "control_database_read_failed"
        if self._fingerprint() != before:
            return False, "control_database_changed_during_probe"
        if integrity != "ok":
            return False, "control_database_integrity_failed"
        if not release.global_control_schema_min <= schema <= release.global_control_schema_max:
            return False, "control_database_schema_incompatible"
        return True, "control_database_read_only_ok"

    def _fingerprint(self) -> tuple[int, int]:
        status = self._path.stat()
        return status.st_size, status.st_mtime_ns
