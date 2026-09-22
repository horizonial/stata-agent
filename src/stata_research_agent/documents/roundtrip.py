"""Evidence-marker injection and deterministic WordprocessingML roundtrip diff."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree

from stata_research_agent.application.document_delivery import DocumentTableCell
from stata_research_agent.application.document_roundtrip import (
    DocumentSemanticDiff,
    DocumentSemanticSnapshot,
    DocumentThreeWayMerge,
    MarkerObservation,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
W = f"{{{W_NS}}}"
W15 = f"{{{W15_NS}}}"


@dataclass(frozen=True, slots=True)
class EvidenceMarker:
    marker_tag: str
    semantic_slot: str
    visible_value: str
    evidence_record_id: str
    table_cell_use_id: str


def inject_evidence_markers(
    payload: bytes,
    cells: tuple[DocumentTableCell, ...],
    *,
    document_revision_id: str,
) -> tuple[bytes, tuple[EvidenceMarker, ...]]:
    """Wrap exact generated table cells; ambiguity fails before a Revision is committed."""

    parts = _read_safe_parts(payload)
    root = ElementTree.fromstring(parts["word/document.xml"])
    parent = {child: node for node in root.iter() for child in node}
    table_text_nodes = [
        node for table in root.iter(f"{W}tbl") for node in table.iter(f"{W}t") if node.text
    ]
    used: set[int] = set()
    markers: list[EvidenceMarker] = []
    search_start = 0
    for cell in cells:
        candidates = [
            (index, node)
            for index, node in enumerate(table_text_nodes)
            if index >= search_start
            and index not in used
            and cell.rendered_text in (node.text or "")
        ]
        if not candidates:
            raise ValueError(
                f"generated DOCX has no ordered exact cell for {cell.semantic_cell_slot}"
            )
        index, text_node = candidates[0]
        used.add(index)
        search_start = index + 1
        marker_digest = hashlib.sha256(
            (
                document_revision_id
                + "\x1f"
                + cell.table_cell_use_id
                + "\x1f"
                + cell.semantic_cell_slot
            ).encode("utf-8")
        ).hexdigest()[:24]
        marker = EvidenceMarker(
            f"sa:{marker_digest}",
            cell.semantic_cell_slot,
            cell.rendered_text,
            cell.evidence_record_id,
            cell.table_cell_use_id,
        )
        _wrap_text_node(text_node, parent, cell.rendered_text, marker)
        markers.append(marker)
        parent = {child: node for node in root.iter() for child in node}
    parts["word/document.xml"] = _serialize_preserving_word_namespaces(
        root, parts["word/document.xml"]
    )
    return _write_parts(parts), tuple(markers)


def inspect_semantics(payload: bytes) -> DocumentSemanticSnapshot:
    parts = _read_safe_parts(payload)
    root = ElementTree.fromstring(parts["word/document.xml"])
    observations: list[MarkerObservation] = []
    for sdt in root.iter(f"{W}sdt"):
        properties = sdt.find(f"{W}sdtPr")
        content = sdt.find(f"{W}sdtContent")
        if properties is None or content is None:
            continue
        tag_node = properties.find(f"{W}tag")
        marker_tag = tag_node.get(f"{W}val", "") if tag_node is not None else ""
        if not marker_tag.startswith("sa:"):
            continue
        alias_node = properties.find(f"{W}alias")
        alias = alias_node.get(f"{W}val", "") if alias_node is not None else ""
        value = "".join(node.text or "" for node in content.iter(f"{W}t"))
        tracked = any(
            node.tag in {f"{W}ins", f"{W}del", f"{W}moveFrom", f"{W}moveTo"}
            for node in content.iter()
        )
        observations.append(MarkerObservation(marker_tag, alias, value, tracked))

    table_rows: list[list[str]] = []
    for table in root.iter(f"{W}tbl"):
        for row in table.findall(f"{W}tr"):
            table_rows.append([_visible_text(cell) for cell in row.findall(f"{W}tc")])
    prose_parts = []
    for paragraph in root.iter(f"{W}p"):
        if _has_ancestor(root, paragraph, f"{W}tbl"):
            continue
        prose_parts.append(_prose_with_marker_tokens(paragraph))
    tracked_change_count = sum(
        1
        for node in root.iter()
        if node.tag in {f"{W}ins", f"{W}del", f"{W}moveFrom", f"{W}moveTo"}
    )
    marker_payload = [
        {
            "marker_tag": item.marker_tag,
            "semantic_slot": item.semantic_slot,
            "visible_value": item.visible_value,
            "tracked_change_inside": item.tracked_change_inside,
        }
        for item in observations
    ]
    prose_json = _canonical(prose_parts)
    table_json = _canonical(table_rows)
    normalized = _canonical(
        {
            "prose": prose_parts,
            "tables": table_rows,
            "markers": marker_payload,
            "tracked_change_count": tracked_change_count,
        }
    )
    return DocumentSemanticSnapshot(
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        hashlib.sha256(prose_json.encode("utf-8")).hexdigest(),
        hashlib.sha256(table_json.encode("utf-8")).hexdigest(),
        tuple(observations),
        tracked_change_count,
    )


def compare_roundtrip(base_payload: bytes, returned_payload: bytes) -> DocumentSemanticDiff:
    base = inspect_semantics(base_payload)
    returned = inspect_semantics(returned_payload)
    findings: list[str] = []
    expected: dict[str, MarkerObservation] = {}
    for marker in base.markers:
        if marker.marker_tag in expected:
            findings.append(f"BASE_MARKER_DUPLICATED:{marker.marker_tag}")
        expected[marker.marker_tag] = marker
    if not expected:
        findings.append("MARKERLESS_DOCUMENT_NO_EVIDENCE_INHERITANCE")
    actual: dict[str, list[MarkerObservation]] = {}
    for marker in returned.markers:
        actual.setdefault(marker.marker_tag, []).append(marker)
    for tag, expected_marker in expected.items():
        candidates = actual.get(tag, [])
        if not candidates:
            findings.append(f"MARKER_DELETED:{tag}")
            continue
        if len(candidates) != 1:
            findings.append(f"MARKER_DUPLICATED:{tag}")
            continue
        observed = candidates[0]
        if observed.semantic_slot != expected_marker.semantic_slot:
            findings.append(f"MARKER_MOVED:{tag}")
        if observed.visible_value != expected_marker.visible_value:
            findings.append(f"EVIDENCE_VALUE_EDIT:{tag}")
        if observed.tracked_change_inside:
            findings.append(f"TRACKED_CHANGE_IN_MANAGED_VALUE:{tag}")
    if returned.table_sha256 != base.table_sha256:
        findings.append("TABLE_STRUCTURE_OR_VALUE_EDIT")
    if returned.tracked_change_count:
        findings.append("TRACKED_CHANGE_PRESENT")
    if any(item.startswith("MARKERLESS") for item in findings):
        classification = "unsupported"
    elif any(
        item.startswith(
            (
                "MARKER_",
                "EVIDENCE_",
                "TRACKED_CHANGE_IN_MANAGED_VALUE",
                "BASE_MARKER_",
            )
        )
        for item in findings
    ):
        classification = "managed_conflict"
    elif "TABLE_STRUCTURE_OR_VALUE_EDIT" in findings:
        classification = "structure_conflict"
    elif returned.tracked_change_count:
        classification = "unsupported"
    elif returned.prose_sha256 != base.prose_sha256:
        classification = "prose_only"
        findings.append("PROSE_EDIT")
    else:
        classification = "no_semantic_change"
    return DocumentSemanticDiff(classification, tuple(sorted(set(findings))), base, returned)


def merge_agent_tables_into_human(
    base_payload: bytes,
    human_payload: bytes,
    agent_payload: bytes,
) -> DocumentThreeWayMerge:
    """Merge human prose with a table-only agent revision; every ambiguity fails."""

    human_diff = compare_roundtrip(base_payload, human_payload)
    if not human_diff.delivery_eligible:
        raise ValueError("human revision is not a prose-preserving roundtrip")
    base = human_diff.base
    human = human_diff.returned
    agent = inspect_semantics(agent_payload)
    if agent.tracked_change_count:
        raise ValueError("agent revision contains tracked changes")
    if agent.prose_sha256 != base.prose_sha256:
        raise ValueError("agent revision also changed prose")
    base_slots = _unique_markers_by_slot(base.markers, "base")
    agent_slots = _unique_markers_by_slot(agent.markers, "agent")
    if set(agent_slots) != set(base_slots):
        raise ValueError("agent revision changed the managed semantic slot set")

    human_parts = _read_safe_parts(human_payload)
    agent_parts = _read_safe_parts(agent_payload)
    human_root = ElementTree.fromstring(human_parts["word/document.xml"])
    agent_root = ElementTree.fromstring(agent_parts["word/document.xml"])
    human_tables = list(human_root.iter(f"{W}tbl"))
    agent_tables = list(agent_root.iter(f"{W}tbl"))
    if not human_tables or len(human_tables) != len(agent_tables):
        raise ValueError("table cardinality changed and cannot be merged automatically")
    human_parents = {child: node for node in human_root.iter() for child in node}
    for human_table, agent_table in zip(human_tables, agent_tables, strict=True):
        parent = human_parents.get(human_table)
        if parent is None:
            raise ValueError("human table has no replaceable parent")
        index = list(parent).index(human_table)
        parent.remove(human_table)
        parent.insert(index, copy.deepcopy(agent_table))
    human_parts["word/document.xml"] = _serialize_preserving_word_namespaces(
        human_root, human_parts["word/document.xml"]
    )
    merged_payload = _write_parts(human_parts)
    merged = inspect_semantics(merged_payload)
    if merged.prose_sha256 != human.prose_sha256:
        raise RuntimeError("merge did not preserve the human prose view")
    if merged.table_sha256 != agent.table_sha256:
        raise RuntimeError("merge did not preserve the agent table view")
    if _unique_markers_by_slot(merged.markers, "merged") != agent_slots:
        raise RuntimeError("merge changed the agent evidence marker view")
    return DocumentThreeWayMerge(
        merged_payload,
        (
            "PRESERVED_HUMAN_PROSE",
            "ADOPTED_AGENT_TABLES",
            "PRESERVED_AGENT_EVIDENCE_MARKERS",
        ),
        merged,
    )


class OoxmlDocumentRoundtripEngine:
    """Application-facing adapter for deterministic OOXML diff and merge."""

    def compare(self, base_payload: bytes, returned_payload: bytes) -> DocumentSemanticDiff:
        return compare_roundtrip(base_payload, returned_payload)

    def merge(
        self, base_payload: bytes, human_payload: bytes, agent_payload: bytes
    ) -> DocumentThreeWayMerge:
        return merge_agent_tables_into_human(base_payload, human_payload, agent_payload)


def _wrap_text_node(
    text_node: ElementTree.Element,
    parent_map: dict[ElementTree.Element, ElementTree.Element],
    visible_value: str,
    marker: EvidenceMarker,
) -> None:
    run = parent_map.get(text_node)
    if run is None or run.tag != f"{W}r":
        raise ValueError("verified table cell text is not contained in a simple Word run")
    run_parent = parent_map.get(run)
    if run_parent is None:
        raise ValueError("verified table cell run has no parent")
    full = text_node.text or ""
    before, separator, after = full.partition(visible_value)
    if not separator:
        raise ValueError("verified table cell value changed before marker injection")
    index = list(run_parent).index(run)
    replacements: list[ElementTree.Element] = []
    if before:
        replacements.append(_clone_run(run, before))
    sdt = ElementTree.Element(f"{W}sdt")
    properties = ElementTree.SubElement(sdt, f"{W}sdtPr")
    alias = ElementTree.SubElement(properties, f"{W}alias")
    alias.set(f"{W}val", marker.semantic_slot)
    tag = ElementTree.SubElement(properties, f"{W}tag")
    tag.set(f"{W}val", marker.marker_tag)
    ElementTree.SubElement(properties, f"{W}text")
    appearance = ElementTree.SubElement(properties, f"{W15}appearance")
    appearance.set(f"{W15}val", "hidden")
    content = ElementTree.SubElement(sdt, f"{W}sdtContent")
    content.append(_clone_run(run, visible_value))
    replacements.append(sdt)
    if after:
        replacements.append(_clone_run(run, after))
    for offset, replacement in enumerate(replacements):
        run_parent.insert(index + offset, replacement)
    run_parent.remove(run)


def _clone_run(run: ElementTree.Element, value: str) -> ElementTree.Element:
    cloned = copy.deepcopy(run)
    texts = list(cloned.iter(f"{W}t"))
    if not texts:
        texts = [ElementTree.SubElement(cloned, f"{W}t")]
    texts[0].text = value
    for extra in texts[1:]:
        extra.text = ""
    return cloned


def _visible_text(node: ElementTree.Element) -> str:
    pieces: list[str] = []

    def visit(current: ElementTree.Element, hidden: bool = False) -> None:
        now_hidden = hidden or current.tag in {f"{W}del", f"{W}moveFrom"}
        if current.tag == f"{W}t" and not now_hidden:
            pieces.append(current.text or "")
        for child in current:
            visit(child, now_hidden)

    visit(node)
    return " ".join("".join(pieces).split())


def _prose_with_marker_tokens(paragraph: ElementTree.Element) -> str:
    pieces: list[str] = []

    def visit(node: ElementTree.Element, hidden: bool = False) -> None:
        now_hidden = hidden or node.tag in {f"{W}del", f"{W}moveFrom"}
        if node.tag == f"{W}sdt":
            properties = node.find(f"{W}sdtPr")
            tag_node = properties.find(f"{W}tag") if properties is not None else None
            tag = tag_node.get(f"{W}val", "") if tag_node is not None else ""
            if tag.startswith("sa:"):
                pieces.append(f"<MARKER:{tag}>")
                return
        if node.tag == f"{W}t" and not now_hidden:
            pieces.append(node.text or "")
        for child in node:
            visit(child, now_hidden)

    visit(paragraph)
    return " ".join("".join(pieces).split())


def _has_ancestor(
    root: ElementTree.Element, target: ElementTree.Element, ancestor_tag: str
) -> bool:
    parent = {child: node for node in root.iter() for child in node}
    current = parent.get(target)
    while current is not None:
        if current.tag == ancestor_tag:
            return True
        current = parent.get(current)
    return False


def _read_safe_parts(payload: bytes) -> dict[str, bytes]:
    if len(payload) > 50 * 1024 * 1024:
        raise ValueError("DOCX exceeds the roundtrip size limit")
    with zipfile.ZipFile(io.BytesIO(payload)) as package:
        infos = package.infolist()
        if len(infos) > 4096:
            raise ValueError("DOCX exceeds the package entry limit")
        names = {info.filename for info in infos}
        if len(names) != len(infos):
            raise ValueError("DOCX contains duplicate package parts")
        expanded_size = sum(info.file_size for info in infos)
        if expanded_size > 200 * 1024 * 1024:
            raise ValueError("DOCX exceeds the expanded package size limit")
        required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
        if not required.issubset(names):
            raise ValueError("DOCX is missing required WordprocessingML parts")
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("DOCX contains an unsafe package part")
            lowered = info.filename.lower()
            if (
                lowered.endswith("vbaproject.bin")
                or lowered.startswith("word/embeddings/")
                or lowered.startswith("word/activex/")
            ):
                raise ValueError("DOCX active or embedded content is unsupported")
            if info.file_size > 1024 * 1024 and info.file_size > max(info.compress_size, 1) * 100:
                raise ValueError("DOCX package compression ratio is unsafe")
        for info in infos:
            if not info.filename.endswith(".rels"):
                continue
            root = ElementTree.fromstring(package.read(info.filename))
            for item in root:
                if item.attrib.get("TargetMode", "").lower() != "external":
                    continue
                relation_type = item.attrib.get("Type", "").lower()
                if relation_type.endswith("/hyperlink"):
                    continue
                raise ValueError("DOCX external relationships are unsupported")
        parts = {info.filename: package.read(info.filename) for info in infos}
        document_root = ElementTree.fromstring(parts["word/document.xml"])
        if any(node.tag == f"{W}altChunk" for node in document_root.iter()):
            raise ValueError("DOCX altChunk content is unsupported")
        return parts


def _write_parts(parts: dict[str, bytes]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name in sorted(parts):
            package.writestr(name, parts[name])
    return target.getvalue()


def _serialize_preserving_word_namespaces(
    root: ElementTree.Element, original_payload: bytes
) -> bytes:
    """Serialize edited Word XML without invalidating mc:Ignorable prefixes.

    ElementTree normally renames namespaces to ``ns0`` and drops declarations
    that occur only inside QName-valued attributes such as ``mc:Ignorable``.
    Word treats those dangling prefix references as package corruption.  Keep
    the source document's prefix vocabulary and restore declarations that the
    serializer cannot infer from element and attribute names.
    """

    original_opening = _root_opening_tag(original_payload)
    declarations = tuple(
        (match.group("prefix").decode("ascii"), match.group("uri"))
        for match in re.finditer(
            rb'xmlns:(?P<prefix>[A-Za-z_][\w.-]*)="(?P<uri>[^"]+)"',
            original_opening,
        )
    )
    for prefix, uri in declarations:
        if re.fullmatch(r"ns\d+", prefix):
            continue
        ElementTree.register_namespace(prefix, uri.decode("utf-8"))

    serialized: bytes = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    serialized_opening = _root_opening_tag(serialized)
    declared_prefixes = {
        match.group("prefix").decode("ascii")
        for match in re.finditer(
            rb'xmlns:(?P<prefix>[A-Za-z_][\w.-]*)="[^"]+"',
            serialized_opening,
        )
    }
    missing = [
        b" xmlns:" + prefix.encode("ascii") + b'="' + uri + b'"'
        for prefix, uri in declarations
        if prefix not in declared_prefixes
    ]
    if not missing:
        return serialized
    opening_end = serialized.find(b">", serialized.find(b"<w:document"))
    if opening_end < 0:
        raise ValueError("serialized DOCX document has no canonical root element")
    return serialized[:opening_end] + b"".join(missing) + serialized[opening_end:]


def _root_opening_tag(payload: bytes) -> bytes:
    declaration_end = payload.find(b"?>")
    root_start = payload.find(b"<", declaration_end + 2 if declaration_end >= 0 else 0)
    root_end = payload.find(b">", root_start)
    if root_start < 0 or root_end < 0:
        raise ValueError("DOCX document XML has no root element")
    return payload[root_start : root_end + 1]


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _unique_markers_by_slot(
    observations: tuple[MarkerObservation, ...], label: str
) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for marker in observations:
        if marker.semantic_slot in result:
            raise ValueError(f"{label} revision duplicates a managed semantic slot")
        result[marker.semantic_slot] = (marker.marker_tag, marker.visible_value)
    if not result:
        raise ValueError(f"{label} revision has no managed evidence markers")
    return result
