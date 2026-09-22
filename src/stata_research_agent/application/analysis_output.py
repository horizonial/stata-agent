"""Typed commands for classifying and adopting exploratory analysis output."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stata_research_agent.domain.analysis_output import AnalysisOutputKind
from stata_research_agent.domain.identifiers import (
    AnalysisDocumentEligibilityId,
    AnalysisOutputAdoptionId,
    AnalysisOutputClassificationId,
    AnalysisOutputId,
    ArtifactId,
    ArtifactVerificationReceiptId,
    CommandId,
    EnvironmentSnapshotId,
    EvidenceRecordId,
    OperationAttemptId,
    OperationId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision


@dataclass(frozen=True, slots=True)
class AnalysisArtifactBinding:
    artifact_id: ArtifactId
    role: str

    def __post_init__(self) -> None:
        if self.role not in {"primary", "preview", "supporting"}:
            raise ValueError("invalid Analysis Output Artifact role")


@dataclass(frozen=True, slots=True)
class AnalysisElement:
    stable_key: str
    value: Any
    rendered_text: str

    def __post_init__(self) -> None:
        if not self.stable_key.strip():
            raise ValueError("Analysis Element stable_key is required")


@dataclass(frozen=True, slots=True)
class ClassifyAnalysisOutputCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId
    operation_id: OperationId
    attempt_id: OperationAttemptId
    executable_artifact_id: ArtifactId
    input_artifact_ids: tuple[ArtifactId, ...]
    outputs: tuple[AnalysisArtifactBinding, ...]
    elements: tuple[AnalysisElement, ...]
    output_kind: AnalysisOutputKind
    method_summary: str
    environment: Mapping[str, Any]
    classification_basis: Mapping[str, Any]
    classifier_id: str = "agent-declared-reviewable"
    classifier_version: str = "1"

    def __post_init__(self) -> None:
        if not self.method_summary.strip() or not self.outputs:
            raise ValueError("method summary and at least one output are required")
        if len({item.artifact_id for item in self.outputs}) != len(self.outputs):
            raise ValueError("output Artifact identities must be unique")
        if len(set(self.input_artifact_ids)) != len(self.input_artifact_ids):
            raise ValueError("input Artifact identities must be unique")
        if len({item.stable_key for item in self.elements}) != len(self.elements):
            raise ValueError("Analysis Element stable keys must be unique")
        if not self.classifier_id.strip() or not self.classifier_version.strip():
            raise ValueError("classifier identity and version are required")


@dataclass(frozen=True, slots=True)
class AnalysisOutputIdentity:
    output_id: AnalysisOutputId
    environment_snapshot_id: EnvironmentSnapshotId
    classification_id: AnalysisOutputClassificationId


@dataclass(frozen=True, slots=True)
class ClassifiedAnalysisOutput:
    analysis_output_id: AnalysisOutputId
    classification_id: AnalysisOutputClassificationId
    output_kind: AnalysisOutputKind
    output_fingerprint: str
    document_eligible: bool
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class AdoptAnalysisOutputCommand:
    command_id: CommandId
    analysis_output_id: AnalysisOutputId
    adopted_by_turn_id: TurnId
    expected_output_fingerprint: str
    preview_artifact_id: ArtifactId
    verification_receipt_ids: tuple[ArtifactVerificationReceiptId, ...]
    confirmation_summary: str

    def __post_init__(self) -> None:
        if len(self.expected_output_fingerprint) != 64:
            raise ValueError("expected Analysis Output fingerprint must be sha256")
        if not self.confirmation_summary.strip():
            raise ValueError("confirmation summary is required")


@dataclass(frozen=True, slots=True)
class AnalysisAdoptionIdentity:
    adoption_id: AnalysisOutputAdoptionId
    evidence_record_id: EvidenceRecordId
    eligibility_id: AnalysisDocumentEligibilityId


@dataclass(frozen=True, slots=True)
class AdoptedAnalysisOutput:
    analysis_output_id: AnalysisOutputId
    adoption_id: AnalysisOutputAdoptionId
    evidence_record_id: EvidenceRecordId
    eligibility_id: AnalysisDocumentEligibilityId
    output_fingerprint: str
    commit_revision: WorkspaceRevision
    replayed: bool
