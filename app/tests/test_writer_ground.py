"""切片 5a：结果句渲染 + 数字 round-trip（篡改即抓）。"""

from __future__ import annotations

from stata_agent.domain.models import Claim, EvidenceCard
from stata_agent.writer.ground import (
    display_for,
    render_claim_sentence,
    validate_sentence,
)


def _cards() -> list[EvidenceCard]:
    return [
        EvidenceCard(card_id="c-coef", kind="numeric",
                     locator={"run_id": "r1", "stat_type": "coef"}, value={"value": -238.894}),
        EvidenceCard(card_id="c-N", kind="numeric",
                     locator={"run_id": "r1", "stat_type": "N"}, value={"value": 74}),
        EvidenceCard(card_id="c-r2", kind="numeric",
                     locator={"run_id": "r1", "stat_type": "r2"}, value={"value": 0.2196}),
    ]


def test_display_policy():
    assert display_for("N", 74) == "74"
    assert display_for("coef", -238.894) == "-238.894"
    assert display_for("r2", 0.2196) == "0.220"


def test_render_and_validate_clean():
    cards = _cards()
    by_id = {c.card_id: c for c in cards}
    claim = Claim(claim_id="c1", statement="价格随油耗下降", cards=[c.card_id for c in cards])
    sentence = render_claim_sentence(claim, by_id)
    assert "系数=-238.894" in sentence and "样本量=74" in sentence and "R²=0.220" in sentence
    assert validate_sentence(sentence, cards) == []  # round-trip 过


def test_validate_catches_tampered_number():
    cards = _cards()
    sentence = "价格随油耗下降（系数=-238.800，样本量=80）"  # 篡改 -238.8 与 N
    bad = validate_sentence(sentence, cards)
    assert "-238.800" in bad and "80" in bad


def test_claim_without_cards_still_renders():
    claim = Claim(claim_id="c2", statement="仅定性描述", cards=[])
    assert render_claim_sentence(claim, {}) == "仅定性描述"
