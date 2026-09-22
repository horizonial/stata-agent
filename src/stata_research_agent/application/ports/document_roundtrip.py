"""Persistence port for human Word roundtrips."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.document_delivery import PublishedDocumentArtifact
from stata_research_agent.application.document_roundtrip import (
    DocumentMergeIdentity,
    DocumentMergeOutcome,
    DocumentReturnIdentity,
    DocumentReturnOutcome,
    DocumentSemanticDiff,
    DocumentThreeWayMerge,
    FinalizedDocumentReturn,
    ImportReturnedDocumentCommand,
    MergeDocumentRevisionsCommand,
    PreparedDocumentMerge,
    PreparedDocumentReturn,
    RejectedDocumentReturn,
)
from stata_research_agent.domain.identifiers import (
    DocumentReturnReportId,
    OperationAttemptId,
    OperationId,
)


class DocumentRoundtripRepository(Protocol):
    def prepare_return(
        self,
        command: ImportReturnedDocumentCommand,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
    ) -> PreparedDocumentReturn: ...

    def finalize_return(
        self,
        command: ImportReturnedDocumentCommand,
        prepared: PreparedDocumentReturn,
        identity: DocumentReturnIdentity,
        payloads: FinalizedDocumentReturn,
        diff: DocumentSemanticDiff,
    ) -> DocumentReturnOutcome: ...

    def reject_return(
        self,
        command: ImportReturnedDocumentCommand,
        prepared: PreparedDocumentReturn,
        return_report_id: DocumentReturnReportId,
        raw_docx: PublishedDocumentArtifact,
        finding_code: str,
        detail: str,
    ) -> RejectedDocumentReturn: ...

    def prepare_merge(
        self,
        command: MergeDocumentRevisionsCommand,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
    ) -> PreparedDocumentMerge: ...

    def finalize_merge(
        self,
        command: MergeDocumentRevisionsCommand,
        prepared: PreparedDocumentMerge,
        identity: DocumentMergeIdentity,
        payloads: FinalizedDocumentReturn,
        merge: DocumentThreeWayMerge,
    ) -> DocumentMergeOutcome: ...


class DocumentRoundtripEngine(Protocol):
    def compare(self, base_payload: bytes, returned_payload: bytes) -> DocumentSemanticDiff: ...

    def merge(
        self, base_payload: bytes, human_payload: bytes, agent_payload: bytes
    ) -> DocumentThreeWayMerge: ...
