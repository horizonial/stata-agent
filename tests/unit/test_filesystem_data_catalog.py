from __future__ import annotations

from pathlib import Path

import pytest

from stata_research_agent.interfaces.filesystem_data_catalog import (
    FilesystemWorkspaceDataCatalog,
)


def test_catalog_discovers_only_workspace_dta_and_excludes_private_storage(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "analysis.DTA").write_bytes(b"dta")
    (tmp_path / "notes.txt").write_text("not data", encoding="utf-8")
    (tmp_path / ".stata-agent").mkdir()
    (tmp_path / ".stata-agent" / "private.dta").write_bytes(b"private")
    (tmp_path / ".runtime").mkdir()
    (tmp_path / ".runtime" / "session.dta").write_bytes(b"runtime")

    candidates = FilesystemWorkspaceDataCatalog(tmp_path).discover()

    assert tuple(item.relative_path for item in candidates) == ("nested/analysis.DTA",)
    assert candidates[0].size_bytes == 3


def test_catalog_rejects_traversal_private_and_non_dta_paths(tmp_path: Path) -> None:
    (tmp_path / "data.dta").write_bytes(b"dta")
    (tmp_path / "notes.txt").write_text("no", encoding="utf-8")
    (tmp_path / ".stata-agent").mkdir()
    (tmp_path / ".stata-agent" / "private.dta").write_bytes(b"private")
    catalog = FilesystemWorkspaceDataCatalog(tmp_path)

    assert catalog.resolve("data.dta") == (tmp_path / "data.dta").resolve()
    for invalid in ("../outside.dta", ".stata-agent/private.dta", "notes.txt"):
        with pytest.raises(ValueError):
            catalog.resolve(invalid)
