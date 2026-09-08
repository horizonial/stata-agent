"""stata-agent —— 切片 0：无 Agent 事件内核。

依据 design/impl-plan.md 切片 0；数据契约见 design/dd-01-domain-events.md。
只做"真相源"：events 账本 + 领域 reducer + 单写者/幂等 + 最小权限与阶段定义，
不依赖 LLM / Stata / 网络。
"""

__version__ = "0.1.0"

from .config import load_env as _load_env  # noqa: E402

_load_env()  # 支持项目根 .env 放 key（优先已有环境变量）
