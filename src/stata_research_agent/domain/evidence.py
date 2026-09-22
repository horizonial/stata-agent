"""Pure evidence rendering and mandatory numeric-coverage rules."""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")
NUMERIC_PATTERN = re.compile(r"(?<![\w.])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?(?!\w|\.\d)")


class NumericCoverageError(ValueError):
    """The final rendered formal scope contains unbound or ambiguous numbers."""


@dataclass(frozen=True, slots=True)
class NumericSpan:
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    lexeme: str


@dataclass(frozen=True, slots=True)
class RenderedSlotOccurrence:
    slot_name: str
    result_element_id: str
    binary64_bits: str
    decimal_places: int
    rendered_text: str
    span: NumericSpan


@dataclass(frozen=True, slots=True)
class CoveredFormalText:
    content: str
    content_utf8_sha256: str
    occurrences: tuple[RenderedSlotOccurrence, ...]


def format_binary64_fixed(binary64_bits: str, decimal_places: int) -> str:
    """Render exact binary64 input under fixed-decimal formatter v1."""

    if len(binary64_bits) != 16:
        raise ValueError("binary64 value must contain exactly 16 hexadecimal characters")
    if not 0 <= decimal_places <= 12:
        raise ValueError("decimal_places must be between 0 and 12")
    try:
        value = struct.unpack(">d", bytes.fromhex(binary64_bits))[0]
    except (ValueError, struct.error) as error:
        raise ValueError("invalid binary64 value") from error
    decimal = Decimal.from_float(value)
    quantum = Decimal(1).scaleb(-decimal_places)
    rounded = decimal.quantize(quantum, rounding=ROUND_HALF_EVEN)
    if rounded.is_zero():
        rounded = abs(rounded)
    return format(rounded, f".{decimal_places}f")


def render_covered_formal_text(
    template: str,
    slots: dict[str, tuple[str, str, int]],
) -> CoveredFormalText:
    """Render placeholders and prove every numeric lexeme is backed by one slot.

    Each slot value is ``(result_element_id, binary64_bits, decimal_places)``. Literal
    numbers in the template are deliberately rejected: a model cannot smuggle an
    untraceable final number around the Evidence renderer.
    """

    if not template:
        raise ValueError("formal result template is required")
    names = PLACEHOLDER_PATTERN.findall(template)
    if not names:
        raise ValueError("formal result template requires at least one Evidence slot")
    if set(names) != set(slots):
        missing = sorted(set(names) - set(slots))
        unused = sorted(set(slots) - set(names))
        raise ValueError(f"template/slot mismatch: missing={missing}, unused={unused}")

    parts: list[str] = []
    occurrences: list[RenderedSlotOccurrence] = []
    source_cursor = 0
    char_cursor = 0
    for match in PLACEHOLDER_PATTERN.finditer(template):
        literal = template[source_cursor : match.start()]
        parts.append(literal)
        char_cursor += len(literal)
        slot_name = match.group(1)
        element_id, bits, places = slots[slot_name]
        rendered = format_binary64_fixed(bits, places)
        start = char_cursor
        end = start + len(rendered)
        prefix = "".join(parts)
        byte_start = len(prefix.encode("utf-8"))
        byte_end = byte_start + len(rendered.encode("utf-8"))
        span = NumericSpan(start, end, byte_start, byte_end, rendered)
        occurrences.append(
            RenderedSlotOccurrence(
                slot_name,
                element_id,
                bits,
                places,
                rendered,
                span,
            )
        )
        parts.append(rendered)
        char_cursor = end
        source_cursor = match.end()
    parts.append(template[source_cursor:])
    content = "".join(parts)

    scanned = tuple(
        NumericSpan(
            match.start(),
            match.end(),
            len(content[: match.start()].encode("utf-8")),
            len(content[: match.end()].encode("utf-8")),
            match.group(0),
        )
        for match in NUMERIC_PATTERN.finditer(content)
    )
    bound = tuple(item.span for item in occurrences)
    if scanned != bound:
        raise NumericCoverageError(
            "mandatory formal scope contains an unbound or ambiguous numeric occurrence"
        )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return CoveredFormalText(content, digest, tuple(occurrences))
