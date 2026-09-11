"""引文接地（DD-05 §4 / SPEC §4.8，A 组第二件）。

只允许引用 citable_evidence 的真实块；正文引文带 marker（chunk_id 可反查），
validator 校验：① 卡里每篇引用对应的 marker 都在正文 ② 正文出现的 marker 都能回链到卡。
style_only / background_only 一律不可引用。
"""

from __future__ import annotations

import re

from ..domain.models import Claim, EvidenceCard
from ..events.schema import (
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_SIGNED,
    ACTOR_EVIDENCE,
    ACTOR_VALIDATOR,
    Event,
)
from ..rag.library import Library
from ..storage.sqlite_store import SQLiteStore

_MARKER = re.compile(r"\[c:[A-Za-z0-9_-]+\]")


def citation_marker(chunk_id: str) -> str:
    return f"[c:{chunk_id}]"


def markers_in(text: str) -> list[str]:
    return _MARKER.findall(text)


def sign_citation_card(
    store: SQLiteStore,
    library: Library,
    chunk_id: str,
    *,
    idea: str = "i1",
) -> str:
    """把一篇 citable 文献块签成 citation 卡（validator）；非 citable 拒绝。"""
    chunk = library.get(chunk_id)
    if chunk is None:
        raise ValueError(f"chunk 不存在: {chunk_id}")
    if not library.is_citable(chunk_id):
        raise ValueError(f"chunk {chunk_id!r} 的 source_role={chunk.source_role} 不可引用（只许 citable_evidence）")
    text_digest = library.text_digest(chunk_id)
    card_id = f"card-cit-{chunk_id}"
    projection = store.project(idea)
    existing = projection.cards.get(card_id)
    if existing is not None:
        existing_locator = existing.locator if isinstance(existing.locator, dict) else {}
        existing_value = existing.value if isinstance(existing.value, dict) else {}
        if (
            existing.kind == "citation"
            and existing_locator.get("chunk_id") == chunk_id
            and existing_locator.get("doc_id") == chunk.doc_id
            and existing_locator.get("page") == chunk.page
            and existing_locator.get("source_role") == chunk.source_role
            and existing_value.get("text_digest") == text_digest
            and existing_value.get("parser_version") == chunk.parser_version
        ):
            return card_id
        raise ValueError("citation card identity collision")
    card = EvidenceCard(
        card_id=card_id,
        kind="citation",
        locator={"chunk_id": chunk_id, "doc_id": chunk.doc_id, "page": chunk.page,
                 "source_role": chunk.source_role},
        value={"text_head": chunk.text[:120], "text_digest": text_digest,
               "parser_version": chunk.parser_version},
        signed_by=ACTOR_VALIDATOR,
    )
    store.append(Event(idea_id=idea, event_type=EVENT_CARD_SIGNED, actor=ACTOR_VALIDATOR,
                       source=ACTOR_VALIDATOR, payload={"card": card.model_dump()}))
    return card.card_id


def cite_claim(
    store: SQLiteStore,
    chunk_id: str,
    statement: str,
    *,
    idea: str = "i1",
    extra_cards: list[str] | None = None,
) -> str:
    """从 citation 卡 + 可选数字卡合成一条 claim（evidence_builder）。
    正文由 writer 加 marker；statement 供渲染。"""
    card_id = f"card-cit-{chunk_id}"
    projection = store.project(idea)
    card = projection.cards.get(card_id)
    if card is None or card.kind != "citation":
        raise ValueError(f"citation card 不存在: {card_id}")
    cards = [card_id] + list(extra_cards or [])
    if len(cards) != len(set(cards)):
        raise ValueError("claim cards 不能重复")
    missing = [cid for cid in cards if cid not in projection.cards]
    if missing:
        raise ValueError(f"claim 引用不存在的 card: {missing}")
    if not statement.strip():
        raise ValueError("claim statement 不能为空")
    claim_id = f"claim-cit-{chunk_id}"
    if claim_id in projection.claims:
        raise ValueError(f"claim 已存在: {claim_id}")
    claim = Claim(claim_id=claim_id, statement=statement, cards=cards,
                  written_by=ACTOR_EVIDENCE)
    store.append(Event(idea_id=idea, event_type=EVENT_CLAIM_SIGNED, actor=ACTOR_EVIDENCE,
                       source=ACTOR_EVIDENCE, payload={"claim": claim.model_dump()}))
    return claim.claim_id


def validate_citations(
    text: str,
    cards: list[EvidenceCard],
    *,
    library: Library | None = None,
    claim_card_ids: list[str] | None = None,
) -> list[str]:
    """返回问题列表（空=通过）。每个 citation 卡必须有其 marker；多余 marker 报未知。"""
    cit_cards = [c for c in cards if c.kind == "citation"]
    markers = markers_in(text)
    present = set(markers)
    issues: list[str] = []
    for card in cit_cards:
        chunk_id = (card.locator or {}).get("chunk_id")
        if library is not None:
            chunk = library.get(str(chunk_id)) if chunk_id else None
            expected_digest = library.text_digest(str(chunk_id)) if chunk_id else None
            actual_digest = (card.value or {}).get("text_digest") if isinstance(card.value, dict) else None
            if chunk is None:
                issues.append(f"引文块不存在: {chunk_id}")
            elif not library.is_citable(str(chunk_id)):
                issues.append(f"引文块不可引用: {chunk_id}")
            elif (
                (card.locator or {}).get("doc_id") != chunk.doc_id
                or (card.locator or {}).get("page") != chunk.page
                or (card.locator or {}).get("source_role") != chunk.source_role
            ):
                issues.append(f"引文定位已变化: {chunk_id}")
            elif actual_digest != expected_digest:
                issues.append(f"引文块已变化: {chunk_id}")
            elif (
                isinstance(card.value, dict)
                and card.value.get("parser_version") is not None
                and card.value.get("parser_version") != chunk.parser_version
            ):
                issues.append(f"引文解析版本已变化: {chunk_id}")
        if chunk_id and citation_marker(str(chunk_id)) not in present:
            issues.append(f"缺引用 marker: {chunk_id}")
        if chunk_id and markers.count(citation_marker(str(chunk_id))) > 1:
            issues.append(f"重复引用 marker: {chunk_id}")
    known = {
        citation_marker(str((c.locator or {}).get("chunk_id")))
        for c in cit_cards if c.locator
    }
    for marker in present:
        if marker not in known:
            issues.append(f"无来源引用 marker: {marker}")
    if claim_card_ids is not None:
        expected = {str(cid) for cid in claim_card_ids}
        actual = {c.card_id for c in cit_cards}
        if expected != actual:
            issues.append("claim 与 citation cards 不一致")
    return issues
