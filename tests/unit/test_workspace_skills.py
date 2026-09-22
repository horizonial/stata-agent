"""Bundled and Workspace main Skill resolution is deterministic and data-only."""

import json
from pathlib import Path

import pytest

from stata_research_agent.interfaces.workspace_skills import (
    FilesystemMainSkillCatalog,
    MainSkillLoadError,
)


def test_bundled_skill_source_metadata_matches_frontmatter() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    catalog = FilesystemMainSkillCatalog(repository_root / "skills")
    workspace = repository_root / ".nonexistent-skill-workspace"
    snapshots = {
        "empirical-research-main": catalog.resolve(workspace),
        "econ-visualization": catalog.load_specialized(workspace, "econ-visualization"),
    }
    for skill_name in ("empirical-research-main", "econ-visualization"):
        skill_root = repository_root / "skills" / skill_name
        source = json.loads((skill_root / "source.json").read_text(encoding="utf-8"))

        assert source["name"] == skill_name
        assert snapshots[skill_name].revision.startswith(source["version"] + "+sha256.")
        assert source["executable_code_included"] is False


def _write_skill(path: Path, *, name: str, version: str, body: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        f"---\nname: {name}\nversion: {version}\ndescription: test\n---\n{body}\n",
        encoding="utf-8",
    )


def test_workspace_override_wins_and_revision_binds_exact_content(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    workspace = tmp_path / "workspace"
    _write_skill(
        bundled / "empirical-research-main" / "SKILL.md",
        name="empirical-research-main",
        version="1.0.0",
        body="bundled",
    )
    override = workspace / "skills" / "research-main" / "SKILL.md"
    _write_skill(override, name="my-research", version="2.0.0", body="workspace")

    first = FilesystemMainSkillCatalog(bundled).resolve(workspace)
    override.write_text(override.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    second = FilesystemMainSkillCatalog(bundled).resolve(workspace)

    assert first.name == "my-research"
    assert first.source_kind == "workspace_override"
    assert first.revision != second.revision
    assert first.content_sha256 != second.content_sha256


def test_bundled_default_is_used_and_escaped_override_is_rejected(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(
        bundled / "empirical-research-main" / "SKILL.md",
        name="empirical-research-main",
        version="1.0.0",
        body="bundled",
    )
    catalog = FilesystemMainSkillCatalog(bundled)
    assert catalog.resolve(workspace).source_kind == "bundled_default"

    outside = tmp_path / "outside" / "SKILL.md"
    _write_skill(outside, name="outside", version="1", body="outside")
    override = workspace / "skills" / "research-main" / "SKILL.md"
    override.parent.mkdir(parents=True)
    try:
        override.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(MainSkillLoadError, match="escaped"):
        catalog.resolve(workspace)


def test_specialized_skill_catalog_discloses_metadata_then_loads_exact_revision(
    tmp_path: Path,
) -> None:
    bundled = tmp_path / "bundled"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(
        bundled / "empirical-research-main" / "SKILL.md",
        name="empirical-research-main",
        version="1.0.0",
        body="main",
    )
    _write_skill(
        bundled / "econ-visualization" / "SKILL.md",
        name="econ-visualization",
        version="2.0.0",
        body="Use figures only when they answer a research question.",
    )
    catalog = FilesystemMainSkillCatalog(bundled)

    index = catalog.specialized_index(workspace)
    assert [(item.name, item.description, item.source_kind) for item in index] == [
        ("econ-visualization", "test", "bundled_specialized")
    ]
    loaded = catalog.load_specialized(workspace, "econ-visualization")
    assert loaded.revision == index[0].revision
    assert loaded.revision.endswith(loaded.content_sha256[:16])
    assert "answer a research question" in loaded.content


def test_workspace_specialized_skill_overrides_same_registered_name(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    workspace = tmp_path / "workspace"
    _write_skill(
        bundled / "empirical-research-main" / "SKILL.md",
        name="empirical-research-main",
        version="1.0.0",
        body="main",
    )
    _write_skill(
        bundled / "econ-visualization" / "SKILL.md",
        name="econ-visualization",
        version="1.0.0",
        body="bundled visual guidance",
    )
    _write_skill(
        workspace / "skills" / "visual-local" / "SKILL.md",
        name="econ-visualization",
        version="3.0.0",
        body="workspace visual guidance",
    )

    loaded = FilesystemMainSkillCatalog(bundled).load_specialized(workspace, "econ-visualization")
    assert loaded.source_kind == "workspace_specialized"
    assert "workspace visual guidance" in loaded.content
