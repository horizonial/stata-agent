"""D-232 canonical payload, SBOM coverage, input locks, and exception scope."""

from __future__ import annotations

import json
import os
import shutil
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stata_research_agent.application.release_supply_chain import (
    ComponentLicenseDecision,
    LicensePolicyDecision,
    ReleaseComponent,
    ReleaseExceptionType,
    ReleasePolicyException,
    ReleaseSecurityFinding,
    ReleaseSupplyChainError,
    VulnerabilityReportBoundary,
    enforce_license_gate,
    enforce_security_gate,
    exception_is_applicable,
)
from stata_research_agent.application.sensitive_output import SensitiveOutputGate
from stata_research_agent.interfaces.release_evidence import (
    CanonicalPayloadScanner,
    ReleaseEvidenceBuilder,
)
from stata_research_agent.interfaces.release_input_audit import ReleaseInputAuditor


def pe_x64(payload: bytes) -> bytes:
    value = bytearray(128 + len(payload))
    value[:2] = b"MZ"
    struct.pack_into("<I", value, 0x3C, 64)
    value[64:68] = b"PE\0\0"
    struct.pack_into("<H", value, 68, 0x8664)
    value[128:] = payload
    return bytes(value)


def payload(tmp_path: Path) -> Path:
    root = tmp_path / "payload"
    (root / "web").mkdir(parents=True)
    (root / "agent.exe").write_bytes(pe_x64(b"entry"))
    (root / "web" / "index.html").write_text("<html></html>", encoding="utf-8")
    return root


def components() -> tuple[ReleaseComponent, ...]:
    return (
        ReleaseComponent(
            "component:app",
            "stata-research-agent",
            "0.1.0",
            "application",
            "pkg:generic/stata-research-agent@0.1.0",
            "Stata Research Agent",
            "signed-source:fixture",
            "a" * 64,
            "Proprietary",
            False,
            ("agent.exe",),
        ),
        ReleaseComponent(
            "component:web",
            "stata-research-agent-web",
            "0.1.0",
            "library",
            "pkg:npm/stata-research-agent-web@0.1.0",
            "Stata Research Agent",
            "package-lock:fixture",
            "b" * 64,
            "Proprietary",
            False,
            ("web/index.html",),
        ),
    )


def test_canonical_manifest_ignores_timestamps_but_not_bytes_or_architecture(
    tmp_path: Path,
) -> None:
    root = payload(tmp_path)
    scanner = CanonicalPayloadScanner()
    first = scanner.scan(root, release_id="release-1")
    os.utime(root / "agent.exe", None)
    second = scanner.scan(root, release_id="release-1")
    scanner.compare(first, second)
    assert first.entries[0].architecture == "x86_64"

    (root / "web" / "index.html").write_text("changed", encoding="utf-8")
    changed = scanner.scan(root, release_id="release-1")
    with pytest.raises(ReleaseSupplyChainError, match="not reproducible"):
        scanner.compare(first, changed)


def test_runtime_and_build_sbom_are_coverage_and_sensitive_output_gated(
    tmp_path: Path,
) -> None:
    manifest = CanonicalPayloadScanner().scan(payload(tmp_path), release_id="release-1")
    builder = ReleaseEvidenceBuilder(SensitiveOutputGate())
    runtime = builder.build_runtime_sbom(manifest, components())
    build = builder.build_build_sbom(
        release_id="release-1",
        build_input_sha256="b" * 64,
        components=(components()[0],),
    )
    assert runtime["bomFormat"] == build["bomFormat"] == "CycloneDX"

    with pytest.raises(ReleaseSupplyChainError, match="coverage mismatch"):
        builder.build_runtime_sbom(manifest, (components()[0],))
    secret_component = components()[0]
    secret_component = ReleaseComponent(
        secret_component.component_id,
        secret_component.name,
        secret_component.version,
        secret_component.component_type,
        secret_component.purl,
        secret_component.supplier,
        "sk-release-secret-canary-123456",
        secret_component.artifact_sha256,
        secret_component.license_expression,
        secret_component.modified,
        secret_component.payload_paths,
    )
    with pytest.raises(ReleaseSupplyChainError, match="Sensitive Output"):
        builder.build_runtime_sbom(manifest, (secret_component, components()[1]))


def test_checked_in_python_and_node_inputs_are_exact_and_script_disabled() -> None:
    root = Path(__file__).parents[2]
    audit = ReleaseInputAuditor().audit(root)
    assert audit.python_locked_packages > 1
    assert audit.node_locked_packages > 1
    assert len(audit.uv_lock_sha256) == 64
    assert len(audit.stata_mcp_lock_sha256) == 64
    assert len(audit.stata_mcp_artifact_sha256) == 64


