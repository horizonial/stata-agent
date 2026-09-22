"""Build a per-user, explicitly unsigned Windows installer rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from stata_research_agent.interfaces.release_evidence import CanonicalPayloadScanner

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "packaging" / "windows" / "local-rehearsal.iss"
RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_iscc(explicit: Path | None = None) -> Path:
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    discovered = shutil.which("ISCC.exe") or shutil.which("iscc")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(
        (
            Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
            Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
            Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        )
    )
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    raise FileNotFoundError("Inno Setup 6 compiler ISCC.exe is not installed")


def validate_candidate(candidate: Path) -> tuple[str, str]:
    root = candidate.resolve()
    receipt = _read_object(root / "evidence" / "candidate-build-receipt.json")
    release_id = str(receipt.get("release_id", ""))
    if RELEASE_ID.fullmatch(release_id) is None:
        raise ValueError("candidate release_id is not safe for an installer path")
    if receipt.get("production_release") is not False:
        raise ValueError("local installer accepts only an unsigned rehearsal candidate")
    manifest = CanonicalPayloadScanner().scan(root / "payload", release_id=release_id)
    expected = str(receipt.get("payload_manifest_sha256", ""))
    if manifest.manifest_sha256 != expected:
        raise RuntimeError("candidate payload does not match its immutable build receipt")
    return release_id, expected


def build_installer(
    candidate: Path,
    output: Path,
    *,
    iscc: Path | None = None,
    verification_report: Path | None = None,
) -> Path:
    candidate_root = candidate.resolve()
    output_root = output.resolve()
    release_id, manifest_sha256 = validate_candidate(candidate_root)
    verification: dict[str, Any] | None = None
    if verification_report is not None:
        verification = _read_object(verification_report.resolve())
        if (
            verification.get("release_id") != release_id
            or verification.get("payload_manifest_sha256") != manifest_sha256
            or verification.get("payload_unchanged") is not True
        ):
            raise ValueError("verification report does not belong to this candidate")
    compiler = find_iscc(iscc)
    output_root.mkdir(parents=True, exist_ok=True)
    command = [
        str(compiler),
        f"/DCandidateRoot={candidate_root}",
        f"/DReleaseId={release_id}",
        f"/DOutputDir={output_root}",
        str(SCRIPT),
    ]
    subprocess.run(command, cwd=PROJECT, check=True)
    installer = output_root / f"StataResearchAgent-{release_id}-local-unsigned.exe"
    if not installer.is_file():
        raise RuntimeError("ISCC completed without producing the expected installer")
    receipt = {
        "schema_version": "stata-research-agent.local-installer-build/v1",
        "release_id": release_id,
        "production_release": False,
        "authenticode_signed": False,
        "distribution_scope": "local_development_only",
        "payload_manifest_sha256": manifest_sha256,
        "installer_sha256": _sha256(installer),
        "installer_size_bytes": installer.stat().st_size,
        "candidate_verification": verification,
        "preserves_workspace_and_application_data_on_uninstall": True,
    }
    receipt_path = output_root / f"StataResearchAgent-{release_id}-installer-receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return installer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--verification-report", type=Path)
    arguments = parser.parse_args()
    installer = build_installer(
        arguments.candidate,
        arguments.output,
        iscc=arguments.iscc,
        verification_report=arguments.verification_report,
    )
    print(installer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
