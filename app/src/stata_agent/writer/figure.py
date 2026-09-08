"""figure 证据（audit C1 / DD-01 §2.6 figure 卡，A 组第三件）。

Stata 图落 artifact → 以 validator 身份签成 figure 卡（locator: run_id/path/kind）；
Word 渲染：插图 + caption（引卡号）。图不落地卡就不许进稿结论。
"""

from __future__ import annotations

from pathlib import Path

from ..domain.models import EvidenceCard
from ..events.schema import EVENT_CARD_SIGNED, ACTOR_VALIDATOR, Event
from ..storage.sqlite_store import SQLiteStore


def sign_figure_card(
    store: SQLiteStore,
    run_id: str,
    figure_path: str | Path,
    *,
    idea: str = "i1",
    kind: str = "event-study",
    caption: str = "",
) -> str:
    """把一张已导出的图签成 figure 卡（须 run 成功、文件存在）。"""
    proj = store.project(idea)
    rec = proj.runs.get(run_id)
    if rec is None or rec.status != "succeeded":
        raise ValueError(f"run {run_id!r} 不存在或未成功，图不能作为证据")
    path = Path(figure_path)
    if not path.exists():
        raise ValueError(f"图文件不存在: {path}")
    card_id = f"card-fig-{path.stem}"
    card = EvidenceCard(
        card_id=card_id,
        kind="figure",
        locator={"run_id": run_id, "path": str(path), "kind": kind},
        value={"caption": caption},
        signed_by=ACTOR_VALIDATOR,
    )
    store.append(Event(idea_id=idea, event_type=EVENT_CARD_SIGNED, actor=ACTOR_VALIDATOR,
                       source=ACTOR_VALIDATOR, payload={"card": card.model_dump()}))
    return card_id


def figure_docx(
    title: str,
    items: list[dict],
) -> bytes:
    """items: [{path, caption, card_id, claim_id?}] → 插图 Word。

    返回 .docx 字节；每图下 caption 带 [图 卡id] 溯源注。
    """
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    doc.add_heading(title, level=0)
    for n, item in enumerate(items, start=1):
        doc.add_picture(str(item["path"]), width=Inches(5.2))
        cap = item.get("caption") or ""
        note = item.get("card_id") or item.get("claim_id") or ""
        doc.add_paragraph(f"图 {n}：{cap}" + (f"　[{note}]" if note else ""))
    import io

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
