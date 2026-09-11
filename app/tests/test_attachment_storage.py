from __future__ import annotations

from pathlib import Path

import pytest

from stata_agent.application.attachment_service import (
    ERROR_FILE_TOO_LARGE,
    AttachmentLimits,
    AttachmentService,
    ParseOutcome,
)
from stata_agent.storage.sqlite_store import SQLiteStore


class _ReadyParser:
    def parse(self, path, **kwargs):
        del path, kwargs
        return ParseOutcome(page_count=1, chunk_count=1, extracted_chars=12)


def test_attachment_schema_and_metadata_only_events(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"))
    service = AttachmentService(store, tmp_path / ".attachments", parser=_ReadyParser())
    record = service.intake(
        "workspace-a",
        "paper.pdf",
        [b"%PDF-", b"safe bytes"],
        declared_media_type="application/pdf",
        idempotency_key="upload-1",
    )

    assert store.schema_version == 4
    assert record.status == "ready"
    assert record.storage_key and not Path(record.storage_key).is_absolute()
    assert record.storage_key.startswith("objects/")
    assert store.connection.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1

    for event in store.scan("workspace-a"):
        assert event.event_type in {"attachment.intake.requested", "artifact.stored"}
        assert not {"filename", "display_name", "path", "text", "content", "exception"}.intersection(event.payload)
    store.close()


def test_attachment_workspace_isolation_and_idempotency(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"))
    service = AttachmentService(store, tmp_path / ".attachments", parser=_ReadyParser())
    first = service.intake("a", "one.pdf", b"%PDF-one", declared_media_type="application/pdf", idempotency_key="same")
    same = service.intake("a", "renamed.pdf", b"different", declared_media_type="application/pdf", idempotency_key="same")
    duplicate = service.intake("a", "renamed.pdf", b"%PDF-one", declared_media_type="application/pdf")
    foreign = service.intake("b", "foreign.pdf", b"%PDF-one", declared_media_type="application/pdf")

    assert same.attachment_id == first.attachment_id
    assert duplicate.attachment_id == first.attachment_id
    assert foreign.attachment_id != first.attachment_id
    assert service.get("a", foreign.attachment_id) is None
    assert service.get("b", first.attachment_id) is None
    store.close()


def test_attachment_filename_and_magic_are_rejected_without_path_escape(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"))
    service = AttachmentService(store, tmp_path / ".attachments", parser=_ReadyParser())
    with pytest.raises(ValueError):
        service.intake("w", "..\\escape.pdf", b"%PDF-x", declared_media_type="application/pdf")
    rejected = service.intake("w", "not-a-pdf.pdf", b"plain bytes", declared_media_type="application/pdf")
    assert rejected.status == "rejected"
    assert rejected.error_code == "magic_mismatch"
    assert not list((tmp_path / ".attachments").rglob("*.part"))
    store.close()


def test_attachment_size_limit_is_rejected_without_retaining_bytes(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"))
    service = AttachmentService(
        store,
        tmp_path / ".attachments",
        parser=_ReadyParser(),
        limits=AttachmentLimits(max_file_bytes=8),
    )
    rejected = service.intake(
        "workspace",
        "too-large.pdf",
        b"%PDF-" + b"x" * 8,
        declared_media_type="application/pdf",
    )

    assert rejected.status == "rejected"
    assert rejected.error_code == ERROR_FILE_TOO_LARGE
    assert not list((tmp_path / ".attachments").rglob("*.part"))
    assert not list((tmp_path / ".attachments").rglob("quarantine/*.pdf"))
    assert not list((tmp_path / ".attachments").rglob("objects/*/*.pdf"))
    store.close()
