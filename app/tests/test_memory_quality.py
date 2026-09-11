"""Task 4: bounded memory quality, parity, and lifecycle guardrails."""

from __future__ import annotations

import pytest

from stata_agent.memory import MemoryStore, SQLiteMemoryStore


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_json_sqlite_search_and_context_projection_have_same_contract(tmp_path, backend):
    if backend == "json":
        store = MemoryStore(tmp_path / "memory.json", workspace_id="ws-a")
    else:
        store = SQLiteMemoryStore(tmp_path / "memory.db", workspace_id="ws-a")

    store.add("固定效应按行业聚类", kind="constraint", source_ids="decision-1", now=1)
    store.add("输出默认使用中文", kind="preference", source_ids="decision-2", now=2)
    store.add("完全无关的备注", kind="preference", source_ids="decision-3", now=3)

    found = store.search("固定效应", workspace_id="ws-a")
    selected = store.select_for_context(
        "固定效应", workspace_id="ws-a", limit=2, token_cap=80, touch=False, whole_items=True
    )

    assert [record["text"] for record in found] == ["固定效应按行业聚类"]
    assert selected.text == "[memory/constraint; working knowledge, not evidence] 固定效应按行业聚类 (provenance: decision-1)"
    assert "not evidence" in selected.text
    assert store.search("紫色鲸鱼", workspace_id="ws-a") == []

    close = getattr(store, "close", None)
    if callable(close):
        close()


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_whole_item_selection_skips_oversized_memory_and_keeps_usage_untouched(tmp_path, backend):
    if backend == "json":
        store = MemoryStore(tmp_path / "memory.json", workspace_id="ws-a")
    else:
        store = SQLiteMemoryStore(tmp_path / "memory.db", workspace_id="ws-a")

    oversized = store.add("固定效应 " + ("非常长的规则 " * 80), now=2)
    fitting = store.add("固定效应 使用行业聚类", now=1)

    selection = store.select_for_context(
        "固定效应",
        workspace_id="ws-a",
        limit=1,
        token_cap=35,
        whole_items=True,
        now=100,
    )

    assert [record["id"] for record in selection] == [fitting["id"]]
    assert selection.truncated is False
    usage = {record["id"]: record["use_count"] for record in store.all(workspace_id="ws-a")}
    assert usage[oversized["id"]] == 0
    assert usage[fitting["id"]] == 1

    close = getattr(store, "close", None)
    if callable(close):
        close()


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_explicit_supersede_links_are_clean_and_orphan_links_fail_closed(tmp_path, backend):
    if backend == "json":
        store = MemoryStore(tmp_path / "memory.json", workspace_id="ws-a")
    else:
        store = SQLiteMemoryStore(tmp_path / "memory.db", workspace_id="ws-a")

    original = store.add("旧的估计规则", workspace_id="ws-a")
    replacement = store.supersede(original["id"], "新的估计规则")
    assert replacement["status"] == "active"
    assert store.lifecycle_issues(workspace_id="ws-a") == ()

    if backend == "json":
        raw = next(record for record in store._entries if record["id"] == replacement["id"])
        raw["supersedes"] = "missing-record"
    else:
        connection = store._repository.connection
        connection.execute(
            "UPDATE memory_records SET supersedes=? WHERE id=?",
            ("missing-record", replacement["id"]),
        )
        connection.commit()

    assert store.lifecycle_issues(workspace_id="ws-a") == ("supersedes_orphan",)
    with pytest.raises(Exception, match="memory_lifecycle_invalid"):
        store.validate_lifecycle(workspace_id="ws-a")

    close = getattr(store, "close", None)
    if callable(close):
        close()
