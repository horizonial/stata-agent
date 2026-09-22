"""Typed local API for explicit Diagnostic Bundle preview and save."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import httpx

from stata_research_agent.application.diagnostic_bundle import DiagnosticBundleMode
from stata_research_agent.application.sensitive_output import SensitiveOutputGate
from stata_research_agent.domain.identifiers import WorkspaceId
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.interfaces.diagnostic_bundle_builder import (
    DiagnosticBundleBuilder,
)
from stata_research_agent.interfaces.filesystem_diagnostics import (
    FilesystemDiagnosticSink,
)
from stata_research_agent.persistence.diagnostic_projection import (
    WorkspaceDiagnosticProjection,
)


def request(app, method: str, path: str, **kwargs: object) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def command(command_id: str, workspace_id: str, command_type: str, payload: dict):
    return {
        "schema_version": "1",
        "command_id": command_id,
        "workspace_id": workspace_id,
        "command_type": command_type,
        "payload": payload,
        "preconditions": {},
    }


def test_diagnostic_bundle_requires_preview_then_saves_once_to_user_path(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(tmp_path / "host")
    sink = FilesystemDiagnosticSink(tmp_path / "app-control" / "diagnostics")
    secret = "sk-api-bundle-secret-123456789"

    def factory(workspace_id: WorkspaceId | None) -> DiagnosticBundleBuilder:
        projection = (
            None
            if workspace_id is None
            else WorkspaceDiagnosticProjection(host.database(workspace_id))
        )
        return DiagnosticBundleBuilder(
            sink,
            SensitiveOutputGate(),
            tmp_path / "app-control" / "bundle-staging",
            system_profile={
                "release_id": "release-test",
                "build_id": "build-test",
                "windows_build": "windows-11",
                "cpu_arch": "x86_64",
                "supported_profile": True,
            },
            workspace_projection=projection,
        )

    app = create_app(
        host,
        diagnostic_bundle_factory=factory,
        diagnostic_protected_values=lambda: (secret,),
    )
    assert (
        request(
            app,
            "POST",
            "/api/v1/commands",
            json=command("cmd_bundle_ws", "ws_bundle_api", "workspace.create", {}),
        ).status_code
        == 200
    )
    assert (
        request(
            app,
            "POST",
            "/api/v1/commands",
            json=command(
                "cmd_bundle_message",
                "ws_bundle_api",
                "message.submit",
                {"content": f"private research content {secret}", "execution_mode": "write"},
            ),
        ).status_code
        == 200
    )

    invalid = request(
        app,
        "POST",
        "/api/v1/diagnostic-bundles/requests",
        json={"mode": DiagnosticBundleMode.SCOPED_WORKSPACE.value},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "WORKSPACE_SCOPE_REQUIRED"

    preview = request(
        app,
        "POST",
        "/api/v1/diagnostic-bundles/requests",
        json={"mode": "scoped_workspace", "workspace_id": "ws_bundle_api"},
    )
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    assert preview.json()["authenticity_claim"] == "not provided"
    request_id = preview.json()["bundle_request_id"]
    output = tmp_path / "user-selected-bundle.zip"
    saved = request(
        app,
        "POST",
        f"/api/v1/diagnostic-bundles/{request_id}/save",
        json={"output_path": str(output)},
    )
    assert saved.status_code == 200
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json()["output_path"] == str(output.absolute())
    with zipfile.ZipFile(output) as archive:
        assert "workspace/safe-structure.json" in archive.namelist()
        assert secret.encode() not in b"".join(archive.read(name) for name in archive.namelist())

    replay = request(
        app,
        "POST",
        f"/api/v1/diagnostic-bundles/{request_id}/save",
        json={"output_path": str(tmp_path / "replay.zip")},
    )
    assert replay.status_code == 404
    assert not (tmp_path / "replay.zip").exists()
