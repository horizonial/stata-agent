"""Canonical payload manifests, coverage-gated SBOM, and release evidence."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from stata_research_agent.application.release_supply_chain import (
    CanonicalPayloadManifest,
    PayloadClassification,
    PayloadEntry,
    ReleaseComponent,
    ReleaseSupplyChainError,
)
from stata_research_agent.application.sensitive_output import SensitiveOutputGate


class CanonicalPayloadScanner:
    def scan(self, payload_root: Path, *, release_id: str) -> CanonicalPayloadManifest:
        root = payload_root.resolve()
        if not root.is_dir() or not release_id:
            raise ReleaseSupplyChainError("payload root or release identity is invalid")
        entries: list[PayloadEntry] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
            if path.is_dir():
                continue
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ReleaseSupplyChainError("payload contains an aliased or invalid member")
            attributes = getattr(path.stat(), "st_file_attributes", 0)
            if attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ReleaseSupplyChainError("payload contains a reparse point")
            relative = path.relative_to(root).as_posix()
            if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                raise ReleaseSupplyChainError("payload member escaped the release root")
            content = path.read_bytes()
            classification = self._classification(relative)
            architecture = self._architecture(content, classification)
            entries.append(
                PayloadEntry(
                    relative,
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                    classification,
                    architecture,
                )
            )
        if not entries:
            raise ReleaseSupplyChainError("payload is empty")
        canonical = json.dumps(
            [
                {
                    "architecture": item.architecture,
                    "classification": item.classification,
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size": item.size,
                }
                for item in entries
            ],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return CanonicalPayloadManifest(
            release_id,
            tuple(entries),
            hashlib.sha256(canonical).hexdigest(),
        )

    @staticmethod
    def compare(first: CanonicalPayloadManifest, second: CanonicalPayloadManifest) -> None:
        if first.release_id != second.release_id or first.entries != second.entries:
            raise ReleaseSupplyChainError("unsigned payload builds are not reproducible")
        if first.manifest_sha256 != second.manifest_sha256:
            raise ReleaseSupplyChainError("canonical payload manifest digest differs")

    @staticmethod
    def _classification(relative_path: str) -> PayloadClassification:
        lower = relative_path.lower()
        suffix = PurePosixPath(lower).suffix
        if suffix == ".exe":
            return PayloadClassification.EXECUTABLE
        if suffix in {".dll", ".pyd"}:
            return PayloadClassification.NATIVE_LIBRARY
        if lower.startswith("web/") and suffix in {".js", ".css", ".html", ".map"}:
            return PayloadClassification.FRONTEND_ASSET
        if lower.startswith("skills/"):
            return PayloadClassification.DEFAULT_SKILL
        if "python" in PurePosixPath(lower).name and suffix in {".zip", ".dll"}:
            return PayloadClassification.PYTHON_RUNTIME
        return PayloadClassification.DATA

    @staticmethod
    def _architecture(content: bytes, classification: PayloadClassification) -> str:
        if classification not in {
            PayloadClassification.EXECUTABLE,
            PayloadClassification.NATIVE_LIBRARY,
        }:
            return "not_applicable"
        if len(content) < 64 or content[:2] != b"MZ":
            raise ReleaseSupplyChainError("executable payload member is not a PE file")
        offset = struct.unpack_from("<I", content, 0x3C)[0]
        if offset + 6 > len(content) or content[offset : offset + 4] != b"PE\0\0":
            raise ReleaseSupplyChainError("executable PE header is invalid")
        machine = struct.unpack_from("<H", content, offset + 4)[0]
        architecture = {0x8664: "x86_64", 0x14C: "x86", 0xAA64: "arm64"}.get(machine)
        if architecture is None:
            raise ReleaseSupplyChainError("executable architecture is unsupported")
        return architecture


class ReleaseEvidenceBuilder:
    def __init__(self, sensitive_output_gate: SensitiveOutputGate) -> None:
        self._gate = sensitive_output_gate

    def build_runtime_sbom(
        self,
        manifest: CanonicalPayloadManifest,
        components: tuple[ReleaseComponent, ...],
    ) -> dict[str, object]:
        owners: dict[str, str] = {}
        payload_paths = {entry.relative_path for entry in manifest.entries}
        for component in components:
            for path in component.payload_paths:
                if path in owners:
                    raise ReleaseSupplyChainError("payload member has multiple SBOM owners")
                owners[path] = component.component_id
        if set(owners) != payload_paths:
            missing = sorted(payload_paths - set(owners))
            extra = sorted(set(owners) - payload_paths)
            raise ReleaseSupplyChainError(
                f"payload-to-SBOM coverage mismatch: missing={missing}, extra={extra}"
            )
        sbom: dict[str, object] = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "serialNumber": "urn:uuid:" + manifest.manifest_sha256[:32],
            "version": 1,
            "metadata": {
                "component": {
                    "type": "application",
                    "name": "stata-research-agent",
                    "version": manifest.release_id,
                }
            },
            "components": [
                {
                    "bom-ref": component.component_id,
                    "type": component.component_type,
                    "name": component.name,
                    "version": component.version,
                    "purl": component.purl,
                    "supplier": {"name": component.supplier},
                    "hashes": [{"alg": "SHA-256", "content": component.artifact_sha256}],
                    "licenses": [{"expression": component.license_expression}],
                    "properties": [
                        {"name": "sra:source", "value": component.source},
                        {"name": "sra:modified", "value": str(component.modified).lower()},
                        {
                            "name": "sra:payload_paths",
                            "value": json.dumps(sorted(component.payload_paths)),
                        },
                    ],
                }
                for component in sorted(components, key=lambda item: item.component_id)
            ],
            "properties": [
                {"name": "sra:payload_manifest_sha256", "value": manifest.manifest_sha256}
            ],
        }
        inspected = self._gate.inspect_json("release.runtime_sbom", sbom)
        if inspected.verdict != "safe":
            raise ReleaseSupplyChainError("release SBOM failed Sensitive Output Gate")
        return dict(inspected.safe_value)

    def build_build_sbom(
        self,
        *,
        release_id: str,
        build_input_sha256: str,
        components: tuple[ReleaseComponent, ...],
    ) -> dict[str, object]:
        if not release_id or len(build_input_sha256) != 64 or not components:
            raise ReleaseSupplyChainError("build SBOM inputs are incomplete")
        sbom: dict[str, object] = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "serialNumber": "urn:uuid:" + build_input_sha256[:32],
            "version": 1,
            "metadata": {
                "component": {
                    "type": "application",
                    "name": "stata-research-agent-build",
                    "version": release_id,
                }
            },
            "components": [
                {
                    "bom-ref": component.component_id,
                    "type": component.component_type,
                    "name": component.name,
                    "version": component.version,
                    "purl": component.purl,
                    "supplier": {"name": component.supplier},
                    "hashes": [{"alg": "SHA-256", "content": component.artifact_sha256}],
                    "licenses": [{"expression": component.license_expression}],
                    "properties": [
                        {"name": "sra:source", "value": component.source},
                        {"name": "sra:modified", "value": str(component.modified).lower()},
                    ],
                }
                for component in sorted(components, key=lambda item: item.component_id)
            ],
            "properties": [{"name": "sra:build_input_sha256", "value": build_input_sha256}],
        }
        inspected = self._gate.inspect_json("release.build_sbom", sbom)
        if inspected.verdict != "safe":
            raise ReleaseSupplyChainError("build SBOM failed Sensitive Output Gate")
        return dict(inspected.safe_value)

    def write_evidence_member(self, path: Path, value: dict[str, object]) -> None:
        inspected = self._gate.inspect_json("release.evidence", value)
        if inspected.verdict != "safe":
            raise ReleaseSupplyChainError("release evidence failed Sensitive Output Gate")
        encoded = json.dumps(
            inspected.safe_value,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise ReleaseSupplyChainError("release evidence is immutable")
        path.write_bytes(encoded)


def payload_manifest_json(manifest: CanonicalPayloadManifest) -> dict[str, object]:
    return {
        "release_id": manifest.release_id,
        "manifest_sha256": manifest.manifest_sha256,
        "entries": [asdict(entry) for entry in manifest.entries],
    }
