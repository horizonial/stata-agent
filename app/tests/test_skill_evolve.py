"""Skill V2 evolution: bounded candidate -> human approval -> one active file."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from stata_agent.harness.telemetry import Telemetry
from stata_agent.skills.evolve import (
    ApprovalError,
    ApprovalRecord,
    CandidateConflictError,
    EpisodeVariant,
    SkillEvolutionError,
    promote,
    skill_candidate_md,
    stage,
)
from stata_agent.skills.loader import load_skill, load_skill_dir


def _episode():
    return [
        EpisodeVariant(id="main", label="主回归", y="fte", cluster="sheet", reason="主",
                       machine={"coef": 2.81, "se": 1.29, "N": 788}),
        EpisodeVariant(id="robust_no_mgr", label="不含管理者", y="fte_nm", cluster="sheet",
                       reason="稳健", machine={"coef": 2.96, "se": 1.28}),
    ]


def _approval(text: str, *, reviewer: str = "researcher") -> ApprovalRecord:
    return ApprovalRecord(
        candidate_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        reviewer=reviewer,
        timestamp="2026-09-11T00:00:00Z",
    )


def test_candidate_md_and_stage_emits_skill_v2_manifest(tmp_path):
    md = skill_candidate_md(name="panel did wage", task="最低工资对就业",
                            episode=_episode(), stable=True, source_note="Card-Krueger")
    assert "schema_version: 2" in md
    assert "最低工资对就业" in md
    assert '"outcome":"fte"' in md
    assert '"2.81"' not in md  # raw machine result never enters a policy package

    p = stage(md, skills_dir=tmp_path)
    manifest_path = p.with_suffix(".manifest.json")
    assert p.exists() and manifest_path.exists() and "_staging" in str(p)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "stata-agent.skill-candidate.v2"
    assert manifest["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    skill = load_skill(p)
    assert skill.schema_version == 2
    assert skill.trigger_phases == ("ESTIMATION",)
    assert skill.trigger_needs == ("did",)
    assert skill.prechecks and skill.steps and skill.rules and skill.examples
    assert skill.evolution["stable"] is True
    assert skill.evolution["source"] == "Card-Krueger"
    assert skill.evolution["task"] == "最低工资对就业"


def test_candidate_requires_stable_source_and_nonempty_episode(tmp_path):
    with pytest.raises(SkillEvolutionError):
        skill_candidate_md(name="did_wage", task="t", episode=_episode(), stable=False)
    with pytest.raises(SkillEvolutionError):
        skill_candidate_md(name="did_wage", task="t", episode=[], stable=True)

    md = skill_candidate_md(name="did_wage", task="t", episode=_episode(), stable=True, source_note="source")
    assert "source: \"\"" not in md
    with pytest.raises(SkillEvolutionError, match="source"):
        stage(md.replace('source: "source"', 'source: ""'), skills_dir=tmp_path)
    with pytest.raises(SkillEvolutionError, match="stable"):
        stage(md.replace("stable: true", "stable: false"), skills_dir=tmp_path)


def test_stage_rejects_traversal_and_oversize_without_writing_outside(tmp_path):
    md = skill_candidate_md(name="did_wage", task="t", episode=_episode(), stable=True)
    with pytest.raises(SkillEvolutionError):
        stage(md, skills_dir=tmp_path, name="..\\escaped")
    with pytest.raises(SkillEvolutionError, match="size limit"):
        stage(md + "x" * (512 * 1024 + 1), skills_dir=tmp_path)
    assert not (tmp_path.parent / "escaped.md").exists()


def test_promote_requires_explicit_human_approval_and_bound_digest(tmp_path):
    md = skill_candidate_md(name="did_wage", task="t", episode=_episode(), stable=True)
    p = stage(md, skills_dir=tmp_path)
    with pytest.raises(PermissionError):
        promote(p, skills_dir=tmp_path, approved=False, approval=_approval(md))
    with pytest.raises(ApprovalError):
        promote(p, skills_dir=tmp_path, approved=True)
    with pytest.raises(ApprovalError):
        promote(p, skills_dir=tmp_path, approved=True,
                approval=ApprovalRecord("0" * 64, "researcher", "2026-09-11T00:00:00Z"))
    with pytest.raises(ApprovalError):
        promote(p, skills_dir=tmp_path, approved=True, approval=_approval(md, reviewer="agent"))


def test_promote_rechecks_hash_and_keeps_one_active_with_history(tmp_path):
    tele = Telemetry(tmp_path / "t.jsonl")
    md1 = skill_candidate_md(name="did_wage", task="task-one", episode=_episode(), stable=True)
    p1 = stage(md1, skills_dir=tmp_path)
    active1 = promote(p1, skills_dir=tmp_path, approved=True, approval=_approval(md1), telemetry=tele, idea="i1")
    assert active1.name == "did_wage_e1.md"

    md2 = skill_candidate_md(name="did_wage", task="task-two", episode=_episode(), stable=True)
    p2 = stage(md2, skills_dir=tmp_path)
    active2 = promote(p2, skills_dir=tmp_path, approved=True, approval=_approval(md2), telemetry=tele)
    assert active2.name == "did_wage_e2.md"
    assert not active1.exists()
    assert (tmp_path / "_history" / "did_wage" / active1.name).exists()
    assert sorted(item.name for item in tmp_path.glob("did_wage_e*.md")) == [active2.name]
    assert load_skill_dir(tmp_path)["did_wage"].evolution["task"] == "task-two"
    active_manifest = json.loads(active2.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert active_manifest["approval"]["reviewer"] == "researcher"
    assert tele.metrics()["by_kind"]["skill.evolved"] == 2


def test_promote_rejects_tampered_staging_and_duplicate_conflict(tmp_path):
    md = skill_candidate_md(name="did_wage", task="t", episode=_episode(), stable=True)
    p = stage(md, skills_dir=tmp_path)
    p.write_text(md + "\nchanged", encoding="utf-8")
    with pytest.raises(SkillEvolutionError, match="digest"):
        promote(p, skills_dir=tmp_path, approved=True, approval=_approval(md))

    # The same digest cannot be silently overwritten after tampering.
    with pytest.raises(CandidateConflictError):
        stage(md, skills_dir=tmp_path)


def test_promoted_skill_loads_by_loader_and_history_is_ignored(tmp_path):
    md = skill_candidate_md(name="did_wage", task="t", episode=_episode(), stable=True)
    p = stage(md, skills_dir=tmp_path)
    active = promote(p, skills_dir=tmp_path, approved=True, approval=_approval(md))
    sk = load_skill(active)
    assert sk.slug == "did_wage"
    assert sk.schema_version == 2
    assert "did" in sk.trigger_needs
    assert load_skill_dir(tmp_path)["did_wage"].path == active


def test_loader_chooses_highest_legacy_version_when_old_files_coexist(tmp_path):
    md = skill_candidate_md(name="did_wage", task="t", episode=_episode(), stable=True)
    (tmp_path / "did_wage_e1.md").write_text(md, encoding="utf-8")
    (tmp_path / "did_wage_e2.md").write_text(md.replace("task：t", "task：t2"), encoding="utf-8")
    assert load_skill_dir(tmp_path)["did_wage"].path.name == "did_wage_e2.md"
