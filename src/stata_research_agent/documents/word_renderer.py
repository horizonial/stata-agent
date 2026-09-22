"""Microsoft Word RTF-to-DOCX adapter and fail-closed OOXML inspection."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from stata_research_agent.application.document_delivery import (
    DocumentTableCell,
    DocxInspection,
    ManuscriptSections,
)

from .roundtrip import inject_evidence_markers


class MicrosoftWordRtfRenderer:
    profile_id = "word.rtf-to-docx.v1"

    def render(
        self,
        rtf_payload: bytes,
        *,
        staging_root: Path,
        manuscript: ManuscriptSections | None = None,
    ) -> Path:
        rtf_path = staging_root / "source-table.rtf"
        docx_path = staging_root / "stata-results.docx"
        # esttab emits a 12 pt default table.  A 10 pt presentation copy keeps
        # the table and its source notes together in ordinary manuscript pages;
        # only font controls change and the already-verified cell text remains
        # byte-for-byte identical.
        rtf_path.write_bytes(rtf_payload.replace(b"\\fs24", b"\\fs20"))
        script = f"""
$ErrorActionPreference = 'Stop'
$inputPath = '{self._ps_quote(str(rtf_path.resolve()))}'
$outputPath = '{self._ps_quote(str(docx_path.resolve()))}'
$word = $null
$document = $null
try {{
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $document = $word.Documents.Open($inputPath, $false, $true)
  $document.SaveAs2($outputPath, 16)
  $document.Close($false)
  $document = $null
}} finally {{
  if ($null -ne $document) {{ $document.Close($false) }}
  if ($null -ne $word) {{
    $word.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
  }}
  [GC]::Collect()
  [GC]::WaitForPendingFinalizers()
}}
"""
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            check=False,
            capture_output=True,
            timeout=90,
        )
        if completed.returncode != 0 or not docx_path.is_file():
            detail = completed.stderr.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"Microsoft Word RTF conversion failed: {detail}")
        if manuscript is not None:
            docx_path.write_bytes(prepend_manuscript_sections(docx_path.read_bytes(), manuscript))
        return docx_path

    def inspect(self, payload: bytes, expected_cell_texts: tuple[str, ...]) -> DocxInspection:
        return inspect_docx(payload, expected_cell_texts)

    def mark_evidence(
        self,
        payload: bytes,
        cells: tuple[DocumentTableCell, ...],
        *,
        document_revision_id: str,
    ) -> tuple[bytes, tuple[str, ...]]:
        marked, markers = inject_evidence_markers(
            payload, cells, document_revision_id=document_revision_id
        )
        return marked, tuple(marker.marker_tag for marker in markers)

    @staticmethod
    def _ps_quote(value: str) -> str:
        return value.replace("'", "''")


def inspect_docx(payload: bytes, expected_cell_texts: tuple[str, ...]) -> DocxInspection:
    if len(payload) > 50 * 1024 * 1024:
        raise ValueError("DOCX exceeds the minimal delivery size limit")
    with zipfile.ZipFile(io.BytesIO(payload)) as package:
        infos = package.infolist()
        if len(infos) > 2_000:
            raise ValueError("DOCX contains too many package parts")
        names = {info.filename for info in infos}
        required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
        if not required.issubset(names):
            raise ValueError("DOCX package is missing required WordprocessingML parts")
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("DOCX package contains an unsafe part name")
            if info.file_size > 20 * 1024 * 1024:
                raise ValueError("DOCX package contains an oversized part")
        forbidden = {
            name
            for name in names
            if name.lower().endswith("vbaproject.bin")
            or name.lower().startswith("word/embeddings/")
        }
        if forbidden:
            raise ValueError("DOCX active or embedded content is not supported")
        relationship_parts = [name for name in names if name.endswith(".rels")]
        for name in relationship_parts:
            root = ElementTree.fromstring(package.read(name))
            if any(
                relation.attrib.get("TargetMode", "").lower() == "external" for relation in root
            ):
                raise ValueError("DOCX external relationships are not supported")
        document = ElementTree.fromstring(package.read("word/document.xml"))
        visible_nodes = [node.text or "" for node in document.iter() if node.tag.endswith("}t")]
        visible = "\n".join(visible_nodes)
        for expected in expected_cell_texts:
            if expected not in visible:
                raise ValueError(f"DOCX is missing verified table cell text: {expected}")
    return DocxInspection(hashlib.sha256(payload).hexdigest(), visible, ())


def prepend_manuscript_sections(payload: bytes, manuscript: ManuscriptSections) -> bytes:
    """Prepend a simple research manuscript while preserving the Stata table package."""

    document_name = "word/document.xml"
    source_buffer = io.BytesIO(payload)
    target_buffer = io.BytesIO()
    with (
        zipfile.ZipFile(source_buffer, "r") as source,
        zipfile.ZipFile(target_buffer, "w") as target,
    ):
        if document_name not in source.namelist():
            raise ValueError("DOCX package is missing word/document.xml")
        for info in source.infolist():
            member = source.read(info.filename)
            if info.filename == document_name:
                member = _manuscript_document_xml(member, manuscript)
            target.writestr(info, member)
    return target_buffer.getvalue()


def _manuscript_document_xml(payload: bytes, manuscript: ManuscriptSections) -> bytes:
    body = re.search(rb"<w:body(?:\s[^>]*)?>", payload)
    if body is None:
        raise ValueError("DOCX document has no canonical w:body element")
    sections = (
        ("Abstract", manuscript.abstract),
        ("Research Question", manuscript.research_question),
        ("Data and Methods", manuscript.data_and_methods),
        ("Results", manuscript.results),
        ("Limitations", manuscript.limitations),
        ("Conclusion", manuscript.conclusion),
    )
    paragraphs = [_word_paragraph_xml(manuscript.title, style="Title", size=32, bold=True)]
    for heading, content in sections:
        paragraphs.append(_word_paragraph_xml(heading, style="Heading1", size=24, bold=True))
        paragraphs.append(_word_paragraph_xml(content, style="Normal", size=22, bold=False))
    paragraphs.append(
        _word_paragraph_xml(
            "Results Table",
            style="Heading1",
            size=24,
            bold=True,
            page_break_before=True,
        )
    )
    fragment = "".join(paragraphs).encode("utf-8")
    return payload[: body.end()] + fragment + payload[body.end() :]


def _word_paragraph_xml(
    text: str,
    *,
    style: str,
    size: int,
    bold: bool,
    page_break_before: bool = False,
) -> str:
    after = "120" if style == "Normal" else "160"
    bold_xml = "<w:b/>" if bold else ""
    page_break_xml = "<w:pageBreakBefore/>" if page_break_before else ""
    return (
        "<w:p><w:pPr>"
        f'<w:pStyle w:val="{escape(style)}"/>'
        f'<w:spacing w:after="{after}" w:line="276" w:lineRule="auto"/>'
        f"{page_break_xml}"
        "</w:pPr><w:r><w:rPr>"
        '<w:color w:val="000000"/>'
        f'<w:sz w:val="{size}"/>{bold_xml}'
        "</w:rPr>"
        f'<w:t xml:space="preserve">{escape(text)}</w:t>'
        "</w:r></w:p>"
    )
