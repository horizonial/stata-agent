"""LedgerStore 契约与异常（DD-01 §3.8）。"""

from __future__ import annotations

from typing import Iterator, Protocol

from ..domain.reducers import Projection
from ..events.schema import Event


class StoreError(Exception):
    """存储层错误基类。"""


class DuplicateFingerprint(StoreError):
    """同 (idea, event_type, fingerprint) 已存在：幂等复用而非重复执行。"""


class LeaseConflict(StoreError):
    """写者租约被他人持有且未过期。"""


class StaleWrite(StoreError):
    """revision/token 过期：旧进程提交被 fence 拒绝。"""


class LedgerStore(Protocol):
    """账本存储接口。实现保证：单写者、append-only、投影可重建。"""

    def append(self, event: Event) -> int:
        """追加一条事件（单写者事务内：校验→落库→bump revision）。返回 seq。"""
        ...

    def append_many(self, events: list[Event]) -> int:
        """原子批量追加，返回末尾 seq。"""
        ...

    def scan(
        self,
        idea_id: str,
        *,
        after_seq: int = 0,
        branch: str | None = None,
        event_types: set[str] | None = None,
    ) -> Iterator[Event]:
        ...

    def project(self, idea_id: str) -> Projection:
        """scan → upcast → fold 成当前投影（派生，非第二份事实源）。"""
        ...
