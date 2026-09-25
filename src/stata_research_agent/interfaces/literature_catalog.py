"""Safe extraction of files from a Workspace's dedicated literature folder."""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import subprocess
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pypdf import PdfReader

from stata_research_agent.application.knowledge_retrieval import (
    CorpusRole,
    ExtractedKnowledgeDocument,
    ExtractedKnowledgePage,
    KnowledgeSourceKind,
    PageParseFinding,
    retrieval_tokens,
)

_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_EXTRACTED_CHARACTERS = 4_000_000
_TEXT_SUFFIXES = {".txt", ".md", ".tex", ".do", ".ado", ".sthlp", ".csv", ".html", ".htm"}


@dataclass(frozen=True, slots=True)
class RichParseOutput:
    pages: tuple[ExtractedKnowledgePage, ...]
    parser_name: str
    parser_version: str
    parser_profile: str
    page_parse_findings: tuple[PageParseFinding, ...] = ()


@dataclass(frozen=True, slots=True)
class PdfPageDiagnostic:
    page_number: int
    score: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PdfEnrichmentPlan:
    diagnostics: tuple[PdfPageDiagnostic, ...]
    selected_pages: tuple[int, ...]

    @property
    def page_specification(self) -> str:
        return _compact_page_ranges(self.selected_pages)


MineruRunner = Callable[[list[str], int], tuple[int, str, str]]


def _run_mineru(command: list[str], timeout_seconds: int) -> tuple[int, str, str]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    return completed.returncode, completed.stdout, completed.stderr


class MineruCliParser:
    """Optional MinerU 4 CLI adapter; its output is normalized into the stable IR."""

    def __init__(
        self,
        executable: Path,
        *,
        version: str,
        tier: Literal["basic", "standard", "advanced"] = "basic",
        timeout_seconds: int = 1800,
        runner: MineruRunner = _run_mineru,
    ) -> None:
        self._executable = executable.resolve(strict=True)
        self._version = version.strip()
        self._tier = tier
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        if not self._version or timeout_seconds < 1:
            raise ValueError("MinerU parser requires version and positive timeout")

    @property
    def tier(self) -> str:
        return self._tier

    def parse(self, path: Path) -> RichParseOutput:
        return self._parse(path, "all")

    def parse_pages(self, path: Path, page_numbers: tuple[int, ...]) -> RichParseOutput:
        """Parse only selected one-based PDF pages, preserving their source locators."""

        if not page_numbers:
            raise ValueError("selective MinerU parse requires at least one page")
        if any(page < 1 for page in page_numbers) or len(set(page_numbers)) != len(page_numbers):
            raise ValueError("selective MinerU pages must be unique positive integers")
        return self._parse(path, _compact_page_ranges(tuple(sorted(page_numbers))))

    def _parse(self, path: Path, page_specification: str) -> RichParseOutput:
        code, stdout, stderr = self._runner(
            [
                str(self._executable),
                "parse",
                str(path.resolve(strict=True)),
                "--tier",
                self._tier,
                "--pages",
                page_specification,
                "--limit",
                str(_MAX_EXTRACTED_CHARACTERS),
                "--wait",
                str(self._timeout_seconds),
                "--json",
            ],
            self._timeout_seconds,
        )
        if code != 0:
            detail = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error"
            raise RuntimeError(f"MinerU parse failed: {detail[:300]}")
        try:
            response: object = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ValueError("MinerU --json returned invalid JSON") from error
        pages = self._markdown_pages(response)
        if not pages:
            raise ValueError("MinerU returned no Markdown content")
        return RichParseOutput(
            pages,
            "mineru-cli",
            self._version,
            f"mineru_{self._tier}",
        )

    @staticmethod
    def _markdown_pages(response: object) -> tuple[ExtractedKnowledgePage, ...]:
        if not isinstance(response, dict):
            return ()
        direct = response.get("content")
        content = direct if isinstance(direct, str) else ""
        if isinstance(direct, dict) and isinstance(direct.get("content"), str):
            content = str(direct["content"])
        result = response.get("result")
        if not content and isinstance(result, dict) and isinstance(result.get("content"), str):
            content = str(result["content"])
        if not content.strip():
            return ()
        markers = tuple(
            re.finditer(r"<!--\s*page\s+(\d+)\s+of\s+\d+\s*-->", content, re.I)
        )
        if not markers:
            return (ExtractedKnowledgePage(None, content.strip()),)
        pages: list[ExtractedKnowledgePage] = []
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(content)
            page_text = content[marker.end() : end].strip()
            if page_text:
                pages.append(ExtractedKnowledgePage(int(marker.group(1)), page_text))
        return tuple(pages)


