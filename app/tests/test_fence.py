"""L0：writer lease + revision fence（DD-01 §3.6）——旧进程覆盖被拒。"""

from __future__ import annotations

import pytest

from stata_agent.events.schema import EVENT_IDEA, ACTOR_AGENT, Event
from stata_agent.storage.store import LeaseConflict, StaleWrite
from stata_agent.storage.sqlite_store import SQLiteStore


def E(idea="i1", kind="x", **kw) -> Event:
    kw.setdefault("source", ACTOR_AGENT)
    return Event(idea_id=idea, event_type=kind, **kw)


def test_second_writer_without_takeover_conflicts(tmp_path):
    path = str(tmp_path / "l.db")
    a = SQLiteStore(path, writer_id="a")
    a.append(E(kind=EVENT_IDEA))
    with pytest.raises(LeaseConflict):
        SQLiteStore(path, writer_id="b")  # 同库新写者，未 takeover → 冲突
    a.close()


def test_stale_writer_rejected_after_hijack(tmp_path):
    path = str(tmp_path / "l.db")
    a = SQLiteStore(path, writer_id="a")
    a.append(E(kind=EVENT_IDEA))

    b = SQLiteStore(path, writer_id="b", takeover=True)  # 强占（模拟另一进程拿到写权）
    b.append(E(kind=EVENT_IDEA))

    with pytest.raises(StaleWrite):  # 旧进程 a 的租约已失效（fence）
        a.append(E(kind=EVENT_IDEA))

    assert b.revision >= 2
    a.close(); b.close()
