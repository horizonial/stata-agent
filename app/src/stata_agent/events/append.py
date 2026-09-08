"""append 前校验（DD-01 §3.3 不变量 5/6：写入权、只 append）。

store 在事务内先跑这些校验，过了才落库。
"""

from __future__ import annotations

from .schema import (
    CARD_SIGNING_SOURCES,
    CHAIN_INTERMEDIATE,
    CHAIN_START,
    CHAIN_TERMINAL,
    CLAIM_SIGNING_SOURCES,
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_RETRACT,
    EVENT_CLAIM_SIGNED,
    RETRACT_SOURCES,
    Event,
)


class AppendError(Exception):
    """事件写入被拒（不可重试的策略性错误）。"""


class WriterNotPermitted(AppendError):
    """非 validator/evidence_builder 无权产出 EvidenceCard/Claim。"""


def validate_writer(ev: Event) -> None:
    """Validate the role boundary for immutable evidence writes.

    ``actor`` and ``source`` are deliberately checked together.  For cards and
    claims the signed object carries a second role assertion (``signed_by`` or
    ``written_by``), which must agree with the event writer.  This keeps the
    existing Event API while preventing the common source-spoofing failure mode
    where a model emits ``source='validator'`` around an agent-authored object.
    """
    kind = ev.event_type
    if kind not in {EVENT_CARD_SIGNED, EVENT_CLAIM_SIGNED, EVENT_CLAIM_RETRACT}:
        return

    payload = ev.payload or {}
    if kind == EVENT_CARD_SIGNED:
        allowed = CARD_SIGNING_SOURCES
        role_key = "signed_by"
        object_key = "card"
    elif kind == EVENT_CLAIM_SIGNED:
        allowed = CLAIM_SIGNING_SOURCES
        role_key = "written_by"
        object_key = "claim"
    else:
        allowed = RETRACT_SOURCES
        role_key = "written_by"
        object_key = None

    if ev.actor != ev.source or ev.source not in allowed:
        raise WriterNotPermitted(
            f"event_type={kind} 不允许 actor={ev.actor!r}, source={ev.source!r}"
            f"（允许 source={sorted(allowed)!r}）"
        )

    # Retracts predate the role field and are still accepted when the payload
    # only contains claim_id.  If a role assertion is present, it is checked.
    if object_key is not None:
        obj = payload.get(object_key, payload)
        if not isinstance(obj, dict) or obj.get(role_key) != ev.source:
            raise WriterNotPermitted(
                f"event_type={kind} 的 payload.{role_key} 必须等于 source={ev.source!r}"
            )
    elif role_key in payload and payload.get(role_key) != ev.source:
        raise WriterNotPermitted(
            f"event_type={kind} 的 payload.{role_key} 必须等于 source={ev.source!r}"
        )


def assert_sane_event(ev: Event) -> None:
    """基础健全性：类型非空、idea 归属、写入权。"""
    if not ev.event_type:
        raise AppendError("event_type 为空")
    if not ev.idea_id:
        raise AppendError("idea_id 为空")
    validate_writer(ev)


def is_execution_start(kind: str) -> bool:
    return kind in CHAIN_START


def is_execution_terminal(kind: str) -> bool:
    return kind in CHAIN_TERMINAL


def is_execution_intermediate(kind: str) -> bool:
    return kind in CHAIN_INTERMEDIATE
