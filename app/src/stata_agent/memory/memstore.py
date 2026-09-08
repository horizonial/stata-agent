"""MemoryStore：研究者偏好/项目决定/否决记录（约束，不作证据）。

文件 JSON；轻量 usage 遥测 + 裁剪（借鉴 codex 记忆的 usage/prune，截断式本地版）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from ..rag.retriever import tokenize

KIND_PREFERENCE = "preference"
KIND_DECISION = "decision"
KIND_RULE = "rule"


class MemoryStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._entries: list[dict] = []
        if self._path.exists():
            try:
                self._entries = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._entries = []

    # ---------------------------------------------------------------- write
    def add(self, text: str, kind: str = KIND_PREFERENCE) -> dict:
        entry = {"id": f"mem-{uuid.uuid4().hex[:8]}", "text": text, "kind": kind,
                 "used": 0, "updated": int(time.time())}
        self._entries.append(entry)
        self._save()
        return entry

    def touch(self, entry_id: str) -> None:
        for e in self._entries:
            if e["id"] == entry_id:
                e["used"] += 1
                e["updated"] = int(time.time())
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._entries, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------------------------------------------------------------- read
    def all(self) -> list[dict]:
        return list(self._entries)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        q = set(tokenize(query))
        scored = []
        for e in self._entries:
            hit = len(q & set(tokenize(e["text"])))
            if hit:
                scored.append((hit, e["updated"], e))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [e for _, _, e in scored[:limit]]

    def to_context(self, max_items: int = 6) -> list[str]:
        ordered = sorted(self._entries, key=lambda e: (e["used"], e["updated"]), reverse=True)
        return [f"[记忆/{e['kind']}] {e['text']}" for e in ordered[:max_items]]

    def prune_unused(self, min_used: int = 1) -> int:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e["used"] >= min_used]
        if len(self._entries) != before:
            self._save()
        return before - len(self._entries)


def remember_decision(memory: MemoryStore, note: str) -> dict | None:
    """把学者批准时的说明记成决策记忆（约束后续，不作证据）。"""
    note = (note or "").strip()
    if not note:
        return None
    return memory.add(f"研究决定：{note}", kind=KIND_DECISION)
