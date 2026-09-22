"""Stable Launcher requires lock, instance identity, and capability handshake."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest

from stata_research_agent.interfaces.loopback_launcher import LoopbackLauncherClient
from stata_research_agent.interfaces.windows_runtime_control import (
    RuntimeDiscoveryRecord,
    WindowsRuntimeControl,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only Launcher substrate")


def test_existing_instance_requires_held_lock_and_exact_identity(tmp_path) -> None:
    runtime = WindowsRuntimeControl(tmp_path / "runtime")
    runtime.acquire()
    record = RuntimeDiscoveryRecord(
        "instance_launcher",
        os.getpid(),
        43_456,
        datetime.now(UTC),
        "launcher_" + "x" * 48,
    )
    runtime.publish(record)
    seen_capability: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_capability.append(request.headers["x-stata-launcher-capability"])
        return httpx.Response(
            200,
            json={
                "schema_version": "1",
                "purpose": "browser_session_exchange",
                "instance_id": record.instance_id,
                "bootstrap_nonce": "nonce_" + "n" * 48,
                "expires_at": "2026-09-19T00:00:00Z",
            },
        )

    try:
        location = LoopbackLauncherClient(
            WindowsRuntimeControl(tmp_path / "runtime"),
            transport=httpx.MockTransport(handler),
        ).connect_existing()
        assert location is not None
        assert location.instance_id == record.instance_id
        assert location.url.startswith("http://127.0.0.1:43456/#bootstrap=")
        assert "bootstrap_nonce=" not in location.url
        assert record.launcher_capability not in location.url
        assert "one-time-bootstrap-redacted" in repr(location)
        assert seen_capability == [record.launcher_capability]
    finally:
        runtime.close()

    assert (
        LoopbackLauncherClient(
            WindowsRuntimeControl(tmp_path / "runtime"),
            transport=httpx.MockTransport(handler),
        ).connect_existing()
        is None
    )
