"""Stable Launcher handshake with an existing authenticated loopback instance."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .windows_runtime_control import RuntimeControlError, WindowsRuntimeControl


@dataclass(frozen=True, slots=True, repr=False)
class BrowserBootstrapLocation:
    instance_id: str
    url: str

    def __repr__(self) -> str:
        return (
            "BrowserBootstrapLocation("
            f"instance_id={self.instance_id!r}, url='<one-time-bootstrap-redacted>')"
        )


class LoopbackLauncherClient:
    def __init__(
        self,
        runtime_control: WindowsRuntimeControl,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Launcher handshake timeout must be positive")
        self._runtime_control = runtime_control
        self._transport = transport
        self._timeout = timeout_seconds

    def connect_existing(self) -> BrowserBootstrapLocation | None:
        try:
            if not self._runtime_control.lock_is_held():
                return None
            discovery = self._runtime_control.read()
        except RuntimeControlError:
            return None
        endpoint = f"http://{discovery.exact_host}"
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    endpoint + "/api/v1/launcher/browser-bootstrap",
                    headers={
                        "host": discovery.exact_host,
                        "x-stata-launcher-capability": discovery.launcher_capability,
                    },
                )
                response.raise_for_status()
                value = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("instance_id") != discovery.instance_id
            or not isinstance(value.get("bootstrap_nonce"), str)
            or len(value["bootstrap_nonce"]) < 32
            or value.get("purpose") != "browser_session_exchange"
        ):
            return None
        fragment = urlencode({"bootstrap": value["bootstrap_nonce"]})
        return BrowserBootstrapLocation(discovery.instance_id, endpoint + "/#" + fragment)
