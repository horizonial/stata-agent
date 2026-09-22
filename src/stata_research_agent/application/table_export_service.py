"""Orchestration for one-model registered esttab RTF export."""

from __future__ import annotations

import hashlib
import math
import re
import struct

from stata_research_agent.domain.identifiers import (
    CommandId,
    EvidenceIssuanceReceiptId,
    EvidenceRecordId,
    TableCellEvidenceUseId,
    TableCoverageManifestId,
    TableExportInputManifestId,
    TableExportManifestId,
    TableRenderReceiptId,
)
from stata_research_agent.domain.table_export import (
    cell_schema_for_result_profile,
    generic_cell_schema,
    verify_esttab_rtf,
)

from .ports.artifact_data import ManagedArtifactStore
from .ports.identity import IdentityGenerator
from .ports.table_export import TableExportRepository
from .stata_operation import ArtifactOutputExpectation, ExecuteStataCommand
from .stata_operation_service import StataOperationService
from .table_export import (
    EsttabTableOutcome,
    ExportEsttabTableCommand,
    RegisterEsttabProfileCommand,
    TableCellIdentity,
    TableVerificationIdentity,
)


class EsttabTableExportService:
    def __init__(
        self,
        repository: TableExportRepository,
        operation_service: StataOperationService,
        managed_store: ManagedArtifactStore,
        identities: IdentityGenerator,
    ) -> None:
        self._repository = repository
        self._operation_service = operation_service
        self._managed_store = managed_store
        self._identities = identities

    def register_builtin_profile(self, command: RegisterEsttabProfileCommand) -> None:
        self._repository.register_profile(command)

    async def export(self, command: ExportEsttabTableCommand) -> EsttabTableOutcome:
        reusable = self._repository.find_reusable_export(command)
        if reusable is not None:
            return reusable
        input_manifest_id = self._identities.new(TableExportInputManifestId)
        # estout/esttab internally derives temporary names from the stored-estimate
        # alias.  Long, otherwise-valid Stata names can collide with those derived
        # temporaries on Windows, so keep the system alias deliberately compact.
        alias = "__sra_" + hashlib.sha256(input_manifest_id.value.encode("utf-8")).hexdigest()[:8]
        relative_path = "tables/baseline-table.rtf"
        prepared = self._repository.prepare_export(
            command,
            input_manifest_id,
            alias,
            relative_path,
        )
        if prepared.result_profile_id == "stata.generic-result.v1":
            fit_statistic = prepared.fit_statistic
            fit_label = prepared.fit_label
        elif prepared.result_profile_id == "binary_model.logit.v1":
            fit_statistic = "r2_p"
            fit_label = "Pseudo R-squared"
        else:
            fit_statistic = "r2"
            fit_label = "R-squared"
        materialized = {item.semantic_key: item.binary64_bits for item in prepared.elements}
        coefficient_values = [
            self._stata_binary64(materialized[f"term.{term}.coefficient"])
            for term in prepared.coefficient_terms
        ]
        standard_errors = [
            self._stata_binary64(materialized[f"term.{term}.se"])
            for term in prepared.coefficient_terms
        ]
        n_key = "scalar.N" if "scalar.N" in materialized else "model.N"
        scalar_fit_key = f"scalar.{fit_statistic}"
        fit_key = (
            scalar_fit_key if scalar_fit_key in materialized else f"model.{fit_statistic}"
        )
        observations = self._stata_binary64(materialized[n_key])
        fit_value = self._stata_binary64(materialized[fit_key])
        dependent_variable = prepared.dependent_variable or "outcome"
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dependent_variable) is None:
            dependent_variable = "outcome"
        dof_option = ""
        if prepared.residual_degrees_of_freedom is not None:
            if not math.isfinite(prepared.residual_degrees_of_freedom):
                raise ValueError("residual degrees of freedom must be finite")
            dof_option = f" dof({prepared.residual_degrees_of_freedom:.17g})"
        variance_assignments = "\n".join(
            f"    matrix `__sra_V'[{index},{index}] = ({standard_error})^2"
            for index, standard_error in enumerate(standard_errors, start=1)
        )
        materializer = "__sra_m_" + prepared.stored_estimate_alias.removeprefix("__sra_")
        term_list = " ".join(prepared.coefficient_terms)
        code = (
            f"capture program drop {materializer}\n"
            f"program define {materializer}, eclass\n"
            "    tempname __sra_b __sra_V\n"
            f"    matrix `__sra_b' = ({','.join(coefficient_values)})\n"
            f"    matrix colnames `__sra_b' = {term_list}\n"
            f"    matrix `__sra_V' = J({len(coefficient_values)},"
            f"{len(coefficient_values)},0)\n"
            f"{variance_assignments}\n"
            f"    matrix rownames `__sra_V' = {term_list}\n"
            f"    matrix colnames `__sra_V' = {term_list}\n"
            f"    ereturn post `__sra_b' `__sra_V', obs({observations}){dof_option}\n"
            f"    ereturn scalar {fit_statistic} = {fit_value}\n"
            '    ereturn local cmd "regress"\n'
            f'    ereturn local depvar "{dependent_variable}"\n'
            "end\n"
            f"{materializer}\n"
            f"capture program drop {materializer}\n"
            f"estimates store {prepared.stored_estimate_alias}\n"
            f"esttab {prepared.stored_estimate_alias} using "
            f'"<ATTEMPT_STAGING>/{relative_path}", '
            "replace rtf b(%18.3f) se(%18.3f) "
            f"keep({' '.join(prepared.coefficient_terms)}) "
            f"stats(N {fit_statistic}, fmt(0 3) "
            f'labels("Observations" "{fit_label}")) '
            f'star(* 0.05 ** 0.01 *** 0.001) title("{command.title}")\n'
            f"estimates drop {prepared.stored_estimate_alias}"
        )
        operation = await self._operation_service.execute(
            ExecuteStataCommand(
                self._identities.new(CommandId),
                command.requested_by_turn_id,
                prepared.session_id,
                code,
                60,
                expected_outputs=(
                    ArtifactOutputExpectation(
                        "table.esttab.regression",
                        relative_path,
                        "table",
                        "application/rtf",
                    ),
                ),
            )
        )
        if operation.status != "completed" or operation.execution_status is None:
            raise RuntimeError("formal Table Export did not complete definitively")
        completed = self._repository.load_completed_artifact(prepared, operation)
        payload = self._managed_store.read_small_payload(
            completed.managed_handle, max_bytes=10 * 1024 * 1024
        )
        if len(payload) != completed.size_bytes:
            raise RuntimeError("managed Table Artifact size changed before verification")
        cell_schema = (
            generic_cell_schema(
                prepared.coefficient_terms,
                prepared.fit_statistic,
                prepared.fit_label,
            )
            if prepared.result_profile_id == "stata.generic-result.v1"
            else cell_schema_for_result_profile(prepared.result_profile_id)
        )
        verified = verify_esttab_rtf(
            payload,
            prepared.elements,
            cell_schema=cell_schema,
        )
        identities = TableVerificationIdentity(
            self._identities.new(TableExportManifestId),
            self._identities.new(TableRenderReceiptId),
            self._identities.new(TableCoverageManifestId),
            tuple(
                TableCellIdentity(
                    self._identities.new(EvidenceRecordId),
                    self._identities.new(EvidenceIssuanceReceiptId),
                    self._identities.new(TableCellEvidenceUseId),
                )
                for _ in verified.cells
            ),
        )
        return self._repository.commit_verification(
            command,
            prepared,
            operation,
            completed,
            verified,
            identities,
            self._identities.new(CommandId),
        )

    @staticmethod
    def _stata_binary64(bits: str) -> str:
        """Render an exact frozen binary64 value as a round-trippable Stata literal."""

        try:
            value = struct.unpack(">d", bytes.fromhex(bits))[0]
        except (ValueError, struct.error) as error:
            raise ValueError("invalid frozen binary64 table value") from error
        if not math.isfinite(value):
            raise ValueError("formal table values must be finite")
        return format(value, ".17g")