def _compact_page_ranges(page_numbers: tuple[int, ...]) -> str:
    if not page_numbers:
        raise ValueError("page range cannot be empty")
    ranges: list[str] = []
    start = previous = page_numbers[0]
    for page in page_numbers[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


class PdfPageEscalationPolicy:
    """Choose exceptional pages for rich layout parsing after a FAST pypdf pass."""

    _MATH_GLYPHS = re.compile(r"[∑∫∂≈≠≤≥∞√αβγδθλμστφΩ]")
    _EQUATION_LINE = re.compile(
        r"(?:^|\s)(?:E|Var|Cov|Pr)\s*[\[(]|[=<>]\s*[-+]?\d|\b(?:lim|argmin|argmax)\b",
        re.I,
    )
    _TABLE_CAPTION = re.compile(r"(?:^|\n)\s*table\s+[A-Z]?\d+", re.I)
    _COMMON_ENGLISH_TOKENS = frozenset(
        "the of and to in a is that for as with by on are this we firms firm investment "
        "cash flow financial capital debt corporate model our from be or an which their"
        .split()
    )

    def __init__(
        self,
        *,
        minimum_score: int = 3,
        max_page_ratio: float = 0.15,
        max_pages: int = 24,
    ) -> None:
        if minimum_score < 1 or not 0 < max_page_ratio <= 1 or max_pages < 1:
            raise ValueError("invalid PDF escalation policy")
        self._minimum_score = minimum_score
        self._max_page_ratio = max_page_ratio
        self._max_pages = max_pages

    def plan(self, pages: tuple[ExtractedKnowledgePage, ...]) -> PdfEnrichmentPlan:
        if not pages or any(page.page_number is None for page in pages):
            raise ValueError("PDF escalation requires numbered FAST pages")
        diagnostics: list[PdfPageDiagnostic] = []
        critical_pages: set[int] = set()
        cross_page_pairs: set[int] = set()
        for index, page in enumerate(pages):
            assert page.page_number is not None
            text = page.text
            reasons: list[str] = []
            score = 0
            compact = re.sub(r"\s+", " ", text).strip()
            if len(compact) < 80:
                reasons.append("missing_or_scanned_text")
                score += 10
                critical_pages.add(page.page_number)
            replacement_ratio = text.count("�") / max(1, len(text))
            control_count = sum(
                ord(character) < 32 and character not in "\n\r\t" for character in text
            )
            if replacement_ratio > 0.002:
                reasons.append("corrupt_text_extraction")
                score += 10
                critical_pages.add(page.page_number)
            elif control_count / max(1, len(text)) > 0.005:
                reasons.append("undecoded_layout_glyphs")
                score += 3
            ascii_tokens = re.findall(r"[A-Za-z]{2,}", text)
            if len(ascii_tokens) >= 80:
                common_ratio = sum(
                    token.casefold() in self._COMMON_ENGLISH_TOKENS
                    for token in ascii_tokens
                ) / len(ascii_tokens)
                mixed_case_ratio = sum(
                    not (token.islower() or token.isupper() or token.istitle())
                    for token in ascii_tokens
                ) / len(ascii_tokens)
                if common_ratio < 0.04 and mixed_case_ratio > 0.10:
                    # Some older PDFs expose a non-empty but ciphered/glyph-mapped text
                    # layer.  Length and replacement-character checks miss it entirely.
                    reasons.append("corrupt_text_extraction")
                    score += 10
                    critical_pages.add(page.page_number)
            word_count = len(re.findall(r"\b[\w'-]+\b", text))
            if len(compact) < 400 and word_count < 45:
                reasons.append("low_text_density")
                score += 2
            math_glyphs = len(self._MATH_GLYPHS.findall(text))
            equation_lines = sum(
                bool(self._EQUATION_LINE.search(line))
                for line in text.splitlines()
                if 3 <= len(line.strip()) <= 180
            )
            if math_glyphs >= 4 or equation_lines >= 4:
                reasons.append("formula_layout_candidate")
                score += 3
            numeric_cells = len(re.findall(r"(?<!\w)[-+]?\d+(?:\.\d+)?(?:\*+)?(?!\w)", text))
            aligned_rows = sum(
                len(re.split(r"\s{2,}", line.strip())) >= 3 for line in text.splitlines()
            )
            if self._TABLE_CAPTION.search(text) and (numeric_cells >= 18 or aligned_rows >= 4):
                reasons.append("table_layout_candidate")
                score += 4
            if index + 1 < len(pages):
                next_text = pages[index + 1].text.lstrip()
                last_line = text.rstrip().splitlines()[-1] if text.strip() else ""
                begins_as_continuation = bool(re.match(r"[a-z,;:)\]]", next_text))
                hyphenated_boundary = last_line.endswith("-")
                ends_as_continuation = bool(
                    hyphenated_boundary
                    or (
                        len(last_line) > 30
                        and not re.search(r"[.!?:;\])}\"']$", last_line)
                    )
                )
                if begins_as_continuation and ends_as_continuation:
                    reasons.append("cross_page_continuation")
                    score += 3 if hyphenated_boundary else 2
                    next_page_number = pages[index + 1].page_number
                    if page.page_number is not None and next_page_number is not None:
                        cross_page_pairs.update((page.page_number, next_page_number))
            if reasons:
                diagnostics.append(PdfPageDiagnostic(page.page_number, score, tuple(reasons)))

        eligible = [item for item in diagnostics if item.score >= self._minimum_score]
        cap = min(self._max_pages, max(1, ceil(len(pages) * self._max_page_ratio)))
        selected: set[int] = set(critical_pages)
        for item in sorted(eligible, key=lambda value: (-value.score, value.page_number)):
            if len(selected) >= cap and item.page_number not in critical_pages:
                continue
            selected.add(item.page_number)
            if "cross_page_continuation" in item.reason_codes:
                for paired_page in sorted(cross_page_pairs):
                    if abs(paired_page - item.page_number) <= 1 and len(selected) < cap:
                        selected.add(paired_page)
        return PdfEnrichmentPlan(tuple(diagnostics), tuple(sorted(selected)))


class AdaptivePdfParser:
    """Keep pypdf as the breadth path and use MinerU only for suspicious pages."""

    def __init__(
        self,
        rich_parser: MineruCliParser,
        *,
        policy: PdfPageEscalationPolicy | None = None,
    ) -> None:
        self._rich_parser = rich_parser
        self._policy = policy or PdfPageEscalationPolicy()

    def parse(
        self,
        path: Path,
        fast_pages: tuple[ExtractedKnowledgePage, ...],
    ) -> RichParseOutput:
        plan = self._policy.plan(fast_pages)
        if not plan.selected_pages:
            return RichParseOutput(
                fast_pages,
                "adaptive-pdf",
                "1",
                "pypdf_fast_only",
                tuple(
                    PageParseFinding(
                        item.page_number,
                        item.reason_codes,
                        item.score,
                        "pypdf",
                    )
                    for item in plan.diagnostics
                ),
            )
        try:
            enriched = self._rich_parser.parse_pages(path, plan.selected_pages)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            # Rich parsing is an enrichment projection.  A failed exceptional-page pass
            # must not throw away the complete pypdf extraction or make a new paper
            # unavailable.  The finding remains visible for diagnosis and later reparse.
            diagnostic_by_page = {item.page_number: item for item in plan.diagnostics}
            return RichParseOutput(
                fast_pages,
                "adaptive-pdf",
                "1",
                f"pypdf_fast+mineru_{self._rich_parser.tier}_degraded-v1",
                tuple(
                    PageParseFinding(
                        page_number,
                        (*diagnostic_by_page[page_number].reason_codes, "rich_parser_failed"),
                        diagnostic_by_page[page_number].score,
                        "pypdf_fallback",
                    )
                    for page_number in plan.selected_pages
                ),
            )
        enriched_by_page = {
            page.page_number: page for page in enriched.pages if page.page_number is not None
        }
        missing = set(plan.selected_pages) - set(enriched_by_page)
        if missing:
            raise ValueError(f"MinerU omitted requested pages: {sorted(missing)}")
        merged = tuple(
            enriched_by_page.get(page.page_number, page)
            if page.page_number is not None
            else page
            for page in fast_pages
        )
        diagnostic_by_page = {item.page_number: item for item in plan.diagnostics}
        findings = tuple(
            PageParseFinding(
                page_number,
                diagnostic_by_page[page_number].reason_codes,
                diagnostic_by_page[page_number].score,
                "mineru-cli",
            )
            for page_number in plan.selected_pages
        )
        retained_findings = tuple(
            PageParseFinding(
                item.page_number,
                item.reason_codes,
                item.score,
                "pypdf",
            )
            for item in plan.diagnostics
            if item.page_number not in plan.selected_pages
        )
        return RichParseOutput(
            merged,
            "adaptive-pdf",
            "1",
            f"pypdf_fast+mineru_{self._rich_parser.tier}_selective-v1",
            findings + retained_findings,
        )


def discover_stata_help_roots() -> tuple[tuple[str, Path], ...]:
    """Discover conventional Windows Stata 18 and user ado roots without requiring them."""

    candidates: list[tuple[str, Path]] = []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(("base", Path(program_files) / "Stata18" / "ado" / "base"))
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidates.append(("plus", Path(user_profile) / "ado" / "plus"))
        candidates.append(("personal", Path(user_profile) / "ado" / "personal"))
    seen: set[Path] = set()
    result: list[tuple[str, Path]] = []
    for scope, candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_dir() or resolved.is_symlink():
            continue
        seen.add(resolved)
        result.append((scope, resolved))
    return tuple(result)


class FilesystemLiteratureCatalog:
    def __init__(
        self,
        workspace_root: Path,
        *,
        folder_name: str = "literature",
        corpus_role: CorpusRole = CorpusRole.LITERATURE_EVIDENCE,
        pdf_parser: MineruCliParser | None = None,
        pdf_enrichment_policy: PdfPageEscalationPolicy | None = None,
    ) -> None:
        self._workspace = workspace_root.resolve(strict=True)
        if not folder_name or "/" in folder_name or "\\" in folder_name or folder_name == "..":
            raise ValueError("knowledge folder name must be a direct Workspace child")
        self._folder_name = folder_name
        self._corpus_role = corpus_role
        self._pdf_parser = (
            AdaptivePdfParser(pdf_parser, policy=pdf_enrichment_policy)
            if pdf_parser is not None
            else None
        )
        self._root = self._workspace / folder_name

    def extract_changed(
        self, known_hashes: dict[str, str]
    ) -> tuple[
        tuple[ExtractedKnowledgeDocument, ...],
        tuple[str, ...],
        tuple[tuple[str, str], ...],
    ]:
        if not self._root.exists():
            return (), (), ()
        if self._root.is_symlink() or not self._root.is_dir():
            raise ValueError("Workspace literature root must be a regular directory")
        documents: list[ExtractedKnowledgeDocument] = []
        observed: list[str] = []
        errors: list[tuple[str, str]] = []
        for path in sorted(self._root.rglob("*"), key=lambda item: item.as_posix().lower()):
            if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES | {".pdf", ".docx"}:
                continue
            relative = path.relative_to(self._workspace).as_posix()
            observed.append(relative)
            try:
                self._require_safe_file(path)
                payload = path.read_bytes()
                digest = hashlib.sha256(payload).hexdigest()
                if known_hashes.get(relative) == digest:
                    continue
                (
                    pages,
                    media_type,
                    parser_name,
                    parser_version,
                    parser_profile,
                    page_parse_findings,
                ) = self._extract(path, payload)
                documents.append(
                    ExtractedKnowledgeDocument(
                        relative,
                        media_type,
                        digest,
                        len(payload),
                        pages,
                        (self._corpus_role,),
                        KnowledgeSourceKind.LOCAL_FILE,
                        parser_name,
                        parser_version,
                        parser_profile,
                        page_parse_findings,
                    )
                )
            except Exception as error:
                errors.append((relative, type(error).__name__))
        return tuple(documents), tuple(observed), tuple(errors)

    def _require_safe_file(self, path: Path) -> None:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self._root.resolve(strict=True)):
            raise ValueError("literature file escaped the Workspace")
        if path.is_symlink() or not resolved.is_file():
            raise ValueError("literature entry must be a regular file")
        stat = resolved.stat()
        if getattr(stat, "st_file_attributes", 0) & getattr(
            os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise ValueError("literature entry cannot be a reparse point")
        if stat.st_size > _MAX_FILE_BYTES:
            raise ValueError("literature file exceeds the extraction limit")

    def _extract(
        self,
        path: Path, payload: bytes
    ) -> tuple[
        tuple[ExtractedKnowledgePage, ...],
        str,
        str,
        str,
        str,
        tuple[PageParseFinding, ...],
    ]:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            reader = PdfReader(io.BytesIO(payload), strict=False)
            fast_pages = tuple(
                ExtractedKnowledgePage(index, (page.extract_text() or "").strip())
                for index, page in enumerate(reader.pages, start=1)
            )
            if self._pdf_parser is not None:
                parsed = self._pdf_parser.parse(path, fast_pages)
                pages = parsed.pages
                parser_name = parsed.parser_name
                parser_version = parsed.parser_version
                parser_profile = parsed.parser_profile
                page_parse_findings = parsed.page_parse_findings
            else:
                pages = fast_pages
                parser_name = "pypdf"
                parser_version = "6"
                parser_profile = "fast"
                page_parse_findings = ()
            media_type = "application/pdf"
        elif suffix == ".docx":
            with zipfile.ZipFile(io.BytesIO(payload)) as package:
                xml = package.read("word/document.xml")
            root = ElementTree.fromstring(xml)
            text = "\n".join(
                "".join(node.itertext()).strip()
                for node in root.iter()
                if node.tag.endswith("}p")
            )
            pages = (ExtractedKnowledgePage(None, text.strip()),)
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            parser_name = "docx-ooxml"
            parser_version = "1"
            parser_profile = "fast"
            page_parse_findings = ()
        else:
            text = payload.decode("utf-8-sig", errors="strict")
            if suffix in {".html", ".htm"}:
                text = html.unescape(re.sub(r"<[^>]+>", " ", text))
            pages = (ExtractedKnowledgePage(None, text.strip()),)
            media_type = "text/plain"
            parser_name = "builtin-text"
            parser_version = "1"
            parser_profile = "fast"
            page_parse_findings = ()
        total = sum(len(page.text) for page in pages)
        if total == 0:
            raise ValueError("literature extraction produced no text")
        if total > _MAX_EXTRACTED_CHARACTERS:
            raise ValueError("literature extracted text exceeds the supported limit")
        return (
            pages,
            media_type,
            parser_name,
            parser_version,
            parser_profile,
            page_parse_findings,
        )


class StataHelpCatalog:
    """Read the actual local Stata/ado help roots without assuming installation paths."""

    def __init__(self, roots: tuple[tuple[str, Path], ...]) -> None:
        normalized: list[tuple[str, Path]] = []
        for scope, root in roots:
            clean_scope = scope.strip().lower()
            if not clean_scope or not re.fullmatch(r"[a-z0-9_.-]+", clean_scope):
                raise ValueError("Stata help scope must be a stable path label")
            resolved = root.resolve(strict=True)
            if resolved.is_symlink() or not resolved.is_dir():
                raise ValueError("Stata help root must be a regular directory")
            normalized.append((clean_scope, resolved))
        self._roots = tuple(normalized)

    def extract_changed(
        self, known_hashes: dict[str, str]
    ) -> tuple[
        tuple[ExtractedKnowledgeDocument, ...],
        tuple[str, ...],
        tuple[tuple[str, str], ...],
    ]:
        documents: list[ExtractedKnowledgeDocument] = []
        observed: list[str] = []
        errors: list[tuple[str, str]] = []
        for scope, root in self._roots:
            for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
                if not path.is_file() or path.suffix.lower() not in {".sthlp", ".hlp"}:
                    continue
                relative_inside_root = path.relative_to(root).as_posix()
                locator = f"stata-help/{scope}/{relative_inside_root}"
                observed.append(locator)
                try:
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(root) or path.is_symlink():
                        raise ValueError("Stata help file escaped its registered root")
                    stat = resolved.stat()
                    if stat.st_size > _MAX_FILE_BYTES:
                        raise ValueError("Stata help file exceeds the extraction limit")
                    payload = resolved.read_bytes()
                    digest = hashlib.sha256(payload).hexdigest()
                    if known_hashes.get(locator) == digest:
                        continue
                    text = payload.decode("utf-8-sig", errors="replace")
                    normalized = _normalize_stata_smcl(text)
                    if not normalized.strip():
                        raise ValueError("Stata help extraction produced no text")
                    documents.append(
                        ExtractedKnowledgeDocument(
                            locator,
                            "application/x-stata-help",
                            digest,
                            len(payload),
                            (ExtractedKnowledgePage(None, normalized),),
                            (CorpusRole.STATA_HELP,),
                            KnowledgeSourceKind.STATA_HELP,
                            "stata-smcl-fast",
                            "1",
                            "fast",
                        )
                    )
                except Exception as error:
                    errors.append((locator, type(error).__name__))
        return tuple(documents), tuple(observed), tuple(errors)

    def extract_candidates(
        self,
        query: str,
        known_hashes: dict[str, str],
        *,
        max_sources: int = 24,
    ) -> tuple[
        tuple[ExtractedKnowledgeDocument, ...],
        tuple[str, ...],
        tuple[tuple[str, str], ...],
    ]:
        """Parse only query-relevant help files for a bounded first-use bootstrap."""

        query_terms = {term.casefold() for term in retrieval_tokens(query) if len(term) >= 2}
        if not query_terms:
            return (), (), ()
        candidates: list[tuple[int, str, Path, bytes]] = []
        for scope, root in self._roots:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in {".sthlp", ".hlp"}:
                    continue
                locator = f"stata-help/{scope}/{path.relative_to(root).as_posix()}"
                try:
                    payload = path.read_bytes()
                except OSError:
                    continue
                raw = payload.decode("utf-8-sig", errors="replace").casefold()
                stem = path.stem.casefold()
                score = sum(30 for term in query_terms if term == stem)
                score += sum(12 for term in query_terms if term in stem)
                score += sum(min(raw.count(term), 5) for term in query_terms)
                if score:
                    candidates.append((score, locator, path, payload))
        selected = sorted(candidates, key=lambda item: (-item[0], item[1]))[:max_sources]
        documents: list[ExtractedKnowledgeDocument] = []
        observed: list[str] = []
        errors: list[tuple[str, str]] = []
        for _score, locator, path, payload in selected:
            observed.append(locator)
            try:
                resolved = path.resolve(strict=True)
                if path.is_symlink() or not any(
                    resolved.is_relative_to(root) for _scope, root in self._roots
                ):
                    raise ValueError("Stata help file escaped its registered root")
                digest = hashlib.sha256(payload).hexdigest()
                if known_hashes.get(locator) == digest:
                    continue
                text = _normalize_stata_smcl(payload.decode("utf-8-sig", errors="replace"))
                if not text:
                    raise ValueError("Stata help extraction produced no text")
                documents.append(
                    ExtractedKnowledgeDocument(
                        locator,
                        "application/x-stata-help",
                        digest,
                        len(payload),
                        (ExtractedKnowledgePage(None, text),),
                        (CorpusRole.STATA_HELP,),
                        KnowledgeSourceKind.STATA_HELP,
                        "stata-smcl-fast",
                        "1",
                        "fast",
                    )
                )
            except Exception as error:
                errors.append((locator, type(error).__name__))
        return tuple(documents), tuple(observed), tuple(errors)


def _normalize_stata_smcl(value: str) -> str:
    """Preserve searchable Stata semantics while removing presentation-only SMCL."""

    text = re.sub(r"\{title:([^}]*)\}", r"\n\n# \1\n\n", value)
    text = re.sub(r"\{marker\s+[^}]+\}", "\n", text)
    text = re.sub(r"\{(?:pstd|phang|pmore|p_end|synoptset[^}]*)\}", "\n", text)
    text = re.sub(r"\{(?:cmd|opt|it|bf|hi|help|manhelp|vieweralsosee)[^:}]*:([^}]*)\}", r"\1", text)
    text = re.sub(r"\{[^}]*\}", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
