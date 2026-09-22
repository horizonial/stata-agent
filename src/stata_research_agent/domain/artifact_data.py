"""Framework-free Artifact and Data Version contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .identifiers import ArtifactId, ArtifactVerificationReceiptId, DataVersionId
from .revisions import WorkspaceRevision


class ArtifactKind(StrEnum):
    DATASET = "dataset"
    CODE = "code"
    LOG = "log"
    TABLE = "table"
    DOCUMENT = "document"
    DIAGNOSTIC = "diagnostic"


class DataVersionKind(StrEnum):
    EXTERNAL_IMPORT = "external_import"
    WORKING_CAPTURE = "working_capture"
    INTERNAL_CHECKPOINT = "internal_checkpoint"


class ArtifactAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    CORRUPT = "corrupt"
    DELETED = "deleted"


class VerificationPurpose(StrEnum):
    INITIAL_CAPTURE = "initial_capture"
    DATA_VERSION_CREATION = "data_version_creation"
    FORMAL_RUN_INPUT = "formal_run_input"
    RESULT_QUALIFICATION = "result_qualification"
    DOCUMENT_DELIVERY = "document_delivery"
    RECOVERY = "recovery"


class VerificationVerdict(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ArtifactVerification:
    receipt_id: ArtifactVerificationReceiptId
    artifact_id: ArtifactId
    purpose: VerificationPurpose
    verdict: VerificationVerdict
    availability: ArtifactAvailability
    expected_size: int
    observed_size: int | None
    expected_sha256: str
    observed_sha256: str | None
    commit_revision: WorkspaceRevision


@dataclass(frozen=True, slots=True)
class CapturedDataVersion:
    artifact_id: ArtifactId
    data_version_id: DataVersionId
    verification_receipt_id: ArtifactVerificationReceiptId
    content_sha256: str
    size_bytes: int
    managed_handle: str
    commit_revision: WorkspaceRevision
    replayed: bool
