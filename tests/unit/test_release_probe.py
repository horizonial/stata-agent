"""Activation control-database probe is immutable and compatibility-aware."""

import sqlite3
from pathlib import Path

from stata_research_agent.application.release_activation import VerifiedRelease
from stata_research_agent.persistence.release_probe import ReadOnlyControlDatabaseProbe


def release(root: Path, minimum: int = 1, maximum: int = 1) -> VerifiedRelease:
    return VerifiedRelease(
        "release-probe",
        "0.1.0",
        "build-probe",
        "publisher-key-v1",
        "a" * 64,
        str(root),
        "agent.exe",
        minimum,
        maximum,
        1,
        26,
        26,
        26,
        "1.0.0",
    )


def test_control_database_probe_is_read_only_and_checks_schema(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker VALUES ('unchanged')")
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    probe = ReadOnlyControlDatabaseProbe(path)
    assert probe(release(tmp_path)) == (True, "control_database_read_only_ok")
    assert path.read_bytes() == before
    assert probe(release(tmp_path, minimum=2, maximum=2)) == (
        False,
        "control_database_schema_incompatible",
    )
    assert path.read_bytes() == before
