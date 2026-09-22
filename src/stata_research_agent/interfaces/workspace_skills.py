"""Filesystem-backed main Skill selection with immutable per-invocation snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

_MAX_SKILL_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class MainSkillSnapshot:
    name: str
    revision: str
    content: str
    source_kind: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class SpecializedSkillDescriptor:
    name: str
    description: str
    revision: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class SpecializedSkillSnapshot:
    name: str
    description: str
    revision: str
    content: str
    source_kind: str
    content_sha256: str


class MainSkillLoadError(RuntimeError):
    pass


class FilesystemMainSkillCatalog:
    """Resolve the always-on Main Skill and optional data-only specialized Skills."""

    def __init__(self, bundled_skills_root: Path) -> None:
        self._bundled_root = bundled_skills_root.resolve()

    def resolve(self, workspace_root: Path) -> MainSkillSnapshot:
        workspace = workspace_root.resolve()
        override = workspace / "skills" / "research-main" / "SKILL.md"
        if override.exists():
            path = override
            source_kind = "workspace_override"
        else:
            path = self._bundled_root / "empirical-research-main" / "SKILL.md"
            source_kind = "bundled_default"
        owning_root = workspace if source_kind == "workspace_override" else self._bundled_root
        return self._load(path, owning_root, source_kind)

    def specialized_index(self, workspace_root: Path) -> tuple[SpecializedSkillDescriptor, ...]:
        return tuple(
            SpecializedSkillDescriptor(
                snapshot.name,
                snapshot.description,
                snapshot.revision,
                snapshot.source_kind,
            )
            for snapshot in self._specialized_snapshots(workspace_root)
        )

    def load_specialized(self, workspace_root: Path, skill_name: str) -> SpecializedSkillSnapshot:
        normalized = skill_name.strip()
        matches = tuple(
            snapshot
            for snapshot in self._specialized_snapshots(workspace_root)
            if snapshot.name == normalized
        )
        if len(matches) != 1:
            raise MainSkillLoadError("specialized Skill is not registered")
        return matches[0]

    def _specialized_snapshots(self, workspace_root: Path) -> tuple[SpecializedSkillSnapshot, ...]:
        workspace = workspace_root.resolve()
        by_name: dict[str, SpecializedSkillSnapshot] = {}
        if self._bundled_root.is_dir():
            for folder in sorted(self._bundled_root.iterdir(), key=lambda item: item.name):
                if not folder.is_dir() or folder.name == "empirical-research-main":
                    continue
                path = folder / "SKILL.md"
                if not path.exists():
                    continue
                snapshot = self._load_specialized(path, self._bundled_root, "bundled_specialized")
                if snapshot.name in by_name:
                    raise MainSkillLoadError("duplicate bundled specialized Skill name")
                by_name[snapshot.name] = snapshot

        workspace_skills = workspace / "skills"
        if workspace_skills.is_dir():
            for folder in sorted(workspace_skills.iterdir(), key=lambda item: item.name):
                if not folder.is_dir() or folder.name == "research-main":
                    continue
                path = folder / "SKILL.md"
                if not path.exists():
                    continue
                snapshot = self._load_specialized(path, workspace, "workspace_specialized")
                by_name[snapshot.name] = snapshot
        return tuple(by_name[name] for name in sorted(by_name))

    @staticmethod
    def _load(path: Path, workspace: Path | None, source_kind: str) -> MainSkillSnapshot:
        name, version, content, digest = FilesystemMainSkillCatalog._read_skill(path, workspace)
        revision = f"{version}+sha256.{digest[:16]}"
        return MainSkillSnapshot(name, revision, content, source_kind, digest)

    @staticmethod
    def _load_specialized(
        path: Path, workspace: Path | None, source_kind: str
    ) -> SpecializedSkillSnapshot:
        name, version, content, digest = FilesystemMainSkillCatalog._read_skill(path, workspace)
        description = FilesystemMainSkillCatalog._frontmatter_value(content, "description")
        if description is None:
            raise MainSkillLoadError("specialized Skill description is required")
        revision = f"{version}+sha256.{digest[:16]}"
        return SpecializedSkillSnapshot(name, description, revision, content, source_kind, digest)

    @staticmethod
    def _read_skill(path: Path, workspace: Path | None) -> tuple[str, str, str, str]:
        try:
            is_symlink = path.is_symlink()
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise MainSkillLoadError("main Skill is unavailable") from error
        if workspace is not None and not resolved.is_relative_to(workspace):
            raise MainSkillLoadError("Skill escaped its registered root")
        if is_symlink or not resolved.is_file():
            raise MainSkillLoadError("main Skill must be a regular file")
        attributes = getattr(resolved.stat(), "st_file_attributes", 0)
        if attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise MainSkillLoadError("main Skill cannot use a reparse point")
        payload = resolved.read_bytes()
        if not payload or len(payload) > _MAX_SKILL_BYTES:
            raise MainSkillLoadError("main Skill size is outside the supported boundary")
        try:
            content = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise MainSkillLoadError("main Skill must be UTF-8") from error
        name = FilesystemMainSkillCatalog._frontmatter_value(content, "name")
        version = FilesystemMainSkillCatalog._skill_version(content)
        if name is None or version is None or content.splitlines()[0] != "---":
            raise MainSkillLoadError("main Skill frontmatter is incomplete")
        if (
            not name.replace("-", "").isalnum()
            or len(name) > 64
            or not version.replace("-", "").replace(".", "").replace("_", "").isalnum()
            or len(version) > 64
        ):
            raise MainSkillLoadError("main Skill identity is invalid")
        digest = hashlib.sha256(payload).hexdigest()
        return name, version, content, digest

    @staticmethod
    def _skill_version(content: str) -> str | None:
        legacy = FilesystemMainSkillCatalog._frontmatter_value(content, "version")
        if legacy is not None:
            return legacy
        metadata = FilesystemMainSkillCatalog._frontmatter_value(content, "metadata")
        if metadata is None:
            return None
        try:
            value = json.loads(metadata)
        except json.JSONDecodeError:
            return None
        version = value.get("version") if isinstance(value, dict) else None
        return version if isinstance(version, str) and version else None

    @staticmethod
    def _frontmatter_value(content: str, key: str) -> str | None:
        lines = content.splitlines()
        if not lines or lines[0] != "---":
            return None
        for index, line in enumerate(lines[1:], start=1):
            if line == "---":
                return None
            prefix = key + ":"
            if line.startswith(prefix):
                value = line[len(prefix) :].strip()
                if value in {">", "|-", "|"}:
                    parts: list[str] = []
                    for continuation in lines[index + 1 :]:
                        if continuation == "---" or (
                            continuation and not continuation[0].isspace()
                        ):
                            break
                        if continuation.strip():
                            parts.append(continuation.strip())
                    value = " ".join(parts)
                elif value.startswith('"') and value.endswith('"'):
                    try:
                        decoded = json.loads(value)
                    except json.JSONDecodeError:
                        return None
                    if not isinstance(decoded, str):
                        return None
                    value = decoded
                return value or None
        return None
