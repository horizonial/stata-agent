"""Contract coverage for the SQLite-backed MemoryStore compatibility facade."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from stata_agent.events.schema import ACTOR_USER, EVENT_USER, Event
from stata_agent.memory import (
    MemoryExtractionPipeline,
    MemoryStore,
    SQLiteMemoryStore,
)
from stata_agent.storage.sqlite_store import SQLiteStore


class _Provider:
    provider = "local"

    def __init__(self) -> None:
        self.requests = []

    def extract(self, request):
        self.requests.append(request)
        return [
            {
                "kind": "preference",
                "text": "以后默认使用中文回答",
                "source_ids": [request.sources[0].source_id],
            }
        ]


def _event(idea_id: str, text: str) -> Event:
    return Event(
        idea_id=idea_id,
        event_type=EVENT_USER,
        actor=ACTOR_USER,
        source=ACTOR_USER,
        payload={"text": text},
    )


def test_importing_facade_has_no_database_or_json_side_effect(tmp_path):
    target = tmp_path / "import-side-effect.sqlite3"
    source_root = Path(__file__).parents[1] / "src"
    code = (
        "import pathlib; import stata_agent.memory.sqlite_store; "
        f"assert not pathlib.Path({str(target)!r}).exists()"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "memory.json").exists()


def test_pipeline_accepts_facade_and_writes_only_sqlite(tmp_path):
    ledger = SQLiteStore(str(tmp_path / "ledger.db"), writer_id="facade-test")
    ledger.append(_event("idea-1", "以后默认使用中文回答"))
    memory = SQLiteMemoryStore(tmp_path / "memory.db", workspace_id="workspace-a")
    provider = _Provider()

    result = MemoryExtractionPipeline().run_once(
        store=ledger,
        memory=memory,
        idea_id="idea-1",
        workspace_id="workspace-a",
        provider=provider,
        privacy_mode="local_strict",
    )

    assert isinstance(memory, MemoryStore)
    assert result.status == "completed"
    assert len(provider.requests) == 1
    candidates = memory.candidates(workspace_id="workspace-a")
    assert len(candidates) == 1
    assert candidates[0]["status"] == "candidate"
    assert memory.search("中文", workspace_id="workspace-a") == []
    assert not (tmp_path / "memory.json").exists()
    memory.close()
    ledger.close()


def test_workspace_global_visibility_context_and_candidate_review(tmp_path):
    database = tmp_path / "memory.db"
    first = SQLiteMemoryStore(database, workspace_id="workspace-a")
    second = SQLiteMemoryStore(database, workspace_id="workspace-b")

    project_record = first.add("A 项目固定效应规则", source_ids="decision-a")
    first.add("所有项目使用中文", scope="global", source_ids="decision-global")
    candidate = first.add_candidate("以后默认保留标题", source_ids="extract-a")
    second.add("B 项目固定效应规则", source_ids="decision-b")

    assert first.search("固定效应") == [project_record]
    assert second.search("A") == []
    assert first.search("中文") == []
    assert first.search("中文", include_global=True)[0]["scope"] == "global"
    assert first.all(workspace_id="workspace-b") == [
        record
        for record in second.all(workspace_id="workspace-b")
    ]
    assert first.candidate(candidate["id"], workspace_id="workspace-b") is None
    assert first.accept_candidate(candidate["id"], workspace_id="workspace-b") is None
    assert first.candidate(candidate["id"], workspace_id="workspace-a") is not None

    selection = first.select_for_context("固定效应", limit=1, token_cap=40, now=100)
    assert len(selection) == 1
    assert "working knowledge, not evidence" in selection.text
    assert selection[0]["use_count"] == 0
    touched = first.all(workspace_id="workspace-a")[0]
    assert touched["use_count"] == 1

    accepted = first.accept_candidate(candidate["id"], provenance="approval-a")
    assert accepted is not None
    assert accepted["status"] == "active"
    rejected = first.add_candidate("不要自动删除中间结果", source_ids="extract-b")
    assert first.reject_candidate(rejected["id"]) is True
    assert first.candidates() == []
    assert first.search("标题")[0]["id"] == accepted["id"]
    assert not (tmp_path / "memory.json").exists()

    second.close()
    first.close()


def test_supersede_retract_prune_and_consolidate_use_repository(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "memory.db", workspace_id="workspace-a")
    original = store.add("旧的估计规则", source_ids="old", now=1)
    duplicate = store.add("旧的估计规则", source_ids="retry", now=2)
    replacement = store.supersede(original["id"], "新的估计规则", provenance="replace", now=3)
    tombstone = store.retract(replacement["id"], provenance={"event_id": "retract"}, now=4)
    expired = store.add("过期的推理记忆", confidence="inferred", expires_at=10, now=1)
    stale = store.add("长期未使用的推理记忆", confidence="inferred", now=1)
    explicit = store.add("始终保留的明确规则", confidence="explicit", now=1)

    assert duplicate["id"] == original["id"]
    assert store.consolidate() == 0
    assert store.all()[0]["id"] in {original["id"], replacement["id"], expired["id"], stale["id"], explicit["id"]}
    assert any(item["status"] == "superseded" for item in store.all())
    assert tombstone is not None
    assert tombstone["status"] == "retracted"
    assert "retract" in tombstone["source_ids"]
    assert store.search("旧的") == []
    assert store.search("新的") == []

    assert store.prune(now=100, max_age_seconds=50) == 2
    remaining = {item["id"] for item in store.all()}
    assert expired["id"] not in remaining
    assert stale["id"] not in remaining
    assert explicit["id"] in remaining

    unused = store.add("从未被使用的规则", now=100)
    used = store.add("被使用的规则", now=100)
    store.touch(used["id"])
    store.touch(explicit["id"])
    assert store.prune_unused() == 1
    assert store.candidate(unused["id"]) is None
    assert store.search("被使用")[0]["id"] == used["id"]
    assert not (tmp_path / "memory.json").exists()
    store.close()


def test_global_record_can_be_retracted_and_superseded(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "memory.db", workspace_id="workspace-a")
    global_record = store.add("全局规则", scope="global", source_ids="global-1")
    replacement = store.supersede(global_record["id"], "全局新规则", now=10)
    assert replacement["workspace_id"] == "global"
    assert store.search("全局新规则", include_global=True)[0]["id"] == replacement["id"]
    assert store.retract(replacement["id"], now=11)["status"] == "retracted"
    assert store.search("全局新规则", include_global=True) == []
    store.close()
