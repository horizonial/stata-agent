"""Verify an installed local-rehearsal version, including its bundled real Stata runtime."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

try:
    from tools.verify_local_candidate import _verify_stata
except ModuleNotFoundError:  # Direct `python tools/...` execution exposes the tools directory.
    from verify_local_candidate import _verify_stata


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def verify_install(
    install_root: Path, release_id: str, stata_home: Path
) -> dict[str, object]:
    root = install_root.resolve()
    active = (root / "launcher" / "active-version.txt").read_text(encoding="utf-8")
    if active != release_id:
        raise RuntimeError("installed active-version pointer does not match the requested release")
    version = root / "versions" / release_id
    manifest = _read_object(version / "evidence" / "payload-manifest.json")
    if manifest.get("release_id") != release_id:
        raise RuntimeError("installed payload manifest has the wrong release identity")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("installed payload manifest is empty")
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("installed payload manifest entry is invalid")
        relative = Path(str(entry["relative_path"]))
        payload = (version / relative).resolve()
        if not payload.is_relative_to(version.resolve()) or not payload.is_file():
            raise RuntimeError(f"installed payload member is missing: {relative}")
        if payload.stat().st_size != int(entry["size"]):
            raise RuntimeError(f"installed payload member size mismatch: {relative}")
        if _sha256(payload) != str(entry["sha256"]):
            raise RuntimeError(f"installed payload member hash mismatch: {relative}")
    service = version / "app" / "stata-research-agent.exe"
    probe = subprocess.run(
        [str(service), "--probe"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    probe_value = json.loads(probe.stdout)
    if not isinstance(probe_value, dict) or probe_value.get("read_only") is not True:
        raise RuntimeError("installed application probe failed")
    stata = await _verify_stata(version, stata_home.resolve())
    return {
        "schema_version": "stata-research-agent.installed-rehearsal-verification/v1",
        "release_id": release_id,
        "payload_entry_count": len(entries),
        "payload_manifest_sha256": manifest.get("manifest_sha256"),
        "active_pointer_valid": True,
        "release_probe": probe_value,
        "stata": stata,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--stata-home", type=Path, required=True)
    arguments = parser.parse_args()
    result = asyncio.run(
        verify_install(arguments.install_root, arguments.release_id, arguments.stata_home)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
