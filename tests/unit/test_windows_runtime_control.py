"""Windows dynamic loopback and private runtime discovery substrate."""

from __future__ import annotations

import os
import socket
from datetime import UTC, datetime

import pytest

from stata_research_agent.interfaces.secured_loopback_instance import (
    SecuredLoopbackInstance,
)
from stata_research_agent.interfaces.windows_runtime_control import (
    LoopbackSocketReservation,
    RuntimeAlreadyActiveError,
    RuntimeDiscoveryRecord,
    WindowsRuntimeControl,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only release substrate")


def test_dynamic_endpoint_is_reserved_on_exact_ipv4_loopback() -> None:
    with LoopbackSocketReservation() as endpoint:
        assert endpoint.socket.getsockname() == ("127.0.0.1", endpoint.port)
        assert endpoint.port > 0


def test_private_discovery_is_locked_roundtripped_and_secret_redacted(tmp_path) -> None:
    runtime = tmp_path / "control"
    owner = WindowsRuntimeControl(runtime)
    owner.acquire()
    try:
        contender = WindowsRuntimeControl(runtime)
        with pytest.raises(RuntimeAlreadyActiveError):
            contender.acquire()
        record = RuntimeDiscoveryRecord(
            instance_id="instance_windows_control",
            pid=os.getpid(),
            port=43_123,
            started_at=datetime.now(UTC),
            launcher_capability="launcher_" + "s" * 48,
        )
        owner.publish(record)
        observed = owner.read()
        assert WindowsRuntimeControl(runtime).lock_is_held()
        assert observed == record
        assert observed.exact_host == "127.0.0.1:43123"
        assert observed.launcher_capability not in repr(observed)
        assert "<redacted>" in repr(observed)
    finally:
        owner.close()
    assert not owner.discovery_path.exists()
    assert not WindowsRuntimeControl(runtime).lock_is_held()


def test_secured_instance_publishes_the_same_capability_and_reserved_endpoint(tmp_path) -> None:
    runtime = tmp_path / "secured-instance"
    with SecuredLoopbackInstance.open(runtime) as instance:
        observed = WindowsRuntimeControl(runtime).read()
        assert observed == instance.discovery
        assert instance.local_session_authority.instance_id == observed.instance_id
        assert instance.local_session_authority.exact_host == observed.exact_host
        assert instance.local_session_authority.launcher_secret == observed.launcher_capability
        with socket.create_connection(("127.0.0.1", observed.port), timeout=1):
            pass
    assert not (runtime / "discovery.json").exists()
