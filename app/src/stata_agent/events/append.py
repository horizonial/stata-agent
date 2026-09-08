"""append 前校验（DD-01 §3.3 不变量 5/6：写入权、只 append）。

store 在事务内先跑这些校验，过了才落库。
"""

from __future__ import annotations

from .schema import (
    CHAIN_INTERMEDIATE,
    CHAIN_START,
    CHAIN_TERMINAL,
    SIGNING_SOURCES,
    Event,
    writer_permission,
)


class AppendError(Exception):
    """事件写入被拒（不可重试的策略性错误）。"""


class WriterNotPermitted(AppendError):
    """非 validator/evidence_builder 无权产出 EvidenceCard/Claim。"""


def validate_writer(ev: Event) -> None:
    """DD-01 §3.3-5：card/claim 只有 validator/evidence_builder 能写。"""
    if not writer_permission(ev.event_type, ev.source):
        raise WriterNotPermitted(
            f"event_type={ev.event_type} 不允许 source={ev.source!r} 写入"
            f"（仅 {sorted(SIGNING_SOURCES)!r} 可签发）"
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
