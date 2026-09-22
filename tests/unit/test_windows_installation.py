"""Per-user installation roots cannot absorb user research Workspaces."""

from pathlib import Path

import pytest

from stata_research_agent.interfaces.windows_installation import WindowsApplicationLayout


def test_per_user_layout_separates_versions_control_and_research(tmp_path: Path) -> None:
    layout = WindowsApplicationLayout.from_local_app_data(tmp_path / "LocalAppData")
    layout.prepare_application_control()
    assert (
        layout.install_root
        == (tmp_path / "LocalAppData" / "Programs" / "StataResearchAgent").resolve()
    )
    assert layout.control_root.is_dir()
    assert layout.runtime_root.is_dir()
    assert layout.uninstall_targets() == (layout.launcher_root, layout.versions_root)

    user_workspace = (tmp_path / "Research" / "PaperOne").resolve()
    assert user_workspace not in layout.uninstall_targets()
    with pytest.raises(ValueError, match="immediate child"):
        layout.assert_version_directory(user_workspace)
