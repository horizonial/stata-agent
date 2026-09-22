"""Composition service for the minimal trustworthy data → Stata → Word journey."""

from __future__ import annotations

from stata_research_agent.domain.identifiers import CommandId

from .artifact_data import AdoptPathDataCommand
from .artifact_service import ArtifactDataService
from .document_delivery import DeliverEsttabDocumentCommand
from .document_delivery_service import DocumentDeliveryService
from .evidence import AdoptPathResultCommand
from .evidence_service import EvidenceService
from .ports.identity import IdentityGenerator
from .research_workflow import ResearchToWordOutcome, RunResearchToWordCommand
from .result_profile import (
    PromoteStataResultCommand,
    RegisterGenericStataResultProfileCommand,
)
from .result_profile_service import RegisteredResultProfileService
from .stata_operation import ExecuteStataCommand
from .stata_operation_service import StataOperationService
from .table_export import ExportEsttabTableCommand, RegisterEsttabProfileCommand
from .table_export_service import EsttabTableExportService


class ResearchToWordService:
    def __init__(
        self,
        stata: StataOperationService,
        profiles: RegisteredResultProfileService,
        artifacts: ArtifactDataService,
        evidence: EvidenceService,
        tables: EsttabTableExportService,
        documents: DocumentDeliveryService,
        identities: IdentityGenerator,
    ) -> None:
        self._stata = stata
        self._profiles = profiles
        self._artifacts = artifacts
        self._evidence = evidence
        self._tables = tables
        self._documents = documents
        self._identities = identities

    async def run(self, command: RunResearchToWordCommand) -> ResearchToWordOutcome:
        self._profiles.register_generic_profile(
            RegisterGenericStataResultProfileCommand(
                self._identities.new(CommandId), command.requested_by_turn_id
            )
        )
        self._tables.register_builtin_profile(
            RegisterEsttabProfileCommand(
                self._identities.new(CommandId), command.requested_by_turn_id
            )
        )
        loaded = await self._stata.execute(
            ExecuteStataCommand(
                self._identities.new(CommandId),
                command.requested_by_turn_id,
                command.session_id,
                f'use "{command.managed_data_handle}", clear',
                input_data_version_id=command.data_version_id,
                input_data_slot_key=command.data_slot_key,
                input_verification_receipt_id=command.verification_receipt_id,
                execution_purpose="data_load",
            )
        )
        if loaded.status != "completed":
            raise RuntimeError(f"Stata data load did not complete: {loaded.status}")
        binding = self._stata.session_data_binding(loaded.operation_id)
        regression_command = "regress " + " ".join((command.dependent_variable, *command.terms))
        estimated = await self._stata.execute(
            ExecuteStataCommand(
                self._identities.new(CommandId),
                command.requested_by_turn_id,
                command.session_id,
                regression_command,
                input_data_version_id=command.data_version_id,
                input_data_slot_key=command.data_slot_key,
                input_verification_receipt_id=command.verification_receipt_id,
                execution_purpose="formal_estimation",
                source_data_state_operation_id=(binding.source_data_state_operation_id),
                expected_data_state_token=binding.data_state_token,
                expected_session_generation=binding.session_generation,
            )
        )
        if estimated.status != "completed":
            raise RuntimeError(f"formal Stata estimation did not complete: {estimated.status}")
        selected_source_keys = tuple(
            key
            for term in (*command.terms, "_cons")
            for key in (f"term.{term}.coefficient", f"term.{term}.se")
        ) + ("scalar.N", "scalar.r2")
        qualified = self._profiles.promote(
            PromoteStataResultCommand(
                self._identities.new(CommandId),
                estimated.operation_id,
                command.requested_by_turn_id,
                command.research_path_id,
                selected_source_keys,
                "Minimal trustworthy data to Stata to Word regression result.",
                command.plan_revision_id,
                command.plan_node_id,
            )
        )
        if qualified.result_id is None:
            raise RuntimeError(
                "formal Result qualification failed: " + ", ".join(qualified.findings)
            )
        self._artifacts.adopt_path_data(
            AdoptPathDataCommand(
                self._identities.new(CommandId),
                command.research_path_id,
                command.data_slot_key,
                command.data_version_id,
                command.expected_data_pointer_revision,
            )
        )
        self._evidence.adopt_path_result(
            AdoptPathResultCommand(
                self._identities.new(CommandId),
                command.research_path_id,
                command.result_slot_key,
                qualified.result_id,
                command.requested_by_turn_id,
                command.expected_result_pointer_revision,
            )
        )
        table = await self._tables.export(
            ExportEsttabTableCommand(
                self._identities.new(CommandId),
                command.requested_by_turn_id,
                command.research_path_id,
                command.result_slot_key,
                command.document_title,
                (command.terms[0], command.terms[1], "_cons"),
                "r2",
                "R-squared",
            )
        )
        if table.estimation_state_gate != "pass" or table.cell_evidence_gate != "pass":
            raise RuntimeError("esttab table did not pass both formal delivery gates")
        delivered = self._documents.deliver(
            DeliverEsttabDocumentCommand(
                self._identities.new(CommandId),
                command.requested_by_turn_id,
                command.research_path_id,
                table.render_receipt_id,
                command.expected_working_document_pointer_revision,
                command.expected_delivery_document_pointer_revision,
            )
        )
        if delivered.verdict != "pass":
            raise RuntimeError("Word delivery gate failed")
        return ResearchToWordOutcome(
            qualified.result_id,
            table.render_receipt_id,
            delivered.document_revision_id,
            delivered.docx_artifact_id.value,
            delivered.verdict,
        )
