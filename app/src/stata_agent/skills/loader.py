"""Load and match file-based skills.

Skills are ordinary directories containing a ``SKILL.md`` with YAML
frontmatter.  The loader intentionally has no PyYAML dependency: it supports
the small, interoperable frontmatter subset used by skills while producing a
path-aware error for malformed or oversized files.  Matching is metadata-only;
the full body is returned only for skills that actually match the request.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_SKILL_BYTES = 512 * 1024


class SkillLoadError(ValueError):
    """A skill cannot be safely loaded or validated."""


@dataclass
class Skill:
    slug: str
    description: str
    triggers: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    requires_ados: list[str] = field(default_factory=list)
    path: Path | None = None
    body: str = ""

    @property
    def metadata(self) -> str:
        """Metadata used for routing before the body is injected."""

        return f"{self.slug}: {self.description}"


def _scalar(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in {'"', "'"}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as error:
            raise SkillLoadError(f"invalid frontmatter scalar: {value!r}") from error
        return str(parsed)
    return value


def _inline_list(value: str) -> list[Any]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise SkillLoadError(f"invalid frontmatter list: {value!r}")
    inner = value[1:-1].strip()
    if not inner:
        return []
    try:
        parsed = ast.literal_eval(value)
        if isinstance(parsed, (list, tuple)):
            return list(parsed)
    except (SyntaxError, ValueError):
        pass

    # YAML permits unquoted values where Python literals do not.  Split only
    # at top-level commas so quoted descriptions and nested lists survive.
    items: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    for index, char in enumerate(inner):
        if quote:
            if char == quote and (index == 0 or inner[index - 1] != "\\"):
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "[({":
            depth += 1
        elif char in "])}":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(inner[start:index].strip())
            start = index + 1
    if quote or depth != 0:
        raise SkillLoadError(f"invalid frontmatter list: {value!r}")
    items.append(inner[start:].strip())
    return [_scalar(item) for item in items if item]


def _list_field(frontmatter: dict[str, Any], key: str) -> list[str]:
    """Flatten scalar/list/dict trigger forms into searchable terms."""

    raw = frontmatter.get(key, [])
    if isinstance(raw, dict):
        values: list[Any] = []
        for value in raw.values():
            values.extend(value if isinstance(value, list) else [value])
        raw = values
    if not isinstance(raw, list):
        raw = [raw]
    out: list[str] = []
    for value in raw:
        if isinstance(value, dict):
            for nested in value.values():
                out.extend(nested if isinstance(nested, list) else [nested])
        elif value is not None:
            text = str(value).strip()
            if text:
                out.append(text)
    return out


def _parse_value(value: str) -> Any:
    value = value.strip()
    if value.startswith("["):
        return _inline_list(value)
    return _scalar(value)


def _parse_frontmatter(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---[ \t]*\r?\n", raw)
    if not match:
        raise SkillLoadError(f"{path}: missing opening frontmatter delimiter (---)")
    closing = re.search(r"^---[ \t]*\r?$", raw[match.end():], re.MULTILINE)
    if closing is None:
        raise SkillLoadError(f"{path}: missing closing frontmatter delimiter (---)")
    fm_text = raw[match.end():match.end() + closing.start()]
    body = raw[match.end() + closing.end():].lstrip("\r\n").strip()

    result: dict[str, Any] = {}
    section: str | None = None
    multiline_key: str | None = None
    multiline_indent = 0
    multiline_lines: list[str] = []

    def finish_multiline() -> None:
        nonlocal multiline_key, multiline_lines
        if multiline_key is not None:
            result[multiline_key] = "\n".join(multiline_lines).strip()
        multiline_key = None
        multiline_lines = []

    for line in fm_text.splitlines():
        if multiline_key is not None:
            indent = len(line) - len(line.lstrip())
            if not line.strip() or indent > multiline_indent:
                multiline_lines.append(line.strip())
                continue
            finish_multiline()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped.startswith("-") and section is not None:
            value = stripped[1:].strip()
            if isinstance(result.get(section), list):
                if ":" in value and not value.startswith(('"', "'")):
                    nested_key, nested_value = value.split(":", 1)
                    result[section].append({nested_key.strip(): _parse_value(nested_value)})
                else:
                    result[section].append(_parse_value(value))
            continue
        if ":" not in stripped:
            raise SkillLoadError(f"{path}: malformed frontmatter line: {line!r}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise SkillLoadError(f"{path}: malformed frontmatter key: {key!r}")
        value = value.strip()
        if indent == 0:
            section = key
        elif section is None:
            raise SkillLoadError(f"{path}: nested field without a section: {line!r}")
        if value in {"|", ">"}:
            multiline_key = key
            multiline_indent = indent
            multiline_lines = []
            continue
        if not value:
            # Empty fields may be a block list or mapping.  The known list
            # fields are enough to disambiguate the skill schemas in the wild.
            result[key] = [] if key in {"triggers", "allowed_tools", "ados"} else {}
            section = key
            continue
        if indent == 0:
            result[key] = _parse_value(value)
            section = None
        elif isinstance(result.get(section), dict):
            result[section][key] = _parse_value(value)
        else:
            result[key] = _parse_value(value)
    finish_multiline()
    return result, body


def load_skill(path: str | Path, *, max_bytes: int = MAX_SKILL_BYTES) -> Skill:
    """Load one skill file, accepting either ``SKILL.md`` or a markdown file."""

    p = Path(path)
    if p.is_dir():
        p = p / "SKILL.md"
    try:
        size = p.stat().st_size
    except OSError as error:
        raise SkillLoadError(f"{p}: cannot stat skill: {error}") from error
    if size > max_bytes:
        raise SkillLoadError(f"{p}: skill is {size} bytes; limit is {max_bytes}")
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SkillLoadError(f"{p}: cannot read skill: {error}") from error
    frontmatter, body = _parse_frontmatter(text, p)
    name = str(frontmatter.get("name") or "").strip()
    if not name:
        raise SkillLoadError(f"{p}: frontmatter requires name")
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        description = next(
            (line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("#")),
            "",
        )
    requires = frontmatter.get("requires")
    requires_map = requires if isinstance(requires, dict) else frontmatter
    allowed = _list_field(frontmatter, "allowed_tools") or _list_field(frontmatter, "allowed-tools")
    ados = _list_field(requires_map, "ados") or _list_field(frontmatter, "requires_ados")
    return Skill(
        slug=name,
        description=description,
        triggers=_list_field(frontmatter, "triggers"),
        allowed_tools=allowed,
        requires_ados=ados,
        path=p,
        body=body,
    )


def _skill_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise SkillLoadError(f"{root}: skill directory does not exist")
    files: set[Path] = set(root.glob("*.md"))
    files.update(root.rglob("SKILL.md"))
    return sorted(
        path for path in files
        if not any(part.startswith((".", "_")) for part in path.relative_to(root).parts[:-1])
    )


def load_skill_dir(directory: str | Path, *, max_bytes: int = MAX_SKILL_BYTES) -> dict[str, Skill]:
    """Recursively load root markdown skills and nested ``*/SKILL.md`` files."""

    out: dict[str, Skill] = {}
    for path in _skill_files(Path(directory)):
        skill = load_skill(path, max_bytes=max_bytes)
        if skill.slug in out:
            raise SkillLoadError(
                f"duplicate skill name {skill.slug!r}: {out[skill.slug].path} and {path}"
            )
        out[skill.slug] = skill
    return out


def match_skills(skills: dict[str, Skill], user_text: str, *, phase: str | None = None) -> list[Skill]:
    """Return only metadata-matched skills; never inject the whole catalog."""

    low = (user_text or "").casefold()
    phase_low = (phase or "").casefold()
    matched: list[Skill] = []
    for skill in skills.values():
        terms = [term.casefold() for term in skill.triggers if term]
        if not terms:
            terms = [skill.slug.casefold()]
            quoted = [
                match.group(1).casefold()
                for match in re.finditer(r"[\"']([^\"']+)[\"']", skill.description)
            ]
            terms.extend(quoted)
            terms.extend(
                token for phrase in quoted
                for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}|[一-鿿]{2,}", phrase)
            )
            terms.extend(part for part in re.split(r"[-_ ]+", skill.slug.casefold()) if part)
        if phase_low:
            terms.append(phase_low)
        if any(term in low for term in terms):
            matched.append(skill)
    return matched
