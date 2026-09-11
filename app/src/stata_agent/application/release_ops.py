"""Verified offline backup and restore operations for the local SQLite product."""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .. import __version__
from ..storage.migrations import LATEST_SCHEMA_VERSION


BUNDLE_SCHEMA = "stata-agent.backup.v1"
DATABASE_MEMBER = "data/agent.sqlite3"
MANIFEST_MEMBER = "manifest.json"
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_BYTES = 1_073_741_824


class ReleaseOperationError(RuntimeError):
    """Base class for stable release-operation failures."""


class BackupValidationError(ReleaseOperationError):
    """A source, destination, acknowledgement, or bound is invalid."""


class BundleIntegrityError(ReleaseOperationError):
    """A backup bundle is malformed, corrupt, or incompatible."""


class RestoreConflictError(ReleaseOperationError):
    """A live/locked target cannot be replaced safely."""


@dataclass(frozen=True, slots=True)
class RestoreResult:
    target_database: Path
    rollback_bundle: Path | None
    restored_asset_count: int
    schema_version: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_member(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\x00" in name:
        raise BundleIntegrityError("bundle contains an unsafe member name")
    member = PurePosixPath(name)
    if member.is_absolute() or any(part in {"", ".", ".."} for part in member.parts):
        raise BundleIntegrityError("bundle contains an unsafe member path")
    return member


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    value = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    return int(value)


def _check_database(path: Path, *, allow_future_schema: bool = False) -> int:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise BundleIntegrityError("database integrity check failed")
            schema = _schema_version(connection)
        finally:
            connection.close()
    except BundleIntegrityError:
        raise
    except sqlite3.Error as error:
        raise BundleIntegrityError("database snapshot is unreadable") from error
    if schema > LATEST_SCHEMA_VERSION and not allow_future_schema:
        raise BundleIntegrityError("backup schema is newer than this application")
    return schema


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class ReleaseOperations:
    """Create, verify, and explicitly restore bounded local backup bundles."""

    def __init__(self, *, max_files: int = DEFAULT_MAX_FILES, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if isinstance(max_files, bool) or max_files < 1:
            raise BackupValidationError("max_files must be positive")
        if isinstance(max_bytes, bool) or max_bytes < 1:
            raise BackupValidationError("max_bytes must be positive")
        self._max_files = int(max_files)
        self._max_bytes = int(max_bytes)

    def create_backup(
        self,
        database: str | Path,
        destination: str | Path,
        *,
        asset_root: str | Path | None = None,
    ) -> dict[str, Any]:
        source = Path(database).resolve()
        output = Path(destination).resolve()
        if not source.is_file() or _is_link_or_reparse(source):
            raise BackupValidationError("database source must be a regular file")
        if output == source:
            raise BackupValidationError("backup destination is invalid")
        output.parent.mkdir(parents=True, exist_ok=True)
        assets = Path(asset_root).resolve() if asset_root is not None else None
        if assets is not None and (not assets.is_dir() or _is_link_or_reparse(assets)):
            raise BackupValidationError("asset root must be a real directory")
        if assets is not None and _is_within(output, assets):
            raise BackupValidationError("backup destination must be outside the asset root")

        with tempfile.TemporaryDirectory(prefix="stata-agent-backup-") as temp_name:
            temp = Path(temp_name)
            snapshot = temp / "agent.sqlite3"
            source_connection = sqlite3.connect(str(source), timeout=5.0)
            target_connection = sqlite3.connect(str(snapshot))
            try:
                source_connection.backup(target_connection)
            except sqlite3.Error as error:
                raise ReleaseOperationError("database backup failed") from error
            finally:
                target_connection.close()
                source_connection.close()
            schema = _check_database(snapshot, allow_future_schema=True)

            files: list[dict[str, Any]] = [
                {"path": DATABASE_MEMBER, "size": snapshot.stat().st_size, "sha256": _sha256(snapshot)}
            ]
            asset_files: list[tuple[Path, str]] = []
            total = snapshot.stat().st_size
            if assets is not None:
                for candidate in sorted(assets.rglob("*"), key=lambda item: item.as_posix()):
                    if candidate.is_dir():
                        if _is_link_or_reparse(candidate):
                            raise BackupValidationError("asset tree contains a link or junction")
                        continue
                    if not candidate.is_file() or _is_link_or_reparse(candidate):
                        raise BackupValidationError("asset tree contains a non-regular file")
                    relative = candidate.relative_to(assets).as_posix()
                    member = f"assets/{relative}"
                    _safe_member(member)
                    size = candidate.stat().st_size
                    total += size
                    if len(files) >= self._max_files or total > self._max_bytes:
                        raise BackupValidationError("backup exceeds configured bounds")
                    files.append({"path": member, "size": size, "sha256": _sha256(candidate)})
                    asset_files.append((candidate, member))

            manifest: dict[str, Any] = {
                "schema": BUNDLE_SCHEMA,
                "app_version": __version__,
                "database_schema_version": schema,
                "created_at": int(time.time()),
                "files": sorted(files, key=lambda item: str(item["path"])),
            }
            canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            partial = output.with_name(f".{output.name}.{os.getpid()}.tmp")
            try:
                with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                    bundle.writestr(MANIFEST_MEMBER, canonical.encode("utf-8"))
                    bundle.write(snapshot, DATABASE_MEMBER)
                    for path, member in asset_files:
                        bundle.write(path, member)
                with partial.open("r+b") as handle:
                    os.fsync(handle.fileno())
                os.replace(partial, output)
            finally:
                partial.unlink(missing_ok=True)
        self.verify_backup(output)
        return manifest

    def verify_backup(self, bundle_path: str | Path) -> dict[str, Any]:
        bundle_file = Path(bundle_path).resolve()
        if not bundle_file.is_file() or _is_link_or_reparse(bundle_file):
            raise BackupValidationError("backup bundle must be a regular file")
        try:
            with zipfile.ZipFile(bundle_file, "r") as bundle:
                infos = bundle.infolist()
                names = [info.filename for info in infos]
                if len(names) != len(set(names)) or len(names) > self._max_files + 1:
                    raise BundleIntegrityError("bundle member count is invalid")
                total = 0
                for info in infos:
                    _safe_member(info.filename)
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise BundleIntegrityError("bundle contains a symbolic link")
                    total += info.file_size
                    if total > self._max_bytes:
                        raise BundleIntegrityError("bundle exceeds configured size")
                if MANIFEST_MEMBER not in names or DATABASE_MEMBER not in names:
                    raise BundleIntegrityError("bundle is missing required members")
                try:
                    manifest = json.loads(bundle.read(MANIFEST_MEMBER).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as error:
                    raise BundleIntegrityError("bundle manifest is invalid") from error
                if not isinstance(manifest, dict) or manifest.get("schema") != BUNDLE_SCHEMA:
                    raise BundleIntegrityError("bundle manifest schema is unsupported")
                records = manifest.get("files")
                if not isinstance(records, list) or not records:
                    raise BundleIntegrityError("bundle manifest file list is invalid")
                expected_names = {MANIFEST_MEMBER}
                with tempfile.TemporaryDirectory(prefix="stata-agent-verify-") as temp_name:
                    snapshot: Path | None = None
                    for record in records:
                        if not isinstance(record, dict):
                            raise BundleIntegrityError("bundle manifest entry is invalid")
                        name = record.get("path")
                        size = record.get("size")
                        digest = record.get("sha256")
                        if not isinstance(name, str) or not isinstance(size, int) or isinstance(size, bool):
                            raise BundleIntegrityError("bundle manifest entry is invalid")
                        if size < 0 or not isinstance(digest, str) or len(digest) != 64:
                            raise BundleIntegrityError("bundle manifest entry is invalid")
                        _safe_member(name)
                        expected_names.add(name)
                        try:
                            info = bundle.getinfo(name)
                        except KeyError as error:
                            raise BundleIntegrityError("bundle member is missing") from error
                        if info.file_size != size:
                            raise BundleIntegrityError("bundle member size mismatch")
                        output = Path(temp_name) / PurePosixPath(name)
                        output.parent.mkdir(parents=True, exist_ok=True)
                        digest_builder = hashlib.sha256()
                        written = 0
                        with bundle.open(info, "r") as source, output.open("xb") as target:
                            while True:
                                block = source.read(1024 * 1024)
                                if not block:
                                    break
                                written += len(block)
                                if written > size:
                                    raise BundleIntegrityError("bundle member exceeds declared size")
                                digest_builder.update(block)
                                target.write(block)
                        if written != size or digest_builder.hexdigest() != digest:
                            raise BundleIntegrityError("bundle member hash mismatch")
                        if name == DATABASE_MEMBER:
                            snapshot = output
                    if expected_names != set(names):
                        raise BundleIntegrityError("bundle contains undeclared members")
                    if snapshot is None:
                        raise BundleIntegrityError("bundle database member is absent")
                    actual_schema = _check_database(snapshot)
                declared_schema = manifest.get("database_schema_version")
                if declared_schema != actual_schema:
                    raise BundleIntegrityError("bundle schema version mismatch")
                return manifest
        except BundleIntegrityError:
            raise
        except (OSError, zipfile.BadZipFile) as error:
            raise BundleIntegrityError("backup bundle is unreadable") from error

    def restore_backup(
        self,
        bundle_path: str | Path,
        target_database: str | Path,
        *,
        acknowledge: bool,
        target_active: bool = False,
        target_asset_root: str | Path | None = None,
    ) -> RestoreResult:
        if acknowledge is not True:
            raise BackupValidationError("restore requires explicit acknowledgement")
        if target_active:
            raise RestoreConflictError("active databases cannot be restored")
        manifest = self.verify_backup(bundle_path)
        records_by_name = {str(record["path"]): record for record in manifest["files"]}
        target = Path(target_database).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and (not target.is_file() or _is_link_or_reparse(target)):
            raise BackupValidationError("restore target must be a regular file")
        self._assert_database_offline(target)

        timestamp = int(time.time())
        rollback = target.with_name(f"{target.name}.pre-restore-{timestamp}.zip") if target.exists() else None
        if rollback is not None:
            suffix = 0
            while rollback.exists():
                suffix += 1
                rollback = target.with_name(f"{target.name}.pre-restore-{timestamp}-{suffix}.zip")
            rollback_assets = target_asset_root if target_asset_root is not None and Path(target_asset_root).is_dir() else None
            self.create_backup(target, rollback, asset_root=rollback_assets)

        assets_restored = 0
        with tempfile.TemporaryDirectory(prefix="stata-agent-restore-", dir=target.parent) as temp_name:
            stage = Path(temp_name)
            with zipfile.ZipFile(Path(bundle_path), "r") as bundle:
                database_stage = stage / "agent.sqlite3"
                with bundle.open(DATABASE_MEMBER, "r") as source, database_stage.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                database_record = records_by_name[DATABASE_MEMBER]
                if database_stage.stat().st_size != database_record["size"] or _sha256(database_stage) != database_record["sha256"]:
                    raise BundleIntegrityError("bundle changed after verification")
                _check_database(database_stage)
                asset_records = [
                    item for item in manifest["files"] if str(item.get("path", "")).startswith("assets/")
                ]
                assets_stage = stage / "assets"
                if asset_records:
                    if target_asset_root is None:
                        raise BackupValidationError("bundle contains assets but no restore asset root was supplied")
                    for record in asset_records:
                        name = str(record["path"])
                        relative = PurePosixPath(name).relative_to("assets")
                        destination = assets_stage / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with bundle.open(name, "r") as source, destination.open("xb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                        if destination.stat().st_size != record["size"] or _sha256(destination) != record["sha256"]:
                            raise BundleIntegrityError("bundle changed after verification")
                        assets_restored += 1

            asset_target = Path(target_asset_root).resolve() if target_asset_root is not None else None
            old_assets: Path | None = None
            if assets_restored and asset_target is not None:
                asset_target.parent.mkdir(parents=True, exist_ok=True)
                if asset_target.exists():
                    if not asset_target.is_dir() or _is_link_or_reparse(asset_target):
                        raise BackupValidationError("restore asset target must be a real directory")
                    old_assets = asset_target.with_name(f"{asset_target.name}.pre-restore-{timestamp}")
                    if old_assets.exists():
                        raise RestoreConflictError("asset rollback target already exists")
                    os.replace(asset_target, old_assets)
                try:
                    os.replace(assets_stage, asset_target)
                except BaseException:
                    if old_assets is not None and not asset_target.exists():
                        os.replace(old_assets, asset_target)
                    raise
            try:
                os.replace(database_stage, target)
            except BaseException:
                if old_assets is not None and asset_target is not None and old_assets.exists():
                    if asset_target.exists():
                        failed_assets = asset_target.with_name(f"{asset_target.name}.failed-{timestamp}")
                        os.replace(asset_target, failed_assets)
                    os.replace(old_assets, asset_target)
                raise
        return RestoreResult(
            target_database=target,
            rollback_bundle=rollback,
            restored_asset_count=assets_restored,
            schema_version=int(manifest["database_schema_version"]),
        )

    @staticmethod
    def _assert_database_offline(path: Path) -> None:
        if not path.exists():
            return
        try:
            connection = sqlite3.connect(str(path), timeout=0.0, isolation_level=None)
            try:
                connection.execute("BEGIN EXCLUSIVE")
                connection.rollback()
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise RestoreConflictError("database is active or locked") from error


def main(argv: list[str] | None = None) -> int:
    """Operator CLI. Restore is deliberately explicit and intended for a stopped UI."""

    parser = argparse.ArgumentParser(prog="stata-agent-backup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--database", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--assets")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--bundle", required=True)
    restore.add_argument("--database", required=True)
    restore.add_argument("--assets")
    restore.add_argument("--acknowledge-data-replacement", action="store_true")
    args = parser.parse_args(argv)
    operations = ReleaseOperations()
    try:
        if args.command == "create":
            manifest = operations.create_backup(args.database, args.output, asset_root=args.assets)
            payload = {"ok": True, "schema": manifest["schema"], "files": len(manifest["files"])}
        elif args.command == "verify":
            manifest = operations.verify_backup(args.bundle)
            payload = {
                "ok": True,
                "schema": manifest["schema"],
                "database_schema_version": manifest["database_schema_version"],
                "files": len(manifest["files"]),
            }
        else:
            result = operations.restore_backup(
                args.bundle,
                args.database,
                acknowledge=args.acknowledge_data_replacement,
                target_asset_root=args.assets,
            )
            payload = {
                "ok": True,
                "database_schema_version": result.schema_version,
                "restored_asset_count": result.restored_asset_count,
                "rollback_created": result.rollback_bundle is not None,
            }
    except ReleaseOperationError as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, sort_keys=True))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


__all__ = [
    "BUNDLE_SCHEMA",
    "BackupValidationError",
    "BundleIntegrityError",
    "ReleaseOperationError",
    "ReleaseOperations",
    "RestoreConflictError",
    "RestoreResult",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised through the console target
    raise SystemExit(main())
