"""Pure verifier for the registered one-model esttab RTF export profile."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .evidence import format_binary64_fixed


@dataclass(frozen=True, slots=True)
class TableElement:
    result_element_id: str
    semantic_key: str
    binary64_bits: str


@dataclass(frozen=True, slots=True)
class VerifiedTableCell:
    semantic_cell_slot: str
    result_element_id: str
    rendered_text: str
    rtf_byte_start: int
    rtf_byte_end: int


@dataclass(frozen=True, slots=True)
class VerifiedEsttabTable:
    visible_table_sha256: str
    cells: tuple[VerifiedTableCell, ...]


CELL_SCHEMA = (
    ("row.mpg.coefficient", "term.mpg.coefficient", 3, b"{mpg}\\cell"),
    ("row.mpg.standard_error", "term.mpg.se", 3, None),
    ("row.weight.coefficient", "term.weight.coefficient", 3, b"{weight}\\cell"),
    ("row.weight.standard_error", "term.weight.se", 3, None),
    ("row._cons.coefficient", "term._cons.coefficient", 3, b"{_cons}\\cell"),
    ("row._cons.standard_error", "term._cons.se", 3, None),
    ("stat.N", "model.N", 0, b"{Observations}\\cell"),
    ("stat.r2", "model.r2", 3, b"{R-squared}\\cell"),
)

LOGIT_CELL_SCHEMA = (
    *CELL_SCHEMA[:-1],
    (
        "stat.r2_p",
        "model.r2_p",
        3,
        b"{Pseudo R-squared}\\cell",
    ),
)


def generic_cell_schema(
    coefficient_terms: tuple[str, ...],
    fit_statistic: str,
    fit_label: str,
) -> tuple[tuple[str, str, int, bytes | None], ...]:
    """Build a verified cell layout from an Agent-selected presentation spec."""

    rows: list[tuple[str, str, int, bytes | None]] = []
    for index, term in enumerate(coefficient_terms, start=1):
        anchor = ("{" + term + "}\\cell").encode("ascii")
        rows.extend(
            (
                (f"row.{index}.coefficient", f"term.{term}.coefficient", 3, anchor),
                (f"row.{index}.standard_error", f"term.{term}.se", 3, None),
            )
        )
    rows.extend(
        (
            ("stat.N", "scalar.N", 0, b"{Observations}\\cell"),
            (
                "stat.fit",
                f"scalar.{fit_statistic}",
                3,
                ("{" + fit_label + "}\\cell").encode("ascii"),
            ),
        )
    )
    return tuple(rows)


def cell_schema_for_result_profile(
    result_profile_id: str,
) -> tuple[tuple[str, str, int, bytes | None], ...]:
    if result_profile_id == "binary_model.logit.v1":
        return LOGIT_CELL_SCHEMA
    if result_profile_id in {
        "linear_model.regress.v1",
        "linear_model.reghdfe.v1",
        "linear_model.ivregress_2sls.v1",
    }:
        return CELL_SCHEMA
    if result_profile_id == "stata.generic-result.v1":
        return generic_cell_schema(("mpg", "weight", "_cons"), "r2", "R-squared")
    raise ValueError("Result Profile has no registered table cell schema")


def verify_esttab_rtf(
    payload: bytes,
    elements: tuple[TableElement, ...],
    *,
    cell_schema: tuple[tuple[str, str, int, bytes | None], ...] = CELL_SCHEMA,
) -> VerifiedEsttabTable:
    """Verify the fixed profile without treating RTF as a numeric authority."""

    if not payload.startswith(b"{\\rtf1"):
        raise ValueError("Table Artifact is not an RTF payload")
    by_key = {element.semantic_key: element for element in elements}
    if set(by_key) != {item[1] for item in cell_schema}:
        raise ValueError("Table verification requires the exact registered cell schema")
    cursor = 0
    cells: list[VerifiedTableCell] = []
    for slot, key, places, anchor in cell_schema:
        element = by_key[key]
        rendered = format_binary64_fixed(element.binary64_bits, places)
        if anchor is not None:
            anchor_at = payload.find(anchor, cursor)
            if anchor_at < 0:
                raise ValueError(f"RTF semantic row is missing: {slot}")
            cursor = anchor_at + len(anchor)
            token = b"{" + rendered.encode("ascii")
        else:
            token = b"{(" + rendered.encode("ascii") + b")}"
        found = payload.find(token, cursor)
        if found < 0:
            raise ValueError(f"RTF cell does not match qualified Result Element: {slot}")
        start = found + 1 + (1 if anchor is None else 0)
        end = start + len(rendered)
        cells.append(VerifiedTableCell(slot, element.result_element_id, rendered, start, end))
        cursor = end
    return VerifiedEsttabTable(hashlib.sha256(payload).hexdigest(), tuple(cells))
