"""Build an immutable paper corpus from a versioned JSON source manifest.

The source manifest is kept in the repository.  PDF payloads, validation records, and
the completed corpus manifest may live on a large external drive.  A failed download
is preserved as a ``.part`` file only until the next attempt; completed corpus records
are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _download(url: str, destination: Path, *, attempts: int = 3) -> None:
    partial = destination.with_suffix(destination.suffix + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "StataResearchAgent-CorpusBuilder/2.0",
                "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                payload = response.read()
            if not payload.startswith(b"%PDF"):
                raise RuntimeError(f"source did not return a PDF: {url}")
            if len(payload) < 50_000:
                raise RuntimeError(f"source returned an unexpectedly small PDF: {url}")
            partial.write_bytes(payload)
            partial.replace(destination)
            return
        except (OSError, RuntimeError, urllib.error.URLError) as error:
            last_error = error
            if attempt < attempts:
                time.sleep(float(attempt * 2))
    raise RuntimeError(f"failed to download after {attempts} attempts: {url}") from last_error


def _validate_pdf(path: Path) -> dict[str, object]:
    reader = PdfReader(path)
    page_count = len(reader.pages)
    if page_count < 3:
        raise RuntimeError(f"PDF has too few pages: {path}")
    sample_pages = sorted({0, page_count // 4, page_count // 2, page_count - 1})
    sample_characters = sum(
        len(reader.pages[index].extract_text() or "") for index in sample_pages
    )
    payload = path.read_bytes()
    return {
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "page_count": page_count,
        "sample_extracted_characters": sample_characters,
        "pypdf_text_usable": sample_characters >= 500,
        "pdf_validated": True,
    }


def _load_source_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = _mapping(json.loads(path.read_text(encoding="utf-8")), "source manifest")
    if root.get("schema_version") != "stata-research-agent/paper-corpus-sources/v1":
        raise ValueError("unsupported paper corpus source manifest")
    papers = root.get("papers")
    if not isinstance(papers, list) or not papers:
        raise ValueError("source manifest papers must be a non-empty array")
    records = [_mapping(item, f"papers[{index}]") for index, item in enumerate(papers)]
    paper_ids = [_text(item.get("paper_id"), "paper_id") for item in records]
    filenames = [_text(item.get("filename"), "filename") for item in records]
    if len(paper_ids) != len(set(paper_ids)):
        raise ValueError("paper_id values must be unique")
    if len(filenames) != len(set(filenames)):
        raise ValueError("filename values must be unique")
    return root, records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    source_path = arguments.sources.resolve()
    output_root = arguments.output_root.resolve()
    manifest_path = output_root / "manifest.json"
    if manifest_path.exists():
        raise ValueError("completed corpus already exists; corpus builds are immutable")

    source_manifest, papers = _load_source_manifest(source_path)
    output_root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for index, paper in enumerate(papers, start=1):
        paper_id = _text(paper.get("paper_id"), "paper_id")
        filename = _text(paper.get("filename"), "filename")
        source_urls = paper.get("source_urls")
        if not isinstance(source_urls, list) or not source_urls:
            raise ValueError(f"{paper_id}: source_urls must be a non-empty array")
        destination = output_root / "papers" / filename
        selected_url: str | None = None
        errors: list[str] = []
        if not destination.exists():
            for raw_url in source_urls:
                url = _text(raw_url, f"{paper_id}.source_url")
                try:
                    _download(url, destination)
                    selected_url = url
                    break
                except RuntimeError as error:
                    errors.append(str(error))
            if selected_url is None:
                raise RuntimeError(f"{paper_id}: every source failed: {' | '.join(errors)}")
        else:
            selected_url = _text(source_urls[0], f"{paper_id}.source_url")

        validation = _validate_pdf(destination)
        record = dict(paper)
        record["selected_source_url"] = selected_url
        record["relative_path"] = destination.relative_to(output_root).as_posix()
        record.update(validation)
        records.append(record)
        print(
            f"[{index}/{len(papers)}] {paper_id}: "
            f"{validation['page_count']} pages, {validation['size_bytes']} bytes"
        )

    manifest = {
        "schema_version": "stata-research-agent/paper-corpus/v1",
        "corpus_id": _text(source_manifest.get("corpus_id"), "corpus_id"),
        "domain": _text(source_manifest.get("domain"), "domain"),
        "source_manifest": str(source_path),
        "source_manifest_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "created_at": datetime.now(UTC).isoformat(),
        "paper_count": len(records),
        "papers": records,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"corpus ready: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
