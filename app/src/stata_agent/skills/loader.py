"""Load and match file-based skills.

Skills are ordinary directories containing a ``SKILL.md`` with YAML
frontmatter.  The loader intentionally has no PyYAML dependency: it supports
the small, interoperable frontmatter subset used by skills while producing a
path-aware error for malformed or oversized files.  Matching is metadata-only;
the full body is returned only for skills that actually match the request.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

MAX_SKILL_BYTES = 512 * 1024
MAX_SKILL_ITEMS = 64
MAX_SKILL_FIELD_CHARS = 4096
MAX_SKILL_DEPTH = 4

_ALLOWED_FRONTMATTER = frozenset({
    "name", "description", "version", "schema_version", "role", "triggers",
    "allowed_tools", "allowed-tools", "requires", "requires_ados", "ados",
    "prechecks", "steps", "rules", "examples", "disabled_when", "evolution",
})
_ALLOWED_ROLES = frozenset({"methodology", "robustness", "mechanism", "writing"})
_TRIGGER_KEYS = frozenset({"phase", "phases", "needs", "need", "terms", "triggers"})
_REQUIRES_KEYS = frozenset({"ados", "stata_min"})
_EVOLUTION_KEYS = frozenset({"stable", "source", "task", "method", "episode_count"})


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
    # Skill V2 metadata.  The legacy fields above intentionally remain the
    # public compatibility surface used by the loop and older integrations.
    schema_version: int = 1
    version: str = "0.0.0"
    role: str = "methodology"
    trigger_phases: tuple[str, ...] = ()
    trigger_needs: tuple[str, ...] = ()
    trigger_text_terms: tuple[str, ...] = ()
    requires_stata_min: str | None = None
    prechecks: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    rules: tuple[Any, ...] = ()
    examples: tuple[Any, ...] = ()
    disabled_when: tuple[Any, ...] = ()
    # Evolution provenance is metadata, never a source of tool permissions or
    # executable code.  Keeping it on the loaded object lets the governance
    # layer re-check a staged candidate without another parser.
    evolution: Mapping[str, Any] = field(default_factory=dict)

    @property
    def metadata(self) -> str:
        """Metadata used for routing before the body is injected."""

        return f"{self.slug}: {self.description}"


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value.casefold() == "true":
        return True
    if value.casefold() == "false":
        return False
    if value.casefold() in {"null", "~"}:
        return None
    if value[0] in {'"', "'"}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as error:
            raise SkillLoadError(f"invalid frontmatter scalar: {value!r}") from error
        return str(parsed)
    if re.fullmatch(r"[+-]?\d+", value):
        try:
            return int(value)
        except ValueError:  # pragma: no cover - regex already bounds the input
            pass
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)", value):
        try:
            return float(value)
        except ValueError:  # pragma: no cover - regex already bounds the input
            pass
    return value


def _inline_list(value: str) -> list[Any] | dict[str, Any]:
    value = value.strip()
    if value.startswith("{") and value.endswith("}"):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError) as error:
                raise SkillLoadError(f"invalid frontmatter mapping: {value!r}") from error
        if not isinstance(parsed, dict):
            raise SkillLoadError(f"invalid frontmatter mapping: {value!r}")
        return {str(key): item for key, item in parsed.items()}
    if not (value.startswith("[") and value.endswith("]")):
        raise SkillLoadError(f"invalid frontmatter list: {value!r}")
    inner = value[1:-1].strip()
    if not inner:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    except (TypeError, ValueError):
        pass
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
    return [_parse_value(item) for item in items if item]


def _list_field(frontmatter: Mapping[str, Any], key: str) -> list[str]:
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
    if value.startswith("[") or value.startswith("{"):
        return _inline_list(value)
    return _scalar(value)


def _frontmatter_lines(text: str) -> list[tuple[int, str, int]]:
    """Return non-empty frontmatter lines with their indentation and number."""

    out: list[tuple[int, str, int]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if "\t" in raw_line[:indent]:
            raise SkillLoadError(f"frontmatter line {line_no}: tabs are not supported")
        out.append((indent, raw_line.strip(), line_no))
    return out


def _bounded_text(value: Any, *, field_name: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SkillLoadError(f"frontmatter {field_name} must be a string")
    text = value.strip()
    if not allow_empty and not text:
        raise SkillLoadError(f"frontmatter {field_name} must not be empty")
    if len(text) > MAX_SKILL_FIELD_CHARS:
        raise SkillLoadError(f"frontmatter {field_name} exceeds {MAX_SKILL_FIELD_CHARS} characters")
    return text


def _bounded_string_list(raw: Any, *, field_name: str, allow_scalar: bool = False) -> tuple[str, ...]:
    if raw is None or raw == {}:
        return ()
    if isinstance(raw, str) and allow_scalar:
        values: list[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        raise SkillLoadError(f"frontmatter {field_name} must be a list")
    if len(values) > MAX_SKILL_ITEMS:
        raise SkillLoadError(f"frontmatter {field_name} has too many items")
    result: list[str] = []
    for value in values:
        result.append(_bounded_text(value, field_name=field_name))
    return tuple(dict.fromkeys(result))


def _bounded_jsonish(value: Any, *, field_name: str, depth: int = 0) -> Any:
    """Copy a parsed frontmatter value while rejecting unbounded structures."""

    if depth > MAX_SKILL_DEPTH:
        raise SkillLoadError(f"frontmatter {field_name} is too deeply nested")
    if isinstance(value, str):
        return _bounded_text(value, field_name=field_name, allow_empty=True)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_SKILL_ITEMS:
            raise SkillLoadError(f"frontmatter {field_name} has too many items")
        return tuple(_bounded_jsonish(item, field_name=field_name, depth=depth + 1) for item in value)
    if isinstance(value, Mapping):
        if len(value) > MAX_SKILL_ITEMS:
            raise SkillLoadError(f"frontmatter {field_name} has too many keys")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
                raise SkillLoadError(f"frontmatter {field_name} has an invalid mapping key")
            result[key] = _bounded_jsonish(item, field_name=field_name, depth=depth + 1)
        return result
    raise SkillLoadError(f"frontmatter {field_name} contains an unsupported value")


def _trigger_values(raw: Any, *, field_name: str) -> tuple[list[str], tuple[str, ...], tuple[str, ...]]:
    """Return legacy terms plus structured phase/needs constraints."""

    if raw is None or raw == {}:
        return [], (), ()
    values: list[Any]
    if isinstance(raw, Mapping):
        values = [raw]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    elif isinstance(raw, str):
        values = [raw]
    else:
        raise SkillLoadError(f"frontmatter {field_name} must be a list, mapping, or string")
    if len(values) > MAX_SKILL_ITEMS:
        raise SkillLoadError(f"frontmatter {field_name} has too many items")
    terms: list[str] = []
    phases: list[str] = []
    needs: list[str] = []

    def add_strings(target: list[str], value: Any, name: str) -> None:
        if isinstance(value, str):
            items: Iterable[Any] = [value]
        elif isinstance(value, (list, tuple)):
            items = value
        else:
            raise SkillLoadError(f"frontmatter trigger {name} must be a string or list")
        for item in items:
            text = _bounded_text(item, field_name=f"triggers.{name}")
            if text not in target:
                target.append(text)

    for value in values:
        if isinstance(value, str):
            text = _bounded_text(value, field_name=field_name)
            if text not in terms:
                terms.append(text)
            continue
        if not isinstance(value, Mapping):
            raise SkillLoadError(f"frontmatter {field_name} items must be strings or mappings")
        unknown = set(value) - _TRIGGER_KEYS
        if unknown:
            raise SkillLoadError(f"frontmatter triggers has unknown keys: {sorted(unknown)!r}")
        if "phase" in value:
            add_strings(phases, value["phase"], "phase")
        if "phases" in value:
            add_strings(phases, value["phases"], "phases")
        if "needs" in value:
            add_strings(needs, value["needs"], "needs")
        if "need" in value:
            add_strings(needs, value["need"], "need")
        for key in ("terms", "triggers"):
            if key in value:
                add_strings(terms, value[key], key)
    return terms, tuple(phases), tuple(needs)


def _disabled_match(value: Any, *, low_text: str, phase_low: str) -> bool:
    if isinstance(value, str):
        return value.casefold() in low_text or value.casefold() == phase_low
    if isinstance(value, Mapping):
        phase = value.get("phase", value.get("phases"))
        needs = value.get("needs", value.get("need"))
        phase_values = [phase] if isinstance(phase, str) else (
            list(phase) if isinstance(phase, (list, tuple)) else []
        )
        need_values = [needs] if isinstance(needs, str) else (
            list(needs) if isinstance(needs, (list, tuple)) else []
        )
        phase_values = [item for item in phase_values if isinstance(item, str)]
        need_values = [item for item in need_values if isinstance(item, str)]
        return (phase_values and phase_low in {str(item).casefold() for item in phase_values}) or any(
            str(item).casefold() in low_text for item in need_values
        )
    return False


def _split_key_value(text: str, path: Path, line_no: int) -> tuple[str, str]:
    if ":" not in text:
        raise SkillLoadError(f"{path}: malformed frontmatter line {line_no}: {text!r}")
    key, value = text.split(":", 1)
    key = key.strip()
    if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
        raise SkillLoadError(f"{path}: malformed frontmatter key: {key!r}")
    return key, value.strip()


def _parse_frontmatter_block(
    lines: list[tuple[int, str, int]],
    start: int,
    indent: int,
    path: Path,
) -> tuple[Any, int]:
    """Parse the small YAML subset used by Skill V2.

    This deliberately supports mappings, scalar lists, list-of-mappings and
    nested mappings, but not arbitrary YAML tags/anchors.  Keeping the parser
    bounded and explicit is safer than silently accepting a partial policy.
    """

    if start >= len(lines) or lines[start][0] < indent:
        return {}, start
    is_list = lines[start][1] == "-" or lines[start][1].startswith("- ")
    if is_list:
        result: list[Any] = []
        index = start
        while index < len(lines):
            current_indent, text, line_no = lines[index]
            if current_indent != indent or not (text == "-" or text.startswith("- ")):
                break
            rest = text[1:].strip()
            if not rest:
                if index + 1 < len(lines) and lines[index + 1][0] > indent:
                    child_indent = lines[index + 1][0]
                    value, index = _parse_frontmatter_block(lines, index + 1, child_indent, path)
                else:
                    value, index = "", index + 1
                result.append(value)
                continue
            # ``- key: value`` starts a mapping item.  Continuation fields at
            # the child indentation are merged into this same mapping.
            if ":" in rest and not rest.startswith(("\"", "'")):
                key, value_text = _split_key_value(rest, path, line_no)
                item: dict[str, Any] = {}
                index += 1
                if value_text in {"|", ">"}:
                    value, index = _parse_multiline(lines, index, indent, value_text == ">")
                elif value_text:
                    value = _parse_value(value_text)
                elif index < len(lines) and lines[index][0] > indent:
                    child_indent = lines[index][0]
                    value, index = _parse_frontmatter_block(lines, index, child_indent, path)
                else:
                    value = {}
                item[key] = value
                if index < len(lines) and lines[index][0] > indent:
                    child_indent = lines[index][0]
                    continuation, index = _parse_frontmatter_block(lines, index, child_indent, path)
                    if not isinstance(continuation, dict):
                        raise SkillLoadError(f"{path}: list item continuation must be a mapping")
                    item.update(continuation)
                result.append(item)
                continue
            result.append(_parse_value(rest))
            index += 1
        return result, index

    result_map: dict[str, Any] = {}
    index = start
    while index < len(lines):
        current_indent, text, line_no = lines[index]
        if current_indent != indent or text.startswith("-"):
            break
        key, value_text = _split_key_value(text, path, line_no)
        if key in result_map:
            raise SkillLoadError(f"{path}: duplicate frontmatter key {key!r}")
        index += 1
        if value_text in {"|", ">"}:
            value, index = _parse_multiline(lines, index, indent, value_text == ">")
        elif value_text:
            value = _parse_value(value_text)
        elif index < len(lines) and lines[index][0] > indent:
            child_indent = lines[index][0]
            value, index = _parse_frontmatter_block(lines, index, child_indent, path)
        else:
            value = {}
        result_map[key] = value
    return result_map, index


def _parse_multiline(
    lines: list[tuple[int, str, int]], start: int, parent_indent: int, folded: bool
) -> tuple[str, int]:
    values: list[str] = []
    index = start
    while index < len(lines) and lines[index][0] > parent_indent:
        values.append(lines[index][1])
        index += 1
    return ((" ".join(values) if folded else "\n".join(values)).strip(), index)


def _parse_frontmatter(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---[ \t]*\r?\n", raw)
    if not match:
        raise SkillLoadError(f"{path}: missing opening frontmatter delimiter (---)")
    closing = re.search(r"^---[ \t]*\r?$", raw[match.end():], re.MULTILINE)
    if closing is None:
        raise SkillLoadError(f"{path}: missing closing frontmatter delimiter (---)")
    fm_text = raw[match.end():match.end() + closing.start()]
    body = raw[match.end() + closing.end():].lstrip("\r\n").strip()

    lines = _frontmatter_lines(fm_text)
    if not lines:
        return {}, body
    result, index = _parse_frontmatter_block(lines, 0, lines[0][0], path)
    if index != len(lines) or not isinstance(result, dict):
        raise SkillLoadError(f"{path}: malformed nested frontmatter")
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
    unknown = set(frontmatter) - _ALLOWED_FRONTMATTER
    if unknown:
        raise SkillLoadError(f"{p}: unknown frontmatter keys: {sorted(unknown)!r}")
    name = str(frontmatter.get("name") or "").strip()
    if not name:
        raise SkillLoadError(f"{p}: frontmatter requires name")
    if len(name) > MAX_SKILL_FIELD_CHARS:
        raise SkillLoadError(f"{p}: frontmatter name is too long")
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        description = next(
            (line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("#")),
            "",
        )
    if len(description) > MAX_SKILL_FIELD_CHARS:
        raise SkillLoadError(f"{p}: frontmatter description is too long")
    version_raw = frontmatter.get("version", "0.0.0")
    if not isinstance(version_raw, str) or not re.fullmatch(
        r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version_raw.strip()
    ):
        raise SkillLoadError(f"{p}: version must be semantic version text")
    version = version_raw.strip()
    schema_raw = frontmatter.get("schema_version", 1)
    if not isinstance(schema_raw, int) or isinstance(schema_raw, bool) or schema_raw not in {1, 2}:
        raise SkillLoadError(f"{p}: schema_version must be 1 or 2")
    role_raw = frontmatter.get("role", "methodology")
    if not isinstance(role_raw, str) or role_raw.strip() not in _ALLOWED_ROLES:
        raise SkillLoadError(f"{p}: role is not supported")
    role = role_raw.strip()
    trigger_text_terms, trigger_phases, trigger_needs = _trigger_values(
        frontmatter.get("triggers", []), field_name="triggers"
    )
    # Keep the flattened legacy ``triggers`` projection for callers that
    # already inspect it, while matching uses the lossless V2 fields above.
    trigger_terms = list(dict.fromkeys((*trigger_text_terms, *trigger_phases, *trigger_needs)))
    requires = frontmatter.get("requires")
    if requires is None:
        requires_map: Mapping[str, Any] = {}
    elif isinstance(requires, Mapping):
        requires_map = requires
        unknown_requires = set(requires_map) - _REQUIRES_KEYS
        if unknown_requires:
            raise SkillLoadError(f"{p}: unknown requires keys: {sorted(unknown_requires)!r}")
    else:
        raise SkillLoadError(f"{p}: requires must be a mapping")
    allowed_raw = frontmatter.get("allowed_tools", frontmatter.get("allowed-tools", []))
    allowed = list(_bounded_string_list(allowed_raw, field_name="allowed_tools"))
    ados_raw = requires_map.get("ados", frontmatter.get("requires_ados", frontmatter.get("ados", [])))
    ados = list(_bounded_string_list(ados_raw, field_name="requires.ados", allow_scalar=True))
    for ado in ados:
        if not re.fullmatch(r"[A-Za-z0-9_]+", ado):
            raise SkillLoadError(f"{p}: invalid ado name")
    stata_min_raw = requires_map.get("stata_min")
    if stata_min_raw is None:
        stata_min = None
    elif isinstance(stata_min_raw, (int, float)) and not isinstance(stata_min_raw, bool):
        stata_min = str(stata_min_raw)
    elif isinstance(stata_min_raw, str):
        stata_min = stata_min_raw.strip()
    else:
        raise SkillLoadError(f"{p}: requires.stata_min must be numeric text")
    if stata_min is not None and not re.fullmatch(r"\d+(?:\.\d+)?", stata_min):
        raise SkillLoadError(f"{p}: requires.stata_min must be numeric text")
    prechecks = _bounded_string_list(frontmatter.get("prechecks", []), field_name="prechecks", allow_scalar=True)
    steps = _bounded_string_list(frontmatter.get("steps", []), field_name="steps", allow_scalar=True)
    rules_raw = frontmatter.get("rules", [])
    if rules_raw == {}:
        rules_raw = []
    if not isinstance(rules_raw, (list, tuple)):
        raise SkillLoadError(f"{p}: rules must be a list")
    rules = tuple(_bounded_jsonish(item, field_name="rules") for item in rules_raw)
    examples_raw = frontmatter.get("examples", [])
    if examples_raw == {}:
        examples_raw = []
    if not isinstance(examples_raw, (list, tuple)):
        raise SkillLoadError(f"{p}: examples must be a list")
    examples = tuple(_bounded_jsonish(item, field_name="examples") for item in examples_raw)
    disabled_raw = frontmatter.get("disabled_when", [])
    if disabled_raw == {}:
        disabled_raw = []
    if isinstance(disabled_raw, (str, Mapping)):
        disabled_raw = [disabled_raw]
    if not isinstance(disabled_raw, (list, tuple)):
        raise SkillLoadError(f"{p}: disabled_when must be a list")
    disabled_when = tuple(_bounded_jsonish(item, field_name="disabled_when") for item in disabled_raw)
    evolution_raw = frontmatter.get("evolution", {})
    if evolution_raw == {}:
        evolution: Mapping[str, Any] = {}
    elif isinstance(evolution_raw, Mapping):
        unknown_evolution = set(evolution_raw) - _EVOLUTION_KEYS
        if unknown_evolution:
            raise SkillLoadError(f"{p}: unknown evolution keys: {sorted(unknown_evolution)!r}")
        evolution = _bounded_jsonish(evolution_raw, field_name="evolution")
        if not isinstance(evolution, Mapping):  # pragma: no cover - guarded above
            raise SkillLoadError(f"{p}: evolution must be a mapping")
    else:
        raise SkillLoadError(f"{p}: evolution must be a mapping")
    return Skill(
        slug=name,
        description=description,
        triggers=trigger_terms,
        allowed_tools=allowed,
        requires_ados=ados,
        path=p,
        body=body,
        version=version,
        schema_version=schema_raw,
        role=role,
        trigger_phases=trigger_phases,
        trigger_needs=trigger_needs,
        trigger_text_terms=tuple(trigger_text_terms),
        requires_stata_min=stata_min,
        prechecks=prechecks,
        steps=steps,
        rules=rules,
        examples=examples,
        disabled_when=disabled_when,
        evolution=evolution,
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
        if not path.name.startswith((".", "_"))
        if not any(part.startswith((".", "_")) for part in path.relative_to(root).parts[:-1])
    )


def load_skill_dir(directory: str | Path, *, max_bytes: int = MAX_SKILL_BYTES) -> dict[str, Skill]:
    """Recursively load root markdown skills and nested ``*/SKILL.md`` files."""

    candidates: dict[str, list[Skill]] = {}
    for path in _skill_files(Path(directory)):
        skill = load_skill(path, max_bytes=max_bytes)
        candidates.setdefault(skill.slug, []).append(skill)

    out: dict[str, Skill] = {}
    for slug, skills in candidates.items():
        if len(skills) == 1:
            out[slug] = skills[0]
            continue
        # Older evolve versions emitted ``<slug>_e<n>.md`` files.  Keep
        # catalog loading compatible by selecting the highest numbered file;
        # new promotion moves the lower versions into ``_history``.
        ranked: list[tuple[tuple[int, int], Skill]] = []
        for skill in skills:
            path_name = skill.path.name if skill.path is not None else ""
            match = re.fullmatch(rf"{re.escape(slug)}_e(\d+)\.md", path_name)
            if match:
                rank = (2, int(match.group(1)))
            elif path_name == "SKILL.md":
                rank = (1, 0)
            else:
                rank = (0, 0)
            ranked.append((rank, skill))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            paths = ", ".join(str(item.path) for _, item in ranked[:2])
            raise SkillLoadError(f"duplicate skill name {slug!r}: {paths}")
        out[slug] = ranked[0][1]
    return out


def match_skills(skills: dict[str, Skill], user_text: str, *, phase: str | None = None) -> list[Skill]:
    """Return metadata-matched skills without treating phase as user text.

    Legacy skills use substring terms.  V2 structured triggers add an
    independent phase gate and a needs gate; both must pass when declared.
    """

    low = (user_text or "").casefold()
    phase_low = (phase or "").casefold()
    matched: list[Skill] = []
    for skill in skills.values():
        if any(_disabled_match(item, low_text=low, phase_low=phase_low) for item in skill.disabled_when):
            continue
        if skill.trigger_phases and phase_low not in {item.casefold() for item in skill.trigger_phases}:
            continue
        if skill.trigger_needs and not any(item.casefold() in low for item in skill.trigger_needs):
            continue
        structured = bool(skill.trigger_phases or skill.trigger_needs)
        if structured:
            terms = [term.casefold() for term in skill.trigger_text_terms if term]
        else:
            terms = [term.casefold() for term in skill.triggers if term]
        if not terms and not skill.trigger_phases and not skill.trigger_needs:
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
        # A purely structured trigger is satisfied by its constraints; a
        # legacy/textual trigger still needs a user-text term.  When both are
        # present the structured gates and the textual term are conjunctive.
        if (not terms or any(term in low for term in terms)):
            matched.append(skill)
    return matched
