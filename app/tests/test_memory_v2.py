"""Focused regression coverage for the project-memory V2 contract."""

from __future__ import annotations

import json

import pytest

from stata_agent.memory import (
    KIND_CONSTRAINT,
    KIND_PREFERENCE,
    MemoryCandidateRejected,
    MemoryStore,
)


def test_v1_file_is_lazy_migrated_and_next_write_is_v2(tmp_path):
    path = tmp_path / "memory.json"
    legacy = [{"id": "old-1", "text": "保留中文标点：是", "kind": "preference", "used": 2, "updated": 7}]
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    store = MemoryStore(path)
    assert path.read_text(encoding="utf-8") == before
    old = store.all()[0]
    assert old["id"] == "old-1"
    assert old["workspace_id"] != "project-a"
    assert old["workspace_id"].startswith("sha256:")
    assert old["schema_version"] == 2
    assert old["use_count"] == 2

    store.add("新的偏好", source_ids=["event-2"])
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(on_disk, list)
    assert all(record["schema_version"] == 2 for record in on_disk)
    assert all("fingerprint" in record and "workspace_id" in record for record in on_disk)


def test_workspace_isolation_and_explicit_global_memory(tmp_path):
    path = tmp_path / "memory.json"
    store = MemoryStore(path, workspace_id="project-a")
    store.add("A 的回归表偏好", kind=KIND_PREFERENCE)
    store.add("所有项目使用中文", scope="global")

    assert [item["text"] for item in store.search("回归表", workspace_id="project-a")] == ["A 的回归表偏好"]
    assert store.search("回归表", workspace_id="project-b") == []
    assert store.search("中文", workspace_id="project-b") == []
    assert store.search("中文", workspace_id="project-b", include_global=True)[0]["scope"] == "global"
    assert store.search("", workspace_id="project-b") == []


def test_duplicate_supersede_retract_and_provenance(tmp_path):
    store = MemoryStore(tmp_path / "memory.json", workspace_id="project-a")
    first = store.add("使用双向固定效应", kind=KIND_CONSTRAINT, source_ids=["approval-1"])
    duplicate = store.add(" 使用双向固定效应 ", kind=KIND_CONSTRAINT, source_ids=["approval-2"])
    assert duplicate["id"] == first["id"]
    assert len(store.all()) == 1

    replacement = store.supersede(first["id"], "使用行业和年份固定效应", source_ids="approval-3")
    assert store.search("双向") == []
    assert store.search("行业 年份")[0]["id"] == replacement["id"]
    assert store.all()[0]["status"] == "superseded"

    tombstone = store.retract(replacement["id"], provenance={"event_id": "retract-1"})
    assert tombstone is not None
    assert tombstone["status"] == "retracted"
    assert store.search("行业 年份") == []
    assert {"approval-3", "retract-1"}.issubset(tombstone["source_ids"])


def test_candidates_require_acceptance(tmp_path):
    store = MemoryStore(tmp_path / "memory.json", workspace_id="project-a")
    candidate = store.add("建议以后默认使用聚类稳健标准误", candidate=True)
    assert candidate["status"] == "candidate"
    assert store.search("聚类") == []
    assert store.all(include_candidates=True)[0]["status"] == "candidate"

    accepted = store.accept_candidate(candidate["id"], provenance="approval-1")
    assert accepted is not None
    assert accepted["status"] == "active"
    assert store.search("聚类")[0]["id"] == accepted["id"]


@pytest.mark.parametrize(
    "text",
    [
        "api_key=sk-test-secret-value",
        "本次回归 p=0.03",
        "claim-123 显示处理效应显著",
        "https://example.com/result",
        "   ",
    ],
)
def test_unsafe_or_evidence_candidates_are_rejected(tmp_path, text):
    store = MemoryStore(tmp_path / "memory.json")
    with pytest.raises(MemoryCandidateRejected):
        store.add(text)
    assert store.all() == []


def test_context_selection_is_bounded_and_touches_only_selected(tmp_path):
    store = MemoryStore(tmp_path / "memory.json", workspace_id="project-a")
    first = store.add("固定效应 选择规则", source_ids=["decision-1"])
    second = store.add("固定效应 备选规则", source_ids=["decision-2"])
    third = store.add("完全不同的内容", source_ids=["decision-3"])

    selected = store.select_for_context("固定效应", token_cap=20, limit=1, now=100)
    assert len(selected) == 1
    assert selected[0]["id"] in {first["id"], second["id"]}
    assert len(selected.text) <= 80
    assert "not evidence" in selected.text
    usage = {entry["id"]: entry["use_count"] for entry in store.all()}
    assert usage[selected[0]["id"]] == 1
    assert usage[third["id"]] == 0


def test_query_relevance_beats_recency_and_prune_keeps_explicit(tmp_path):
    store = MemoryStore(tmp_path / "memory.json", workspace_id="project-a")
    relevant = store.add("固定效应必须按行业聚类", confidence="inferred", now=1)
    recent_irrelevant = store.add("输出使用宽表", confidence="inferred", now=99)
    explicit = store.add("始终保留回归表标题", confidence="explicit", now=1)

    assert store.search("固定效应", now=100)[0]["id"] == relevant["id"]
    assert store.prune(now=100, max_age_seconds=50) == 1
    remaining = {entry["id"] for entry in store.all()}
    assert relevant["id"] not in remaining
    assert recent_irrelevant["id"] in remaining
    assert explicit["id"] in remaining


def test_consolidate_legacy_duplicates_marks_tombstone(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text(
        json.dumps(
            [
                {"id": "a", "text": "同一偏好", "kind": "preference", "updated": 1},
                {"id": "b", "text": "同一偏好", "kind": "preference", "updated": 2},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = MemoryStore(path)
    assert store.consolidate() == 1
    assert len(store.search("同一偏好")) == 1
    assert sum(entry["status"] == "superseded" for entry in store.all()) == 1
