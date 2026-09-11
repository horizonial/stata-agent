from __future__ import annotations

import multiprocessing
from pathlib import Path

from stata_agent.application.attachment_service import (
    ERROR_ENCRYPTED_PDF,
    ERROR_PARSER_CRASHED,
    ERROR_PARSER_TIMEOUT,
    AttachmentLimits,
    AttachmentService,
    ParseOutcome,
    SpawnPdfParser,
)
from stata_agent.storage.sqlite_store import SQLiteStore


class _Parser:
    def __init__(self, outcome: ParseOutcome):
        self.outcome = outcome

    def parse(self, path, **kwargs):
        del path, kwargs
        return self.outcome


def test_quarantine_and_bounded_reconcile(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"))
    service = AttachmentService(
        store,
        tmp_path / ".attachments",
        parser=_Parser(ParseOutcome(page_count=2, scanned_suspect=True)),
        limits=AttachmentLimits(quarantine_ttl_seconds=2, reconcile_batch=1),
    )
    record = service.intake("workspace", "scan.pdf", b"%PDF-scanned", declared_media_type="application/pdf")
    assert record.status == "quarantined"
    assert list((tmp_path / ".attachments").rglob(f"{record.attachment_id}.pdf"))

    changed = service.reconcile("workspace", now=record.expires_at + 1)  # type: ignore[operator]
    assert changed and changed[0].status == "rejected"
    assert not list((tmp_path / ".attachments").rglob(f"{record.attachment_id}.pdf"))
    store.close()


def test_parser_failure_is_stable_and_does_not_leak_exception(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"))

    class Crash:
        def parse(self, path, **kwargs):
            del path, kwargs
            raise RuntimeError("secret parser path and bytes")

    service = AttachmentService(store, tmp_path / ".attachments", parser=Crash())
    record = service.intake("workspace", "crash.pdf", b"%PDF-crash", declared_media_type="application/pdf")
    assert record.status == "quarantined"
    assert record.error_code == "parser_crashed"
    assert all("secret" not in str(event.payload) for event in store.scan("workspace"))
    store.close()


def test_default_spawn_parser_accepts_short_text_pdf(tmp_path: Path) -> None:
    import fitz

    pdf = tmp_path / "short.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "short text")
    document.save(str(pdf))
    document.close()
    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"))
    service = AttachmentService(store, tmp_path / ".attachments")
    record = service.intake("workspace", pdf.name, pdf.read_bytes(), declared_media_type="application/pdf")
    assert record.status == "ready"
    store.close()


def test_default_spawn_parser_quarantines_encrypted_pdf(tmp_path: Path) -> None:
    import fitz

    pdf = tmp_path / "encrypted.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "confidential text")
    document.save(
        str(pdf),
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-password",
        user_pw="user-password",
    )
    document.close()

    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"))
    service = AttachmentService(store, tmp_path / ".attachments")
    record = service.intake("workspace", pdf.name, pdf.read_bytes(), declared_media_type="application/pdf")

    assert record.status == "quarantined"
    assert record.error_code == ERROR_ENCRYPTED_PDF
    assert list((tmp_path / ".attachments").rglob(f"{record.attachment_id}.pdf"))
    assert not list((tmp_path / ".attachments").rglob("objects/*/*.pdf"))
    store.close()


def test_default_spawn_parser_quarantines_truncated_pdf_with_stable_error(tmp_path: Path) -> None:
    import fitz

    pdf = tmp_path / "truncated.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "text that will be truncated")
    document.save(str(pdf))
    document.close()
    content = pdf.read_bytes()
    pdf.write_bytes(content[: max(8, len(content) // 3)])

    store = SQLiteStore(str(tmp_path / "ledger.sqlite3"))
    service = AttachmentService(store, tmp_path / ".attachments")
    record = service.intake("workspace", pdf.name, pdf.read_bytes(), declared_media_type="application/pdf")

    assert record.status == "quarantined"
    assert record.error_code == ERROR_PARSER_CRASHED
    assert list((tmp_path / ".attachments").rglob(f"{record.attachment_id}.pdf"))
    store.close()


class _FakePipe:
    def __init__(self, payload=None):
        self.payload = payload
        self.closed = False

    def poll(self, timeout: float) -> bool:
        del timeout
        return self.payload is not None

    def recv(self):
        return self.payload

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, *, alive: bool):
        self.alive = alive
        self.terminated = False
        self.killed = False

    def start(self) -> None:
        return None

    def join(self, timeout: float) -> None:
        del timeout

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def kill(self) -> None:
        self.killed = True
        self.alive = False


class _FakeParserContext:
    def __init__(self, *, alive: bool, payload=None):
        self.parent = _FakePipe(payload)
        self.child = _FakePipe()
        self.process = _FakeProcess(alive=alive)

    def Pipe(self, *, duplex: bool):
        assert duplex is False
        return self.parent, self.child

    def Process(self, *, target, args):
        del target, args
        return self.process


def _spawn_parse_with_fake_context(monkeypatch, *, alive: bool, payload=None) -> tuple[ParseOutcome, _FakeParserContext]:
    context = _FakeParserContext(alive=alive, payload=payload)
    monkeypatch.setattr(multiprocessing, "get_context", lambda method: context)
    outcome = SpawnPdfParser(timeout_seconds=0.01).parse(
        "unused.pdf",
        attachment_id="attachment-1",
        source_role="style_only",
        max_pages=1,
        max_chunks=1,
        max_chars=100,
    )
    return outcome, context


def test_spawn_parser_timeout_is_stable_and_terminates_worker(monkeypatch) -> None:
    outcome, context = _spawn_parse_with_fake_context(monkeypatch, alive=True)

    assert outcome.error_code == ERROR_PARSER_TIMEOUT
    assert context.process.terminated is True
    assert context.process.alive is False


def test_spawn_parser_missing_payload_is_stable_crash(monkeypatch) -> None:
    outcome, context = _spawn_parse_with_fake_context(monkeypatch, alive=False)

    assert outcome.error_code == ERROR_PARSER_CRASHED
    assert context.process.terminated is False
