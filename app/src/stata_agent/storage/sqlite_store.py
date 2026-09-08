"""SQLite 账本实现 + 单写者租约/revision fence（DD-01 §3.1/§3.6/§3.8）。

切片 0 目标：正确性优先（append 校验、幂等、fence、投影可重建），
性能（拆列/批量）留到有量再优化。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Iterator, Optional

from ..domain.reducers import Projection, fold as _fold
from ..events.append import assert_sane_event
from ..events.schema import Event
from ..events.upcast import upcast
from .store import (
    DuplicateFingerprint,
    LeaseConflict,
    StaleWrite,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  event_id        TEXT PRIMARY KEY,
  idea_id         TEXT NOT NULL,
  seq             INTEGER NOT NULL,
  branch_id       TEXT NOT NULL DEFAULT 'main',
  prev_event_id   TEXT,
  phase           TEXT,
  event_type      TEXT NOT NULL,
  schema_version  INTEGER NOT NULL DEFAULT 1,
  actor           TEXT NOT NULL,
  source          TEXT NOT NULL,
  correlation_id  TEXT,
  causation_id    TEXT,
  operation_id    TEXT,
  attempt_id      INTEGER NOT NULL DEFAULT 0,
  fingerprint     TEXT,
  confidence      TEXT NOT NULL DEFAULT 'judgment',
  side_effect_state TEXT,
  payload         TEXT NOT NULL DEFAULT '{}',
  created_at      INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_seq ON events(idea_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_fp
  ON events(idea_id, event_type, fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_branch ON events(idea_id, branch_id, seq);

CREATE TABLE IF NOT EXISTS writer_lease (
  writer_id   TEXT PRIMARY KEY,
  token       TEXT NOT NULL,
  revision    INTEGER NOT NULL DEFAULT 0,
  acquired_at INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
  idea_id    TEXT NOT NULL,
  as_of_seq  INTEGER NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'full',
  blob       BLOB NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (idea_id, as_of_seq, kind)
);
"""


