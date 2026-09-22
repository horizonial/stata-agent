"""Persistence port for minimal Word delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from stata_research_agent.application.document_delivery import (
    DeliverEsttabDocumentCommand,
    DocumentDeliveryIdentity,
    DocumentDeliveryOutcome,
    DocumentTableCell,
    DocxInspection,
    ManuscriptSections,
    PreparedDocumentRender,
    PublishedDocumentArtifact,
)
from stata_research_agent.domain.identifiers import (
    CommandId,
    DocumentId,
    OperationAttemptId,
    OperationId,
)


class DocumentDeliveryRepository(Protocol):
    def prepare_render(
        self,
        command: DeliverEsttabDocumentCommand,
        document_id: DocumentId,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
    ) -> PreparedDocumentRender: ...

    def finalize_delivery(
        self,
        command: DeliverEsttabDocumentCommand,
        prepared: PreparedDocumentRender,
        identities: DocumentDeliveryIdentity,
        docx: PublishedDocumentArtifact,
        manifest: PublishedDocumentArtifact,
        manifest_json: str,
        inspection: DocxInspection,
        finalization_command_id: CommandId,
    ) -> DocumentDeliveryOutcome: ...


class DocumentRenderer(Protocol):
    @property
    def profile_id(self) -> str: ...

    def render(
        self,
        rtf_payload: bytes,
        *,
        staging_root: Path,
        manuscript: ManuscriptSections | None = None,
    ) -> Path: ...

    def mark_evidence(
        self,
        payload: bytes,
        cells: tuple[DocumentTableCell, ...],
        *,
        document_revision_id: str,
    ) -> tuple[bytes, tuple[str, ...]]: ...

    def inspect(self, payload: bytes, expected_cell_texts: tuple[str, ...]) -> DocxInspection: ...
