"""Filesystem verification for immutable side-by-side release bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from stata_research_agent.application.release_activation import (
    ReleaseActivationError,
    VerifiedRelease,
)


class ManifestSignatureVerifier(Protocol):
    def verify(
        self,
        manifest_bytes: bytes,
        signature_bytes: bytes,
        *,
        publisher_key_id: str,
    ) -> bool: ...


class EntryAuthenticodeVerifier(Protocol):
    def verify(self, entry_path: Path, *, publisher_key_id: str) -> bool: ...


class FilesystemReleaseBundleVerifier:
    """Require trusted manifest identity, exact payload hashes, and PE trust."""

    def __init__(
        self,
        versions_root: Path,
        signature_verifier: ManifestSignatureVerifier,
        authenticode_verifier: EntryAuthenticodeVerifier,
        *,
        launcher_version: str,
        global_control_schema: int,
    ) -> None:
        self._versions_root = versions_root.resolve()
        self._signature_verifier = signature_verifier
        self._authenticode_verifier = authenticode_verifier
        self._launcher_version = launcher_version
        self._global_control_schema = global_control_schema

    def verify(self, version_directory: str) -> VerifiedRelease:
        directory = Path(version_directory).resolve()
        if directory.parent != self._versions_root or not directory.is_dir():
            raise ReleaseActivationError("release directory is outside the versions root")
        manifest_path = directory / "release-manifest.json"
        signature_path = directory / "release-manifest.sig"
        manifest_bytes = manifest_path.read_bytes()
        signature_bytes = signature_path.read_bytes()
        try:
            value = json.loads(manifest_bytes)
        except json.JSONDecodeError as error:
            raise ReleaseActivationError("release manifest is invalid JSON") from error
        if not isinstance(value, dict):
            raise ReleaseActivationError("release manifest must be an object")
        required = {
            "manifest_schema_version",
            "release_id",
            "semantic_version",
            "build_id",
            "publisher_key_id",
            "entry_point",
            "file_manifest",
            "global_control_schema_range",
            "workspace_schema_read_range",
            "workspace_schema_write_range",
            "minimum_launcher_version",
        }
        if set(value) != required or value["manifest_schema_version"] != "1.0":
            raise ReleaseActivationError("release manifest schema is unsupported")
        publisher_key_id = self._bounded_text(value, "publisher_key_id")
        if not self._signature_verifier.verify(
            manifest_bytes,
            signature_bytes,
            publisher_key_id=publisher_key_id,
        ):
            raise ReleaseActivationError("release manifest signature is not trusted")
        release_id = self._bounded_text(value, "release_id")
        if directory.name != release_id:
            raise ReleaseActivationError("release directory does not match release identity")
        control_range = self._range(value, "global_control_schema_range")
        if not control_range[0] <= self._global_control_schema <= control_range[1]:
            raise ReleaseActivationError("release cannot read the current control schema")
        minimum_launcher = self._bounded_text(value, "minimum_launcher_version")
        if self._version_tuple(self._launcher_version) < self._version_tuple(minimum_launcher):
            raise ReleaseActivationError("stable Launcher is too old for this release")
        files = value["file_manifest"]
        if not isinstance(files, list) or not files:
            raise ReleaseActivationError("release file manifest is empty")
        declared: set[str] = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                raise ReleaseActivationError("release file record is invalid")
            relative = self._relative_path(item["path"])
            if relative in declared:
                raise ReleaseActivationError("release file path is duplicated")
            declared.add(relative)
            payload = directory / Path(*PurePosixPath(relative).parts)
            self._assert_regular_payload(directory, payload)
            size = item["size"]
            digest = item["sha256"]
            if type(size) is not int or size < 0:
                raise ReleaseActivationError("release file size is invalid")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ReleaseActivationError("release file digest is invalid")
            observed = payload.read_bytes()
            if len(observed) != size or hashlib.sha256(observed).hexdigest() != digest:
                raise ReleaseActivationError("release payload does not match signed manifest")
        actual = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path.name not in {"release-manifest.json", "release-manifest.sig"}
        }
        if actual != declared:
            raise ReleaseActivationError("release bundle contains undeclared or missing files")
        entry_point = self._relative_path(value["entry_point"])
        if entry_point not in declared:
            raise ReleaseActivationError("release entry point is not in the file manifest")
        entry_path = directory / Path(*PurePosixPath(entry_point).parts)
        if not self._authenticode_verifier.verify(entry_path, publisher_key_id=publisher_key_id):
            raise ReleaseActivationError("release entry point Authenticode is not trusted")
        read_range = self._range(value, "workspace_schema_read_range")
        write_range = self._range(value, "workspace_schema_write_range")
        return VerifiedRelease(
            release_id,
            self._bounded_text(value, "semantic_version"),
            self._bounded_text(value, "build_id"),
            publisher_key_id,
            hashlib.sha256(manifest_bytes).hexdigest(),
            str(directory),
            entry_point,
            control_range[0],
            control_range[1],
            read_range[0],
            read_range[1],
            write_range[0],
            write_range[1],
            minimum_launcher,
        )

    @staticmethod
    def _bounded_text(value: Mapping[str, Any], name: str) -> str:
        candidate = value.get(name)
        if (
            not isinstance(candidate, str)
            or not candidate
            or len(candidate) > 256
            or "\n" in candidate
            or "\r" in candidate
        ):
            raise ReleaseActivationError(f"release manifest field is invalid: {name}")
        return candidate

    @staticmethod
    def _range(value: Mapping[str, Any], name: str) -> tuple[int, int]:
        candidate = value.get(name)
        if (
            not isinstance(candidate, list)
            or len(candidate) != 2
            or any(type(item) is not int for item in candidate)
            or candidate[0] < 0
            or candidate[1] < candidate[0]
        ):
            raise ReleaseActivationError(f"release schema range is invalid: {name}")
        return int(candidate[0]), int(candidate[1])

    @staticmethod
    def _relative_path(value: object) -> str:
        if not isinstance(value, str) or not value or "\\" in value:
            raise ReleaseActivationError("release file path is invalid")
        pure = PurePosixPath(value)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ReleaseActivationError("release file path escapes the version directory")
        return pure.as_posix()

    @staticmethod
    def _assert_regular_payload(root: Path, payload: Path) -> None:
        if (
            not payload.is_file()
            or payload.is_symlink()
            or not payload.resolve().is_relative_to(root)
        ):
            raise ReleaseActivationError("release payload is missing or aliased")
        attributes = getattr(payload.stat(), "st_file_attributes", 0)
        if attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ReleaseActivationError("release payload uses a reparse point")

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, ...]:
        if re.fullmatch(r"\d+(?:\.\d+)*", value) is None:
            raise ReleaseActivationError("Launcher version is invalid")
        return tuple(int(part) for part in value.split("."))


class ReadOnlyActivationProbe:
    """Run only registered read-only checks; Workspace paths are never accepted."""

    def __init__(
        self,
        checks: tuple[Callable[[VerifiedRelease], tuple[bool, str]], ...],
    ) -> None:
        if not checks:
            raise ValueError("activation probe requires at least one read-only check")
        self._checks = checks

    def run(self, release: VerifiedRelease) -> tuple[bool, tuple[str, ...]]:
        codes: list[str] = []
        passed = True
        for check in self._checks:
            accepted, code = check(release)
            if not code or len(code) > 128 or "\n" in code or "\r" in code:
                raise ReleaseActivationError("activation probe returned an unsafe code")
            codes.append(code)
            passed = passed and accepted
        return passed, tuple(codes)
