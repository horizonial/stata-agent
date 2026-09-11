from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from stata_agent.application.release_ops import (
    BackupValidationError,
    BundleIntegrityError,
    ReleaseOperations,
    RestoreConflictError,
    main,
)
from stata_agent.events.schema import ACTOR_USER, EVENT_USER, Event
from stata_agent.release_doctor import MANUAL_GATES, main as doctor_main, run_checks
from stata_agent.storage.sqlite_store import SQLiteStore


def _database(path: Path, *, text: str = "before") -> None:
    store = SQLiteStore(str(path), takeover=True)
    try:
        store.append(Event(idea_id="release", event_type=EVENT_USER, actor=ACTOR_USER, payload={"text": text}))
    finally:
        store.close()


def _event_text(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        payload = connection.execute("SELECT payload FROM events ORDER BY seq DESC LIMIT 1").fetchone()[0]
    return str(json.loads(payload)["text"])


def test_backup_verify_restore_and_rollback(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    _database(source, text="restored")
    _database(target, text="original")
    assets = tmp_path / "assets"
    (assets / "objects").mkdir(parents=True)
    (assets / "objects" / "paper.pdf").write_bytes(b"%PDF-safe")

    operations = ReleaseOperations(max_files=20, max_bytes=5_000_000)
    bundle = tmp_path / "backup.zip"
    manifest = operations.create_backup(source, bundle, asset_root=assets)
    assert manifest["schema"] == "stata-agent.backup.v1"
    assert operations.verify_backup(bundle) == manifest

    restored_assets = tmp_path / "restored-assets"
    result = operations.restore_backup(
        bundle,
        target,
        acknowledge=True,
        target_asset_root=restored_assets,
    )
    assert _event_text(target) == "restored"
    assert result.rollback_bundle is not None and result.rollback_bundle.is_file()
    assert ReleaseOperations().verify_backup(result.rollback_bundle)["schema"] == "stata-agent.backup.v1"
    assert (restored_assets / "objects" / "paper.pdf").read_bytes() == b"%PDF-safe"
    assert result.restored_asset_count == 1


def test_restore_rejects_missing_ack_and_active_database(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    _database(source)
    _database(target)
    bundle = tmp_path / "backup.zip"
    operations = ReleaseOperations()
    operations.create_backup(source, bundle)
    with pytest.raises(BackupValidationError, match="acknowledgement"):
        operations.restore_backup(bundle, target, acknowledge=False)
    with sqlite3.connect(target, isolation_level=None) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        with pytest.raises(RestoreConflictError, match="active or locked"):
            operations.restore_backup(bundle, target, acknowledge=True)
        connection.rollback()
    assert _event_text(target) == "before"


def test_verify_rejects_tamper_and_unsafe_members_before_restore(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    _database(source)
    _database(target, text="sentinel")
    valid = tmp_path / "valid.zip"
    operations = ReleaseOperations()
    operations.create_backup(source, valid)

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(valid, "r") as old, zipfile.ZipFile(tampered, "w") as new:
        for info in old.infolist():
            data = old.read(info.filename)
            if info.filename == "data/agent.sqlite3":
                data += b"tamper"
            new.writestr(info.filename, data)
    with pytest.raises(BundleIntegrityError):
        operations.restore_backup(tampered, target, acknowledge=True)
    assert _event_text(target) == "sentinel"

    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape", b"no")
    with pytest.raises(BundleIntegrityError, match="unsafe"):
        operations.verify_backup(unsafe)
    assert not (tmp_path.parent / "escape").exists()


def test_asset_links_and_bounds_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite3"
    _database(database)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "large.pdf").write_bytes(b"x" * 100)
    with pytest.raises(BackupValidationError, match="bounds"):
        ReleaseOperations(max_bytes=10).create_backup(database, tmp_path / "bounded.zip", asset_root=assets)


def test_backup_cli_and_release_doctor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "source.sqlite3"
    _database(database)
    bundle = tmp_path / "backup.zip"
    assert main(["verify", "--bundle", str(tmp_path / "missing.zip")]) == 2
    capsys.readouterr()
    assert main(["create", "--database", str(database), "--output", str(bundle)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main(["verify", "--bundle", str(bundle)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    report = run_checks()
    assert report["automated_ok"] is True
    assert report["release_ready"] is False
    assert set(report["manual"]["pending"]) == set(MANUAL_GATES)
    assert doctor_main(["--offline", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["automated_ok"] is True
    assert doctor_main(["--offline", "--require-release-ready"]) == 1
    capsys.readouterr()

    attestation = tmp_path / "attestation.json"
    attestation.write_text(
        json.dumps({"schema": "stata-agent.release-attestation.v1", **dict.fromkeys(MANUAL_GATES, True)}),
        encoding="utf-8",
    )
    assert run_checks(attestation=attestation)["release_ready"] is True
