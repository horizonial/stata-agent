"""M5-01a typed in-memory local capabilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from stata_research_agent.application.local_session import (
    LocalSessionAuthority,
    LocalSessionAuthorizationError,
)


def authority(now: list[datetime]) -> LocalSessionAuthority:
    tokens = count(1)
    return LocalSessionAuthority(
        instance_id="instance_test",
        exact_host="127.0.0.1:43123",
        launcher_secret="launcher_" + "a" * 40,
        nonce_ttl=timedelta(seconds=30),
        now=lambda: now[0],
        token_factory=lambda: f"capability_{next(tokens):064d}",
    )


def test_nonce_is_single_use_purpose_bound_and_session_is_memory_only() -> None:
    now = [datetime(2026, 9, 19, tzinfo=UTC)]
    sessions = authority(now)
    bootstrap = sessions.issue_bootstrap_nonce("launcher_" + "a" * 40)
    issued = sessions.exchange(
        bootstrap.nonce,
        host="127.0.0.1:43123",
        origin="http://127.0.0.1:43123",
        fetch_site="same-origin",
    )
    sessions.authorize(
        issued.session_token,
        host="127.0.0.1:43123",
        origin="http://127.0.0.1:43123",
    )
    with pytest.raises(LocalSessionAuthorizationError, match="nonce rejected"):
        sessions.exchange(
            bootstrap.nonce,
            host="127.0.0.1:43123",
            origin="http://127.0.0.1:43123",
            fetch_site="same-origin",
        )
    sessions.revoke_all()
    with pytest.raises(LocalSessionAuthorizationError, match="session rejected"):
        sessions.authorize(
            issued.session_token,
            host="127.0.0.1:43123",
            origin=None,
        )


def test_wrong_capability_host_origin_site_and_expired_nonce_fail_closed() -> None:
    now = [datetime(2026, 9, 19, tzinfo=UTC)]
    sessions = authority(now)
    with pytest.raises(LocalSessionAuthorizationError, match="launcher"):
        sessions.issue_bootstrap_nonce("capability_" + "b" * 40)
    bootstrap = sessions.issue_bootstrap_nonce("launcher_" + "a" * 40)
    for host, origin, site in (
        ("localhost:43123", "http://127.0.0.1:43123", "same-origin"),
        ("127.0.0.1:43123", "null", "same-origin"),
        ("127.0.0.1:43123", "http://127.0.0.1:43123", "cross-site"),
    ):
        with pytest.raises(LocalSessionAuthorizationError):
            sessions.exchange(bootstrap.nonce, host=host, origin=origin, fetch_site=site)
    now[0] += timedelta(seconds=31)
    with pytest.raises(LocalSessionAuthorizationError, match="nonce rejected"):
        sessions.exchange(
            bootstrap.nonce,
            host="127.0.0.1:43123",
            origin="http://127.0.0.1:43123",
            fetch_site="same-origin",
        )
