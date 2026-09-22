"""Filesystem adapter that only exposes user-owned .dta files inside one Workspace."""

from __future__ import annotations

from pathlib import Path

from stata_research_agent.application.data_intake import WorkspaceDataCandidate


class FilesystemWorkspaceDataCatalog:
    PRIVATE_PARTS = frozenset({".stata-agent", ".runtime"})

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root.resolve()

    def discover(self) -> tuple[WorkspaceDataCandidate, ...]:
        candidates: list[WorkspaceDataCandidate] = []
        if not self._root.is_dir():
            return ()
        for path in self._root.rglob("*"):
            try:
                relative = path.relative_to(self._root)
                if any(part.casefold() in self.PRIVATE_PARTS for part in relative.parts):
                    continue
                resolved = path.resolve(strict=True)
                if (
                    path.is_symlink()
                    or not resolved.is_relative_to(self._root)
                    or not resolved.is_file()
                    or resolved.suffix.casefold() != ".dta"
                ):
                    continue
                observed = resolved.stat()
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                continue
            candidates.append(
                WorkspaceDataCandidate(
                    relative.as_posix(),
                    resolved.name,
                    int(observed.st_size),
                    int(observed.st_mtime_ns),
                )
            )
        return tuple(sorted(candidates, key=lambda item: item.relative_path.casefold()))

    def resolve(self, relative_path: str) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or ".." in raw.parts or not raw.parts:
            raise ValueError("Workspace Data path is unsafe")
        if any(part.casefold() in self.PRIVATE_PARTS for part in raw.parts):
            raise ValueError("private Workspace storage is not a Data intake source")
        target = (self._root / raw).resolve(strict=True)
        if (
            not target.is_relative_to(self._root)
            or not target.is_file()
            or target.suffix.casefold() != ".dta"
        ):
            raise ValueError("Workspace Data candidate is unavailable")
        return target
