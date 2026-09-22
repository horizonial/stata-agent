"""Commands and immutable facts for the registered esttab table export vertical."""

from __future__ import annotations

import re
from dataclasses import dataclass

from stata_research_agent.domain.identifiers import (
    ArtifactId,
    CommandId,
    EvidenceIssuanceReceiptId,
    EvidenceRecordId,
    ResearchPathId,
    ResultId,
    TableCellEvidenceUseId,
    TableCoverageManifestId,
    TableExportInputManifestId,
    TableExportManifestId,
    TableRenderReceiptId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.table_export import TableElement


@dataclass(frozen=True, slots=True)
class RegisterEsttabProfileCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId


@dataclass(frozen=True, slots=True)
class ExportEsttabTableCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId
    research_path_id: ResearchPathId
    result_slot_key: str
    title: str = "Baseline regression"
    coefficient_terms: tuple[str, ...] = ("mpg", "weight", "_cons")
    fit_statistic: str = "r2"
    fit_label: str = "R-squared"

    def __post_init__(self) -> None:
        if not self.result_slot_key.strip() or not self.title.strip():
            raise ValueError("result_slot_key and title are required")
        if '"' in self.title or "\n" in self.title or "\r" in self.title:
            raise ValueError("table title contains unsupported Stata command characters")
        safe_term = re.compile(r"^[A-Za-z0-9_:.#]+$")
        if not 1 <= len(self.coefficient_terms) <= 64:
            raise ValueError("between one and 64 coefficient terms are required")
        if len(set(self.coefficient_terms)) != len(self.coefficient_terms) or any(
            safe_term.fullmatch(term) is None for term in self.coefficient_terms
        ):
            raise ValueError("coefficient terms must be unique safe Stata terms")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.fit_statistic) is None:
            raise ValueError("fit statistic must be a safe Stata returned-scalar name")
        if self.fit_statistic == "N":
            raise ValueError("fit statistic cannot duplicate the built-in N statistic")
        if (
            not self.fit_label.strip()
            or not self.fit_label.isascii()
            or '"' in self.fit_label
            or "\n" in self.fit_label
            or "\r" in self.fit_label
        ):
            raise ValueError("fit label contains unsupported Stata command characters")


@dataclass(frozen=True, slots=True)
class PreparedTableExport:
    input_manifest_id: TableExportInputManifestId
    export_profile_id: str
    result_profile_id: str
    result_id: ResultId
    result_slot_id: str
    stata_run_id: str
    session_id: str
    session_generation: int
    data_state_token: str
    source_exec_seq: int
    stored_estimate_alias: str
    output_relative_path: str
    elements: tuple[TableElement, ...]
    coefficient_terms: tuple[str, ...]
    fit_statistic: str
    fit_label: str
    residual_degrees_of_freedom: float | None
    dependent_variable: str | None
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class CompletedTableArtifact:
    artifact_id: ArtifactId
    managed_handle: str
    size_bytes: int
    sha256: str
    actual_session_generation: int
    actual_data_state_token: str
    actual_exec_seq: int


@dataclass(frozen=True, slots=True)
class TableCellIdentity:
    evidence_record_candidate_id: EvidenceRecordId
    evidence_issuance_receipt_id: EvidenceIssuanceReceiptId
    table_cell_use_id: TableCellEvidenceUseId


@dataclass(frozen=True, slots=True)
class TableVerificationIdentity:
    export_manifest_id: TableExportManifestId
    render_receipt_id: TableRenderReceiptId
    coverage_manifest_id: TableCoverageManifestId
    cells: tuple[TableCellIdentity, ...]


@dataclass(frozen=True, slots=True)
class EsttabTableOutcome:
    input_manifest_id: TableExportInputManifestId
    export_manifest_id: TableExportManifestId
    render_receipt_id: TableRenderReceiptId
    coverage_manifest_id: TableCoverageManifestId
    table_artifact_id: ArtifactId
    estimation_state_gate: str
    cell_evidence_gate: str
    cell_count: int
    commit_revision: WorkspaceRevision
    replayed: bool
