"""Release readiness remains fail-closed before immutable RC registration."""

from __future__ import annotations

import json
from pathlib import Path

from stata_research_agent.interfaces.release_evidence import (
    CanonicalPayloadScanner,
    payload_manifest_json,
)
from stata_research_agent.interfaces.release_readiness import LocalReleaseReadinessAuditor


def _candidate(root: Path, release_id: str, *, production: bool) -> Path:
    payload = root / "payload"
    payload.mkdir(parents=True)
    (payload / "fixture.txt").write_text("stable candidate", encoding="utf-8")
    manifest = CanonicalPayloadScanner().scan(payload, release_id=release_id)
    evidence = root / "evidence"
    evidence.mkdir()
    (evidence / "payload-manifest.json").write_text(
        json.dumps(payload_manifest_json(manifest)), encoding="utf-8"
    )
    (evidence / "candidate-build-receipt.json").write_text(
        json.dumps(
            {
                "release_id": release_id,
                "payload_manifest_sha256": manifest.manifest_sha256,
                "source_manifest_sha256": "a" * 64,
                "build_input_sha256": "b" * 64,
                "production_release": production,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_matching_rehearsals_are_not_eligible_for_rc_registration(
    tmp_path: Path, monkeypatch: object
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _candidate(tmp_path / "first", "release-1", production=False)
    second = _candidate(tmp_path / "second", "release-1", production=False)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        LocalReleaseReadinessAuditor,
        "_git_facts",
        staticmethod(lambda _project: ("revision-1", True, True)),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        LocalReleaseReadinessAuditor,
        "_find_iscc",
        staticmethod(lambda: None),
    )

    report = LocalReleaseReadinessAuditor().audit(project, first, second)

    assert not report.ready_to_register
    assert not report.ready_for_g7
    failed = {item.check_id: item.reason_code for item in report.checks if not item.passed}
    assert failed["candidate.production_release"] == "REHEARSAL_BUILD_ONLY"
    assert failed["toolchain.python_wheelhouse_lock"] == "PYTHON_WHEELHOUSE_LOCK_MISSING"
    assert failed["distribution.inno_setup"] == "INSTALLER_TOOLCHAIN_MISSING"


def test_payload_change_is_detected_even_when_receipt_is_unchanged(
    tmp_path: Path, monkeypatch: object
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _candidate(tmp_path / "first", "release-1", production=True)
    second = _candidate(tmp_path / "second", "release-1", production=True)
    (second / "payload" / "fixture.txt").write_text("changed", encoding="utf-8")
    monkeypatch.setattr(  # type: ignore[attr-defined]
        LocalReleaseReadinessAuditor,
        "_git_facts",
        staticmethod(lambda _project: ("revision-1", True, True)),
    )

    report = LocalReleaseReadinessAuditor().audit(project, first, second)

    observed = {item.check_id: item for item in report.checks}
    assert not observed["candidate_b.payload_integrity"].passed
    assert observed["candidate_b.payload_integrity"].reason_code == "PAYLOAD_INTEGRITY_MISMATCH"
