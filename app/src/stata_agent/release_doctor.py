"""Offline release-integrity checks with explicit manual-attestation status."""

from __future__ import annotations

import argparse
import importlib.resources
import json
import platform
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from . import __version__
from .application.release_ops import ReleaseOperations
from .storage.migrations import LATEST_SCHEMA_VERSION
from .storage.sqlite_store import SQLiteStore


REPORT_SCHEMA = "stata-agent.release-check.v1"
MANUAL_GATES = (
    "windows_signature_verified",
    "clean_install_verified",
    "upgrade_verified",
    "rollback_verified",
    "backup_restore_verified",
)


def _package_asset(relative: str) -> bool:
    target = importlib.resources.files("stata_agent").joinpath(relative)
    return target.is_file()


def _wheel_check(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "not_requested"}
    wheel = Path(path)
    if not wheel.is_file() or wheel.suffix != ".whl":
        return {"status": "failed", "code": "wheel_missing"}
    required_suffixes = (
        ".dist-info/entry_points.txt",
        "stata_agent/ui.py",
        "stata_agent/webui/index.html",
        "stata_agent/webui/app.js",
        "stata_agent/eval/golden.json",
    )
    try:
        with zipfile.ZipFile(wheel, "r") as archive:
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return {"status": "failed", "code": "wheel_invalid"}
    missing = [suffix for suffix in required_suffixes if not any(name.endswith(suffix) for name in names)]
    if missing:
        return {"status": "failed", "code": "wheel_content_missing", "missing_count": len(missing)}
    return {"status": "passed"}


def _manual_check(path: str | Path | None) -> dict[str, Any]:
    values: dict[str, bool] = {name: False for name in MANUAL_GATES}
    if path is not None:
        try:
            loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "failed", "code": "attestation_invalid", "gates": values}
        if not isinstance(loaded, dict) or loaded.get("schema") != "stata-agent.release-attestation.v1":
            return {"status": "failed", "code": "attestation_invalid", "gates": values}
        for name in MANUAL_GATES:
            values[name] = loaded.get(name) is True
    pending = [name for name, passed in values.items() if not passed]
    return {"status": "passed" if not pending else "pending", "pending": pending, "gates": values}


def run_checks(
    *,
    wheel: str | Path | None = None,
    attestation: str | Path | None = None,
) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "python": {
            "status": "passed" if tuple(map(int, platform.python_version_tuple())) >= (3, 12, 0) else "failed",
            "major_minor": ".".join(platform.python_version_tuple()[:2]),
        },
        "package_assets": {
            "status": "passed"
            if all(
                _package_asset(path)
                for path in ("webui/index.html", "webui/app.js", "eval/golden.json")
            )
            else "failed"
        },
        "wheel": _wheel_check(wheel),
    }
    with tempfile.TemporaryDirectory(prefix="stata-agent-release-check-") as temp_name:
        root = Path(temp_name)
        database = root / "doctor.sqlite3"
        store = SQLiteStore(str(database), takeover=True)
        try:
            migrated = store.schema_version
        finally:
            store.close()
        bundle = root / "doctor-backup.zip"
        operations = ReleaseOperations()
        manifest = operations.create_backup(database, bundle)
        verified = operations.verify_backup(bundle)
        checks["migration"] = {
            "status": "passed" if migrated == LATEST_SCHEMA_VERSION else "failed",
            "schema_version": migrated,
        }
        checks["backup_roundtrip"] = {
            "status": "passed" if verified == manifest else "failed",
            "file_count": len(manifest["files"]),
        }
    automated_ok = all(
        value.get("status") in {"passed", "not_requested"} for value in checks.values()
    )
    manual = _manual_check(attestation)
    manual_ok = manual.get("status") == "passed"
    return {
        "schema": REPORT_SCHEMA,
        "app_version": __version__,
        "automated_ok": automated_ok,
        "release_ready": automated_ok and manual_ok,
        "checks": checks,
        "manual": manual,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stata-agent-release-check")
    parser.add_argument("--offline", action="store_true", help="run local checks without network access")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--wheel")
    parser.add_argument("--attestation")
    parser.add_argument("--require-release-ready", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_checks(wheel=args.wheel, attestation=args.attestation)
    except Exception as error:  # noqa: BLE001 - CLI returns only the stable class name
        payload = {"schema": REPORT_SCHEMA, "automated_ok": False, "release_ready": False, "error": type(error).__name__}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"release-check: automated={'PASS' if report['automated_ok'] else 'FAIL'}")
        print(f"release-ready: {'YES' if report['release_ready'] else 'PENDING MANUAL ATTESTATION'}")
    if not report["automated_ok"]:
        return 2
    if args.require_release_ready and not report["release_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
