"""Composition object for one authenticated, dynamically bound local service instance."""

from __future__ import annotations

import os
import secrets
import socket
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from stata_research_agent.application.local_session import LocalSessionAuthority

from .windows_runtime_control import (
    LoopbackSocketReservation,
    RuntimeDiscoveryRecord,
    WindowsRuntimeControl,
)


class SecuredLoopbackInstance:
    """Own the resources whose joint lifetime defines a browser trust domain."""

    def __init__(
        self,
        control: WindowsRuntimeControl,
        endpoint: LoopbackSocketReservation,
        authority: LocalSessionAuthority,
        discovery: RuntimeDiscoveryRecord,
    ) -> None:
        self._control = control
        self._endpoint = endpoint
        self._authority = authority
        self._discovery = discovery
        self._closed = False

    @classmethod
    def open(cls, runtime_directory: Path) -> Self:
        control = WindowsRuntimeControl(runtime_directory)
        control.acquire()
        endpoint: LoopbackSocketReservation | None = None
        try:
            endpoint = LoopbackSocketReservation()
            instance_id = f"instance_{secrets.token_hex(16)}"
            launcher_capability = secrets.token_urlsafe(48)
            discovery = RuntimeDiscoveryRecord(
                instance_id=instance_id,
                pid=os.getpid(),
                port=endpoint.port,
                started_at=datetime.now(UTC),
                launcher_capability=launcher_capability,
            )
            authority = LocalSessionAuthority(
                instance_id=instance_id,
                exact_host=discovery.exact_host,
                launcher_secret=launcher_capability,
            )
            control.publish(discovery)
            return cls(control, endpoint, authority, discovery)
        except BaseException:
            if endpoint is not None:
                endpoint.close()
            control.close()
            raise

    @property
    def local_session_authority(self) -> LocalSessionAuthority:
        return self._authority

    @property
    def discovery(self) -> RuntimeDiscoveryRecord:
        return self._discovery

    @property
    def server_socket(self) -> socket.socket:
        """Reserved socket passed directly to uvicorn.Server.serve(sockets=[...])."""

        return self._endpoint.socket

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._authority.revoke_all()
        self._endpoint.close()
        self._control.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()
