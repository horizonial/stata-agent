"""skills/loader 扩展单测：frontmatter 解析错误路径 / 边界 / matching。"""

from __future__ import annotations

import pathlib

import pytest

from stata_agent.skills.loader import (
    MAX_SKILL_BYTES,
    SkillLoadError,
    load_skill,
    load_skill_dir,
    match_skills,
)

WRITE = lambda p, s: pathlib.Path(p).write_text(s, encoding="utf-8")  # noqa: E731

GOOD = """---
name: demo-skill
description: Demo skill for tests
triggers: [did, 回归]
allowed_tools: [run_stata, read_artifact]
ados:
  - reghdfe
---
# Demo

How to do things.
"""


def _write(tmp_path, content=GOOD, name="demo-skill.md"):
    p = tmp_path / name
    WRITE(p, content)
    return p


def test_load_ok(tmp_path):
    sk = load_skill(_write(tmp_path))
    assert sk.slug == "demo-skill"
    assert sk.description == "Demo skill for tests"
    assert sk.allowed_tools == ["run_stata", "read_artifact"]
    assert sk.requires_ados == ["reghdfe"]
    assert "How to do things" in sk.body


def test_missing_opening_delimiter(tmp_path):
    p = _write(tmp_path, content="name: x\ndescription: y\n---\nbody")
    with pytest.raises(SkillLoadError):
        load_skill(p)


def test_missing_closing_delimiter(tmp_path):
    p = _write(tmp_path, content="---\nname: x\ndescription: y\nbody")
    with pytest.raises(SkillLoadError):
        load_skill(p)


def test_missing_name(tmp_path):
    p = _write(tmp_path, content="---\ndescription: y\n---\nbody")
    with pytest.raises(SkillLoadError):
        load_skill(p)


def test_bad_key(tmp_path):
    p = _write(tmp_path, content="---\nname: ok\ndescription: y\nbad key!: v\n---\nbody")
    with pytest.raises(SkillLoadError):
        load_skill(p)


def test_malformed_line(tmp_path):
    p = _write(tmp_path, content="---\nname: ok\ndescription: y\nnotacolon\n---\nbody")
    with pytest.raises(SkillLoadError):
        load_skill(p)


def test_file_too_big(tmp_path):
    p = _write(tmp_path, content="---\nname: big\ndescription: y\n---\n" + "x" * 5000)
    with pytest.raises(SkillLoadError):
        load_skill(p, max_bytes=1024)


def test_directory_loads_skill_md(tmp_path):
    d = tmp_path / "sk"
    d.mkdir()
    WRITE(d / "SKILL.md", GOOD)
    sk = load_skill(d)
    assert sk.slug == "demo-skill"


def test_empty_list_fields_are_lists(tmp_path):
    p = _write(tmp_path, content="""---
name: x
description: y
triggers:
allowed_tools:
ados:
---
body
""")
    sk = load_skill(p)
    assert sk.triggers == [] and sk.allowed_tools == [] and sk.requires_ados == []


def test_list_of_mappings_flattened(tmp_path):
    # 前端 YAML 里 `- key: value` 的 triggers → _list_field 有意拍平成可搜索 term
    p = _write(tmp_path, content="""---
name: x
description: y
triggers:
  - phase: ESTIMATION
  - needs: did
---
body
""")
    sk = load_skill(p)
    assert "ESTIMATION" in sk.triggers and "did" in sk.triggers


def test_multiline_value(tmp_path):
    p = _write(tmp_path, content="""---
name: x
description: |
  line one
  line two
---
body
""")
    sk = load_skill(p)
    assert "line one" in sk.description and "line two" in sk.description


def test_load_skill_dir_and_match(tmp_path):
    _write(tmp_path)
    skills = load_skill_dir(tmp_path)
    assert "demo-skill" in skills
    # 命中 trigger（大小写不敏感 + 中文词）
    assert [s.slug for s in match_skills(skills, "run a DID regression")] == ["demo-skill"]
    assert [s.slug for s in match_skills(skills, "做个回归分析")] == ["demo-skill"]
    # 无关话题不命中（不注入全目录）
    assert match_skills(skills, "帮我写首诗") == []
