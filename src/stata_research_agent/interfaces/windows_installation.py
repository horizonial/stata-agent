"""Per-user Windows installation and application-control layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WindowsApplicationLayout:
    install_root: Path
    launcher_root: Path
    versions_root: Path
    application_data_root: Path
    control_root: Path
    runtime_root: Path
    logs_root: Path
    recovery_root: Path

    @classmethod
    def from_local_app_data(cls, local_app_data: Path) -> WindowsApplicationLayout:
        base = local_app_data.resolve()
        if not base.is_absolute():
            raise ValueError("LOCALAPPDATA must resolve to an absolute path")
        install = base / "Programs" / "StataResearchAgent"
        data = base / "StataResearchAgent"
        return cls(
            install,
            install / "launcher",
            install / "versions",
            data,
            data / "control",
            data / "runtime",
            data / "logs",
            data / "recovery",
        )

    def prepare_application_control(self) -> None:
        for directory in (
            self.control_root,
            self.runtime_root,
            self.logs_root,
            self.recovery_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def uninstall_targets(self) -> tuple[Path, ...]:
        """Research Workspaces are intentionally absent from this closed target set."""
        return (self.launcher_root, self.versions_root)

    def assert_version_directory(self, candidate: Path) -> Path:
        resolved = candidate.resolve()
        if resolved.parent != self.versions_root.resolve():
            raise ValueError("version directory must be an immediate child of versions root")
        return resolved
