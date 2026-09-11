"""回归表语义（DD-05 §2，A 组第一件）。

TableModel = 表的中立语义表示（行=系数/统计，列=spec，格=stat 值）。
- esttab/tsv → TableModel（解析）；TableModel → Markdown / Word 表。
- 数字格必须能回链 card（round-trip），无主数字不许交付。
只做"我们生成的表 + tsv fixture"语义；真实 esttab 方言健壮性靠 fixture 集后补。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from collections.abc import Mapping

from ..domain.models import Claim, EvidenceCard


@dataclass
class Cell:
    text: str
    stat_type: str = "value"
    numeric: float | None = None
    card_id: str | None = None


@dataclass
class Row:
    label: str
    cells: list[Cell] = field(default_factory=list)


@dataclass
class TableModel:
    title: str
    columns: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)

    def cell(self, row_index: int, col_index: int) -> Cell | None:
        if 0 <= row_index < len(self.rows) and 0 <= col_index < len(self.rows[row_index].cells):
            return self.rows[row_index].cells[col_index]
        return None


def _num(text: str) -> float | None:
    cleaned = text.strip()
    if cleaned in {"", "Yes", "X", "—", "-", "No"}:
        return None
    try:
        return float(cleaned.replace(",", ""))
    except ValueError:
        return None


def table_from_tsv(text: str, *, title: str = "") -> TableModel:
    """TSV 表 → TableModel。首行=列名；之后每行 label + 单元格(制表符分隔)。"""
    model = TableModel(title=title)
    for lineno, line in enumerate(text.strip().splitlines()):
        line = line.rstrip("\r")
        parts = line.split("\t")
        if not parts or not any(p.strip() for p in parts):
            continue
        if lineno == 0:
            model.columns = [p.strip() for p in parts]
            continue
        label = parts[0].strip()
        cells = []
        for p in parts[1:]:
            p = p.strip()
            cells.append(Cell(text=p, numeric=_num(p)))
        model.rows.append(Row(label=label, cells=cells))
    return model


def numeric_cells(model: TableModel):
    for ri, row in enumerate(model.rows):
        for ci, cell in enumerate(row.cells):
            if cell.numeric is not None:
                yield ri, ci, cell


def _numeric_text(text: str) -> float | None:
    """Read the numeric prefix of a display value, including significance stars."""

    match = re.fullmatch(r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))(?:\*{1,4})?\s*", text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _card_matches_cell(cell: Cell, card: EvidenceCard) -> bool:
    if card.kind != "numeric" or not isinstance(card.value, dict):
        return False
    raw = card.value.get("value")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
        return False
    locator = card.locator if isinstance(card.locator, dict) else {}
    card_stat = str(locator.get("stat_type") or "")
    if card_stat and cell.stat_type not in {"", "value", card_stat}:
        return False
    parsed = _numeric_text(cell.text)
    if parsed is None:
        return False
    if cell.numeric is not None:
        try:
            numeric_value = float(cell.numeric)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(numeric_value):
            return False
        if card_stat == "N" or cell.stat_type == "N":
            if int(round(numeric_value)) != int(round(parsed)):
                return False
        elif f"{numeric_value:.3f}" != f"{parsed:.3f}":
            return False
    expected = float(raw)
    if card_stat == "N" or cell.stat_type == "N":
        return int(round(parsed)) == int(round(expected)) and abs(parsed - round(parsed)) < 1e-9
    # A table display may round a canonical value, but must equal the same
    # deterministic display representation (stars are presentation-only).
    expected_display = f"{expected:.3f}"
    actual_display = f"{parsed:.3f}"
    return actual_display == expected_display


def validate_cell(cell: Cell, cards: Mapping[str, EvidenceCard], *, require_card_id: bool = True) -> str | None:
    """Return a stable error token for one numeric cell, or None when valid."""

    if not cell.card_id:
        return "missing_card" if require_card_id else None
    card = cards.get(cell.card_id)
    if card is None or not _card_matches_cell(cell, card):
        return "card_mismatch"
    return None


def render_markdown(model: TableModel) -> str:
    lines = [f"**{model.title}**" if model.title else ""]
    header = ["指标"] + list(model.columns)
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for row in model.rows:
        cells = [c.text for c in row.cells]
        lines.append("| " + " | ".join([row.label] + cells) + " |")
    return "\n".join(l for l in lines if l)


def add_table_to_doc(doc, model: TableModel, *, title_heading: bool = True) -> None:
    """把 TableModel 渲染成 Word 表。"""
    if title_heading and model.title:
        doc.add_heading(model.title, level=2)
    cols = ["指标"] + list(model.columns)
    table = doc.add_table(rows=1, cols=max(len(cols), 1))
    table.style = "Light Grid Accent 1"
    for j, col in enumerate(cols):
        table.rows[0].cells[j].text = col
    for row in model.rows:
        cells_row = table.add_row().cells
        cells_row[0].text = row.label
        for j, cell in enumerate(row.cells[: max(len(cols) - 1, 0)]):
            cells_row[j + 1].text = cell.text


def validate_cells(
    model: TableModel,
    cards: list[EvidenceCard],
    *,
    require_card_ids: bool = False,
) -> list[str]:
    """Validate numeric cells.

    The default remains compatible with the original token-only helper used by
    legacy fixtures. Product delivery passes require_card_ids=True so every
    numeric cell is checked against its explicitly attached card.
    """
    from .ground import numeric_tokens

    by_id = {card.card_id: card for card in cards}
    allowed = numeric_tokens(cards)
    bad: list[str] = []
    for _ri, _ci, cell in numeric_cells(model):
        if require_card_ids:
            if not cell.card_id:
                bad.append(f"missing_card:{cell.text}")
                continue
            card = by_id.get(cell.card_id)
            if card is None or not _card_matches_cell(cell, card):
                bad.append(f"card_mismatch:{cell.card_id}:{cell.text}")
            continue
        if cell.text.strip() not in allowed:
            bad.append(cell.text)
    return bad


_STAT_LABELS = {"coef": "核心系数", "N": "样本量", "r2": "R²", "se": "SE"}


def build_run_table(proj, run_id: str, *, title: str = "主回归") -> TableModel:
    """从一次 run 的机器层构造单列回归表。

    A succeeded run is not printable merely because it has ``machine`` values:
    every numeric cell must have its validator-issued numeric card and the card
    must point back to this run's machine hash.
    """
    from .ground import display_for
    import hashlib
    import json

    rec = proj.runs.get(run_id)
    if rec is None or rec.status != "succeeded":
        raise ValueError(f"run {run_id!r} 不存在或未成功，不能出表")
    if not rec.machine:
        raise ValueError(f"run {run_id!r} 无机器层数字，不能出表")
    machine_hash = hashlib.sha256(
        json.dumps(rec.machine, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    from ..tools.result_verifier import verify_run_record
    from ..tools.evidence_signer import validate_run_provenance

    report = verify_run_record(rec)
    if not report.evidence_ready:
        failed = next((item.code for item in report.checks if not item.passed), "verification_failed")
        raise ValueError(f"run {run_id!r} 结果合同未通过: {failed}（缺 EvidenceCard）")
    contract = rec.result_contract if isinstance(rec.result_contract, dict) else {}
    target_term = contract.get("target_term")
    provenance_kind = validate_run_provenance(rec.provenance or {})
    cards_by_stat: dict[str, list[str]] = {}
    for card in proj.cards.values():
        if card.kind == "numeric" and isinstance(card.locator, dict):
            if card.locator.get("run_id") == run_id:
                stat = str(card.locator.get("stat_type") or "")
                cards_by_stat.setdefault(stat, []).append(card.card_id)
    stats = ["coef", "se", "N", "r2"]
    order = [s for s in stats if s in rec.machine]
    model = TableModel(title=title, columns=["主回归"])
    for stat in order:
        raw = rec.machine[stat]
        value = float(raw)
        stat_cards = cards_by_stat.get(stat, [])
        if not stat_cards:
            raise ValueError(f"run {run_id!r} 的 {stat} 缺 numeric EvidenceCard，不能出表")
        if len(stat_cards) != 1:
            raise ValueError(f"run {run_id!r} 的 {stat} 存在重复 numeric EvidenceCard，不能出表")
        cid = stat_cards[0]
        card = proj.cards.get(cid)
        if card is None or card.kind != "numeric" or card.signed_by != "validator":
            raise ValueError(f"run {run_id!r} 的 {stat} card 不完整，不能出表")
        if card.locator.get("run_id") != run_id or card.locator.get("stat_type") != stat:
            raise ValueError(f"run {run_id!r} 的 {stat} card stat/run provenance 不匹配，不能出表")
        recorded_machine_hash = card.locator.get("machine_hash")
        if (
            card.machine_hash != machine_hash
            or (recorded_machine_hash is not None and recorded_machine_hash != machine_hash)
            or card.locator.get("contract_hash") != report.contract_hash
            or card.locator.get("verification_schema_version") != report.schema_version
            or card.locator.get("target_term") != target_term
            or card.locator.get("provenance_kind") != provenance_kind
        ):
            raise ValueError(f"run {run_id!r} 的 {stat} card provenance 不匹配，不能出表")
        try:
            card_value = float(card.value.get("value")) if isinstance(card.value, dict) else float("nan")
        except (TypeError, ValueError, OverflowError):
            card_value = float("nan")
        if not math.isfinite(card_value) or card_value != value:
            raise ValueError(f"run {run_id!r} 的 {stat} card 数值与机器层不一致，不能出表")
        display = display_for(stat, value) if stat != "se" else str(value)
        model.rows.append(Row(
            label=_STAT_LABELS.get(stat, stat),
            cells=[Cell(text=display, stat_type=stat, numeric=value, card_id=cid)],
        ))
    return model
