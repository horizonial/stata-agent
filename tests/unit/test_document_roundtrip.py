from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

from stata_research_agent.application.document_delivery import DocumentTableCell
from stata_research_agent.documents.roundtrip import (
    W,
    compare_roundtrip,
    inject_evidence_markers,
    inspect_semantics,
    merge_agent_tables_into_human,
)


def _docx(*, prose: str = "Baseline paragraph.", value: str = "1.25") -> bytes:
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{prose}</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>""".encode()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("_rels/.rels", "<Relationships/>")
        package.writestr("word/document.xml", document)
    return target.getvalue()


def _marked() -> bytes:
    payload, markers = inject_evidence_markers(
        _docx(),
        (
            DocumentTableCell(
                "tablecell_1", "baseline.price.coef", "1.25", "evidence_1", "element_1"
            ),
        ),
        document_revision_id="docrev_roundtrip",
    )
    assert len(markers) == 1
    assert markers[0].marker_tag.startswith("sa:")
    return payload


def _mutate(payload: bytes, mutate) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload)) as package:
        parts = {item.filename: package.read(item.filename) for item in package.infolist()}
    root = ElementTree.fromstring(parts["word/document.xml"])
    mutate(root)
    parts["word/document.xml"] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in sorted(parts.items()):
            package.writestr(name, content)
    return target.getvalue()


def test_marker_injection_is_stable_and_unchanged_roundtrip_is_eligible() -> None:
    marked = _marked()
    snapshot = inspect_semantics(marked)

    assert len(snapshot.markers) == 1
    assert snapshot.markers[0].semantic_slot == "baseline.price.coef"
    assert snapshot.markers[0].visible_value == "1.25"
    assert compare_roundtrip(marked, marked).classification == "no_semantic_change"


def test_marker_injection_preserves_word_ignorable_namespace_declarations() -> None:
    source = _docx()
    with zipfile.ZipFile(io.BytesIO(source)) as package:
        parts = {item.filename: package.read(item.filename) for item in package.infolist()}
    parts["word/document.xml"] = parts["word/document.xml"].replace(
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
        (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            b'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            b'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
            b'mc:Ignorable="w14">'
        ),
    )
    source_buffer = io.BytesIO()
    with zipfile.ZipFile(source_buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in sorted(parts.items()):
            package.writestr(name, content)

    marked, _ = inject_evidence_markers(
        source_buffer.getvalue(),
        (
            DocumentTableCell(
                "tablecell_1", "baseline.price.coef", "1.25", "evidence_1", "element_1"
            ),
        ),
        document_revision_id="docrev_namespace",
    )
    with zipfile.ZipFile(io.BytesIO(marked)) as package:
        document = package.read("word/document.xml")

    assert b'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"' in document
    assert b'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"' in document
    assert b'mc:Ignorable="w14"' in document
    ElementTree.fromstring(document)


def test_prose_only_edit_is_delivery_eligible() -> None:
    marked = _marked()

    def edit(root: ElementTree.Element) -> None:
        root.find(f".//{W}body/{W}p/{W}r/{W}t").text = "Human revised paragraph."

    diff = compare_roundtrip(marked, _mutate(marked, edit))

    assert diff.classification == "prose_only"
    assert diff.delivery_eligible
    assert diff.findings == ("PROSE_EDIT",)


def test_managed_value_edit_fails_closed() -> None:
    marked = _marked()

    def edit(root: ElementTree.Element) -> None:
        root.find(f".//{W}sdtContent/{W}r/{W}t").text = "9.99"

    diff = compare_roundtrip(marked, _mutate(marked, edit))

    assert diff.classification == "managed_conflict"
    assert any(item.startswith("EVIDENCE_VALUE_EDIT:") for item in diff.findings)
    assert not diff.delivery_eligible


def test_deleted_or_duplicated_marker_fails_closed() -> None:
    marked = _marked()

    def delete(root: ElementTree.Element) -> None:
        table_cell = root.find(f".//{W}tc/{W}p")
        marker = table_cell.find(f"{W}sdt")
        table_cell.remove(marker)

    deleted = compare_roundtrip(marked, _mutate(marked, delete))
    assert deleted.classification == "managed_conflict"
    assert any(item.startswith("MARKER_DELETED:") for item in deleted.findings)

    def duplicate(root: ElementTree.Element) -> None:
        import copy

        table_cell = root.find(f".//{W}tc/{W}p")
        marker = table_cell.find(f"{W}sdt")
        table_cell.append(copy.deepcopy(marker))

    duplicated = compare_roundtrip(marked, _mutate(marked, duplicate))
    assert duplicated.classification == "managed_conflict"
    assert any(item.startswith("MARKER_DUPLICATED:") for item in duplicated.findings)


def test_tracked_change_and_markerless_documents_are_not_inherited() -> None:
    marked = _marked()

    def track(root: ElementTree.Element) -> None:
        content = root.find(f".//{W}sdtContent")
        run = content.find(f"{W}r")
        content.remove(run)
        inserted = ElementTree.SubElement(content, f"{W}ins")
        inserted.append(run)

    tracked = compare_roundtrip(marked, _mutate(marked, track))
    assert tracked.classification == "managed_conflict"
    assert "TRACKED_CHANGE_PRESENT" in tracked.findings

    markerless = compare_roundtrip(_docx(), _docx(prose="Edited."))
    assert markerless.classification == "unsupported"
    assert markerless.findings == ("MARKERLESS_DOCUMENT_NO_EVIDENCE_INHERITANCE",)


def test_three_way_merge_preserves_human_prose_and_agent_table() -> None:
    base = _marked()

    def edit_prose(root: ElementTree.Element) -> None:
        root.find(f".//{W}body/{W}p/{W}r/{W}t").text = "Human interpretation."

    human = _mutate(base, edit_prose)

    def edit_agent_table(root: ElementTree.Element) -> None:
        root.find(f".//{W}sdtContent/{W}r/{W}t").text = "2.50"

    agent = _mutate(base, edit_agent_table)
    merged = merge_agent_tables_into_human(base, human, agent)

    assert merged.decisions == (
        "PRESERVED_HUMAN_PROSE",
        "ADOPTED_AGENT_TABLES",
        "PRESERVED_AGENT_EVIDENCE_MARKERS",
    )
    snapshot = inspect_semantics(merged.payload)
    assert snapshot.prose_sha256 == inspect_semantics(human).prose_sha256
    assert snapshot.table_sha256 == inspect_semantics(agent).table_sha256
    assert snapshot.markers[0].visible_value == "2.50"


def test_three_way_merge_rejects_agent_prose_changes() -> None:
    base = _marked()

    def edit_agent(root: ElementTree.Element) -> None:
        root.find(f".//{W}body/{W}p/{W}r/{W}t").text = "Agent changed prose."

    try:
        merge_agent_tables_into_human(base, base, _mutate(base, edit_agent))
    except ValueError as error:
        assert str(error) == "agent revision also changed prose"
    else:
        raise AssertionError("agent prose mutation must not auto-merge")


def test_package_safety_rejects_external_payload_but_allows_plain_hyperlink() -> None:
    payload = _docx()

    def with_relation(relation_type: str) -> bytes:
        with zipfile.ZipFile(io.BytesIO(payload)) as package:
            parts = {item.filename: package.read(item.filename) for item in package.infolist()}
        parts["word/_rels/document.xml.rels"] = f"""
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="{relation_type}"
            Target="https://example.invalid/resource" TargetMode="External"/>
        </Relationships>
        """.encode()
        target = io.BytesIO()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
            for name, content in sorted(parts.items()):
                package.writestr(name, content)
        return target.getvalue()

    hyperlink = with_relation(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
    )
    assert inspect_semantics(hyperlink).markers == ()

    linked_image = with_relation(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    )
    try:
        inspect_semantics(linked_image)
    except ValueError as error:
        assert str(error) == "DOCX external relationships are unsupported"
    else:
        raise AssertionError("external payload relationships must fail closed")
