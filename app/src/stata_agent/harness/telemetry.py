"""两本账遥测（audit §E / codex_report）：业务账本(events)管"做了什么"，
遥测(Telemetry)管"多慢/多贵/错几次"，二者不互唯一真相。

Telemetry = append-only jsonl，默认不记内容（只记 kind/耗时/量/关联 seq）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class Telemetry:
    def __init__(self, path: str | Path, *, record_content: bool = False):
        self._path = Path(path)
        self._record_content = record_content
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, *, kind: str, ms: float = 0.0, tokens: int | None = None,
               idea: str | None = None, event_seq: int | None = None, detail: str | None = None) -> None:
        row = {
            "ts": int(time.time() * 1000),
            "kind": kind,
            "ms": round(ms, 1),
            "tokens": tokens,
            "idea": idea,
            "event_seq": event_seq,
        }
        if self._record_content and detail is not None:
            row["detail"] = detail[:500]
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def rows(self) -> list[dict]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def metrics(self) -> dict:
        rows = self.rows()
        kinds: dict[str, int] = {}
        total_ms = 0.0
        for r in rows:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            total_ms += float(r.get("ms", 0.0))
        return {"events": len(rows), "by_kind": kinds, "total_ms": round(total_ms, 1)}
