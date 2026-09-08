"""回归表语义（DD-05 §2，A 组第一件）。

TableModel = 表的中立语义表示（行=系数/统计，列=spec，格=stat 值）。
- esttab/tsv → TableModel（解析）；TableModel → Markdown / Word 表。
- 数字格必须能回链 card（round-trip），无主数字不许交付。
只做"我们生成的表 + tsv fixture"语义；真实 esttab 方言健壮性靠 fixture 集后补。
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


def validate_cells(model: TableModel, cards: list[EvidenceCard]) -> list[str]:
    """表格里每个数字必须命中某张 numeric 卡的显示值；返回无主数字。"""
    from .ground import numeric_tokens

    allowed = numeric_tokens(cards)
    bad: list[str] = []
    for _ri, _ci, cell in numeric_cells(model):
        if cell.text.strip() not in allowed:
            bad.append(cell.text)
    return bad


_STAT_LABELS = {"coef": "核心系数", "N": "样本量", "r2": "R²", "se": "SE"}


def build_run_table(proj, run_id: str, *, title: str = "主回归") -> TableModel:
    """从一次 run 的机器层构造单列回归表，并尽量给每个数字格挂 card_id。"""
    from .ground import display_for

    rec = proj.runs.get(run_id)
    if rec is None or rec.status != "succeeded":
        raise ValueError(f"run {run_id!r} 不存在或未成功，不能出表")
    cards_by_stat = {}
    for card in proj.cards.values():
        if card.kind == "numeric" and isinstance(card.locator, dict):
            if card.locator.get("run_id") == run_id:
                cards_by_stat[str(card.locator.get("stat_type"))] = card.card_id
    stats = ["coef", "N", "r2"]
    order = [s for s in stats if s in rec.machine]
    model = TableModel(title=title, columns=["主回归"])
    for stat in order:
        raw = rec.machine[stat]
        value = float(raw)
        cid = cards_by_stat.get(stat)
        display = display_for(stat, value) if stat != "se" else str(value)
        model.rows.append(Row(
            label=_STAT_LABELS.get(stat, stat),
            cells=[Cell(text=display, stat_type=stat, numeric=value, card_id=cid)],
        ))
    return model
