"""E：可跑评测检查（DD-06）。每项 check -> {"name": (pass, note)}。

- L0-对抗（数字/引文防幻觉）：把"带幻觉数字/缺引用"的稿子喂 writer/citation 校验器，必须抓。
- L3-单元格数值：结构先 exact，数值再容差（审计 B1 精神）。
run() 聚合；任何 fail 记 ok=False。
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain.models import EvidenceCard
from ..writer.citation import citation_marker, validate_citations
from ..writer.ground import display_for, validate_sentence

Check = Callable[[], tuple[bool, str]]


def _numeric_cards() -> list[EvidenceCard]:
    return [
        EvidenceCard(card_id="coef", kind="numeric", locator={"run_id": "r1", "stat_type": "coef"},
                     value={"value": -238.894}),
        EvidenceCard(card_id="n", kind="numeric", locator={"run_id": "r1", "stat_type": "N"},
                     value={"value": 74}),
    ]


def _citation_cards() -> list[EvidenceCard]:
    return [EvidenceCard(card_id="cit", kind="citation",
                         locator={"chunk_id": "d1", "doc_id": "AER.pdf", "page": 3})]


def _c1_numbers() -> tuple[bool, str]:
    cards = _numeric_cards()
    good = f"价格随油耗下降，系数={display_for('coef', -238.894)}，样本量={display_for('N', 74)}。"
    tampered = "价格随油耗下降，系数=-238.800，样本量=80。"  # 幻觉数字
    return (validate_sentence(good, cards) == [] and "-238.800" in validate_sentence(tampered, cards),
            "数字对抗：干净句过、篡改被抓")


def _c2_citation() -> tuple[bool, str]:
    cards = _citation_cards()
    good = "平行趋势是前提。" + citation_marker("d1")
    missing = "平行趋势是前提。"
    return (validate_citations(good, cards) == []
            and any("缺引用" in i for i in validate_citations(missing, cards)),
            "引文对抗：带 marker 过、缺引用被抓")


def _c3_l3_numeric() -> tuple[bool, str]:
    expected, rel_tol, abs_tol = -238.894, 0.01, 1e-3

    def grade(n: int, coef: float):
        if n != 74:                      # 结构先 exact：N 对不上直接 fail
            return False, "structural:N"
        within = abs(expected - coef) <= max(abs_tol, rel_tol * abs(expected))
        return within, "numeric"

    structural_flagged, _ = grade(80, -238.9)      # N 错被拦，且没进数值
    numeric_ok, note = grade(74, -238.900)          # 同 N 时数值容差内过
    ok = structural_flagged is False and numeric_ok
    return ok, f"L3：N错被拦(structural，不进数值)；同N时 coef 容差内过({note})"


CHECKS: dict[str, Check] = {
    "adversarial_numbers": _c1_numbers,
    "adversarial_citation": _c2_citation,
    "l3_cell_numeric": _c3_l3_numeric,
}


def run(names: list[str] | None = None) -> dict:
    selected = names or list(CHECKS)
    results: dict[str, tuple[bool, str]] = {}
    for name in selected:
        results[name] = CHECKS[name]()
    ok = all(passed for passed, _ in results.values())
    return {"ok": ok, "results": {k: {"pass": p, "note": n} for k, (p, n) in results.items()}}
