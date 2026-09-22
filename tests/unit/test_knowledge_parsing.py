"""Canonical knowledge parsing and optional MinerU adapter contracts."""

from __future__ import annotations

import json
from pathlib import Path

from stata_research_agent.application.knowledge_retrieval import (
    ExtractedKnowledgeDocument,
    ExtractedKnowledgePage,
    KnowledgeNodeKind,
    build_canonical_nodes,
)
from stata_research_agent.application.rag_evaluation import (
    ParserGoldCase,
    evaluate_parser_case,
)
from stata_research_agent.interfaces.literature_catalog import (
    AdaptivePdfParser,
    MineruCliParser,
    PdfPageEscalationPolicy,
)


def test_fast_canonical_parser_preserves_open_structural_node_types() -> None:
    document = ExtractedKnowledgeDocument(
        "literature/structure.md",
        "text/markdown",
        "a" * 64,
        100,
        (
            ExtractedKnowledgePage(
                1,
                "# Results\n\n"
                "| Variable | Estimate |\n|---|---|\n| x | 1.2 |\n\n"
                "$$ y = a + bx $$\n\n"
                "- first check\n- second check\n\n"
                "Figure 1. Event-study estimates.",
            ),
        ),
    )

    nodes = build_canonical_nodes(document)
    kinds = {node.node_kind for node in nodes}

    assert KnowledgeNodeKind.SECTION in kinds
    assert KnowledgeNodeKind.TABLE in kinds
    assert KnowledgeNodeKind.EQUATION in kinds
    assert KnowledgeNodeKind.LIST in kinds
    assert KnowledgeNodeKind.CAPTION in kinds
    content_nodes = tuple(node for node in nodes if node.node_kind != KnowledgeNodeKind.DOCUMENT)
    assert all(node.source_span_start is not None for node in content_nodes)
    assert all(node.source_span_end is not None for node in content_nodes)
    table = next(node for node in nodes if node.node_kind == KnowledgeNodeKind.TABLE)
    assert table.structured_payload is not None
    assert table.structured_payload["rows"][2] == ["x", "1.2"]
    assert table.structured_payload["evidence_eligible"] is True
    score = evaluate_parser_case(
        ParserGoldCase(
            "structured-fast-fixture",
            ("section", "table", "equation", "list", "caption"),
            ("Estimate", "Event-study"),
            ("Event-study",),
        ),
        nodes,
    )
    assert score.node_kind_recall == 1
    assert score.content_marker_recall == 1
    assert score.locator_coverage == 1


def test_mineru_cli_adapter_uses_full_document_json_contract(tmp_path: Path) -> None:
    executable = tmp_path / "mineru.exe"
    executable.write_bytes(b"placeholder")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-placeholder")
    observed: list[object] = []

    def runner(command: list[str], timeout: int) -> tuple[int, str, str]:
        observed.extend((command, timeout))
        return (
            0,
            json.dumps(
                {
                    "parse": {"status": "done"},
                    "content": {
                        "content": (
                            "<!-- page 1 of 2 -->\n# Result\n\nEstimate text.\n"
                            "<!-- page 2 of 2 -->\n# Appendix\n\nRobustness."
                        )
                    },
                }
            ),
            "",
        )

    parsed = MineruCliParser(
        executable, version="4.x", timeout_seconds=321, runner=runner
    ).parse(source)

    assert parsed.parser_name == "mineru-cli"
    assert parsed.parser_profile == "mineru_basic"
    assert parsed.pages[0].text.startswith("# Result")
    assert parsed.pages[0].page_number == 1
    assert parsed.pages[1].page_number == 2
    assert observed[0] == [
        str(executable.resolve()),
        "parse",
        str(source.resolve()),
        "--tier",
        "basic",
        "--pages",
        "all",
        "--wait",
        "321",
        "--json",
    ]
    assert observed[1] == 321


def test_mineru_cli_adapter_compacts_selective_page_ranges(tmp_path: Path) -> None:
    executable = tmp_path / "mineru.exe"
    executable.write_bytes(b"placeholder")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-placeholder")
    observed: list[str] = []

    def runner(command: list[str], _timeout: int) -> tuple[int, str, str]:
        observed.extend(command)
        return (
            0,
            json.dumps(
                {
                    "content": {
                        "content": (
                            "<!-- page 2 of 8 -->\nSecond.\n"
                            "<!-- page 3 of 8 -->\nThird.\n"
                            "<!-- page 8 of 8 -->\nEighth."
                        )
                    }
                }
            ),
            "",
        )

    parsed = MineruCliParser(executable, version="4.x", runner=runner).parse_pages(
        source, (8, 2, 3)
    )

    assert tuple(page.page_number for page in parsed.pages) == (2, 3, 8)
    assert observed[observed.index("--pages") + 1] == "2-3,8"


def test_adaptive_pdf_parser_enriches_only_diagnostic_pages(tmp_path: Path) -> None:
    executable = tmp_path / "mineru.exe"
    executable.write_bytes(b"placeholder")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-placeholder")
    calls: list[list[str]] = []

    def runner(command: list[str], _timeout: int) -> tuple[int, str, str]:
        calls.append(command)
        return (
            0,
            json.dumps(
                {
                    "content": {
                        "content": (
                            "<!-- page 2 of 4 -->\n"
                            "## Model\n\n$$ E[y] = \\alpha + \\beta x $$"
                        )
                    }
                }
            ),
            "",
        )

    fast_pages = (
        ExtractedKnowledgePage(1, "Ordinary prose. " * 50),
        ExtractedKnowledgePage(
            2,
            "Model\nE[y] = 1\nVar[y] = 2\nCov[y,x] = 3\nPr(y=1) = 4\n" + "Text " * 80,
        ),
        ExtractedKnowledgePage(3, "More ordinary prose. " * 50),
        ExtractedKnowledgePage(4, "References. " * 50),
    )
    output = AdaptivePdfParser(
        MineruCliParser(executable, version="4.x", runner=runner),
        policy=PdfPageEscalationPolicy(max_page_ratio=1.0),
    ).parse(source, fast_pages)

    assert output.parser_name == "adaptive-pdf"
    assert output.pages[0] == fast_pages[0]
    assert output.pages[1].text.startswith("## Model")
    assert output.pages[2:] == fast_pages[2:]
    assert calls[0][calls[0].index("--pages") + 1] == "2"
    selected = tuple(
        finding.page_number
        for finding in output.page_parse_findings
        if finding.selected_parser == "mineru-cli"
    )
    assert selected == (2,)


def test_adaptive_pdf_parser_keeps_fast_parse_when_mineru_fails(tmp_path: Path) -> None:
    executable = tmp_path / "mineru.exe"
    executable.write_bytes(b"placeholder")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-placeholder")

    def runner(_command: list[str], _timeout: int) -> tuple[int, str, str]:
        return 1, "", "model unavailable"

    fast_pages = (
        ExtractedKnowledgePage(
            1,
            "Model\nE[y] = 1\nVar[y] = 2\nCov[y,x] = 3\nPr(y=1) = 4\n" + "Text " * 80,
        ),
    )
    output = AdaptivePdfParser(
        MineruCliParser(executable, version="4.x", runner=runner),
        policy=PdfPageEscalationPolicy(max_page_ratio=1.0),
    ).parse(source, fast_pages)

    assert output.pages == fast_pages
    assert output.parser_profile.endswith("degraded-v1")
    assert output.page_parse_findings[0].selected_parser == "pypdf_fallback"
    assert "rich_parser_failed" in output.page_parse_findings[0].reason_codes
