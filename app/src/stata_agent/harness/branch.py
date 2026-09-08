"""分支语义（DD-01 §3.4 fork/leaf，B3）。

append-only 事件表里"换 spec = 新分支"表达为：在 fork 点写 `branch.created`，
其后的事件归属新分支。切片 = 给每个事件按"最近一个 from_seq<该事件 seq 的 fork"
归属分支（线性换向），从而能按分支回放/对比。

说明（诚实边界）：并行多分支同时折叠（同一时刻两个 leaf 都进 ResearchState）还没接，
先支持：换 spec=fork、按分支取事件、父链；把"活动分支写入"留给 runner 编排层。
"""

from __future__ import annotations

from ..events.schema import EVENT_BRANCH, ACTOR_ORCH, Event
from ..storage.sqlite_store import SQLiteStore

MAIN = "main"


def _forks(events: list[Event]) -> list[dict]:
    forks = []
    for e in events:
        if e.event_type == EVENT_BRANCH:
            p = e.payload or {}
            forks.append({"seq": e.seq, "branch_id": p.get("branch_id") or MAIN,
                          "from_seq": p.get("from_seq") or 0, "parent": p.get("parent") or MAIN,
                          "reason": p.get("reason") or ""})
    return forks


def branch_of(seq: int, forks: list[dict]) -> str:
    """事件 seq 归属哪个分支：取 from_seq < seq 的最近一个 fork。"""
    chosen = None
    for f in forks:
        if f["from_seq"] < seq:
            chosen = f
    return chosen["branch_id"] if chosen else MAIN


def fork(store: SQLiteStore, idea: str, *, reason: str, branch_id: str | None = None) -> int:
    """在账本当前尾部打一个 fork 点（换 spec/放弃最近尝试），返回新分支 id 语义 seq。"""
    events = list(store.scan(idea))
    last = events[-1].seq if events else 0
    parent = active_branch(events)
    bid = branch_id or f"b{len(_forks(events)) + 1}"
    return store.append(Event(idea_id=idea, event_type=EVENT_BRANCH, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                              payload={"branch_id": bid, "from_seq": last, "parent": parent, "reason": reason}))


def active_branch(events: list[Event]) -> str:
    """当前活动（最末）分支 = 最后一个 fork 的分支；无 fork 为 main。"""
    forks = _forks(events)
    return forks[-1]["branch_id"] if forks else MAIN


def events_in(store: SQLiteStore, idea: str, branch: str = MAIN) -> list[Event]:
    """返回某分支路径下的事件（fork 点属于其父分支侧）。"""
    events = list(store.scan(idea))
    forks = _forks(events)
    return [e for e in events if e.event_type != EVENT_BRANCH and branch_of(e.seq or 0, forks) == branch]


def parent_of(events: list[Event], branch: str) -> str:
    for f in reversed(_forks(events)):
        if f["branch_id"] == branch:
            return f["parent"]
    return MAIN
