"""Read-only release readiness audit for exact local candidate builds.

This module deliberately stops before creating an immutable Release Candidate.  It
collects facts from two independently built candidate directories and reports the
remaining release prerequisites without turning missing evidence into a passing
result.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from stata_research_agent.interfaces.release_evidence import CanonicalPayloadScanner


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    check_id: str
    gate: str
    passed: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class ReleaseReadinessReport:
    schema_version: str
    release_id: str
    candidate_a: str
    candidate_b: str
    source_revision: str
    unsigned_payload_sha256: str
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready_to_register(self) -> bool:
        return all(item.passed for item in self.checks if item.gate == "G0")

    @property
    def ready_for_g7(self) -> bool:
        return all(item.passed for item in self.checks)

    def to_json(self) -> str:
        value = asdict(self)
        value["ready_to_register"] = self.ready_to_register
        value["ready_for_g7"] = self.ready_for_g7
        return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)


class ReleaseReadinessError(RuntimeError):
    pass


class LocalReleaseReadinessAuditor:
    """Audit exact candidate inputs without changing either candidate or a ledger."""

    _RECEIPT = Path("evidence/candidate-build-receipt.json")
    _PAYLOAD_MANIFEST = Path("evidence/payload-manifest.json")

    def audit(
        self,
        project_root: Path,
        candidate_a: Path,
        candidate_b: Path,
    ) -> ReleaseReadinessReport:
        project = project_root.resolve()
        first = candidate_a.resolve()
        second = candidate_b.resolve()
        first_receipt = self._json_object(first / self._RECEIPT)
        second_receipt = self._json_object(second / self._RECEIPT)
        first_manifest = self._json_object(first / self._PAYLOAD_MANIFEST)
        second_manifest = self._json_object(second / self._PAYLOAD_MANIFEST)

        release_id = self._text(first_receipt, "release_id")
        payload_sha = self._text(first_receipt, "payload_manifest_sha256")
        source_sha = self._text(first_receipt, "source_manifest_sha256")
        build_sha = self._text(first_receipt, "build_input_sha256")
        source_revision, signature_good, source_clean = self._git_facts(project)

        checks = [
            ReadinessCheck(
                "candidate.release_identity_equal",
                "G0",
                release_id == second_receipt.get("release_id"),
                "PASS" if release_id == second_receipt.get("release_id") else "RELEASE_ID_DIFFERS",
            ),
            ReadinessCheck(
                "candidate.source_manifest_equal",
                "G0",
                source_sha == second_receipt.get("source_manifest_sha256"),
                "PASS"
                if source_sha == second_receipt.get("source_manifest_sha256")
                else "SOURCE_MANIFEST_DIFFERS",
            ),
            ReadinessCheck(
                "candidate.build_input_equal",
                "G0",
                build_sha == second_receipt.get("build_input_sha256"),
                "PASS"
                if build_sha == second_receipt.get("build_input_sha256")
                else "BUILD_INPUT_DIFFERS",
            ),
            ReadinessCheck(
                "candidate.payload_manifest_equal",
                "G0",
                payload_sha == second_receipt.get("payload_manifest_sha256"),
                "PASS"
                if payload_sha == second_receipt.get("payload_manifest_sha256")
                else "PAYLOAD_MANIFEST_DIFFERS",
            ),
            self._payload_check(first, first_manifest, release_id, "candidate_a"),
            self._payload_check(second, second_manifest, release_id, "candidate_b"),
        ]
        production = (
            first_receipt.get("production_release") is True
            and second_receipt.get("production_release") is True
        )
        checks.append(
            ReadinessCheck(
                "candidate.production_release",
                "G0",
                production,
                "PASS" if production else "REHEARSAL_BUILD_ONLY",
            )
        )
        checks.extend(
            (
                ReadinessCheck(
                    "source.signed_revision",
                    "G0",
                    signature_good,
                    "PASS" if signature_good else "SOURCE_REVISION_NOT_SIGNED",
                ),
                ReadinessCheck(
                    "source.clean_candidate_inputs",
                    "G0",
                    source_clean,
                    "PASS" if source_clean else "CANDIDATE_INPUTS_NOT_CLEAN",
                ),
                self._file_check(
                    project / "build" / "python-wheelhouse.lock.json",
                    "toolchain.python_wheelhouse_lock",
                    "G0",
                    "PYTHON_WHEELHOUSE_LOCK_MISSING",
                ),
                self._file_check(
                    project / "build" / "build-environment.lock.json",
                    "toolchain.build_environment_lock",
                    "G0",
                    "BUILD_ENVIRONMENT_LOCK_MISSING",
                ),
                self._file_check(
                    project / "verification" / "release" / "vulnerability-report.json",
                    "supply_chain.vulnerability_report",
                    "G5",
                    "VULNERABILITY_REPORT_MISSING",
                ),
                self._file_check(
                    project / "verification" / "release" / "license-decisions.json",
                    "supply_chain.license_decisions",
                    "G5",
                    "LICENSE_DECISIONS_MISSING",
                ),
            )
        )
        installer = self._find_iscc()
        checks.append(
            ReadinessCheck(
                "distribution.inno_setup",
                "G7",
                installer is not None,
                "PASS" if installer is not None else "INSTALLER_TOOLCHAIN_MISSING",
            )
        )
        signer_configured = bool(
            os.environ.get("SRA_PUBLISHER_KEY_ID")
            and os.environ.get("SRA_PUBLISHER_CERT_THUMBPRINT")
        )
        checks.append(
            ReadinessCheck(
                "distribution.publisher_identity",
                "G7",
                signer_configured,
                "PASS" if signer_configured else "PUBLISHER_IDENTITY_NOT_CONFIGURED",
            )
        )
        return ReleaseReadinessReport(
            "stata-research-agent.release-readiness/v1",
            release_id,
            str(first),
            str(second),
            source_revision,
            payload_sha,
            tuple(checks),
        )

    def _payload_check(
        self,
        candidate: Path,
        manifest: dict[str, Any],
        release_id: str,
        label: str,
    ) -> ReadinessCheck:
        observed = CanonicalPayloadScanner().scan(candidate / "payload", release_id=release_id)
        expected = manifest.get("manifest_sha256")
        passed = observed.manifest_sha256 == expected
        return ReadinessCheck(
            f"{label}.payload_integrity",
            "G0",
            passed,
            "PASS" if passed else "PAYLOAD_INTEGRITY_MISMATCH",
        )

    @staticmethod
    def _file_check(path: Path, check_id: str, gate: str, missing: str) -> ReadinessCheck:
        passed = path.is_file() and path.stat().st_size > 0
        return ReadinessCheck(check_id, gate, passed, "PASS" if passed else missing)

    @staticmethod
    def _json_object(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReleaseReadinessError(f"release evidence is unreadable: {path}") from error
        if not isinstance(value, dict):
            raise ReleaseReadinessError(f"release evidence is not an object: {path}")
        return value

    @staticmethod
    def _text(value: dict[str, Any], key: str) -> str:
        result = value.get(key)
        if not isinstance(result, str) or not result:
            raise ReleaseReadinessError(f"release evidence field is invalid: {key}")
        return result

    @staticmethod
    def _git_facts(project: Path) -> tuple[str, bool, bool]:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if root.returncode != 0:
            return "unavailable", False, False
        repository = Path(root.stdout.strip()).resolve()
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        signature = subprocess.run(
            ["git", "verify-commit", "HEAD"],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        relative = project.relative_to(repository).as_posix()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--", relative],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        revision_text = revision.stdout.strip() if revision.returncode == 0 else "unavailable"
        return (
            revision_text,
            signature.returncode == 0,
            status.returncode == 0 and not status.stdout,
        )

    @staticmethod
    def _find_iscc() -> Path | None:
        direct = shutil.which("ISCC.exe") or shutil.which("iscc")
        if direct:
            return Path(direct).resolve()
        for environment_name in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(environment_name)
            if not base:
                continue
            candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
            if candidate.is_file():
                return candidate.resolve()
        return None


def sha256_file(path: Path) -> str:
    """Return a streaming hash for release scripts that must not load large evidence."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
