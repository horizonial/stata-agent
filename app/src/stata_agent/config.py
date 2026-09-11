"""统一配置：从环境变量读，可选支持项目根 .env（推广给他人时给个放 key 的地方）。

优先级：已有环境变量 > .env（仅当未设置时填入）。.env 不应提交（见 .gitignore）。
"""

from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Mapping

_loaded = False
_dotenv_values: dict[str, str] = {}
_dotenv_injected: dict[str, str] = {}
_dotenv_path: Path | None = None

COMPACTION_SUMMARY_ENV = "STATA_AGENT_COMPACTION_SUMMARY"
MEMORY_EXTRACTION_ENV = "STATA_AGENT_MEMORY_EXTRACTION"
LIVE_PROVIDER_ENV = "STATA_AGENT_LIVE"
COMPACTION_SUMMARY_MODES = frozenset({"deterministic", "provider"})
MEMORY_EXTRACTION_MODES = frozenset({"off", "provider"})


def _configured_choice(
    name: str,
    default: str,
    allowed: frozenset[str],
    *,
    environ: dict[str, str] | None = None,
) -> str:
    """Read an optional feature switch and fail closed on invalid values."""

    env = os.environ if environ is None else environ
    value = str(env.get(name, default) or default).strip().lower()
    return value if value in allowed else default


def compaction_summary_mode(*, environ: dict[str, str] | None = None) -> str:
    """Return ``deterministic`` unless provider summaries are explicitly enabled."""

    return _configured_choice(
        COMPACTION_SUMMARY_ENV,
        "deterministic",
        COMPACTION_SUMMARY_MODES,
        environ=environ,
    )


def memory_extraction_mode(*, environ: dict[str, str] | None = None) -> str:
    """Return ``off`` unless background provider extraction is explicitly enabled."""

    return _configured_choice(
        MEMORY_EXTRACTION_ENV,
        "off",
        MEMORY_EXTRACTION_MODES,
        environ=environ,
    )


def live_provider_enabled(*, environ: dict[str, str] | None = None) -> bool:
    """Require an explicit opt-in before any live provider is selected.

    A key and an approved privacy mode are configuration prerequisites, not
    consent to make network calls.  The separate switch keeps offline tests,
    startup/health checks and default local runs network-free.
    """

    env = os.environ if environ is None else environ
    return str(env.get(LIVE_PROVIDER_ENV, "") or "").strip().lower() in {"1", "true", "yes"}


# Descriptive aliases for callers that prefer configuration-oriented names.
configured_compaction_summary = compaction_summary_mode
configured_memory_extraction = memory_extraction_mode
configured_live_provider = live_provider_enabled


def app_root() -> Path:
    # src/stata_agent/config.py -> parents: config? config.py 在 src/stata_agent/ 下
    return Path(__file__).resolve().parents[2]


def load_env(path: str | Path | None = None) -> Path | None:
    """把 .env 读进 os.environ（setdefault，不覆盖已有）。返回文件路径或 None。"""
    global _loaded, _dotenv_path
    if _loaded:
        return None
    _loaded = True
    env_file = Path(path) if path else Path(os.environ.get("STATA_AGENT_ENV", app_root() / ".env"))
    if not env_file.exists():
        return None
    _dotenv_path = env_file
    parsed = parse_dotenv(env_file)
    _dotenv_values.update(parsed)
    for key, value in parsed.items():
        # Keep the compatibility behaviour (setdefault), while recording
        # which values were actually injected.  A resolver can therefore
        # distinguish an explicit process environment value from a dotenv
        # fallback even though legacy callers still read os.environ.
        if key not in os.environ:
            os.environ[key] = value
            _dotenv_injected[key] = value
    return env_file


def parse_dotenv(path: str | Path | None) -> dict[str, str]:
    """Parse a bounded dotenv file without mutating process environment.

    The parser intentionally supports the small compatibility format used by
    this project: ``KEY=VALUE`` lines with optional surrounding quotes and
    comments/blank lines.  Invalid lines are ignored exactly as ``load_env``
    historically did.  Callers use this function for source-aware settings
    resolution; it never logs or returns anything other than the parsed
    mapping to the caller.
    """

    if path is None:
        return {}
    env_file = Path(path)
    try:
        text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    parsed: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            parsed[key] = value
    return parsed


def dotenv_values() -> dict[str, str]:
    """Return the values loaded from the compatibility dotenv source."""

    return dict(_dotenv_values)


def dotenv_path() -> Path | None:
    """Return the loaded dotenv path, if one was successfully loaded."""

    return _dotenv_path


def explicit_environment(environ: Mapping[str, object] | None = None) -> dict[str, str]:
    """Return environment values that are explicit rather than injected.

    ``load_env`` remains intentionally backwards compatible and places
    dotenv values in ``os.environ``.  This helper removes only values that are
    still byte-for-byte equal to what it injected, so a later explicit
    process override is retained and wins precedence.
    """

    source = os.environ if environ is None else environ
    result: dict[str, str] = {}
    for key, raw in source.items():
        if not isinstance(raw, str):
            continue
        if key in _dotenv_injected and raw == _dotenv_injected[key]:
            continue
        result[str(key)] = raw
    return result


def reset_dotenv_state() -> None:
    """Reset source tracking for isolated tests and embedded hosts.

    This does not remove values from ``os.environ``; callers that need a clean
    process environment should manage that separately.  Production code does
    not call this function.
    """

    global _loaded, _dotenv_path
    _loaded = False
    _dotenv_path = None
    _dotenv_values.clear()
    _dotenv_injected.clear()
