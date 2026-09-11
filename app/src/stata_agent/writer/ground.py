"""数字接地 round-trip（切片 5a）：结果句的每个数字只能来自某张已签 NumericCard。

显示策略（先固定，后续 esttab 解析复用）：coef/r2 用 %.3f，N 用整数。
validator 拿"该句允许出现的数字 token 集合"，扫描句中数字并核对——篡改即抓。
"""

from __future__ import annotations

import math
import re

from ..domain.models import Claim, EvidenceCard

_NUM = re.compile(r"[-+]?[0-9]+(?:\.[0-9]+)?")

_LABEL = {"coef": "系数", "N": "样本量", "r2": "R²", "se": "SE"}


def display_for(stat_type: str, value: float) -> str:
    if stat_type == "N":
        return str(int(round(value)))
    return f"{value:.3f}"


def numeric_tokens(cards: list[EvidenceCard]) -> set[str]:
    """该组卡允许出现的数字 token（按显示策略展开）。"""
    tokens: set[str] = set()
    for c in cards:
        if c.kind != "numeric" or not isinstance(c.value, dict) or "value" not in c.value:
            continue
        raw = c.value["value"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        v = float(raw)
        if not math.isfinite(v):
            continue
        st = c.locator.get("stat_type", "")
        tokens.add(display_for(st, v))
    return tokens


def render_claim_sentence(claim: Claim, cards_by_id: dict[str, EvidenceCard]) -> str:
    """把一条 claim 渲染成"结果句"；数字一律取自其卡（用 display 值）。"""
    cards = [cards_by_id[c] for c in claim.cards if c in cards_by_id]
    parts: list[str] = []
    for c in cards:
        if c.kind != "numeric":
            continue
        raw = c.value.get("value") if isinstance(c.value, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"numeric card {c.card_id!r} value invalid")
        v = float(raw)
        if not math.isfinite(v):
            raise ValueError(f"numeric card {c.card_id!r} value invalid")
        locator = c.locator if isinstance(c.locator, dict) else {}
        st = locator.get("stat_type", "")
        parts.append(f"{_LABEL.get(st, st)}={display_for(st, v)}")
    sentence = f"{claim.statement}"
    if parts:
        sentence += f"（{', '.join(parts)}）"
    citation_ids = [
        str((cards_by_id[card_id].locator or {}).get("chunk_id"))
        for card_id in claim.cards
        if card_id in cards_by_id
        and cards_by_id[card_id].kind == "citation"
        and (cards_by_id[card_id].locator or {}).get("chunk_id")
    ]
    if citation_ids:
        from .citation import citation_marker

        sentence += "".join(citation_marker(chunk_id) for chunk_id in citation_ids)
    return sentence


def validate_sentence(text: str, cards: list[EvidenceCard]) -> list[str]:
    """返回句子中不在允许 token 集合里的数字（= 无来源数字）。空列表=通过。"""
    allowed = numeric_tokens(cards)
    bad: list[str] = []
    # Citation markers are provenance metadata, not numeric claims.
    text_without_markers = re.sub(r"\[c:[A-Za-z0-9_-]+\]", "", text)
    # A generated claim may carry its opaque run id (for example
    # ``run-fake-1234``).  That identifier is provenance metadata, not a
    # statistical value; remove only run ids present on the selected cards so
    # ordinary year/count claims remain subject to grounding.
    for card in cards:
        locator = card.locator if isinstance(card.locator, dict) else {}
        run_id = str(locator.get("run_id") or "")
        if run_id:
            text_without_markers = text_without_markers.replace(run_id, "")
    for tok in _NUM.findall(text_without_markers):
        # 去掉千分位/前导处理：仅比显示 token
        if tok not in allowed:
            bad.append(tok)
    return bad
