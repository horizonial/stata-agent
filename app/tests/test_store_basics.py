"""L0：append/scan/seq、fingerprint 幂等、写入权、确定性重放。"""

from __future__ import annotations

import pytest

from stata_agent.events.append import WriterNotPermitted
from stata_agent.events.schema import (
    EVENT_CLAIM_SIGNED,
    EVENT_IDEA,
    EVENT_SPEC_FREEZE,
    ACTOR_AGENT,
    ACTOR_VALIDATOR,
    Event,
)
from stata_agent.storage.store import DuplicateFingerprint
from stata_agent.storage.sqlite_store import SQLiteStore


def E(idea="i1", kind="x", **kw) -> Event:
    kw.setdefault("source", ACTOR_AGENT)
    kw.setdefault("actor", ACTOR_AGENT)
    return Event(idea_id=idea, event_type=kind, **kw)


def test_seq_monotonic_and_scan_order(tmp_path):
    s = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    a = s.append(E(kind=EVENT_IDEA, payload={"q": "研究问题"}))
    b = s.append(E(kind=EVENT_SPEC_FREEZE, payload={"spec_id": "s1"}, source=ACTOR_AGENT))
    c = s.append(E(kind=EVENT_CLAIM_SIGNED, payload={"claim": {"claim_id": "cl1", "statement": "x", "cards": []}},
                  source=ACTOR_VALIDATOR))
    assert (a, b, c) == (1, 2, 3)
    kinds = [ev.event_type for ev in s.scan("i1")]
    assert kinds == [EVENT_IDEA, EVENT_SPEC_FREEZE, EVENT_CLAIM_SIGNED]
    assert [ev.seq for ev in s.scan("i1")] == [1, 2, 3]
    # after_seq 增量
    assert [ev.seq for ev in s.scan("i1", after_seq=2)] == [3]
    s.close()


def test_fingerprint_duplicate_rejected(tmp_path):
    s = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    s.append(E(kind="tool.call", operation_id="op1", fingerprint="fp1", payload={"side_effect": "read"}))
    with pytest.raises(DuplicateFingerprint):
        s.append(E(kind="tool.call", operation_id="op2", fingerprint="fp1", payload={"side_effect": "read"}))
    s.close()


def test_writer_permission_signing_denied_for_agent(tmp_path):
    s = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    with pytest.raises(WriterNotPermitted):
        s.append(E(kind=EVENT_CLAIM_SIGNED, source=ACTOR_AGENT,
                   payload={"claim": {"claim_id": "cl1", "statement": "x", "cards": []}}))
    s.close()


def test_deterministic_replay(tmp_path):
    evs = [
        E(kind=EVENT_IDEA, payload={"q": "Q"}),
        E(kind=EVENT_SPEC_FREEZE, payload={"spec_id": "s1"}, source=ACTOR_AGENT),
        E(kind="phase.transition", payload={"from": "IDEA", "to": "DESIGN"}),
    ]
    s1 = SQLiteStore(str(tmp_path / "a.db"), writer_id="a")
    for ev in evs:
        s1.append(ev)
    proj1 = s1.project("i1")

    s2 = SQLiteStore(str(tmp_path / "b.db"), writer_id="a")
    for ev in evs:
        s2.append(ev)
    proj2 = s2.project("i1")

    assert proj1.summary() == proj2.summary()
    assert proj1.summary()[1] == "DESIGN"          # phase 跟随迁移
    assert proj1.summary()[2] == "s1"              # current_spec_id
    s1.close(); s2.close()
