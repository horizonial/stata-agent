"""Per-process typed capabilities for the authenticated loopback browser session."""

from __future__ import annotations

import hmac
import secrets
import threading
from collections.abc import Callable, Container
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class LocalSessionAuthorizationError(RuntimeError):
    """Safe, non-secret-bearing local session rejection."""


@dataclass(frozen=True, slots=True)
class BrowserBootstrapReceipt:
    nonce: str
    instance_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BrowserSessionReceipt:
    session_token: str
    instance_id: str


@dataclass(slots=True)
class _NonceState:
    expires_at: datetime
    used: bool = False


class LocalSessionAuthority:
    """Issue purpose-limited nonce/session capabilities that die with this process."""

    def __init__(
        self,
        *,
        instance_id: str,
        exact_host: str,
        launcher_secret: str | None = None,
        nonce_ttl: timedelta = timedelta(seconds=60),
        now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if not instance_id or not exact_host:
            raise ValueError("instance identity and exact host are required")
        if nonce_ttl <= timedelta(0):
            raise ValueError("bootstrap nonce TTL must be positive")
        self._instance_id = instance_id
        self._exact_host = exact_host
        self._exact_origin = f"http://{exact_host}"
        self._launcher_secret = launcher_secret or secrets.token_urlsafe(48)
        self._nonce_ttl = nonce_ttl
        self._now = now or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(48))
        self._nonces: dict[str, _NonceState] = {}
        self._sessions: set[str] = set()
        self._lock = threading.Lock()

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def exact_host(self) -> str:
        return self._exact_host

    @property
    def exact_origin(self) -> str:
        return self._exact_origin

    @property
    def launcher_secret(self) -> str:
        """Only the trusted launcher/discovery adapter may consume this value."""

        return self._launcher_secret

    def issue_bootstrap_nonce(self, launcher_capability: str) -> BrowserBootstrapReceipt:
        if not hmac.compare_digest(launcher_capability, self._launcher_secret):
            raise LocalSessionAuthorizationError("launcher capability rejected")
        with self._lock:
            nonce = self._unique_token(self._nonces)
            expires_at = self._now() + self._nonce_ttl
            self._nonces[nonce] = _NonceState(expires_at)
        return BrowserBootstrapReceipt(nonce, self._instance_id, expires_at)

    def exchange(
        self,
        nonce: str,
        *,
        host: str,
        origin: str,
        fetch_site: str,
    ) -> BrowserSessionReceipt:
        self._validate_browser_request(host=host, origin=origin, fetch_site=fetch_site)
        with self._lock:
            state = self._nonces.get(nonce)
            if state is None or state.used or self._now() >= state.expires_at:
                raise LocalSessionAuthorizationError("bootstrap nonce rejected")
            state.used = True
            session_token = self._unique_token(self._sessions)
            self._sessions.add(session_token)
        return BrowserSessionReceipt(session_token, self._instance_id)

    def authorize(self, session_token: str, *, host: str, origin: str | None) -> None:
        self.validate_host(host)
        if origin is not None and not hmac.compare_digest(origin, self._exact_origin):
            raise LocalSessionAuthorizationError("request origin rejected")
        with self._lock:
            if not any(
                hmac.compare_digest(session_token, candidate) for candidate in self._sessions
            ):
                raise LocalSessionAuthorizationError("browser session rejected")

    def authorize_mutation(
        self,
        session_token: str,
        *,
        host: str,
        origin: str,
        fetch_site: str,
    ) -> None:
        self._validate_browser_request(host=host, origin=origin, fetch_site=fetch_site)
        with self._lock:
            if not any(
                hmac.compare_digest(session_token, candidate) for candidate in self._sessions
            ):
                raise LocalSessionAuthorizationError("browser session rejected")

    def validate_host(self, host: str) -> None:
        if not hmac.compare_digest(host, self._exact_host):
            raise LocalSessionAuthorizationError("request host rejected")

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._nonces.clear()

    def _validate_browser_request(self, *, host: str, origin: str, fetch_site: str) -> None:
        if not hmac.compare_digest(host, self._exact_host):
            raise LocalSessionAuthorizationError("request host rejected")
        if not hmac.compare_digest(origin, self._exact_origin):
            raise LocalSessionAuthorizationError("request origin rejected")
        if fetch_site != "same-origin":
            raise LocalSessionAuthorizationError("request site rejected")

    def _unique_token(self, existing: Container[str]) -> str:
        while True:
            candidate = self._token_factory()
            if len(candidate) < 32:
                raise ValueError("capability token factory returned insufficient entropy")
            if candidate not in existing:
                return candidate
