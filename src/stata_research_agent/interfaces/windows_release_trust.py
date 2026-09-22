"""Windows trust-root adapters for release CMS and Authenticode signatures."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path


class WindowsReleaseTrustError(RuntimeError):
    pass


class _PinnedPublisherTrust:
    def __init__(self, publisher_thumbprints: Mapping[str, str]) -> None:
        normalized = {
            key: self._normalize_thumbprint(value) for key, value in publisher_thumbprints.items()
        }
        if not normalized or any(not key for key in normalized):
            raise ValueError("at least one pinned publisher identity is required")
        self._publishers = normalized

    def expected_thumbprint(self, publisher_key_id: str) -> str | None:
        return self._publishers.get(publisher_key_id)

    @staticmethod
    def _normalize_thumbprint(value: str) -> str:
        compact = "".join(value.split()).upper()
        if len(compact) != 40 or any(character not in "0123456789ABCDEF" for character in compact):
            raise ValueError("publisher certificate thumbprint must be SHA-1 hex")
        return compact


class WindowsCmsManifestSignatureVerifier(_PinnedPublisherTrust):
    """Verify detached CMS bytes and bind the signer to a provisioned trust root."""

    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security.Cryptography.Pkcs
$content = [IO.File]::ReadAllBytes($args[0])
$signature = [IO.File]::ReadAllBytes($args[1])
$info = [System.Security.Cryptography.Pkcs.ContentInfo]::new($content)
$cms = [System.Security.Cryptography.Pkcs.SignedCms]::new($info, $true)
$cms.Decode($signature)
$cms.CheckSignature($true)
if ($cms.SignerInfos.Count -ne 1) { throw 'exactly one signer is required' }
$certificate = $cms.SignerInfos[0].Certificate
if ($null -eq $certificate) { throw 'signer certificate is unavailable' }
[pscustomobject]@{ thumbprint = $certificate.Thumbprint } | ConvertTo-Json -Compress
"""

    def verify(
        self,
        manifest_bytes: bytes,
        signature_bytes: bytes,
        *,
        publisher_key_id: str,
    ) -> bool:
        expected = self.expected_thumbprint(publisher_key_id)
        if expected is None or os.name != "nt":
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="sra-release-trust-") as temporary:
                root = Path(temporary)
                manifest = root / "manifest.json"
                signature = root / "manifest.p7s"
                manifest.write_bytes(manifest_bytes)
                signature.write_bytes(signature_bytes)
                value = _run_powershell_json(self._SCRIPT, manifest, signature)
            observed = value.get("thumbprint")
            return isinstance(observed, str) and self._normalize_thumbprint(observed) == expected
        except (OSError, ValueError, WindowsReleaseTrustError):
            return False


class WindowsAuthenticodeEntryVerifier(_PinnedPublisherTrust):
    _SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $args[0]
if ($signature.Status -ne 'Valid') { throw ('invalid Authenticode: ' + $signature.Status) }
if ($null -eq $signature.SignerCertificate) { throw 'signer certificate is unavailable' }
[pscustomobject]@{ thumbprint = $signature.SignerCertificate.Thumbprint } |
    ConvertTo-Json -Compress
"""

    def verify(self, entry_path: Path, *, publisher_key_id: str) -> bool:
        expected = self.expected_thumbprint(publisher_key_id)
        if expected is None or os.name != "nt" or not entry_path.is_file():
            return False
        try:
            value = _run_powershell_json(self._SCRIPT, entry_path)
            observed = value.get("thumbprint")
            return isinstance(observed, str) and self._normalize_thumbprint(observed) == expected
        except (OSError, ValueError, WindowsReleaseTrustError):
            return False


def _run_powershell_json(script: str, *paths: Path) -> dict[str, object]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise WindowsReleaseTrustError("Windows PowerShell is unavailable")
    completed = subprocess.run(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "AllSigned",
            "-Command",
            script,
            *[str(path.resolve()) for path in paths],
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        errors="strict",
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise WindowsReleaseTrustError("Windows release signature verification failed")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise WindowsReleaseTrustError("Windows trust verifier returned invalid output") from error
    if not isinstance(value, dict):
        raise WindowsReleaseTrustError("Windows trust verifier returned an invalid shape")
    return value
