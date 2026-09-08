"""事件 schema 升级（DD-01 §3.7 upcaster）：按 (event_type, schema_version) 迁移到当前版本。

切片 0 无历史，注册表为空即恒等；实现形态先定下来。
"""

from __future__ import annotations

from typing import Callable

from .schema import Event

UpcasterFn = Callable[[Event], Event]

_registry: dict[tuple[str, int], UpcasterFn] = {}


def register(event_type: str, schema_version: int) -> Callable[[UpcasterFn], UpcasterFn]:
    def deco(fn: UpcasterFn) -> UpcasterFn:
        _registry[(event_type, schema_version)] = fn
        return fn

    return deco


CURRENT_SCHEMA_VERSION = 1


def upcast(ev: Event) -> Event:
    """把事件提升到 CURRENT_SCHEMA_VERSION；找不到迁移则要求已在当前版本。"""
    while ev.schema_version < CURRENT_SCHEMA_VERSION:
        fn = _registry.get((ev.event_type, ev.schema_version))
        if fn is None:
            raise ValueError(
                f"no upcaster for {ev.event_type}@v{ev.schema_version} (current v{CURRENT_SCHEMA_VERSION})"
            )
        ev = fn(ev)
    return ev
