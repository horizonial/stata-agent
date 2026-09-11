"""Focused tests for the revisioned non-secret settings repository."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from stata_agent.settings.local_repository import (
    LocalSettingsRepository,
    SETTINGS_SCHEMA,
    SettingsDocumentError,
    SettingsRepositoryError,
    SettingsRevisionConflict,
)


def test_missing_repository_is_safe_and_healthy(tmp_path: Path):
    repo = LocalSettingsRepository(tmp_path / "settings.json")

    document = repo.load()

    assert document.revision == 0
    assert document.values == {}
    assert repo.health.ok
    assert repo.health.code == "missing"


def test_compare_and_write_round_trip_and_revision(tmp_path: Path):
    path = tmp_path / "settings.json"
    repo = LocalSettingsRepository(path, clock=lambda: 123)

    saved = repo.compare_and_write(
        0,
        {
            "privacy.mode": "approved_remote",
            "agent.interactive_max_steps": 24,
            "agent.goal_max_steps": 80,
            "agent.max_tool_calls": 160,
        },
    )
    loaded = LocalSettingsRepository(path).load()

    assert saved.revision == 1
    assert saved.updated_at == 123
    assert loaded == saved
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": SETTINGS_SCHEMA,
        "revision": 1,
        "updated_at": 123,
        "values": {
            "agent.goal_max_steps": 80,
            "agent.interactive_max_steps": 24,
            "agent.max_tool_calls": 160,
            "privacy.mode": "approved_remote",
        },
    }


def test_stale_revision_does_not_overwrite(tmp_path: Path):
    repo = LocalSettingsRepository(tmp_path / "settings.json")
    repo.compare_and_write(0, {"privacy.mode": "local_strict"})

    with pytest.raises(SettingsRevisionConflict) as error:
        repo.compare_and_write(0, {"privacy.mode": "approved_remote"})

    assert error.value.code == "settings_revision_conflict"
    assert error.value.expected == 0
    assert error.value.actual == 1
    assert repo.load().values == {"privacy.mode": "local_strict"}


def test_concurrent_same_revision_has_one_winner(tmp_path: Path):
    path = tmp_path / "settings.json"

    def write(value: str):
        try:
            return LocalSettingsRepository(path).compare_and_write(0, {"privacy.mode": value})
        except Exception as error:  # collect both winner and expected loser
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("approved_remote", "mixed_sanitized")))

    successes = [item for item in results if not isinstance(item, Exception)]
    conflicts = [item for item in results if isinstance(item, SettingsRevisionConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert LocalSettingsRepository(path).load().revision == 1


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"{not json", "settings_corrupt"),
        (
            json.dumps(
                {"schema": "stata-agent.settings.v2", "revision": 4, "updated_at": 1, "values": {}}
            ).encode(),
            "settings_future_schema",
        ),
        (
            json.dumps(
                {
                    "schema": SETTINGS_SCHEMA,
                    "revision": 1,
                    "updated_at": 1,
                    "values": {"unknown.key": True},
                }
            ).encode(),
            "settings_invalid_document",
        ),
    ],
)
def test_invalid_document_is_preserved_and_fails_closed(tmp_path: Path, payload: bytes, code: str):
    path = tmp_path / "settings.json"
    path.write_bytes(payload)
    before = path.read_bytes()
    repo = LocalSettingsRepository(path)

    document = repo.load()

    assert document.revision == 0
    assert document.values == {}
    assert repo.health.code == code
    assert path.read_bytes() == before
    with pytest.raises(SettingsRepositoryError):
        repo.compare_and_write(0, {"privacy.mode": "local_strict"})
    assert path.read_bytes() == before


def test_oversized_document_is_preserved(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"x" * 129)
    repo = LocalSettingsRepository(path, max_bytes=128)

    assert repo.load().values == {}
    assert repo.health.code == "settings_oversized"
    assert path.read_bytes() == b"x" * 129


def test_secret_and_unknown_keys_cannot_be_persisted(tmp_path: Path):
    repo = LocalSettingsRepository(tmp_path / "settings.json")

    with pytest.raises(SettingsDocumentError):
        repo.compare_and_write(0, {"provider.deepseek.api_key": "secret-marker"})
    with pytest.raises(SettingsDocumentError):
        repo.compare_and_write(0, {"unknown.key": "value"})
    assert not repo.path.exists()


def test_failed_atomic_replace_keeps_previous_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "settings.json"
    repo = LocalSettingsRepository(path)
    repo.compare_and_write(0, {"privacy.mode": "local_strict"})
    before = path.read_bytes()

    def fail(_: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repo, "_atomic_replace", fail)
    with pytest.raises(SettingsRepositoryError):
        repo.compare_and_write(1, {"privacy.mode": "approved_remote"})

    assert path.read_bytes() == before
    assert LocalSettingsRepository(path).load().values == {"privacy.mode": "local_strict"}
