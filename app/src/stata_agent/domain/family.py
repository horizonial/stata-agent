"""ExperimentFamily（DD-01 §2.4，B 组余项）：同一假设下的全成员 run + 主结果选择。

成员与主结果都以事件记录（append-only、可审计），不写内存第二份事实。
"""

from __future__ import annotations

from ..events.schema import (
    EVENT_FAMILY_RUN,
    EVENT_MAIN_RESULT,
    ACTOR_ORCH,
    Event,
)
from ..storage.sqlite_store import SQLiteStore


def register_run(store: SQLiteStore, idea: str, family_id: str, run_id: str, *,
                 label: str = "", variant: str = "") -> int:
    """把一个 run 记为该 family 的成员（防只记录喜欢规格：失败也要先试）。"""
    return store.append(Event(
        idea_id=idea, event_type=EVENT_FAMILY_RUN, actor=ACTOR_ORCH, source=ACTOR_ORCH,
        payload={"family_id": family_id, "run_id": run_id, "label": label, "variant": variant},
    ))


def select_main(store: SQLiteStore, idea: str, family_id: str, run_id: str, *, reason: str) -> int:
    """选主结果（须带理由，防选择报告；DD-01 §2.4）。"""
    return store.append(Event(
        idea_id=idea, event_type=EVENT_MAIN_RESULT, actor=ACTOR_ORCH, source=ACTOR_ORCH,
        payload={"family_id": family_id, "run_id": run_id, "reason": reason},
    ))


def family_view(store: SQLiteStore, idea: str, family_id: str) -> dict:
    """从事件折叠出 family 视图（成员 run 顺序 + 主结果）。"""
    members: list[dict] = []
    main: dict | None = None
    for e in store.scan(idea, event_types={EVENT_FAMILY_RUN, EVENT_MAIN_RESULT}):
        p = e.payload or {}
        if p.get("family_id") != family_id:
            continue
        if e.event_type == EVENT_FAMILY_RUN:
            members.append({"run_id": p.get("run_id"), "label": p.get("label") or "",
                            "variant": p.get("variant") or "", "seq": e.seq})
        else:
            main = {"run_id": p.get("run_id"), "reason": p.get("reason") or "", "seq": e.seq}
    return {"family_id": family_id, "members": members, "main": main}
