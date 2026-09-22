"""Build and verify the fixed real-paper corpus used by RAG evaluations."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True, slots=True)
class Paper:
    paper_id: str
    filename: str
    title: str
    authors: str
    year: int
    source_url: str
    landing_url: str
    benchmark_role: str
    topics: tuple[str, ...]


PAPERS = (
    Paper(
        "goodman-bacon-2018",
        "01-goodman-bacon-treatment-timing.pdf",
        "Difference-in-Differences with Variation in Treatment Timing",
        "Andrew Goodman-Bacon",
        2018,
        "https://www.nber.org/system/files/working_papers/w25018/w25018.pdf",
        "https://www.nber.org/papers/w25018",
        "core_method",
        ("TWFE decomposition", "staggered adoption", "negative weights"),
    ),
    Paper(
        "callaway-santanna-2021",
        "02-callaway-santanna-multiple-periods.pdf",
        "Difference-in-Differences with Multiple Time Periods",
        "Brantly Callaway and Pedro H. C. Sant'Anna",
        2021,
        "https://arxiv.org/pdf/1803.09015",
        "https://arxiv.org/abs/1803.09015",
        "core_method",
        ("group-time ATT", "doubly robust", "multiple periods"),
    ),
    Paper(
        "sun-abraham-2021",
        "03-sun-abraham-event-studies.pdf",
        (
            "Estimating Dynamic Treatment Effects in Event Studies with "
            "Heterogeneous Treatment Effects"
        ),
        "Liyang Sun and Sarah Abraham",
        2021,
        "https://arxiv.org/pdf/1804.05785",
        "https://arxiv.org/abs/1804.05785",
        "core_method",
        ("event study", "heterogeneous effects", "contamination"),
    ),
    Paper(
        "borusyak-jaravel-spiess-2024",
        "04-borusyak-jaravel-spiess-event-study.pdf",
        "Revisiting Event Study Designs: Robust and Efficient Estimation",
        "Kirill Borusyak, Xavier Jaravel, and Jann Spiess",
        2024,
        "https://arxiv.org/pdf/2108.12419",
        "https://arxiv.org/abs/2108.12419",
        "core_method",
        ("imputation", "event study", "efficient estimation"),
    ),
    Paper(
        "de-chaisemartin-dhaultfoeuille-2022",
        "05-de-chaisemartin-dhaultfoeuille-survey.pdf",
        (
            "Two-Way Fixed Effects and Differences-in-Differences with Heterogeneous "
            "Treatment Effects: A Survey"
        ),
        "Clément de Chaisemartin and Xavier D'Haultfoeuille",
        2022,
        "https://www.nber.org/system/files/working_papers/w29734/w29734.pdf",
        "https://www.nber.org/papers/w29734",
        "method_survey",
        ("TWFE", "heterogeneous effects", "Stata estimators"),
    ),
    Paper(
        "roth-2018-pretrends",
        "06-roth-pretrends.pdf",
        "Should We Adjust for the Test for Pre-trends in Difference-in-Difference Designs?",
        "Jonathan Roth",
        2018,
        "https://arxiv.org/pdf/1804.01208",
        "https://arxiv.org/abs/1804.01208",
        "diagnostics",
        ("pre-trends", "pre-testing", "conditional inference"),
    ),
    Paper(
        "rambachan-roth-2023",
        "07-rambachan-roth-parallel-trends.pdf",
        "A More Credible Approach to Parallel Trends",
        "Ashesh Rambachan and Jonathan Roth",
        2023,
        "https://asheshrambachan.github.io/assets/files/hpt-draft.pdf",
        "https://economics.mit.edu/research/publications/more-credible-approach-parallel-trends",
        "diagnostics",
        ("parallel trends", "sensitivity analysis", "robust inference"),
    ),
    Paper(
        "roth-santanna-bilinski-poe-2023",
        "08-roth-et-al-whats-trending.pdf",
        (
            "What's Trending in Difference-in-Differences? A Synthesis of the Recent "
            "Econometrics Literature"
        ),
        "Jonathan Roth, Pedro H. C. Sant'Anna, Alyssa Bilinski, and John Poe",
        2023,
        "https://arxiv.org/pdf/2201.01194",
        "https://arxiv.org/abs/2201.01194",
        "method_survey",
        ("synthesis", "practitioner recommendations", "inference"),
    ),
    Paper(
        "callaway-goodman-bacon-santanna-2021",
        "09-callaway-goodman-bacon-santanna-continuous.pdf",
        "Difference-in-Differences with a Continuous Treatment",
        "Brantly Callaway, Andrew Goodman-Bacon, and Pedro H. C. Sant'Anna",
        2021,
        "https://arxiv.org/pdf/2107.02637",
        "https://arxiv.org/abs/2107.02637",
        "method_extension",
        ("continuous treatment", "dose response", "TWFE interpretation"),
    ),
    Paper(
        "baker-et-al-2025",
        "10-baker-et-al-practitioners-guide.pdf",
        "Difference-in-Differences Designs: A Practitioner's Guide",
        (
            "Andrew Baker, Brantly Callaway, Scott Cunningham, Andrew Goodman-Bacon, "
            "and Pedro H. C. Sant'Anna"
        ),
        2025,
        "https://arxiv.org/pdf/2503.13323",
        "https://arxiv.org/abs/2503.13323",
        "selected_style_and_synthesis",
        ("practitioner guide", "design", "covariates and weights"),
    ),
)


def _download(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "StataResearchAgent-CorpusBuilder/1.0"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        content_type = response.headers.get("Content-Type", "")
        payload = response.read()
    if not payload.startswith(b"%PDF"):
        raise RuntimeError(f"source did not return a PDF ({content_type}): {url}")
    if len(payload) < 100_000:
        raise RuntimeError(f"source returned an unexpectedly small PDF: {url}")
    return payload


def _validate_pdf(path: Path) -> dict[str, object]:
    reader = PdfReader(path)
    page_count = len(reader.pages)
    if page_count < 5:
        raise RuntimeError(f"PDF has too few pages: {path}")
    sample_pages = sorted({0, page_count // 2, page_count - 1})
    extracted_characters = sum(
        len(reader.pages[index].extract_text() or "") for index in sample_pages
    )
    if extracted_characters < 500:
        raise RuntimeError(f"PDF has insufficient extractable text: {path}")
    payload = path.read_bytes()
    return {
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "page_count": page_count,
        "sample_extracted_characters": extracted_characters,
        "pdf_validated": True,
    }


def _write_readme(output_root: Path, records: list[dict[str, object]]) -> None:
    lines = [
        "# Modern Difference-in-Differences benchmark corpus",
        "",
        "This immutable evaluation corpus contains ten publicly accessible papers on modern",
        "difference-in-differences and event-study methods. Original publisher/author URLs",
        "and content hashes are recorded in `manifest.json`.",
        "",
        "The corpus is for retrieval, parsing, multi-hop reasoning, grounding, and selected",
        "style-exemplar evaluation. It is not a product knowledge whitelist.",
        "",
        "| # | Paper | Role | Pages | SHA-256 |",
        "|---:|---|---|---:|---|",
    ]
    for index, record in enumerate(records, start=1):
        lines.append(
            f"| {index} | {record['title']} | `{record['benchmark_role']}` | "
            f"{record['page_count']} | `{str(record['sha256'])[:16]}…` |"
        )
    lines.extend(
        [
            "",
            "Rebuild with:",
            "",
            "```powershell",
            ".\\.venv\\Scripts\\python.exe tools\\build_did_paper_corpus.py "
            "--output-root verification\\corpora\\did-methods-v1",
            "```",
            "",
        ]
    )
    (output_root / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    output_root = arguments.output_root.resolve()
    manifest_path = output_root / "manifest.json"
    if manifest_path.exists():
        raise ValueError("completed corpus already exists; corpus builds are immutable")
    output_root.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, object]] = []
    for paper in PAPERS:
        destination = output_root / "papers" / paper.filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            partial = destination.with_suffix(".pdf.part")
            partial.write_bytes(_download(paper.source_url))
            partial.replace(destination)
        record = asdict(paper)
        record["topics"] = list(paper.topics)
        record["relative_path"] = destination.relative_to(output_root).as_posix()
        record.update(_validate_pdf(destination))
        records.append(record)
        print(f"validated {paper.paper_id}: {record['page_count']} pages")

    manifest = {
        "schema_version": "stata-research-agent/paper-corpus/v1",
        "corpus_id": "did-methods-v1",
        "domain": "modern difference-in-differences and event-study methods",
        "created_at": datetime.now(UTC).isoformat(),
        "paper_count": len(records),
        "papers": records,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_readme(output_root, records)
    print(f"corpus ready: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
