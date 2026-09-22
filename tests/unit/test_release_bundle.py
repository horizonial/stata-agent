"""D-230 immutable release bundle trust and path verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from stata_research_agent.application.release_activation import ReleaseActivationError
from stata_research_agent.interfaces.release_bundle import FilesystemReleaseBundleVerifier


class ExactSignature:
    def verify(
        self,
        manifest_bytes: bytes,
        signature_bytes: bytes,
        *,
        publisher_key_id: str,
    ) -> bool:
        return (
            publisher_key_id == "publisher-key-v1"
            and signature_bytes == hashlib.sha256(b"trusted-root:" + manifest_bytes).digest()
        )


class ExactAuthenticode:
    def verify(self, entry_path: Path, *, publisher_key_id: str) -> bool:
        return entry_path.name == "agent.exe" and publisher_key_id == "publisher-key-v1"


def write_release(versions: Path, release_id: str = "release-1") -> Path:
    root = versions / release_id
    (root / "web").mkdir(parents=True)
    payloads = {
        "agent.exe": b"signed-entry-fixture",
        "web/index.html": b"<html>fixture</html>",
    }
    for name, payload in payloads.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    manifest = {
        "manifest_schema_version": "1.0",
        "release_id": release_id,
        "semantic_version": "0.1.0",
        "build_id": "build-1",
        "publisher_key_id": "publisher-key-v1",
        "entry_point": "agent.exe",
        "file_manifest": [
            {
                "path": name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(payloads.items())
        ],
        "global_control_schema_range": [1, 1],
        "workspace_schema_read_range": [1, 26],
        "workspace_schema_write_range": [26, 26],
        "minimum_launcher_version": "1.0.0",
    }
    encoded = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    (root / "release-manifest.json").write_bytes(encoded)
    (root / "release-manifest.sig").write_bytes(hashlib.sha256(b"trusted-root:" + encoded).digest())
    return root


def verifier(versions: Path) -> FilesystemReleaseBundleVerifier:
    return FilesystemReleaseBundleVerifier(
        versions,
        ExactSignature(),
        ExactAuthenticode(),
        launcher_version="1.0.0",
        global_control_schema=1,
    )


def test_release_requires_trusted_manifest_exact_payload_and_entry_authenticode(
    tmp_path: Path,
) -> None:
    versions = tmp_path / "versions"
    release = write_release(versions)
    observed = verifier(versions).verify(str(release))
    assert observed.release_id == "release-1"
    assert observed.entry_point == "agent.exe"
    assert observed.version_directory == str(release.resolve())

    (release / "web" / "index.html").write_text("tampered", encoding="utf-8")
    with pytest.raises(ReleaseActivationError, match="does not match"):
        verifier(versions).verify(str(release))


def test_release_rejects_path_injection_undeclared_file_and_untrusted_signature(
    tmp_path: Path,
) -> None:
    versions = tmp_path / "versions"
    release = write_release(versions)
    (release / "undeclared.dll").write_bytes(b"not-declared")
    with pytest.raises(ReleaseActivationError, match="undeclared"):
        verifier(versions).verify(str(release))

    (release / "undeclared.dll").unlink()
    (release / "release-manifest.sig").write_bytes(b"self-signed-is-not-trusted")
    with pytest.raises(ReleaseActivationError, match="signature"):
        verifier(versions).verify(str(release))

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ReleaseActivationError, match="outside"):
        verifier(versions).verify(str(outside))
