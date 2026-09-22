"""Pinned publisher identity is mandatory before invoking Windows trust APIs."""

from pathlib import Path

import pytest

from stata_research_agent.interfaces.windows_release_trust import (
    WindowsAuthenticodeEntryVerifier,
    WindowsCmsManifestSignatureVerifier,
)


def test_unprovisioned_publisher_identity_is_never_accepted(tmp_path: Path) -> None:
    thumbprint = "A" * 40
    cms = WindowsCmsManifestSignatureVerifier({"publisher-v1": thumbprint})
    authenticode = WindowsAuthenticodeEntryVerifier({"publisher-v1": thumbprint})
    entry = tmp_path / "agent.exe"
    entry.write_bytes(b"not-signed")
    assert not cms.verify(b"{}", b"not-cms", publisher_key_id="attacker-key")
    assert not authenticode.verify(entry, publisher_key_id="attacker-key")


def test_invalid_trust_root_thumbprint_is_rejected() -> None:
    with pytest.raises(ValueError, match="thumbprint"):
        WindowsCmsManifestSignatureVerifier({"publisher-v1": "not-a-thumbprint"})
