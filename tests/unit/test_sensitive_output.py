"""Unified fail-closed Sensitive Output Gate."""

from __future__ import annotations

import base64
import io
import urllib.parse
import zipfile

import pytest

from stata_research_agent.application.sensitive_output import (
    SensitiveOutputGate,
    SensitiveOutputGateUnavailable,
)


def test_known_value_and_encoded_variants_are_redacted_without_secret_findings() -> None:
    gate = SensitiveOutputGate()
    secret = "sk-CaseSensitive-Canary-123456"
    candidate = " | ".join(
        (
            secret,
            secret.lower(),
            secret.upper(),
            urllib.parse.quote(secret, safe=""),
            base64.b64encode(secret.encode()).decode(),
            secret.encode().hex(),
        )
    )
    result = gate.inspect_text("provider.response", candidate, protected_values=(secret,))
    assert result.verdict == "redacted"
    assert result.finding_codes == ("KNOWN_PROTECTED_VALUE",)
    assert secret not in str(result.safe_value)
    assert "Canary" not in str(result.safe_value)


def test_generic_provider_private_key_url_and_stata_license_patterns_are_caught() -> None:
    gate = SensitiveOutputGate()
    candidate = {
        "provider": "Bearer abcdefghijklmnop",
        "key": "-----BEGIN PRIVATE KEY-----",
        "url": "https://user:password@provider.invalid/path",
        "stata": "Stata 18\nLicensed to: Person Name\nSerial: 123456789",
    }
    result = gate.inspect_json("diagnostic", candidate)
    assert result.verdict == "redacted"
    assert set(result.finding_codes) == {
        "AUTHORITY_URL",
        "BEARER_TOKEN",
        "PRIVATE_KEY",
        "STATA_LICENSE_IDENTITY",
    }
    assert "Person Name" not in str(result.safe_value)
    assert "password" not in str(result.safe_value)


def test_binary_boundary_blocks_instead_of_partially_rewriting() -> None:
    secret = "binary-secret-canary"
    result = SensitiveOutputGate().assert_clean_bytes(
        "artifact.delivery", b"prefix\x00" + secret.encode(), protected_values=(secret,)
    )
    assert result.verdict == "blocked"
    assert result.safe_value is None


def test_ooxml_archive_members_are_scanned_before_delivery() -> None:
    secret = "sk-ooxml-canary-123456789"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f"<w:t>{secret}</w:t>")
        archive.writestr("docProps/core.xml", "<title>safe</title>")
    result = SensitiveOutputGate().assert_clean_archive_bytes(
        "document.delivery", buffer.getvalue(), protected_values=(secret,)
    )
    assert result.verdict == "blocked"
    assert result.safe_value is None


def test_nested_archive_is_scanned_and_traversal_or_duplicate_members_fail_closed() -> None:
    secret = "nested-sensitive-canary"
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("payload.txt", secret)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("nested.zip", nested.getvalue())
    result = SensitiveOutputGate().assert_clean_archive_bytes(
        "diagnostic.bundle", outer.getvalue(), protected_values=(secret,)
    )
    assert result.verdict == "blocked"

    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape.txt", "safe")
    with pytest.raises(SensitiveOutputGateUnavailable, match="path is unsafe"):
        SensitiveOutputGate().assert_clean_archive_bytes("diagnostic.bundle", traversal.getvalue())

    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("member.txt", "one")
        archive.writestr("MEMBER.TXT", "two")
    with pytest.raises(SensitiveOutputGateUnavailable, match="duplicated"):
        SensitiveOutputGate().assert_clean_archive_bytes("diagnostic.bundle", duplicate.getvalue())


def test_gate_unavailable_fails_closed() -> None:
    gate = SensitiveOutputGate(available=False)
    with pytest.raises(SensitiveOutputGateUnavailable):
        gate.inspect_text("trace", "ordinary text")
    with pytest.raises(SensitiveOutputGateUnavailable):
        gate.assert_clean_bytes("artifact", b"ordinary bytes")
