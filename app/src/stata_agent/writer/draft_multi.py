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

    from .table import build_run_table

    ok_runs = [(rid, rec) for rid, rec in proj.runs.items()
               if rec.status == "succeeded" and rec.machine]
    # A raw machine dict is not publishable.  Build/validate every run table
    # first so a missing card fails loudly instead of producing a partial DOCX.
    run_models = {rid: build_run_table(proj, rid, title="") for rid, _rec in ok_runs}

    active_claims = []
    for claim in sorted(proj.claims.values(), key=lambda c: c.claim_id):
        if claim.status != "supported":
            continue
        missing = [card_id for card_id in claim.cards if card_id not in proj.cards]
        if missing:
            raise ValueError(f"active claim {claim.claim_id!r} 缺 EvidenceCard: {missing}")
        if not claim.cards:
            raise ValueError(f"active claim {claim.claim_id!r} 没有 EvidenceCard，不能出稿")
        for card_id in claim.cards:
            card = proj.cards[card_id]
            if card.kind != "numeric":
                continue
            loc = card.locator or {}
            run_id = loc.get("run_id")
            stat_type = loc.get("stat_type")
            rec = proj.runs.get(run_id)
            if rec is None or rec.status != "succeeded" or stat_type not in rec.machine:
                raise ValueError(f"active claim {claim.claim_id!r} 的 numeric card {card_id!r} provenance 不完整")
            import hashlib
            import json
            machine_hash = hashlib.sha256(
                json.dumps(rec.machine, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            if card.machine_hash != machine_hash or not isinstance(card.value, dict):
                raise ValueError(f"active claim {claim.claim_id!r} 的 numeric card {card_id!r} provenance 不匹配")
            try:
                if float(card.value.get("value")) != float(rec.machine[stat_type]):
                    raise ValueError(f"active claim {claim.claim_id!r} 的 numeric card {card_id!r} 数值不一致")
            except (TypeError, ValueError) as error:
                if isinstance(error, ValueError) and "数值不一致" in str(error):
                    raise
                raise ValueError(f"active claim {claim.claim_id!r} 的 numeric card {card_id!r} 数值无效") from error
        active_claims.append(claim)
    citation_cards = [c for c in proj.cards.values() if c.kind == "citation"]
    if not ok_runs and not active_claims and not citation_cards:
        raise ValueError("没有 active claim 或含 provenance 的 run，不能出稿")
    if ok_runs:
        model = TableModel(title="回归结果（由事件账本渲染）",
                           columns=[rid[:14] for rid, _ in ok_runs])
        labels = {
            "系数": "核心系数",
            "标准误": "SE",
            "样本量": "样本量",
            "R²": "R²",
        }
        for label, source_label in labels.items():
            cells = []
            for rid, _rec in ok_runs:
                source = next(
                    (row.cells[0] for row in run_models[rid].rows if row.label == source_label),
                    None,
                )
                if source is None:
                    cells.append(Cell(text="-", stat_type=label))
                else:
                    cells.append(Cell(text=source.text, stat_type=label,
                                      numeric=source.numeric, card_id=source.card_id))
            model.rows.append(Row(label=label, cells=cells))
        add_table_to_doc(doc, model)

    for claim in active_claims:
        doc.add_paragraph(render_claim_sentence(claim, proj.cards) + f"  [{claim.claim_id}]")

    cit = citation_cards
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
