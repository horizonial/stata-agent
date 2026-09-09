from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

from stata_agent.application.workspace_service import (
    CreateWorkspaceRequest,
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspaceRepository,
    WorkspaceService,
    WorkspaceValidationError,
)
from stata_agent.memory.sqlite_repository import SQLiteMemoryRepository


class FakeWorkspaceRepository:
    """Small duck-typed repository used to keep service tests framework-free."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def register_workspace(
        self,
        workspace_id: str,
        *,
        root: str | Path | None = None,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        current = self.rows.get(workspace_id)
        if current is None:
            timestamp = 0 if now is None else now
            current = {
                "workspace_id": workspace_id,
                "root": str(root) if root is not None else None,
                "name": name,
                "metadata": dict(metadata or {}),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            self.rows[workspace_id] = current
        else:
            if root is not None:
                current["root"] = str(root)
            if name is not None:
                current["name"] = name
            if metadata is not None:
                current["metadata"] = dict(metadata)
            current["updated_at"] = current["updated_at"] if now is None else now
        return dict(current)

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        row = self.rows.get(workspace_id)
        return dict(row) if row is not None else None

    def list_workspaces(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows.values()]


def test_repository_protocol_is_structural_and_id_name_validation_is_stable(tmp_path: Path) -> None:
    repository = FakeWorkspaceRepository()
    assert isinstance(repository, WorkspaceRepository)
    service = WorkspaceService(repository, root_base=tmp_path)

    assert service.validate_id(None) == "ui"
    assert service.validate_id("  study_1  ") == "study_1"
    with pytest.raises(WorkspaceValidationError):
        service.validate_id("../outside")
    with pytest.raises(WorkspaceValidationError):
        service.validate_id("a" * 65)

    assert service.validate_name("  a\n research  ") == "a research"
    with pytest.raises(WorkspaceValidationError):
        service.validate_name(" ")
    with pytest.raises(WorkspaceValidationError):
        service.validate_name("x" * 121)


def test_canonical_root_and_workspace_id_are_stable_for_missing_paths(tmp_path: Path) -> None:
    service = WorkspaceService(FakeWorkspaceRepository(), root_base=tmp_path)
    missing = tmp_path / "not-created" / "project" / ".." / "project"

    canonical = service.canonical_root(missing)
    assert canonical == (tmp_path / "not-created" / "project").resolve(strict=False)
    assert not canonical.exists()
    assert service.workspace_id(missing) == service.workspace_id(canonical)
    assert service.workspace_id(missing) == service.workspace_id_for_root(canonical)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive spelling is platform-specific")
def test_windows_drive_paths_are_canonicalized_without_existing_drive() -> None:
    service = WorkspaceService(FakeWorkspaceRepository(), root_base=Path.cwd())
    forward = "C:/stata-agent/nonexistent/project/../project"
    backslash = str(PureWindowsPath("C:/stata-agent/nonexistent/project/../project"))

    assert service.canonical_root(forward) == service.canonical_root(backslash)


def test_default_workspace_is_created_once_and_listed_first(tmp_path: Path) -> None:
    repository = FakeWorkspaceRepository()
    service = WorkspaceService(repository, root_base=tmp_path, clock=lambda: 10)

    first = service.list()
    second = service.list()
    assert [record.id for record in first] == ["ui"]
    assert [record.id for record in second] == ["ui"]
    assert first[0].workspace_id == service.workspace_id(tmp_path / "ui")
    assert first[0].root == (tmp_path / "ui").resolve(strict=False)
    assert len(repository.rows) == 1


def test_create_uses_canonical_root_as_idempotent_identity(tmp_path: Path) -> None:
    repository = FakeWorkspaceRepository()
    service = WorkspaceService(repository, root_base=tmp_path, clock=lambda: 20)
    root = tmp_path / "selected" / ".." / "selected"

    first = service.create(CreateWorkspaceRequest(id="study", name="最低工资", root=root, now=21))
    retry = service.create(CreateWorkspaceRequest(id="renamed", name="别的名字", root=first.root, now=99))

    assert retry == first
    assert retry.id == "study"
    assert retry.workspace_id == service.workspace_id(root)
    assert len(repository.rows) == 1


def test_explicit_id_conflict_and_generated_slug_collision(tmp_path: Path) -> None:
    repository = FakeWorkspaceRepository()
    service = WorkspaceService(repository, root_base=tmp_path, clock=lambda: 30)

    service.create(CreateWorkspaceRequest(id="study", name="First", root=tmp_path / "first"))
    with pytest.raises(WorkspaceConflictError):
        service.create(CreateWorkspaceRequest(id="study", name="Other", root=tmp_path / "second"))

    # The generated id collides, but a distinct explicit root gets the same
    # suffix semantics as the legacy UI.
    first = service.create(CreateWorkspaceRequest(name="A study", root=tmp_path / "third"))
    second = service.create(CreateWorkspaceRequest(name="A study", root=tmp_path / "fourth"))
    assert first.id == "a-study"
    assert second.id == "a-study-2"


def test_resolve_rejects_unknown_ids_and_touch_name_preserves_user_name(tmp_path: Path) -> None:
    repository = FakeWorkspaceRepository()
    service = WorkspaceService(repository, root_base=tmp_path, clock=lambda: 40)

    default = service.resolve(None)
    touched = service.touch_name(None, "  第一轮研究问题\n")
    assert touched.id == default.id == "ui"
    assert touched.name == "第一轮研究问题"
    assert service.resolve("ui").name == "第一轮研究问题"
    assert service.touch_name("ui", "后来的一句话").name == "第一轮研究问题"
    assert service.touch_name("ui", " ").name == "第一轮研究问题"

    with pytest.raises(WorkspaceNotFoundError):
        service.resolve("missing")
    with pytest.raises(WorkspaceValidationError):
        service.resolve("../outside")


def test_sqlite_memory_repository_matches_workspace_protocol(tmp_path: Path) -> None:
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    try:
        assert isinstance(repository, WorkspaceRepository)
        service = WorkspaceService(repository, root_base=tmp_path, clock=lambda: 50)
        created = service.create(
            CreateWorkspaceRequest(
                id="sqlite-project",
                name="SQLite project",
                root=tmp_path / "missing" / ".." / "project",
                metadata={"kind": "test"},
                now=51,
            )
        )
        fetched = service.get("sqlite-project")
        assert fetched == created
        assert fetched is not None
        assert fetched.metadata["kind"] == "test"
        assert fetched.metadata["id"] == "sqlite-project"
        assert [item.id for item in service.list()] == ["ui", "sqlite-project"]
    finally:
        repository.close()
