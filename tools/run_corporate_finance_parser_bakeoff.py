"""Compare pypdf, Docling, and selective MinerU on difficult corporate-finance PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from stata_research_agent.application.knowledge_retrieval import ExtractedKnowledgePage
from stata_research_agent.interfaces.literature_catalog import (
    AdaptivePdfParser,
    MineruCliParser,
)


def _text_metrics(text: str, markers: tuple[str, ...]) -> dict[str, object]:
    normalized = re.sub(r"\s+", " ", text).casefold()
    marker_hits = tuple(marker for marker in markers if marker.casefold() in normalized)
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)
    return {
        "characters": len(text),
        "word_count": len(words),
        "marker_recall": len(marker_hits) / len(markers),
        "marker_hits": marker_hits,
        "replacement_character_count": text.count("�"),
        "markdown_table_row_count": sum(line.count("|") >= 2 for line in text.splitlines()),
        "display_equation_count": text.count("$$") // 2,
    }


def _docling_markdown(path: Path) -> str:
    from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]

    result = DocumentConverter().convert(path)
    return str(result.document.export_to_markdown())


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--mineru-executable", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--skip-docling", action="store_true")
    arguments = parser.parse_args()
    output_root = arguments.output_root.resolve()
    if output_root.exists():
        raise ValueError("parser bake-off output already exists")
    output_root.mkdir(parents=True)
    raw_cases = _mapping(
        json.loads(arguments.cases.resolve().read_text(encoding="utf-8")), "case set"
    )
    if raw_cases.get("schema_version") != "corporate-finance-parser-challenge/v1":
        raise ValueError("unsupported parser challenge schema")
    mineru = MineruCliParser(
        arguments.mineru_executable,
        version="4.0.4",
        tier="basic",
        timeout_seconds=arguments.timeout_seconds,
    )
    adaptive = AdaptivePdfParser(mineru)
    results: list[dict[str, object]] = []
    for raw_case in raw_cases["cases"]:
        case = _mapping(raw_case, "parser challenge case")
        paper_id = str(case["paper_id"])
        markers = tuple(str(value) for value in case["required_markers"])
        source = arguments.corpus_root.resolve() / "papers" / str(case["filename"])
        case_root = output_root / paper_id
        case_root.mkdir()
        local_pdf = case_root / source.name
        shutil.copy2(source, local_pdf)

        started = time.perf_counter()
        reader = PdfReader(local_pdf, strict=False)
        fast_pages = tuple(
            ExtractedKnowledgePage(index, (page.extract_text() or "").strip())
            for index, page in enumerate(reader.pages, start=1)
        )
        fast_elapsed = time.perf_counter() - started
        fast_text = "\n\n".join(page.text for page in fast_pages)
        (case_root / "pypdf.txt").write_text(fast_text, encoding="utf-8")

        started = time.perf_counter()
        docling_error: str | None = "skipped_by_experiment"
        docling_text = ""
        if not arguments.skip_docling:
            docling_error = None
            try:
                docling_text = _docling_markdown(local_pdf)
            except Exception as error:  # retain candidate failure as experiment evidence
                docling_error = f"{type(error).__name__}: {str(error)[:500]}"
        docling_elapsed = time.perf_counter() - started
        (case_root / "docling.md").write_text(docling_text, encoding="utf-8")

        started = time.perf_counter()
        rich = adaptive.parse(local_pdf, fast_pages)
        mineru_elapsed = time.perf_counter() - started
        mineru_text = "\n\n".join(page.text for page in rich.pages)
        (case_root / "selective-mineru.md").write_text(mineru_text, encoding="utf-8")
        selected_pages = tuple(
            item.page_number
            for item in rich.page_parse_findings
            if item.selected_parser == "mineru-cli"
        )
        results.append(
            {
                "paper_id": paper_id,
                "filename": source.name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "page_count": len(reader.pages),
                "required_markers": markers,
                "pypdf": {
                    "elapsed_seconds": fast_elapsed,
                    **_text_metrics(fast_text, markers),
                },
                "docling": {
                    "elapsed_seconds": docling_elapsed,
                    "error": docling_error,
                    **_text_metrics(docling_text, markers),
                },
                "selective_mineru": {
                    "elapsed_seconds": mineru_elapsed,
                    "selected_page_count": len(selected_pages),
                    "selected_pages": selected_pages,
                    "parser_profile": rich.parser_profile,
                    **_text_metrics(mineru_text, markers),
                },
            }
        )
        print(f"completed {paper_id}")

    report = {
        "schema_version": "corporate-finance-parser-bakeoff/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "case_count": len(results),
        "results": results,
    }
    report_path = output_root / "parser-bakeoff-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(str(report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
