"""多表实证初稿（Wave-1 论文化最小）：描述表 + 主表(带星) + 稳健性小结 → .docx。

数据来源：run_variants 的 results（machine: coef/se/N/r2）；数字格可回链 run。
"""

from __future__ import annotations

import csv

from docx import Document

from ..tools.robustness import check_robustness, stars
from .table import Cell, Row, TableModel, add_table_to_doc


def _f(x, digits=3):
    return f"{x:.{digits}f}" if isinstance(x, (int, float)) else "-"


def regression_table_from_results(results: dict[str, dict]) -> TableModel:
    ok = [(k, v) for k, v in results.items() if v.get("machine")]
    model = TableModel(title="主回归与稳健性", columns=[v.get("label", k) for k, v in ok])
    stats = {
        "系数": lambda m: _f(m.get("coef")) + stars(m.get("coef"), m.get("se")),
        "标准误": lambda m: _f(m.get("se")),
        "样本量": lambda m: str(int(m.get("N", 0))),
        "R²": lambda m: _f(m.get("r2")),
    }
    for label, fmt in stats.items():
        model.rows.append(Row(label=label, cells=[Cell(text=fmt(v["machine"])) for _, v in ok]))
    return model


def table1_from_long(path) -> TableModel | None:
    """ck_long.csv → 描述表：按 (nj,wave) 的 fte 均值（观测数）。"""
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    vals: dict[tuple[int, int], list[float]] = {}
    for r in rows:
        if r["fte"] not in (None, ""):
            vals.setdefault((int(r["nj"]), int(r["wave"])), []).append(float(r["fte"]))
    group = {k: (sum(v) / len(v), len(v)) for k, v in vals.items()}
    model = TableModel(title="表 1 描述统计：FTE 均值（观测数）", columns=["政策前", "政策后"])
    for nj, name in ((0, "宾州 PA"), (1, "新泽西 NJ")):
        texts = []
        for w in (1, 2):
            g = group.get((nj, w))
            texts.append(f"{g[0]:.2f}（{g[1]}）" if g else "-")
        model.rows.append(Row(label=name, cells=[Cell(text=t) for t in texts]))
    return model


def draft_from_ledger(proj, *, title: str = "实证研究初稿",
                      method: str = "", limits: str = "") -> bytes:
    """从事件账本投影渲染完整初稿：方法段 + 运行表 + 已签 claims + 引文块 + 局限。

    数字一律来自 run.machine / claim 卡（可回链），不做任何现写。
    """
    from ..writer.ground import render_claim_sentence

    doc = Document()
    doc.add_heading(title, level=0)
    if method:
        doc.add_paragraph(method)

    ok_runs = [(rid, rec) for rid, rec in proj.runs.items()
               if rec.status == "succeeded" and rec.machine]
    if ok_runs:
        model = TableModel(title="回归结果（由事件账本渲染）",
                           columns=[rid[:14] for rid, _ in ok_runs])
        stats = {
            "系数": lambda m: _f(m.get("coef")),
            "标准误": lambda m: _f(m.get("se")) if m.get("se") is not None else "-",
            "样本量": lambda m: str(int(m.get("N", 0))) if m.get("N") is not None else "-",
            "R²": lambda m: _f(m.get("r2")),
        }
        for label, fmt in stats.items():
            model.rows.append(Row(label=label,
                                  cells=[Cell(text=fmt(rec.machine)) for _, rec in ok_runs]))
        add_table_to_doc(doc, model)

    for claim in sorted(proj.claims.values(), key=lambda c: c.claim_id):
        doc.add_paragraph(render_claim_sentence(claim, proj.cards) + f"  [{claim.claim_id}]")

    cit = [c for c in proj.cards.values() if c.kind == "citation"]
    if cit:
        doc.add_paragraph("引用文献（citable 块）")
        for c in sorted(cit, key=lambda x: x.card_id):
            loc = c.locator or {}
            doc.add_paragraph(f"- {loc.get('doc_id', '?')} p{loc.get('page', '?')}  {c.card_id}")
    if limits:
        doc.add_paragraph("局限：" + limits)

    import io

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def draft_docx(results: dict[str, dict], *, title: str = "实证研究初稿",
               table1_path: str | None = None) -> bytes:
    doc = Document()
    doc.add_heading(title, level=0)
    if table1_path:
        t1 = table1_from_long(table1_path)
        if t1:
            add_table_to_doc(doc, t1)
    add_table_to_doc(doc, regression_table_from_results(results))
    rb = check_robustness(results)
    doc.add_paragraph("稳健性小结：" + ("各口径符号与显著性一致。" if rb["stable"] else "存在口径不一致，需逐行核对（见下表）。"))
    import io

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
