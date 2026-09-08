"""统一配置：从环境变量读，可选支持项目根 .env（推广给他人时给个放 key 的地方）。

优先级：已有环境变量 > .env（仅当未设置时填入）。.env 不应提交（见 .gitignore）。
"""

from __future__ import annotations

import os
from pathlib import Path

_loaded = False


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
