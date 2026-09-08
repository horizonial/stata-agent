"""skill 自进化：episode→staging→人工批准→promote(新文件不覆盖) + 可被引擎重载。"""

from __future__ import annotations

from pathlib import Path

import pytest

from stata_agent.harness.telemetry import Telemetry
from stata_agent.skills.evolve import (
    EpisodeVariant,
    promote,
    skill_candidate_md,
    stage,
)


def _episode():
    return [
        EpisodeVariant(id="main", label="主回归", y="fte", cluster="sheet", reason="主",
                       machine={"coef": 2.81, "se": 1.29, "N": 788}),
        EpisodeVariant(id="robust_no_mgr", label="不含管理者", y="fte_nm", cluster="sheet",
                       reason="稳健", machine={"coef": 2.96, "se": 1.28}),
    ]


def test_candidate_md_and_stage(tmp_path):
    md = skill_candidate_md(name="panel did wage", task="最低工资对就业",
                            episode=_episode(), stable=True, source_note="Card-Krueger")
    p = stage(md, skills_dir=tmp_path)
    assert p.exists() and "_staging" in str(p)
    text = p.read_text(encoding="utf-8")
    assert '"cluster": "sheet"' in text and "forbid: 多 spec 只挑显著报" in text


def test_promote_requires_approval_then_new_file(tmp_path):
    md = skill_candidate_md(name="did_wage", task="t", episode=_episode(), stable=True)
    tele = Telemetry(tmp_path / "t.jsonl")
    with pytest.raises(PermissionError):
        promote(md, skills_dir=tmp_path, approved=False)

    p1 = promote(md, skills_dir=tmp_path, approved=True, telemetry=tele, idea="i1")
    p2 = promote(md, skills_dir=tmp_path, approved=True, telemetry=tele)  # 再沉淀一次 → e2
    assert p1.name == "did_wage_e1.md"
    assert p2.name == "did_wage_e2.md"
    assert tele.metrics()["by_kind"]["skill.evolved"] == 2


def test_promoted_skill_loads_by_loader(tmp_path):
    from stata_agent.skills.loader import load_skill

    md = skill_candidate_md(name="did_wage", task="t", episode=_episode(), stable=True)
    p = promote(md, skills_dir=tmp_path, approved=True)
    sk = load_skill(p)              # 沉淀的 skill 可被 loader 加载（读元数据）
    assert sk.slug == "did_wage"
    assert "DID" in sk.body or "双重差分" in sk.body or "DID" in sk.description
