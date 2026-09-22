from __future__ import annotations

import io
import struct
import zipfile

import pytest

from stata_research_agent.application.document_delivery import ManuscriptSections
from stata_research_agent.application.table_export import ExportEsttabTableCommand
from stata_research_agent.documents.word_renderer import (
    inspect_docx,
    prepend_manuscript_sections,
)
from stata_research_agent.domain.identifiers import CommandId, ResearchPathId, TurnId
from stata_research_agent.domain.table_export import TableElement, verify_esttab_rtf


def _bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def test_table_export_rejects_n_as_a_duplicate_fit_statistic() -> None:
    with pytest.raises(ValueError, match="cannot duplicate"):
        ExportEsttabTableCommand(
            CommandId("cmd_table_duplicate_n"),
            TurnId("turn_table_duplicate_n"),
            ResearchPathId("path_table_duplicate_n"),
            "baseline.primary",
            coefficient_terms=("mpg",),
            fit_statistic="N",
        )


def _table_elements() -> tuple[TableElement, ...]:
    values = {
        "term.mpg.coefficient": -1.2346,
        "term.mpg.se": 0.1114,
        "term.weight.coefficient": 2.3456,
        "term.weight.se": 0.2224,
        "term._cons.coefficient": 3.4567,
        "term._cons.se": 0.3334,
        "model.N": 74.0,
        "model.r2": 0.2934,
    }
    return tuple(
        TableElement(f"element-{index}", key, _bits(value))
        for index, (key, value) in enumerate(values.items(), start=1)
    )


def _rtf_payload() -> bytes:
    return (
        b"{\\rtf1"
        b"{mpg}\\cell{-1.235\\cell}{(0.111)}"
        b"{weight}\\cell{2.346\\cell}{(0.222)}"
        b"{_cons}\\cell{3.457\\cell}{(0.333)}"
        b"{Observations}\\cell{74\\cell}"
        b"{R-squared}\\cell{0.293\\cell}"
        b"}"
    )


def test_esttab_verifier_rejects_a_changed_numeric_cell() -> None:
    verified = verify_esttab_rtf(_rtf_payload(), _table_elements())
    assert len(verified.cells) == 8

    corrupted = _rtf_payload().replace(b"{2.346\\cell}", b"{9.999\\cell}")
    with pytest.raises(ValueError, match="does not match qualified Result Element"):
        verify_esttab_rtf(corrupted, _table_elements())


def _docx_payload(*, external_relationship: bool) -> bytes:
    target_mode = (
        ' TargetMode="External" Target="https://example.invalid"' if external_relationship else ""
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="document"{target_mode}/>'
        "</Relationships>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>2.346</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("_rels/.rels", relationships)
        package.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_docx_inspection_rejects_external_relationships() -> None:
    valid = inspect_docx(_docx_payload(external_relationship=False), ("2.346",))
    assert valid.visible_text == "2.346"

    with pytest.raises(ValueError, match="external relationships"):
        inspect_docx(_docx_payload(external_relationship=True), ("2.346",))


def _manuscript() -> ManuscriptSections:
    return ManuscriptSections(
        "Automobile Prices and Fuel Economy",
        "This draft studies how fuel economy and vehicle weight relate to price.",
        "The analysis asks whether price varies systematically with the selected predictors.",
        "The study uses the supplied automobile data and an ordinary least squares baseline.",
        (
            "The estimates indicate a negative association for fuel economy and a positive "
            "association for weight."
        ),
        "The design is descriptive and does not establish a causal effect.",
        "The baseline offers a reproducible starting point for deeper model checks.",
    )


def test_manuscript_prepend_keeps_verified_table_and_adds_readable_sections() -> None:
    composed = prepend_manuscript_sections(
        _docx_payload(external_relationship=False), _manuscript()
    )
    inspected = inspect_docx(composed, ("2.346",))

    assert inspected.visible_text.startswith("Automobile Prices and Fuel Economy")
    assert "Abstract" in inspected.visible_text
    assert "Results Table" in inspected.visible_text
    assert inspected.visible_text.endswith("2.346")

    with zipfile.ZipFile(io.BytesIO(composed), "r") as package:
        document_xml = package.read("word/document.xml").decode("utf-8")
    results_table_paragraph = document_xml.split("Results Table", maxsplit=1)[0].rsplit(
        "<w:p>", maxsplit=1
    )[1]
    assert "<w:pageBreakBefore/>" in results_table_paragraph


def test_manuscript_roundtrip_preserves_chinese_and_latin_identifiers() -> None:
    manuscript = ManuscriptSections(
        "汽车价格研究",
        "本文研究燃油经济性与汽车价格之间的关系。",
        "研究问题是变量 mpg 与 weight 如何关联 price。",
        "数据来自 auto.dta，正式估计由 Stata 执行。",
        "结果部分保留英文变量名 price、mpg 和 weight。",
        "当前设计属于描述性分析。",
        "该结果为后续研究提供可复现起点。",
    )
    composed = prepend_manuscript_sections(
        _docx_payload(external_relationship=False), manuscript
    )
    inspected = inspect_docx(composed, ("2.346",))
    assert "汽车价格研究" in inspected.visible_text
    assert "auto.dta" in inspected.visible_text
    assert "price、mpg 和 weight" in inspected.visible_text


def test_manuscript_rejects_unbound_numeric_claims() -> None:
    with pytest.raises(ValueError, match="unbound numeric occurrence"):
        ManuscriptSections(
            "Automobile Study",
            "The draft reports a coefficient of 2.3.",
            "The question concerns vehicle prices.",
            "The study uses the supplied data.",
            "The predictors are associated with price.",
            "The design is descriptive.",
            "The baseline is a starting point.",
        )
