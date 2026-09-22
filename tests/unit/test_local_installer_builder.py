from __future__ import annotations

import json
from pathlib import Path

import pytest

from stata_research_agent.interfaces.release_evidence import CanonicalPayloadScanner
from tools.build_local_installer import find_iscc, validate_candidate


def _candidate(root: Path, *, release_id: str = "local-0.0.0") -> Path:
    payload = root / "payload"
    payload.mkdir(parents=True)
    (payload / "data.txt").write_text("safe payload", encoding="utf-8")
    manifest = CanonicalPayloadScanner().scan(payload, release_id=release_id)
    evidence = root / "evidence"
    evidence.mkdir()
    (evidence / "candidate-build-receipt.json").write_text(
        json.dumps(
            {
                "release_id": release_id,
                "payload_manifest_sha256": manifest.manifest_sha256,
                "production_release": False,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_local_installer_accepts_only_matching_unsigned_candidate(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path / "candidate")
    release_id, digest = validate_candidate(candidate)
    assert release_id == "local-0.0.0"
    assert len(digest) == 64

    (candidate / "payload" / "data.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="immutable build receipt"):
        validate_candidate(candidate)


def test_local_installer_rejects_unsafe_release_identity(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path / "candidate", release_id="../escape")
    with pytest.raises(ValueError, match="not safe"):
        validate_candidate(candidate)


def test_find_iscc_honors_explicit_compiler(tmp_path: Path) -> None:
    compiler = tmp_path / "ISCC.exe"
    compiler.write_bytes(b"placeholder")
    assert find_iscc(compiler) == compiler.resolve()


def test_installer_contract_is_per_user_and_does_not_delete_research_data() -> None:
    script = (
        Path(__file__).parents[2] / "packaging" / "windows" / "local-rehearsal.iss"
    ).read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in script
    assert "{localappdata}\\Programs\\StataResearchAgent" in script
    assert "Documents\\Stata Research Agent" not in script
    uninstall_contract = script.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
    assert "{app}\\launcher" in uninstall_contract
    assert "{app}\\versions" in uninstall_contract
    assert "{localappdata}\\StataResearchAgent" not in uninstall_contract
    assert "local-unsigned" in script