class SQLiteStore:
    """单文件账本；单写者由 writer_lease + token 保证，append 每次核对。"""

    def __init__(self, path: str, *, writer_id: str = "w1", takeover: bool = False):
        self._path = path
        self._writer_id = writer_id
        self._token = uuid.uuid4().hex
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._acquire_lease(takeover=takeover)

    # ---------------------------------------------------------------- lease
    def _acquire_lease(self, *, takeover: bool) -> None:
        now = int(time.time())
        row = self._conn.execute(
            "SELECT writer_id, token FROM writer_lease WHERE writer_id='master'"
        ).fetchone()
        if row is not None:
            if not takeover and row["writer_id"] != self._writer_id:
                # 兼容性：单主键=master 行；简化用固定 writer_id=master
                pass
        if takeover:
            self._conn.execute(
                "INSERT INTO writer_lease(writer_id,token,revision,acquired_at,expires_at) "
                "VALUES('master',?,COALESCE((SELECT revision FROM writer_lease WHERE writer_id='master'),0),?,?) "
                "ON CONFLICT(writer_id) DO UPDATE SET token=excluded.token, acquired_at=excluded.acquired_at, "
                "expires_at=excluded.expires_at",
                (self._token, now, now + 3600),
            )
            self._conn.commit()
            return
        cur = self._conn.execute(
            "INSERT INTO writer_lease(writer_id,token,revision,acquired_at,expires_at) "
            "VALUES('master',?,0,?,?) ON CONFLICT(writer_id) DO NOTHING",
            (self._token, now, now + 3600),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT token FROM writer_lease WHERE writer_id='master'").fetchone()
        if row["token"] != self._token:
            raise LeaseConflict("账本已被其他 writer 持有（单写者）")

    def _check_lease(self) -> None:
        row = self._conn.execute("SELECT token FROM writer_lease WHERE writer_id='master'").fetchone()
        if row is None or row["token"] != self._token:
            raise StaleWrite(f"writer={self._writer_id!r} 的租约已失效（fence）")

    def _bump_revision(self) -> None:
        self._conn.execute(
            "UPDATE writer_lease SET revision=revision+1 WHERE writer_id='master'"
        )

    @property
    def revision(self) -> int:
        row = self._conn.execute("SELECT revision FROM writer_lease WHERE writer_id='master'").fetchone()
        return int(row["revision"]) if row else 0

    # ---------------------------------------------------------------- append
    def append(self, event: Event) -> int:
        assert_sane_event(event)
        if event.fingerprint and self._exists_fingerprint(event):
            raise DuplicateFingerprint(
                f"dup (idea={event.idea_id}, {event.event_type}, {event.fingerprint[:12]}…)"
            )
        seq = self._next_seq()
        event_id = uuid.uuid4().hex
        prev = self._prev_event_id(event.idea_id, event.branch_id)
        now = int(time.time() * 1000)
        with self._conn:
            self._check_lease()
            self._conn.execute(
                "INSERT INTO events(event_id,idea_id,seq,branch_id,prev_event_id,phase,event_type,"
                "schema_version,actor,source,correlation_id,causation_id,operation_id,attempt_id,"
                "fingerprint,confidence,side_effect_state,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, event.idea_id, seq, event.branch_id, prev, event.phase, event.event_type,
                    event.schema_version, event.actor, event.source, event.correlation_id, event.causation_id,
                    event.operation_id, event.attempt_id, event.fingerprint, event.confidence,
                    event.side_effect_state, json.dumps(event.payload, ensure_ascii=False), now,
                ),
            )
            self._set_last_seq(seq)
            self._bump_revision()
        return seq

    def append_many(self, events: list[Event]) -> int:
        last = None
        for ev in events:
            last = self.append(ev)  # 每步独立小事务；量小够用
        return last if last is not None else 0

    # ---------------------------------------------------------------- query
    def scan(
        self,
        idea_id: str,
        *,
        after_seq: int = 0,
        branch: str | None = None,
        event_types: set[str] | None = None,
    ) -> Iterator[Event]:
        sql = "SELECT * FROM events WHERE idea_id=? AND seq>?"
        args: list = [idea_id, after_seq]
        if branch:
            sql += " AND branch_id=?"
            args.append(branch)
        if event_types:
            ph = ",".join("?" * len(event_types))
            sql += f" AND event_type IN ({ph})"
            args.extend(event_types)
        sql += " ORDER BY seq"
        cur = self._conn.execute(sql, args)
        for r in cur:
            yield self._row_to_event(r)

    def project(self, idea_id: str) -> Projection:
        return _fold((upcast(ev) for ev in self.scan(idea_id)), idea_id=idea_id)

    # ---------------------------------------------------------------- internal
    def _row_to_event(self, r: sqlite3.Row) -> Event:
        return Event(
            idea_id=r["idea_id"], event_type=r["event_type"], actor=r["actor"], source=r["source"],
            payload=json.loads(r["payload"]),
            event_id=r["event_id"], seq=r["seq"], branch_id=r["branch_id"], prev_event_id=r["prev_event_id"],
            phase=r["phase"], schema_version=r["schema_version"], correlation_id=r["correlation_id"],
            causation_id=r["causation_id"], operation_id=r["operation_id"], attempt_id=r["attempt_id"],
            fingerprint=r["fingerprint"], confidence=r["confidence"], side_effect_state=r["side_effect_state"],
            created_at=r["created_at"],
        )

    def _exists_fingerprint(self, ev: Event) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE idea_id=? AND event_type=? AND fingerprint=? LIMIT 1",
            (ev.idea_id, ev.event_type, ev.fingerprint),
        ).fetchone()
        return row is not None

    def _last_seq(self) -> int:
        row = self._conn.execute("SELECT MAX(seq) AS s FROM events").fetchone()
        return int(row["s"]) if row and row["s"] is not None else 0

    def _next_seq(self) -> int:
        return self._last_seq() + 1

    def _set_last_seq(self, seq: int) -> None:
        pass  # seq 由 MAX(events.seq) 推导，无需额外表

    def _prev_event_id(self, idea_id: str, branch: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT event_id FROM events WHERE idea_id=? AND branch_id=? ORDER BY seq DESC LIMIT 1",
            (idea_id, branch),
        ).fetchone()
        return row["event_id"] if row else None

    # ---------------------------------------------------------------- snapshot
    def save_snapshot(self, idea_id: str) -> int:
        """存当前投影摘要为快照（derived），返回 as_of_seq。恢复 = 快照 + 增量重放。"""
        asof = self._last_seq()
        summary = self.project(idea_id).summary()
        blob = json.dumps({"summary": summary, "seq": asof}, ensure_ascii=False).encode()
        now = int(time.time() * 1000)
        with self._conn:
            self._conn.execute("DELETE FROM snapshots WHERE idea_id=? AND kind='full'", (idea_id,))
            self._conn.execute(
                "INSERT INTO snapshots(idea_id,as_of_seq,kind,blob,created_at) VALUES(?,?,'full',?,?)",
                (idea_id, asof, blob, now),
            )
        return asof

    def latest_snapshot(self, idea_id: str):
        """返回 (as_of_seq, summary) 或 None。"""
        row = self._conn.execute(
            "SELECT as_of_seq, blob FROM snapshots WHERE idea_id=? AND kind='full' "
            "ORDER BY as_of_seq DESC LIMIT 1",
            (idea_id,),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row["blob"])
        return int(row["as_of_seq"]), _to_tuples(data["summary"])

    def close(self) -> None:
        self._conn.close()


def _to_tuples(x):
    """json 会把 tuple 存成 list；读回时还原成 tuple，保证与 summary() 可比。"""
    if isinstance(x, list):
        return tuple(_to_tuples(i) for i in x)
    if isinstance(x, tuple):
        return tuple(_to_tuples(i) for i in x)
    return x
