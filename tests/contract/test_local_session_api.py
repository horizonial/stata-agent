"""M5-01a authenticated loopback API boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from stata_research_agent.application.local_session import LocalSessionAuthority
from stata_research_agent.application.provider_credentials import (
    CredentialUnavailableError,
    ProviderCredentialService,
)
from stata_research_agent.interfaces.api import WorkspaceHost, create_app
from stata_research_agent.persistence.global_credentials import (
    GlobalCredentialDatabase,
    SqliteProviderCredentialRepository,
)

HOST = "127.0.0.1:43123"
ORIGIN = f"http://{HOST}"
LAUNCHER_SECRET = "launcher_" + "a" * 40


def send(app, method: str, path: str, **kwargs: object) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(request())


def secured_app(tmp_path: Path):
    authority = LocalSessionAuthority(
        instance_id="instance_contract",
        exact_host=HOST,
        launcher_secret=LAUNCHER_SECRET,
    )
    return create_app(WorkspaceHost(tmp_path / "host"), local_session_authority=authority)


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def write(self, target_name: str, secret: str) -> None:
        self.values[target_name] = secret

    def read(self, target_name: str) -> str:
        try:
            return self.values[target_name]
        except KeyError as error:
            raise CredentialUnavailableError("credential is unavailable") from error

    def contains(self, target_name: str) -> bool:
        return target_name in self.values

    def delete(self, target_name: str) -> None:
        self.values.pop(target_name, None)


def browser_token(app) -> str:
    nonce = send(
        app,
        "POST",
        "/api/v1/launcher/browser-bootstrap",
        headers={"host": HOST, "x-stata-launcher-capability": LAUNCHER_SECRET},
    ).json()["bootstrap_nonce"]
    return str(
        send(
            app,
            "POST",
            "/api/v1/session/exchange",
            headers=bootstrap_headers(),
            json={"bootstrap_nonce": nonce},
        ).json()["browser_session_token"]
    )


def bootstrap_headers() -> dict[str, str]:
    return {
        "host": HOST,
        "origin": ORIGIN,
        "sec-fetch-site": "same-origin",
    }


def test_launcher_nonce_exchange_and_all_research_api_require_session(tmp_path: Path) -> None:
    app = secured_app(tmp_path)
    denied = send(app, "GET", "/api/v1/workspace-attention", headers={"host": HOST})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "BROWSER_SESSION_REQUIRED"

    nonce_response = send(
        app,
        "POST",
        "/api/v1/launcher/browser-bootstrap",
        headers={"host": HOST, "x-stata-launcher-capability": LAUNCHER_SECRET},
    )
    assert nonce_response.status_code == 200
    assert nonce_response.headers["cache-control"] == "no-store"
    nonce = nonce_response.json()["bootstrap_nonce"]
    exchange = send(
        app,
        "POST",
        "/api/v1/session/exchange",
        headers=bootstrap_headers(),
        json={"bootstrap_nonce": nonce},
    )
    assert exchange.status_code == 200
    assert exchange.headers["cache-control"] == "no-store"
    token = exchange.json()["browser_session_token"]
    accepted = send(
        app,
        "GET",
        "/api/v1/workspace-attention",
        headers={**bootstrap_headers(), "x-stata-browser-session": token},
    )
    assert accepted.status_code == 200
    assert accepted.headers["content-security-policy"].startswith("default-src 'self'")
    assert accepted.headers["referrer-policy"] == "no-referrer"
    assert accepted.headers["x-content-type-options"] == "nosniff"
    assert accepted.headers["cross-origin-resource-policy"] == "same-origin"


def test_authenticated_mutation_requires_exact_origin_and_fetch_metadata(tmp_path: Path) -> None:
    app = secured_app(tmp_path)
    nonce = send(
        app,
        "POST",
        "/api/v1/launcher/browser-bootstrap",
        headers={"host": HOST, "x-stata-launcher-capability": LAUNCHER_SECRET},
    ).json()["bootstrap_nonce"]
    token = send(
        app,
        "POST",
        "/api/v1/session/exchange",
        headers=bootstrap_headers(),
        json={"bootstrap_nonce": nonce},
    ).json()["browser_session_token"]
    command = {
        "schema_version": "1",
        "command_id": "cmd_secured_workspace_create",
        "workspace_id": "ws_secured",
        "command_type": "workspace.create",
        "payload": {},
        "preconditions": {},
    }
    missing_browser_context = send(
        app,
        "POST",
        "/api/v1/commands",
        headers={"host": HOST, "x-stata-browser-session": token},
        json=command,
    )
    assert missing_browser_context.status_code == 401
    accepted = send(
        app,
        "POST",
        "/api/v1/commands",
        headers={**bootstrap_headers(), "x-stata-browser-session": token},
        json=command,
    )
    assert accepted.status_code == 200


def test_dedicated_credential_endpoint_never_echoes_or_persists_secret(tmp_path: Path) -> None:
    authority = LocalSessionAuthority(
        instance_id="instance_credential_contract",
        exact_host=HOST,
        launcher_secret=LAUNCHER_SECRET,
    )
    database_path = tmp_path / "global-control.sqlite3"
    connection = GlobalCredentialDatabase(database_path).open()
    provider_credentials = ProviderCredentialService(
        SqliteProviderCredentialRepository(connection), MemorySecretStore()
    )
    app = create_app(
        WorkspaceHost(tmp_path / "host"),
        local_session_authority=authority,
        provider_credentials=provider_credentials,
    )
    token = browser_token(app)
    headers = {**bootstrap_headers(), "x-stata-browser-session": token}
    canary = "sk-api-contract-canary-never-persist"
    created = send(
        app,
        "POST",
        "/api/v1/provider-credentials",
        headers=headers,
        json={
            "provider_kind": "deepseek",
            "endpoint": "https://api.deepseek.com/chat/completions",
            "account_label": "Primary",
            "secret": canary,
        },
    )
    assert created.status_code == 200
    assert created.headers["cache-control"] == "no-store"
    assert canary not in created.text
    receipt = created.json()
    assert set(receipt) == {
        "schema_version",
        "provider_profile_id",
        "credential_version_id",
        "status",
    }
    listed = send(
        app,
        "GET",
        "/api/v1/provider-credentials",
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json() == {
        "schema_version": "1",
        "items": [
            {
                "provider_profile_id": receipt["provider_profile_id"],
                "provider_kind": "deepseek",
                "endpoint": "https://api.deepseek.com/chat/completions",
                "account_label": "Primary",
                "status": "enabled",
                "credential_version_id": receipt["credential_version_id"],
            }
        ],
    }
    assert canary not in listed.text
    assert "credential_ref" not in listed.text
    assert "target_name" not in listed.text
    rotated = send(
        app,
        "POST",
        f"/api/v1/provider-credentials/{receipt['provider_profile_id']}/rotate",
        headers=headers,
        json={"secret": "sk-rotated-api-contract-canary"},
    )
    assert rotated.status_code == 200
    assert rotated.json()["credential_version_id"] != receipt["credential_version_id"]
    deleted = send(
        app,
        "DELETE",
        f"/api/v1/provider-credentials/{receipt['provider_profile_id']}",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    assert deleted.headers["cache-control"] == "no-store"
    assert canary not in "\n".join(connection.iterdump())
    assert canary.encode() not in database_path.read_bytes()
    connection.close()


def test_nonce_replay_wrong_origin_and_capability_cross_use_are_rejected(tmp_path: Path) -> None:
    app = secured_app(tmp_path)
    issued = send(
        app,
        "POST",
        "/api/v1/launcher/browser-bootstrap",
        headers={"host": HOST, "x-stata-launcher-capability": LAUNCHER_SECRET},
    ).json()
    nonce = issued["bootstrap_nonce"]
    wrong_origin = send(
        app,
        "POST",
        "/api/v1/session/exchange",
        headers={**bootstrap_headers(), "origin": "http://evil.invalid"},
        json={"bootstrap_nonce": nonce},
    )
    assert wrong_origin.status_code == 401
    first = send(
        app,
        "POST",
        "/api/v1/session/exchange",
        headers=bootstrap_headers(),
        json={"bootstrap_nonce": nonce},
    )
    assert first.status_code == 200
    replay = send(
        app,
        "POST",
        "/api/v1/session/exchange",
        headers=bootstrap_headers(),
        json={"bootstrap_nonce": nonce},
    )
    assert replay.status_code == 401
    cross_use = send(
        app,
        "GET",
        "/api/v1/workspace-attention",
        headers={**bootstrap_headers(), "x-stata-browser-session": LAUNCHER_SECRET},
    )
    assert cross_use.status_code == 401
