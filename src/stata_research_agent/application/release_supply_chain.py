"""Typed release-candidate, payload, component, and policy-exception contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class PayloadClassification(StrEnum):
    EXECUTABLE = "executable"
    NATIVE_LIBRARY = "native_library"
    PYTHON_RUNTIME = "python_runtime"
    FRONTEND_ASSET = "frontend_asset"
    DEFAULT_SKILL = "default_skill"
    DATA = "data"


@dataclass(frozen=True, slots=True)
class PayloadEntry:
    relative_path: str
    size: int
    sha256: str
    classification: PayloadClassification
    architecture: str


@dataclass(frozen=True, slots=True)
class CanonicalPayloadManifest:
    release_id: str
    entries: tuple[PayloadEntry, ...]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ReleaseComponent:
    component_id: str
    name: str
    version: str
    component_type: str
    purl: str | None
    supplier: str
    source: str
    artifact_sha256: str
    license_expression: str
    modified: bool
    payload_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.artifact_sha256) != 64:
            raise ValueError("release component artifact hash is invalid")


class ReleaseExceptionType(StrEnum):
    INSTALL_SCRIPT = "INSTALL_SCRIPT"
    SDIST_BUILD = "SDIST_BUILD"
    VULNERABILITY_RISK_ACCEPTANCE = "VULNERABILITY_RISK_ACCEPTANCE"
    NONDETERMINISTIC_MEMBER = "NONDETERMINISTIC_MEMBER"
    LICENSE_EXCEPTION = "LICENSE_EXCEPTION"


@dataclass(frozen=True, slots=True)
class ReleasePolicyException:
    exception_id: str
    exception_type: ReleaseExceptionType
    release_id: str
    component_name: str
    component_version: str
    artifact_sha256: str
    reason: str
    reviewer: str
    expires_at: datetime | None
    member_path: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.exception_id,
            self.release_id,
            self.component_name,
            self.component_version,
            self.artifact_sha256,
            self.reason,
            self.reviewer,
        )
        if any(not value or "\n" in value or "\r" in value for value in required):
            raise ValueError("release exception scope is incomplete")
        if len(self.artifact_sha256) != 64:
            raise ValueError("release exception artifact hash is invalid")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("release exception expiry must be timezone-aware")
        if (
            self.exception_type == ReleaseExceptionType.NONDETERMINISTIC_MEMBER
            and self.member_path is None
        ):
            raise ValueError("nondeterministic member exception requires an exact path")


@dataclass(frozen=True, slots=True)
class VulnerabilityReportBoundary:
    scanner_name: str
    scanner_version: str
    scanner_sha256: str
    advisory_database_source: str
    advisory_database_retrieved_at: datetime
    scanned_input_sha256: str
    policy_result: str

    def __post_init__(self) -> None:
        if self.advisory_database_retrieved_at.tzinfo is None:
            raise ValueError("advisory database time must be timezone-aware")
        if self.policy_result not in {"PASS", "BLOCK", "INCONCLUSIVE"}:
            raise ValueError("vulnerability policy result is invalid")


@dataclass(frozen=True, slots=True)
class ReleaseSecurityFinding:
    finding_id: str
    finding_kind: str
    component_name: str
    component_version: str
    artifact_sha256: str
    blocking: bool


def enforce_security_gate(
    report: VulnerabilityReportBoundary,
    findings: tuple[ReleaseSecurityFinding, ...],
    exceptions: tuple[ReleasePolicyException, ...],
    *,
    release_id: str,
    observed_at: datetime,
    maximum_database_age: timedelta,
) -> None:
    if observed_at.tzinfo is None or maximum_database_age.total_seconds() <= 0:
        raise ValueError("security gate time policy is invalid")
    if report.policy_result != "PASS":
        raise ReleaseSupplyChainError("security scanner did not produce a passing result")
    if observed_at - report.advisory_database_retrieved_at > maximum_database_age:
        raise ReleaseSupplyChainError("security advisory database is stale")
    unwaivable = {"MALWARE", "INTEGRITY", "SIGNING", "TRUST_ROOT", "DYNAMIC_FETCH"}
    for finding in findings:
        if not finding.blocking:
            continue
        if finding.finding_kind in unwaivable:
            raise ReleaseSupplyChainError(
                f"unwaivable release security finding: {finding.finding_id}"
            )
        accepted = any(
            exception.exception_type == ReleaseExceptionType.VULNERABILITY_RISK_ACCEPTANCE
            and exception_is_applicable(
                exception,
                release_id=release_id,
                component_name=finding.component_name,
                component_version=finding.component_version,
                artifact_sha256=finding.artifact_sha256,
                observed_at=observed_at,
            )
            for exception in exceptions
        )
        if not accepted:
            raise ReleaseSupplyChainError(
                f"blocking vulnerability is unresolved: {finding.finding_id}"
            )


class ReleaseSupplyChainError(RuntimeError):
    pass


class LicensePolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class ComponentLicenseDecision:
    component_name: str
    component_version: str
    artifact_sha256: str
    declared_license: str
    concluded_license: str
    distribution_form: str
    modified: bool
    notice_obligations: tuple[str, ...]
    policy_decision: LicensePolicyDecision
    reviewer: str
    rationale: str


def enforce_license_gate(
    components: tuple[ReleaseComponent, ...],
    decisions: tuple[ComponentLicenseDecision, ...],
    exceptions: tuple[ReleasePolicyException, ...],
    *,
    release_id: str,
    observed_at: datetime,
) -> None:
    indexed = {
        (item.component_name, item.component_version, item.artifact_sha256): item
        for item in decisions
    }
    for component in components:
        key = (component.name, component.version, component.artifact_sha256)
        decision = indexed.get(key)
        if decision is None:
            raise ReleaseSupplyChainError(f"license decision missing for {component.name}")
        if decision.policy_decision == LicensePolicyDecision.DENY:
            raise ReleaseSupplyChainError(f"license policy denies {component.name}")
        if decision.policy_decision == LicensePolicyDecision.REVIEW_REQUIRED:
            allowed = any(
                exception.exception_type == ReleaseExceptionType.LICENSE_EXCEPTION
                and exception_is_applicable(
                    exception,
                    release_id=release_id,
                    component_name=component.name,
                    component_version=component.version,
                    artifact_sha256=component.artifact_sha256,
                    observed_at=observed_at,
                )
                for exception in exceptions
            )
            if not allowed:
                raise ReleaseSupplyChainError(
                    f"license review remains unresolved for {component.name}"
                )


def exception_is_applicable(
    exception: ReleasePolicyException,
    *,
    release_id: str,
    component_name: str,
    component_version: str,
    artifact_sha256: str,
    observed_at: datetime,
) -> bool:
    if observed_at.tzinfo is None:
        raise ValueError("release policy observation must be timezone-aware")
    return (
        exception.release_id == release_id
        and exception.component_name == component_name
        and exception.component_version == component_version
        and exception.artifact_sha256 == artifact_sha256
        and (exception.expires_at is None or observed_at <= exception.expires_at)
    )
