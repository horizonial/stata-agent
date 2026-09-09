"""Focused tests for the versioned SQLite memory persistence boundary."""

from __future__ import annotations

import json
import sqlite3

import pytest

from stata_agent.memory.sqlite_repository import (
    STATUS_ACCEPTED,
    STATUS_ACTIVE,
    SQLiteMemoryRepository,
)
from stata_agent.storage.migrations import Migration, MigrationError, MigrationRunner


def test_migrations_are_idempotent_and_failure_rolls_back(tmp_path):
    database = tmp_path / "memory.sqlite3"
    connection = sqlite3.connect(database)
    runner = MigrationRunner(connection)

    assert runner.run() == 1
    assert runner.run() == 1
    assert [item["version"] for item in runner.applied()] == [1]
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('schema_migrations', 'memory_records', 'memory_candidates', 'workspace_registry')"
        )
    }
    assert tables == {"schema_migrations", "memory_records", "memory_candidates", "workspace_registry"}
    connection.close()

    failed_connection = sqlite3.connect(tmp_path / "failed.sqlite3")

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_be_rolled_back (id INTEGER)")
        raise RuntimeError("synthetic migration failure")

    failed_runner = MigrationRunner(
        failed_connection,
        (
            Migration(1, "first", lambda conn: conn.execute("CREATE TABLE first (id INTEGER)")),
            Migration(2, "fails", fail_after_ddl),
        ),
    )
    with pytest.raises(RuntimeError, match="synthetic"):
        failed_runner.run()
    assert failed_runner.current_version == 0
    assert failed_connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('schema_migrations', 'first', 'should_be_rolled_back')"
    ).fetchall() == []
    failed_connection.close()


def test_migrations_reject_downgrade_and_schema_history_drift(tmp_path):
    connection = sqlite3.connect(tmp_path / "drift.sqlite3")
    runner = MigrationRunner(connection)
    assert runner.run() == 1

    with pytest.raises(MigrationError, match="older than current"):
        runner.run(target_version=0)

    connection.execute(
        "UPDATE schema_migrations SET name='rewritten-history' WHERE version=1"
    )
    connection.commit()
    with pytest.raises(MigrationError, match="name mismatch"):
        runner.run()
    connection.close()

    with pytest.raises(MigrationError, match="contiguous"):
        MigrationRunner(
            sqlite3.connect(":memory:"),
            (Migration(2, "starts-late", lambda conn: None),),
        )


def test_repository_configures_sqlite_and_covers_schema(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3", workspace_id="project-a", busy_timeout_ms=3210)
    assert repository.schema_version == 1
    assert repository.applied_migrations()[0]["name"] == "memory_storage_v1"
    assert repository.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert repository.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 3210
    assert repository.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    columns = {
        row[1]
        for row in repository.connection.execute("PRAGMA table_info(memory_records)")
    }
    assert {
        "workspace_id",
        "scope",
        "kind",
        "text",
        "status",
        "confidence",
        "source_ids",
        "fingerprint",
        "created_at",
        "updated_at",
        "last_used_at",
        "use_count",
        "supersedes",
        "expires_at",
        "sensitive",
        "quarantined",
        "schema_version",
    } <= columns
    repository.close()


def test_records_are_idempotent_and_isolated_by_workspace(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3", workspace_id="project-a")
    first = repository.add("  使用中文输出  ", source_ids="event-a", now=10)
    duplicate = repository.add("使用中文输出", source_ids="event-retry", now=20)
    other = repository.add("使用英文输出", workspace_id="project-b", now=11)
    global_record = repository.add("保留工作区规则", scope="global", now=12)

    assert duplicate["id"] == first["id"]
    assert repository.get_record(other["id"]) is None
    assert {record["id"] for record in repository.list_records()} == {first["id"]}
    assert {record["id"] for record in repository.list_records(workspace_id="project-b")} == {other["id"]}
    assert {
        record["id"] for record in repository.list_records(include_global=True)
    } == {first["id"], global_record["id"]}
    assert repository.find_by_fingerprint(
        first["fingerprint"], workspace_id="project-a"
    )["id"] == first["id"]

    updated = repository.update_record(first["id"], text="使用中文和可复现输出", now=30)
    assert updated is not None
    assert updated["text"] == "使用中文和可复现输出"
    assert repository.touch(first["id"], now=31) is True
    assert repository.get_record(first["id"])["use_count"] == 1
    assert repository.delete_record(first["id"]) is True
    assert repository.get_record(first["id"]) is None
    repository.close()


def test_candidate_decisions_are_atomic_and_workspace_scoped(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3", workspace_id="project-a")
    candidate = repository.add_candidate("以后默认使用稳健标准误", source_ids="extract-1", now=10)
    duplicate = repository.add_candidate("以后默认使用稳健标准误", source_ids="extract-2", now=11)
    assert duplicate["id"] == candidate["id"]
    assert repository.accept_candidate(candidate["id"], workspace_id="project-b") is None
    assert repository.get_candidate(candidate["id"]) is not None

    broken = repository.add_candidate("替换一个不存在的规则", supersedes="missing", now=12)
    with pytest.raises(KeyError, match="missing"):
        repository.accept_candidate(broken["id"])
    assert repository.get_candidate(broken["id"]) is not None
    assert repository.list_records() == []

    accepted = repository.accept_candidate(candidate["id"], provenance="approval-1", now=20)
    assert accepted is not None
    assert accepted["status"] == STATUS_ACTIVE
    assert repository.get_candidate(candidate["id"]) is None
    decided = repository.get_candidate(candidate["id"], include_decided=True)
    assert decided is not None
    assert decided["status"] == STATUS_ACCEPTED
    assert decided["accepted_record_id"] == accepted["id"]
    assert repository.reject_candidate(broken["id"], reason="不采用", now=21) is True
    assert repository.list_candidates() == []
    repository.close()


def test_json_import_is_explicit_read_only_idempotent_and_targetable(tmp_path):
    source = tmp_path / "memory.json"
    payload = {
        "schema_version": 2,
        "records": [
            {
                "id": "old-1",
                "workspace_id": "legacy-project",
                "text": "保留中文标点：是",
                "kind": "preference",
                "updated": 7,
                "used": 2,
                "source_ids": ["event-1"],
            }
        ],
        "candidates": [
            {
                "id": "candidate-1",
                "workspace_id": "legacy-project",
                "text": "建议默认保留标题",
                "kind": "decision",
                "confidence": "inferred",
                "updated_at": 8,
            }
        ],
    }
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = source.read_text(encoding="utf-8")

    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3", workspace_id="project-target")
    first = repository.import_json(source, workspace_id="project-target")
    second = repository.import_json(source, workspace_id="project-target")

    assert first["records_imported"] == 1
    assert first["candidates_imported"] == 1
    assert second["records_imported"] == 0
    assert second["candidates_imported"] == 0
    assert second["duplicates_skipped"] == 2
    assert source.read_text(encoding="utf-8") == before
    imported = repository.get_record("old-1")
    assert imported is not None
    assert imported["workspace_id"] == "project-target"
    assert imported["use_count"] == 2
    assert repository.get_candidate("candidate-1") is not None
    assert repository.get_record("old-1", workspace_id="legacy-project") is None

    registry = repository.register_workspace(
        "project-target",
        root=tmp_path,
        name="Target",
        metadata={"kind": "demo"},
        now=50,
    )
    assert registry["workspace_id"] == "project-target"
    assert repository.get_workspace("project-target")["metadata"] == {"kind": "demo"}
    repository.close()
