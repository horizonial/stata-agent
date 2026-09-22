"""Fail-closed sensitive output classification for every shared consumption boundary."""

from __future__ import annotations

import base64
import io
import re
import urllib.parse
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REDACTED = "[REDACTED_SENSITIVE_OUTPUT]"
_GENERIC_PATTERNS = (
    ("BEARER_TOKEN", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("API_TOKEN", re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}")),
    (
        "PRIVATE_KEY",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("AUTHORITY_URL", re.compile(r"(?i)https?://[^\s/:]+:[^\s/@]+@")),
    ("STATA_LICENSE_IDENTITY", re.compile(r"(?im)^(?:Licensed to|Serial):[^\r\n]+")),
)


class SensitiveOutputGateUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SensitiveOutputResult:
    surface: str
    verdict: str
    safe_value: Any
    finding_codes: tuple[str, ...]
    policy_revision: str


class SensitiveOutputGate:
    """Redact known/generic sensitive values without retaining matched material."""

    def __init__(
        self,
        *,
        policy_revision: str = "sensitive-output-v1",
        available: bool = True,
    ) -> None:
        if not policy_revision:
            raise ValueError("Sensitive Output policy revision is required")
        self._policy_revision = policy_revision
        self._available = available

    def inspect_text(
        self,
        surface: str,
        candidate: str,
        *,
        protected_values: Sequence[str] = (),
    ) -> SensitiveOutputResult:
        self._assert_available()
        safe = candidate
        findings: set[str] = set()
        for protected in protected_values:
            if not protected:
                continue
            for variant in self._protected_variants(protected):
                if variant in safe:
                    safe = safe.replace(variant, _REDACTED)
                    findings.add("KNOWN_PROTECTED_VALUE")
        for code, pattern in _GENERIC_PATTERNS:
            safe, count = pattern.subn(_REDACTED, safe)
            if count:
                findings.add(code)
        return SensitiveOutputResult(
            surface,
            "safe" if not findings else "redacted",
            safe,
            tuple(sorted(findings)),
            self._policy_revision,
        )

    def inspect_json(
        self,
        surface: str,
        candidate: Mapping[str, Any],
        *,
        protected_values: Sequence[str] = (),
    ) -> SensitiveOutputResult:
        self._assert_available()
        findings: set[str] = set()

        def visit(value: Any) -> Any:
            if isinstance(value, str):
                result = self.inspect_text(surface, value, protected_values=protected_values)
                findings.update(result.finding_codes)
                return result.safe_value
            if isinstance(value, Mapping):
                return {str(key): visit(item) for key, item in value.items()}
            if isinstance(value, list):
                return [visit(item) for item in value]
            if value is None or isinstance(value, (bool, int, float)):
                return value
            raise SensitiveOutputGateUnavailable(
                "Sensitive Output Gate cannot preserve the candidate structure"
            )

        safe = visit(candidate)
        return SensitiveOutputResult(
            surface,
            "safe" if not findings else "redacted",
            safe,
            tuple(sorted(findings)),
            self._policy_revision,
        )

    def assert_clean_bytes(
        self,
        surface: str,
        candidate: bytes,
        *,
        protected_values: Sequence[str] = (),
    ) -> SensitiveOutputResult:
        self._assert_available()
        findings: set[str] = set()
        lower = candidate.lower()
        for protected in protected_values:
            for variant in self._protected_variants(protected):
                encoded = variant.encode("utf-8")
                if encoded in candidate or encoded.lower() in lower:
                    findings.add("KNOWN_PROTECTED_VALUE")
        text = candidate.decode("utf-8", errors="ignore")
        for code, pattern in _GENERIC_PATTERNS:
            if pattern.search(text):
                findings.add(code)
        return SensitiveOutputResult(
            surface,
            "safe" if not findings else "blocked",
            None if findings else candidate,
            tuple(sorted(findings)),
            self._policy_revision,
        )

    def assert_clean_archive_bytes(
        self,
        surface: str,
        candidate: bytes,
        *,
        protected_values: Sequence[str] = (),
        max_uncompressed_bytes: int = 512 * 1024 * 1024,
        max_depth: int = 5,
    ) -> SensitiveOutputResult:
        self._assert_available()
        findings: set[str] = set()
        expanded = [0]

        def inspect_archive(payload: bytes, member_surface: str, depth: int) -> None:
            if depth > max_depth:
                raise SensitiveOutputGateUnavailable(
                    "Sensitive Output archive nesting limit exceeded"
                )
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    infos = archive.infolist()
                    if len(infos) > 10_000:
                        raise SensitiveOutputGateUnavailable(
                            "Sensitive Output archive member limit exceeded"
                        )
                    seen: set[str] = set()
                    for info in infos:
                        normalized = info.filename.replace("\\", "/")
                        parts = tuple(part for part in normalized.split("/") if part)
                        if (
                            not parts
                            or normalized.startswith("/")
                            or ":" in parts[0]
                            or ".." in parts
                        ):
                            raise SensitiveOutputGateUnavailable(
                                "Sensitive Output archive member path is unsafe"
                            )
                        identity = normalized.casefold()
                        if identity in seen:
                            raise SensitiveOutputGateUnavailable(
                                "Sensitive Output archive member is duplicated"
                            )
                        seen.add(identity)
                        expanded[0] += info.file_size
                        if expanded[0] > max_uncompressed_bytes:
                            raise SensitiveOutputGateUnavailable(
                                "Sensitive Output archive expansion limit exceeded"
                            )
                        member = archive.read(info)
                        inspected = self.assert_clean_bytes(
                            f"{member_surface}:{normalized}",
                            member,
                            protected_values=protected_values,
                        )
                        findings.update(inspected.finding_codes)
                        suffix = Path(normalized).suffix.lower()
                        if suffix in {".zip", ".docx", ".xlsx"} or member.startswith(b"PK\x03\x04"):
                            inspect_archive(member, f"{member_surface}:{normalized}", depth + 1)
            except zipfile.BadZipFile as error:
                raise SensitiveOutputGateUnavailable(
                    "Sensitive Output archive inspection failed"
                ) from error

        inspect_archive(candidate, surface, 1)
        return SensitiveOutputResult(
            surface,
            "safe" if not findings else "blocked",
            None if findings else candidate,
            tuple(sorted(findings)),
            self._policy_revision,
        )

    def assert_clean_file(
        self,
        surface: str,
        path: Path,
        *,
        protected_values: Sequence[str] = (),
    ) -> SensitiveOutputResult:
        self._assert_available()
        if path.suffix.lower() in {".docx", ".xlsx", ".zip"}:
            return self.assert_clean_archive_bytes(
                surface, path.read_bytes(), protected_values=protected_values
            )
        variants = tuple(
            variant.encode("utf-8")
            for secret in protected_values
            for variant in self._protected_variants(secret)
        )
        findings: set[str] = set()
        overlap = max((len(item) for item in variants), default=0)
        overlap = max(overlap, 4096)
        tail = b""
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                window = tail + chunk
                inspected = self.assert_clean_bytes(
                    surface, window, protected_values=protected_values
                )
                findings.update(inspected.finding_codes)
                tail = window[-overlap:]
        return SensitiveOutputResult(
            surface,
            "safe" if not findings else "blocked",
            None if findings else path,
            tuple(sorted(findings)),
            self._policy_revision,
        )

    def _assert_available(self) -> None:
        if not self._available:
            raise SensitiveOutputGateUnavailable("Sensitive Output Gate policy is unavailable")

    @staticmethod
    def _protected_variants(secret: str) -> tuple[str, ...]:
        variants = {
            secret,
            secret.lower(),
            secret.upper(),
            urllib.parse.quote(secret, safe=""),
            base64.b64encode(secret.encode("utf-8")).decode("ascii"),
            secret.encode("utf-8").hex(),
        }
        return tuple(sorted((item for item in variants if item), key=len, reverse=True))
