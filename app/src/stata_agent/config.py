"""统一配置：从环境变量读，可选支持项目根 .env（推广给他人时给个放 key 的地方）。

优先级：已有环境变量 > .env（仅当未设置时填入）。.env 不应提交（见 .gitignore）。
"""

from __future__ import annotations

import os
from pathlib import Path

_loaded = False

COMPACTION_SUMMARY_ENV = "STATA_AGENT_COMPACTION_SUMMARY"
MEMORY_EXTRACTION_ENV = "STATA_AGENT_MEMORY_EXTRACTION"
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


# Descriptive aliases for callers that prefer configuration-oriented names.
configured_compaction_summary = compaction_summary_mode
configured_memory_extraction = memory_extraction_mode


def app_root() -> Path:
    # src/stata_agent/config.py -> parents: config? config.py 在 src/stata_agent/ 下
    return Path(__file__).resolve().parents[2]


def load_env(path: str | Path | None = None) -> Path | None:
    """把 .env 读进 os.environ（setdefault，不覆盖已有）。返回文件路径或 None。"""
    global _loaded
    if _loaded:
        return None
    _loaded = True
    env_file = Path(path) if path else Path(os.environ.get("STATA_AGENT_ENV", app_root() / ".env"))
    if not env_file.exists():
        return None
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
    return env_file