def test_stata_mcp_artifact_lock_rejects_tampered_wheel(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    copied = tmp_path / "candidate"
    lock = json.loads((root / "build/stata-mcp.lock.json").read_text(encoding="utf-8"))
    artifact_path = str(lock["artifact_path"])
    for relative in (
        "pyproject.toml",
        "uv.lock",
        "web/package.json",
        "web/package-lock.json",
        "web/.npmrc",
        "build/stata-mcp.lock.json",
        artifact_path,
    ):
        source = root / relative
        destination = copied / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    wheel = copied / artifact_path
    wheel.write_bytes(wheel.read_bytes() + b"tampered")

    with pytest.raises(ReleaseSupplyChainError, match="hash does not match"):
        ReleaseInputAuditor().audit(copied)


def test_release_policy_exception_never_inherits_across_scope_or_expiry() -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    exception = ReleasePolicyException(
        "exception-1",
        ReleaseExceptionType.VULNERABILITY_RISK_ACCEPTANCE,
        "release-1",
        "component-a",
        "1.0.0",
        "a" * 64,
        "reachable path is mitigated",
        "reviewer@example.invalid",
        now + timedelta(days=1),
    )
    exact = {
        "release_id": "release-1",
        "component_name": "component-a",
        "component_version": "1.0.0",
        "artifact_sha256": "a" * 64,
    }
    assert exception_is_applicable(exception, **exact, observed_at=now)
    assert not exception_is_applicable(
        exception,
        **{**exact, "release_id": "release-2"},
        observed_at=now,
    )
    assert not exception_is_applicable(
        exception,
        **exact,
        observed_at=now + timedelta(days=2),
    )


def test_license_exception_can_close_review_but_never_override_deny() -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    component = components()[0]
    exception = ReleasePolicyException(
        "license-exception-1",
        ReleaseExceptionType.LICENSE_EXCEPTION,
        "release-1",
        component.name,
        component.version,
        component.artifact_sha256,
        "distribution terms reviewed",
        "legal-reviewer@example.invalid",
        None,
    )
    reviewed = ComponentLicenseDecision(
        component.name,
        component.version,
        component.artifact_sha256,
        component.license_expression,
        component.license_expression,
        "binary",
        False,
        (),
        LicensePolicyDecision.REVIEW_REQUIRED,
        "legal-reviewer@example.invalid",
        "manual distribution review",
    )
    enforce_license_gate(
        (component,),
        (reviewed,),
        (exception,),
        release_id="release-1",
        observed_at=now,
    )
    denied = ComponentLicenseDecision(
        component.name,
        component.version,
        component.artifact_sha256,
        component.license_expression,
        component.license_expression,
        "binary",
        False,
        (),
        LicensePolicyDecision.DENY,
        "legal-reviewer@example.invalid",
        "policy denial",
    )
    with pytest.raises(ReleaseSupplyChainError, match="denies"):
        enforce_license_gate(
            (component,),
            (denied,),
            (exception,),
            release_id="release-1",
            observed_at=now,
        )


def test_malware_is_unwaivable_and_stale_scanner_cannot_report_clean() -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    report = VulnerabilityReportBoundary(
        "osv-scanner",
        "2.0.0",
        "c" * 64,
        "offline-db-snapshot",
        now,
        "d" * 64,
        "PASS",
    )
    component = components()[0]
    finding = ReleaseSecurityFinding(
        "malware-fixture",
        "MALWARE",
        component.name,
        component.version,
        component.artifact_sha256,
        True,
    )
    exception = ReleasePolicyException(
        "exception-malware",
        ReleaseExceptionType.VULNERABILITY_RISK_ACCEPTANCE,
        "release-1",
        component.name,
        component.version,
        component.artifact_sha256,
        "must not waive malware",
        "reviewer@example.invalid",
        now + timedelta(days=1),
    )
    with pytest.raises(ReleaseSupplyChainError, match="unwaivable"):
        enforce_security_gate(
            report,
            (finding,),
            (exception,),
            release_id="release-1",
            observed_at=now,
            maximum_database_age=timedelta(days=7),
        )
    with pytest.raises(ReleaseSupplyChainError, match="stale"):
        enforce_security_gate(
            report,
            (),
            (),
            release_id="release-1",
            observed_at=now + timedelta(days=8),
            maximum_database_age=timedelta(days=7),
        )
