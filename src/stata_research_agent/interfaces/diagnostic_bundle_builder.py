"""Local, fail-closed Diagnostic Bundle builder with frozen source boundaries."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from stata_research_agent.application.diagnostic_bundle import (
    DiagnosticBundleMode,
    DiagnosticBundleOutcome,
    DiagnosticBundleRequest,
)
from stata_research_agent.application.diagnostics import utc_now
from stata_research_agent.application.ports.diagnostics import (
    DiagnosticSink,
    DiagnosticWorkspaceProjection,
)
from stata_research_agent.application.sensitive_output import SensitiveOutputGate

_SYSTEM_PROFILE_KEYS = {
    "release_id",
    "build_id",
    "windows_build",
    "cpu_arch",
    "browser_capability",
    "stata_version",
    "stata_edition",
    "stata_arch",
    "supported_profile",
    "settings_flags",
    "resource_limits",
}
_ID_KEY_EXCEPTIONS = {"release_id", "build_id"}
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class DiagnosticBundleBuilder:
    def __init__(
        self,
        sink: DiagnosticSink,
        gate: SensitiveOutputGate,
        staging_root: Path,
        *,
        system_profile: Mapping[str, Any],
        workspace_projection: DiagnosticWorkspaceProjection | None = None,
    ) -> None:
        unknown = set(system_profile) - _SYSTEM_PROFILE_KEYS
        if unknown:
            raise ValueError("Diagnostic system profile contains unregistered fields")
        self._validate_system_profile(system_profile)
        self._sink = sink
        self._gate = gate
        self._staging_root = staging_root.resolve()
        self._staging_root.mkdir(parents=True, exist_ok=True)
        self._system_profile = dict(system_profile)
        self._workspace = workspace_projection

    def create_request(
        self,
        mode: DiagnosticBundleMode,
        *,
        turn_id: str | None = None,
        operation_id: str | None = None,
        requesting_user_action: bool,
    ) -> DiagnosticBundleRequest:
        if not requesting_user_action:
            raise ValueError("Diagnostic Bundle requires an explicit user action")
        workspace_revision = None
        if mode is DiagnosticBundleMode.SCOPED_WORKSPACE:
            if self._workspace is None:
                raise ValueError("SCOPED_WORKSPACE requires a Workspace projection")
            workspace_revision = self._workspace.current_revision()
        elif turn_id is not None or operation_id is not None:
            raise ValueError("SYSTEM_ONLY cannot select Workspace identities")
        health = self._sink.health()
        return DiagnosticBundleRequest(
            f"bundlerequest_{secrets.token_hex(16)}",
            mode,
            utc_now(),
            self._sink.snapshot_end(),
            workspace_revision,
            turn_id,
            operation_id,
            "sensitive-output-v1",
            json.dumps(
                {
                    "last_successful_write_at": health.last_successful_write_at,
                    "last_failure_code": health.last_failure_code,
                    "degraded_since": health.degraded_since,
                    "sink_generation": health.sink_generation,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            json.dumps(
                [dict(capsule) for capsule in self._sink.read_crash_capsules()],
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            secrets.token_bytes(32),
            True,
        )

    def preview(self, request: DiagnosticBundleRequest) -> Mapping[str, Any]:
        categories = [
            "release_and_system_profile",
            "structured_operational_diagnostics",
            "diagnostics_health",
            "safe_crash_capsules",
        ]
        if request.mode is DiagnosticBundleMode.SCOPED_WORKSPACE:
            categories.append("allowlisted_workspace_structure")
        return {
            "bundle_request_id": request.bundle_request_id,
            "mode": request.mode.value,
            "diagnostic_snapshot_end": request.diagnostic_snapshot_end,
            "requested_workspace_revision": request.requested_workspace_revision,
            "included_categories": categories,
            "excluded_categories": [
                "workspace_sqlite",
                "research_artifact_payloads",
                "messages_prompts_model_text",
                "tool_raw_output",
                "credentials_and_capabilities",
                "license_identity",
                "process_memory_dump",
            ],
            "integrity_claim": "member consistency only",
            "authenticity_claim": "not provided",
        }

    def build(
        self,
        request: DiagnosticBundleRequest,
        output_path: Path,
        *,
        protected_values: tuple[str, ...] = (),
    ) -> DiagnosticBundleOutcome:
        if not request.requesting_user_action:
            raise ValueError("Diagnostic Bundle request lacks explicit user action")
        output = output_path.absolute()
        if output.suffix.lower() != ".zip" or output.exists() or not output.parent.is_dir():
            raise ValueError("Diagnostic Bundle target must be a new ZIP in an existing folder")
        staging = Path(tempfile.mkdtemp(prefix="diagnostic-bundle-", dir=self._staging_root))
        temporary_zip = staging / "bundle.partial.zip"
        try:
            members = self._members(request)
            scanned: dict[str, bytes] = {}
            for name, payload in members.items():
                self._validate_member_name(name)
                result = self._gate.assert_clean_bytes(
                    f"diagnostic.bundle.member:{name}",
                    payload,
                    protected_values=protected_values,
                )
                if result.verdict != "safe":
                    raise ValueError("Diagnostic Bundle member failed Sensitive Output Gate")
                scanned[name] = payload

            redaction_report = self._json_bytes(
                {
                    "schema_version": "diagnostic-redaction-report/1.0",
                    "policy_version": request.redaction_policy_version,
                    "finding_counts": {},
                    "verdict": "safe",
                }
            )
            scanned["redaction-report.json"] = redaction_report
            manifest = self._manifest(request, scanned)
            scanned["bundle-manifest.json"] = self._json_bytes(manifest)
            with zipfile.ZipFile(temporary_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in sorted(scanned):
                    archive.writestr(name, scanned[name])
            final_scan = self._gate.assert_clean_archive_bytes(
                "diagnostic.bundle.final_zip",
                temporary_zip.read_bytes(),
                protected_values=protected_values,
            )
            if final_scan.verdict != "safe":
                raise ValueError("Diagnostic Bundle final ZIP failed recursive scan")
            os.replace(temporary_zip, output)
            size = output.stat().st_size
            digest = self._sha256(output)
            return DiagnosticBundleOutcome(
                str(manifest["bundle_id"]),
                output,
                size,
                digest,
                len(scanned),
                request.diagnostic_snapshot_end,
                request.requested_workspace_revision,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def verify_integrity(path: Path) -> bool:
        """Verify unsigned member consistency without claiming producer authenticity."""

        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if len({name.casefold() for name in names}) != len(names):
                    return False
                manifest = json.loads(archive.read("bundle-manifest.json"))
                declared = manifest["members"]
                declared_names = {str(item["path"]) for item in declared}
                if set(names) != declared_names | {"bundle-manifest.json"}:
                    return False
                for item in declared:
                    payload = archive.read(str(item["path"]))
                    if len(payload) != int(item["size_bytes"]):
                        return False
                    if hashlib.sha256(payload).hexdigest() != str(item["sha256"]):
                        return False
                return bool(manifest.get("authenticity") == "not provided; bundle is unsigned")
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
            return False

    def _members(self, request: DiagnosticBundleRequest) -> dict[str, bytes]:
        events = [
            self._pseudonymize(event.to_payload(), request.bundle_salt)
            for event in self._sink.read_through(request.diagnostic_snapshot_end)
        ]
        health = json.loads(request.health_snapshot_json)
        crash_capsules = json.loads(request.crash_capsules_snapshot_json)
        members = {
            "README.txt": (
                b"Stata Research Agent diagnostic bundle. Non-authoritative; "
                b"integrity does not imply producer authenticity.\n"
            ),
            "system-profile.json": self._json_bytes(self._system_profile),
            "diagnostics/events.json": self._json_bytes(events),
            "diagnostics/health.json": self._json_bytes(
                {
                    "last_successful_write_at": health["last_successful_write_at"],
                    "last_failure_code": health["last_failure_code"],
                    "degraded_since": health["degraded_since"],
                    "sink_generation": health["sink_generation"],
                }
            ),
            "diagnostics/safe-crash-capsules.json": self._json_bytes(
                [
                    self._pseudonymize(dict(capsule), request.bundle_salt)
                    for capsule in crash_capsules
                ]
            ),
        }
        if request.mode is DiagnosticBundleMode.SCOPED_WORKSPACE:
            if self._workspace is None or request.requested_workspace_revision is None:
                raise ValueError("SCOPED_WORKSPACE request is incomplete")
            projection = self._workspace.safe_projection(
                requested_revision=request.requested_workspace_revision,
                turn_id=request.turn_id,
                operation_id=request.operation_id,
            )
            members["workspace/safe-structure.json"] = self._json_bytes(
                self._pseudonymize(dict(projection), request.bundle_salt)
            )
        return members

    def _manifest(
        self, request: DiagnosticBundleRequest, members: Mapping[str, bytes]
    ) -> Mapping[str, Any]:
        return {
            "schema_version": "diagnostic-bundle-manifest/1.0",
            "bundle_id": f"bundle_{secrets.token_hex(16)}",
            "bundle_request_id": request.bundle_request_id,
            "mode": request.mode.value,
            "requested_at": request.requested_at,
            "requested_workspace_revision": request.requested_workspace_revision,
            "diagnostic_snapshot_end": request.diagnostic_snapshot_end,
            "redaction_policy_version": request.redaction_policy_version,
            "integrity": "sha256 member consistency",
            "authenticity": "not provided; bundle is unsigned",
            "omitted": self.preview(request)["excluded_categories"],
            "known_limitations": [
                "Diagnostics are non-authoritative and may be incomplete.",
                "Use Product Trace/Evidence for operation or research provenance.",
            ],
            "members": [
                {
                    "path": name,
                    "media_type": ("text/plain" if name.endswith(".txt") else "application/json"),
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "producer": "diagnostic_bundle_builder",
                    "content_classification": "safe_diagnostic_projection",
                }
                for name, payload in sorted(members.items())
            ],
        }

    @staticmethod
    def _pseudonymize(value: Any, salt: bytes, key: str = "") -> Any:
        if isinstance(value, dict):
            return {
                item_key: DiagnosticBundleBuilder._pseudonymize(item_value, salt, item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [DiagnosticBundleBuilder._pseudonymize(item, salt, key) for item in value]
        if (
            isinstance(value, str)
            and key not in _ID_KEY_EXCEPTIONS
            and (key.endswith("_id") or key.endswith("_ref"))
        ):
            digest = hmac.new(salt, f"{key}:{value}".encode(), hashlib.sha256).hexdigest()
            return f"pseudo_{digest[:24]}"
        return value

    @staticmethod
    def _validate_member_name(name: str) -> None:
        normalized = name.replace("\\", "/")
        parts = tuple(part for part in normalized.split("/") if part)
        if not parts or normalized.startswith("/") or ":" in parts[0] or ".." in parts:
            raise ValueError("Diagnostic Bundle member name is unsafe")

    @staticmethod
    def _validate_system_profile(profile: Mapping[str, Any]) -> None:
        def validate(value: Any) -> None:
            if isinstance(value, (bool, int)):
                return
            if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value):
                return
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if not isinstance(key, str) or _SAFE_TOKEN.fullmatch(key) is None:
                        raise ValueError("Diagnostic system profile key is unsafe")
                    validate(item)
                return
            raise ValueError("Diagnostic system profile value is not safe metadata")

        for item in profile.values():
            validate(item)

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return (
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
