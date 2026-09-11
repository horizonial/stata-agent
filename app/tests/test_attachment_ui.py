from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from stata_agent import ui
from stata_agent.application.attachment_service import (
    ERROR_PARSER_TIMEOUT,
    AttachmentLimits,
    AttachmentService,
    ParseOutcome,
)
from stata_agent.storage.sqlite_store import SQLiteStore


class _TextPdfParser:
    def parse(self, path: Path, **kwargs) -> ParseOutcome:
        del path, kwargs
        return ParseOutcome(
            parser_version=1,
            page_count=1,
            chunk_count=1,
            extracted_chars=32,
            scanned_suspect=False,
        )


def _install_service(monkeypatch, tmp_path: Path, *, parser=None, limits=None) -> None:
    database = tmp_path / "ledger.sqlite3"
    root = tmp_path / ".attachments"

    @contextmanager
    def open_service():
        store = SQLiteStore(str(database), takeover=True)
        try:
            yield AttachmentService(
                store,
                root,
                parser=parser if parser is not None else _TextPdfParser(),
                limits=limits,
            )
        finally:
            store.close()

    monkeypatch.setattr(ui, "_open_attachment_service", open_service)
    monkeypatch.setattr(ui, "_resolve_workspace", lambda value: value or "ui")
    monkeypatch.setattr(ui, "_workspace_id", lambda idea="ui": f"sha256:{idea}")


def test_upload_list_and_idempotent_duplicate(monkeypatch, tmp_path: Path) -> None:
    _install_service(monkeypatch, tmp_path)
    client = TestClient(ui.app)
    headers = {
        "x-attachment-filename": quote("研究文献.pdf"),
        "x-attachment-role": "citable_evidence",
        "x-idempotency-key": "upload-1",
        "content-type": "application/pdf",
    }
    first = client.post("/api/attachments?ws=ui", content=b"%PDF-1.4\ntext", headers=headers)
    assert first.status_code == 201
    body = first.json()
    assert body["attachment"]["status"] == "ready"
    assert body["attachment"]["display_name"] == "研究文献.pdf"
    assert body["attachment"]["retryable"] is False
    assert "storage_key" not in body["attachment"]
    assert "sha256" not in body["attachment"]

    duplicate = client.post("/api/attachments?ws=ui", content=b"different", headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["attachment"]["attachment_id"] == body["attachment"]["attachment_id"]

    listed = client.get("/api/attachments?ws=ui&limit=10")
    assert listed.status_code == 200
    assert [item["attachment_id"] for item in listed.json()["items"]] == [body["attachment"]["attachment_id"]]


def test_upload_rejection_is_stable_and_non_disclosing(monkeypatch, tmp_path: Path) -> None:
    _install_service(monkeypatch, tmp_path)
    client = TestClient(ui.app)
    response = client.post(
        "/api/attachments?ws=ui",
        content=b"not a pdf C:/Users/private/secret.dta",
        headers={
            "x-attachment-filename": quote("paper.pdf"),
            "x-idempotency-key": "upload-bad",
            "content-type": "application/pdf",
        },
    )
    assert response.status_code == 415
    payload = response.json()
    assert payload["attachment"]["status"] == "rejected"
    assert payload["attachment"]["error_code"] == "magic_mismatch"
    assert payload["attachment"]["retryable"] is False
    assert "C:/Users" not in response.text
    assert "storage_key" not in response.text


def test_upload_declared_oversize_is_stable_and_not_retryable(monkeypatch, tmp_path: Path) -> None:
    _install_service(monkeypatch, tmp_path, limits=AttachmentLimits(max_file_bytes=8))
    client = TestClient(ui.app)
    response = client.post(
        "/api/attachments?ws=ui",
        content=b"%PDF-" + b"x" * 8,
        headers={
            "x-attachment-filename": quote("too-large.pdf"),
            "content-type": "application/pdf",
        },
    )

    assert response.status_code == 413
    payload = response.json()
    assert payload["error"]["code"] == "http_413"
    assert payload["error"].get("retryable") is not True
    assert "attachment" not in payload
    assert not list((tmp_path / ".attachments").rglob("*.part"))


class _TimeoutParser:
    def parse(self, path: Path, **kwargs) -> ParseOutcome:
        del path, kwargs
        return ParseOutcome(error_code=ERROR_PARSER_TIMEOUT)


def test_quarantine_status_survives_refresh_with_stable_error_code(monkeypatch, tmp_path: Path) -> None:
    _install_service(monkeypatch, tmp_path, parser=_TimeoutParser())
    client = TestClient(ui.app)
    headers = {
        "x-attachment-filename": quote("parser-timeout.pdf"),
        "content-type": "application/pdf",
    }
    uploaded = client.post("/api/attachments?ws=ui", content=b"%PDF-timeout", headers=headers)

    assert uploaded.status_code == 202
    item = uploaded.json()["attachment"]
    assert item["status"] == "quarantined"
    assert item["error_code"] == ERROR_PARSER_TIMEOUT
    assert item["retryable"] is True

    refreshed = client.get("/api/attachments?ws=ui&limit=10")
    assert refreshed.status_code == 200
    assert refreshed.json()["items"] == [item]
