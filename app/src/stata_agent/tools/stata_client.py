"""stata-mcp 的 stdio 客户端（切片 3）。每次调用起一次连接（先求通，性能后优化）。

对齐 DESIGN：agent 不碰 pystata，只经 MCP 协议拿 结构化结果 + provenance。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..harness.cancellation import cancellation_reason, is_cancel_requested

# 默认指向本机 stata-mcp 仓库（可用 env 覆盖）
STATA_MCP_DIR = Path(os.environ.get("STATA_MCP_DIR", r"C:\Users\user\stata-mcp"))
_STATA_PY = STATA_MCP_DIR / ".venv" / "Scripts" / "python.exe"


def server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=str(_STATA_PY),
        args=["-m", "stata_mcp.server"],
        cwd=str(STATA_MCP_DIR),
    )


@dataclass
class CallResult:
    text: str = ""
    is_error: bool = False
    structured: dict = field(default_factory=dict)

    @property
    def rc(self) -> int | None:
        # 尽力从 envelope/文本里取 rc
        if "rc" in self.structured:
            try:
                return int(self.structured["rc"])
            except (TypeError, ValueError):
                return None
        return None


class StataCancelledError(RuntimeError):
    """Cancellation outcome from a Stata transport.

    ``uncertain`` is true once ``stata_run`` was submitted.  Closing the
    stdio transport is best effort; it cannot prove that an already-running
    external command had no side effect.
    """

    cancelled = True

    def __init__(self, reason: str = "cancelled", *, uncertain: bool = False) -> None:
        self.reason = str(reason or "cancelled")
        self.uncertain = bool(uncertain)
        self.cancel_requested = True
        super().__init__(self.reason)


async def _call(
    code: str,
    tool: str = "stata_run",
    timeout: float = 180.0,
    cancellation=None,
    cancel_token=None,
) -> CallResult:
    token = cancellation or cancel_token
    if is_cancel_requested(token):
        raise StataCancelledError(cancellation_reason(token), uncertain=False)
    params = server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            task = asyncio.create_task(sess.call_tool(tool, {"code": code}))
            started = True
            deadline = monotonic() + float(timeout)
            try:
                while True:
                    if is_cancel_requested(token):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise StataCancelledError(cancellation_reason(token), uncertain=started)
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise TimeoutError(f"stata transport timeout after {timeout:g}s")
                    done, _ = await asyncio.wait({task}, timeout=min(remaining, 0.05))
                    if done:
                        res = task.result()
                        break
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
    text = ""
    for item in res.content or []:
        if getattr(item, "type", None) == "text":
            text += getattr(item, "text", "") or ""
    structured: dict = {}
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            structured = obj
    except json.JSONDecodeError:
        pass
    return CallResult(
        text=text.strip(),
        is_error=bool(getattr(res, "is_error", False)),
        structured=structured,
    )


class StataClient:
    """同步门面（harness 是同步的；内部 asyncio.run 一次调用）。"""

    def run_code(self, code: str, timeout: float = 180.0, cancellation=None, cancel_token=None) -> CallResult:
        return asyncio.run(
            _call(code, timeout=timeout, cancellation=cancellation, cancel_token=cancel_token)
        )

    async def list_tools(self) -> list[str]:
        params = server_params()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                tools = await sess.list_tools()
                return sorted(t.name for t in tools.tools)


def quick_check() -> dict:
    """连通性自检：起服务、列工具、跑一条只读命令。"""
    c = StataClient()
    tools = asyncio.run(c.list_tools())
    res = c.run_code("sysuse auto, clear")
    return {"tools": tools, "rc": res.rc, "is_error": res.is_error,
            "text_head": res.text[:160]}


class StataSession:
    """持久 stdio 会话（跨多次 tool call 保留 Stata 内存状态）。

    为何需要：server 对"一条 code 内多行语句"返回空文本；正确用法是同一会话里
    逐条单行命令执行。本类在专用线程里跑事件循环 + 常驻 stdio，暴露同步 call()。
    """

    def __init__(self, timeout: float = 180.0):
        self._timeout = timeout
        self._ready = threading.Event()
        self._err: Exception | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session = None
        self._closed_fut: asyncio.Future | None = None
        self._state_lock = threading.RLock()
        self._closing = False
        self._closed = False
        self._active: set[concurrent.futures.Future] = set()
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()

    def _main(self) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def _start() -> None:
            self._closed_fut = loop.create_future()
            if self._closing:
                return
            async with stdio_client(server_params()) as (read, write):
                async with ClientSession(read, write) as sess:
                    await sess.initialize()
                    self._session = sess
                    self._ready.set()
                    if self._closing:
                        return
                    await self._closed_fut  # 常驻直到 close()

        try:
            loop.run_until_complete(_start())
        except BaseException as e:  # noqa: BLE001 - startup/close must release waiters
            if not self._closing:
                self._err = e if isinstance(e, Exception) else RuntimeError(str(e))
            self._ready.set()
        finally:
            with self._state_lock:
                self._session = None
                self._closed = True
            if not loop.is_closed():
                loop.close()

    def _wait_ready(self, cancellation=None) -> None:
        deadline = monotonic() + 60
        while not self._ready.wait(min(0.05, max(0.0, deadline - monotonic()))):
            if is_cancel_requested(cancellation):
                self.close()
                raise StataCancelledError(cancellation_reason(cancellation), uncertain=False)
            if monotonic() >= deadline:
                self.close()
                raise TimeoutError("stata 会话启动超时")
        if self._err:
            raise self._err

    def _abort_future(self, future: concurrent.futures.Future) -> None:
        try:
            future.cancel()
        except Exception:  # noqa: BLE001 - cleanup must be repeatable
            pass
        self.close()

    def call(self, code: str, cancellation=None, cancel_token=None) -> CallResult:
        token = cancellation or cancel_token
        if is_cancel_requested(token):
            raise StataCancelledError(cancellation_reason(token), uncertain=False)
        self._wait_ready(token)
        with self._state_lock:
            if self._closing or self._closed or self._loop is None or self._session is None:
                raise RuntimeError("stata 会话已关闭")
            loop = self._loop
            session = self._session

        async def _do(session, code, timeout):
            res = await asyncio.wait_for(session.call_tool("stata_run", {"code": code}), timeout)
            text = ""
            for item in res.content or []:
                if getattr(item, "type", None) == "text":
                    text += getattr(item, "text", "") or ""
            structured: dict = {}
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    structured = obj
            except json.JSONDecodeError:
                pass
            return CallResult(text=text.strip(), is_error=bool(getattr(res, "is_error", False)),
                              structured=structured)

        fut = asyncio.run_coroutine_threadsafe(_do(session, code, self._timeout), loop)
        with self._state_lock:
            self._active.add(fut)
        deadline = monotonic() + float(self._timeout)
        try:
            while True:
                if is_cancel_requested(token):
                    self._abort_future(fut)
                    raise StataCancelledError(cancellation_reason(token), uncertain=True)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._abort_future(fut)
                    raise TimeoutError(f"stata transport timeout after {self._timeout:g}s")
                try:
                    return fut.result(timeout=min(remaining, 0.05))
                except concurrent.futures.TimeoutError:
                    # ``concurrent.futures.TimeoutError`` aliases the built-in
                    # TimeoutError on current Python.  Distinguish a polling
                    # timeout from an exception raised by the coroutine.
                    if fut.done():
                        raised = fut.exception()
                        if raised is not None:
                            self._abort_future(fut)
                            raise raised from None
                        return fut.result()
                    continue
                except concurrent.futures.CancelledError as exc:
                    if is_cancel_requested(token):
                        raise StataCancelledError(cancellation_reason(token), uncertain=True) from exc
                    raise RuntimeError("stata 调用被取消") from exc
        finally:
            with self._state_lock:
                self._active.discard(fut)

    def run_batch(self, codes: list[str], cancellation=None, cancel_token=None) -> list[CallResult]:
        """同一会话内逐条执行（每条单行），返回结果列表。"""
        token = cancellation or cancel_token
        out: list[CallResult] = []
        for code in codes:
            if code.strip():
                if is_cancel_requested(token):
                    raise StataCancelledError(cancellation_reason(token), uncertain=False)
                out.append(self.call(code, cancellation=token))
        return out

    def close(self) -> None:
        """Best-effort, idempotent shutdown of session and stdio child."""

        with self._state_lock:
            self._closing = True
            loop = self._loop
            closed_fut = self._closed_fut
            active = list(self._active)
        for future in active:
            try:
                future.cancel()
            except Exception:  # noqa: BLE001
                pass
        if loop is not None and not loop.is_closed():
            def _shutdown() -> None:
                if closed_fut is not None and not closed_fut.done():
                    closed_fut.set_result(None)

            try:
                loop.call_soon_threadsafe(_shutdown)
            except Exception:  # noqa: BLE001 - already-closing loop is harmless
                pass
        if threading.current_thread() is not self._thread:
            try:
                self._thread.join(timeout=10)
            except Exception:  # noqa: BLE001 - close remains idempotent
                pass


__all__ = ["CallResult", "StataCancelledError", "StataClient", "StataSession", "quick_check"]
