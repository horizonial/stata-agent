"""Evaluate FAST pypdf plus selective local MinerU on representative real papers."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from pypdf import PdfReader

from stata_research_agent.application.knowledge_retrieval import (
    ExtractedKnowledgePage,
    build_canonical_nodes,
)
from stata_research_agent.application.rag_evaluation import (
    ParserGoldCase,
    evaluate_parser_case,
)
from stata_research_agent.interfaces.literature_catalog import (
    FilesystemLiteratureCatalog,
    MineruCliParser,
)

CASES = (
    {
        "paper_id": "callaway-santanna-2021",
        "filename": "02-callaway-santanna-multiple-periods.pdf",
        "expected_pages": 45,
        "markers": ("doubly-robust", "inverse probability weighting", "References"),
    },
    {
        "paper_id": "sun-abraham-2021",
        "filename": "03-sun-abraham-event-studies.pdf",
        "expected_pages": 51,
        "markers": ("interaction-weighted", "event-study", "contamination", "References"),
    },
    {
        "paper_id": "de-chaisemartin-dhaultfoeuille-2022",
        "filename": "05-de-chaisemartin-dhaultfoeuille-survey.pdf",
        "expected_pages": 35,
        "markers": ("eventstudyinteract", "csdid", "did_imputation", "References"),
    },
    {
        "paper_id": "callaway-goodman-bacon-santanna-2021",
        "filename": "09-callaway-goodman-bacon-santanna-continuous.pdf",
        "expected_pages": 43,
        "markers": ("continuous treatment", "dose-response", "References"),
    },
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _structural_counts(text: str) -> dict[str, int]:
    return {
        "characters": len(text),
        "display_equations": text.count("$$") // 2,
        "markdown_table_rows": sum(line.count("|") >= 2 for line in text.splitlines()),
        "headings": sum(
            bool(re.match(r"^#{1,6}\s+", line)) for line in text.splitlines()
        ),
    }


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _score_structure_gold(
    gold: dict[str, object],
    page_records: dict[str, dict[str, object]],
) -> dict[str, object]:
    if gold.get("schema_version") != "adaptive-pdf-structure-gold/v1":
        raise ValueError("unsupported adaptive PDF structure gold schema")
    cases = gold.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("adaptive PDF structure gold requires cases")
    scores: list[dict[str, object]] = []
    for raw_case in cases:
        if not isinstance(raw_case, dict):
            raise ValueError("adaptive PDF structure case must be an object")
        paper_id = str(raw_case["paper_id"])
        page_number = int(raw_case["page_number"])
        kind = str(raw_case["kind"])
        records = page_records[paper_id]
        fast_text = str(records["fast_pages"][page_number])
        rich_text = str(records["rich_pages"][page_number])
        enriched_pages = set(records["enriched_pages"])
        markers = tuple(str(marker) for marker in raw_case["required_markers"])

        def observe(text: str) -> tuple[bool, tuple[str, ...], int]:
            normalized = _normalized(text)
            missing = tuple(marker for marker in markers if _normalized(marker) not in normalized)
            markdown_rows = sum(
                line.strip().startswith("|")
                and line.strip().endswith("|")
                and line.count("|") >= 3
                for line in text.splitlines()
            )
            if kind == "formula":
                structured = text.count("$$") >= 2
            elif kind == "table":
                structured = markdown_rows >= int(raw_case["minimum_markdown_rows"])
            else:
                raise ValueError(f"unsupported structure gold kind: {kind}")
            return structured and not missing, missing, markdown_rows

        baseline_passed, baseline_missing, baseline_rows = observe(fast_text)
        rich_passed, rich_missing, rich_rows = observe(rich_text)
        scores.append(
            {
                "case_id": str(raw_case["case_id"]),
                "paper_id": paper_id,
                "page_number": page_number,
                "kind": kind,
                "page_was_enriched": page_number in enriched_pages,
                "baseline_passed": baseline_passed,
                "rich_passed": rich_passed,
                "baseline_missing_markers": baseline_missing,
                "rich_missing_markers": rich_missing,
                "baseline_markdown_rows": baseline_rows,
                "rich_markdown_rows": rich_rows,
            }
        )
    formula_scores = tuple(score for score in scores if score["kind"] == "formula")
    table_scores = tuple(score for score in scores if score["kind"] == "table")
    return {
        "schema_version": str(gold["schema_version"]),
        "case_count": len(scores),
        "formula_case_count": len(formula_scores),
        "table_case_count": len(table_scores),
        "formula_recall": sum(bool(score["rich_passed"]) for score in formula_scores)
        / len(formula_scores),
        "table_recall": sum(bool(score["rich_passed"]) for score in table_scores)
        / len(table_scores),
        "locator_accuracy": sum(bool(score["page_was_enriched"]) for score in scores)
        / len(scores),
        "baseline_failure_recovery": sum(
            not bool(score["baseline_passed"]) and bool(score["rich_passed"])
            for score in scores
        )
        / len(scores),
        "scores": scores,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mineru-executable", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--structure-gold",
        type=Path,
        default=Path("verification/pdf-structure-gold.v1.json"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    arguments = parser.parse_args()
    output_root = arguments.output_root.resolve()
    if output_root.exists():
        raise ValueError("evaluation output root already exists")
    output_root.mkdir(parents=True)
    normalized_root = output_root / "normalized-markdown"
    normalized_root.mkdir()
    parser_adapter = MineruCliParser(
        arguments.mineru_executable,
        version="4.0.4",
        tier="basic",
        timeout_seconds=arguments.timeout_seconds,
    )
    results: list[dict[str, object]] = []
    page_records: dict[str, dict[str, object]] = {}
    for case in CASES:
        workspace = output_root / f"workspace-{case['paper_id']}"
        literature = workspace / "literature"
        literature.mkdir(parents=True)
        source = arguments.corpus_root.resolve() / "papers" / str(case["filename"])
        destination = literature / str(case["filename"])
        destination.write_bytes(source.read_bytes())
        fast_started = time.perf_counter()
        reader = PdfReader(source, strict=False)
        fast_pages = tuple(
            ExtractedKnowledgePage(index, (page.extract_text() or "").strip())
            for index, page in enumerate(reader.pages, start=1)
        )
        fast_elapsed_seconds = time.perf_counter() - fast_started
        fast_by_page = {page.page_number: page.text for page in fast_pages}
        started = time.perf_counter()
        catalog = FilesystemLiteratureCatalog(
            workspace,
            pdf_parser=parser_adapter,
        )
        documents, observed, errors = catalog.extract_changed({})
        elapsed_seconds = time.perf_counter() - started
        if errors or len(documents) != 1:
            raise RuntimeError(f"MinerU extraction failed for {case['paper_id']}: {errors}")
        document = documents[0]
        markdown = "\n\n".join(
            f"<!-- page {page.page_number} -->\n{page.text}" for page in document.pages
        )
        markdown_path = normalized_root / f"{case['paper_id']}.md"
        markdown_path.write_text(markdown, encoding="utf-8")
        nodes = build_canonical_nodes(document)
        parser_score = evaluate_parser_case(
            ParserGoldCase(
                str(case["paper_id"]),
                ("section", "paragraph", "equation"),
                tuple(str(marker) for marker in case["markers"]),
                tuple(str(marker) for marker in case["markers"]),
            ),
            nodes,
        )
        page_numbers = tuple(
            page.page_number for page in document.pages if page.page_number is not None
        )
        enriched_findings = tuple(
            finding
            for finding in document.page_parse_findings
            if finding.selected_parser == "mineru-cli"
        )
        enriched_pages = tuple(sorted(finding.page_number for finding in enriched_findings))
        reason_counts = Counter(
            reason for finding in enriched_findings for reason in finding.reason_codes
        )
        merged_by_page = {
            page.page_number: page.text
            for page in document.pages
            if page.page_number is not None
        }
        page_records[str(case["paper_id"])] = {
            "fast_pages": fast_by_page,
            "rich_pages": merged_by_page,
            "enriched_pages": enriched_pages,
        }
        unselected_pages = set(page_numbers) - set(enriched_pages)
        unselected_unchanged = all(
            merged_by_page[page_number] == fast_by_page[page_number]
            for page_number in unselected_pages
        )
        fast_enriched_text = "\n\n".join(fast_by_page[page] for page in enriched_pages)
        rich_enriched_text = "\n\n".join(merged_by_page[page] for page in enriched_pages)
        results.append(
            {
                "paper_id": case["paper_id"],
                "source_sha256": _sha256(source),
                "source_bytes": source.stat().st_size,
                "parser_name": document.parser_name,
                "parser_version": document.parser_version,
                "parser_profile": document.parser_profile,
                "fast_pypdf_elapsed_seconds": fast_elapsed_seconds,
                "elapsed_seconds": elapsed_seconds,
                "observed_locators": observed,
                "expected_page_count": case["expected_pages"],
                "parsed_page_count": len(page_numbers),
                "unique_page_count": len(set(page_numbers)),
                "page_range_complete": page_numbers
                == tuple(range(1, int(case["expected_pages"]) + 1)),
                "enriched_page_count": len(enriched_pages),
                "enriched_page_ratio": len(enriched_pages) / len(page_numbers),
                "enriched_pages": enriched_pages,
                "avoided_rich_parse_page_count": len(page_numbers) - len(enriched_pages),
                "enrichment_reason_counts": dict(sorted(reason_counts.items())),
                "unselected_fast_pages_unchanged": unselected_unchanged,
                "selected_page_fast_structure": _structural_counts(fast_enriched_text),
                "selected_page_rich_structure": _structural_counts(rich_enriched_text),
                "markdown_characters": len(markdown),
                "display_equation_count": markdown.count("$$") // 2,
                "markdown_table_row_count": sum(
                    line.count("|") >= 2 for line in markdown.splitlines()
                ),
                "heading_count": sum(
                    bool(re.match(r"^#{1,6}\s+", line)) for line in markdown.splitlines()
                ),
                "parser_gold_score": asdict(parser_score),
                "normalized_markdown": str(markdown_path),
            }
        )
    structure_gold = json.loads(arguments.structure_gold.resolve().read_text(encoding="utf-8"))
    structure_score = _score_structure_gold(structure_gold, page_records)
    accepted = all(
        bool(result["page_range_complete"])
        and 0 < int(result["enriched_page_count"]) < int(result["parsed_page_count"])
        and bool(result["unselected_fast_pages_unchanged"])
        and result["parser_gold_score"]["content_marker_recall"] == 1.0
        and result["parser_gold_score"]["locator_coverage"] == 1.0
        and result["parser_gold_score"]["node_kind_recall"] == 1.0
        for result in results
    ) and all(
        float(structure_score[metric]) == 1.0
        for metric in (
            "formula_recall",
            "table_recall",
            "locator_accuracy",
            "baseline_failure_recovery",
        )
    )
    report = {
        "schema_version": "adaptive-pdf-evaluation/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "mineru_version": "4.0.4",
        "tier": "basic",
        "strategy": "pypdf_all_pages_then_selective_mineru",
        "remote_used": False,
        "paper_count": len(results),
        "total_page_count": sum(int(item["parsed_page_count"]) for item in results),
        "total_enriched_page_count": sum(
            int(item["enriched_page_count"]) for item in results
        ),
        "total_avoided_rich_parse_page_count": sum(
            int(item["avoided_rich_parse_page_count"]) for item in results
        ),
        "structure_gold_path": str(arguments.structure_gold.resolve()),
        "structure_score": structure_score,
        "accepted": accepted,
        "results": results,
    }
    report_path = output_root / "adaptive-pdf-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "accepted": accepted,
                "paper_count": len(results),
                "total_page_count": report["total_page_count"],
                "total_enriched_page_count": report["total_enriched_page_count"],
                "total_avoided_rich_parse_page_count": report[
                    "total_avoided_rich_parse_page_count"
                ],
                "elapsed_seconds": sum(float(item["elapsed_seconds"]) for item in results),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
