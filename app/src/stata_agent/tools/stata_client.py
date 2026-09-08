"""stata-mcp 的 stdio 客户端（切片 3）。每次调用起一次连接（先求通，性能后优化）。

对齐 DESIGN：agent 不碰 pystata，只经 MCP 协议拿 结构化结果 + provenance。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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


async def _call(code: str, tool: str = "stata_run", timeout: float = 180.0) -> CallResult:
    params = server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            res = await asyncio.wait_for(sess.call_tool(tool, {"code": code}), timeout=timeout)
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
    return CallResult(text=text.strip(), is_error=bool(getattr(res, "is_error", False)), structured=structured)


class StataClient:
    """同步门面（harness 是同步的；内部 asyncio.run 一次调用）。"""

    def run_code(self, code: str, timeout: float = 180.0) -> CallResult:
        return asyncio.run(_call(code, timeout=timeout))

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
        import threading

        self._timeout = timeout
        self._ready = threading.Event()
        self._err: Exception | None = None
        self._loop = None
        self._session = None
        self._closed_fut = None
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()

    def _main(self) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def _start() -> None:
            self._closed_fut = loop.create_future()
            async with stdio_client(server_params()) as (read, write):
                async with ClientSession(read, write) as sess:
                    await sess.initialize()
                    self._session = sess
                    self._ready.set()
                    await self._closed_fut  # 常驻直到 close()

        try:
            loop.run_until_complete(_start())
        except Exception as e:  # noqa: BLE001
            self._err = e
            self._ready.set()
        finally:
            loop.close()

    def _wait_ready(self) -> None:
        import threading

        if not self._ready.wait(60):
            raise RuntimeError("stata 会话启动超时")
        if self._err:
            raise self._err

    def call(self, code: str) -> CallResult:
        import asyncio

        self._wait_ready()

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

        fut = asyncio.run_coroutine_threadsafe(_do(self._session, code, self._timeout), self._loop)
        return fut.result()

    def run_batch(self, codes: list[str]) -> list[CallResult]:
        """同一会话内逐条执行（每条单行），返回结果列表。"""
        out: list[CallResult] = []
        for code in codes:
            if code.strip():
                out.append(self.call(code))
        return out

    def close(self) -> None:
        import asyncio

        if self._loop is not None and self._closed_fut is not None:
            async def _shutdown():
                if not self._closed_fut.done():
                    self._closed_fut.set_result(None)

            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=10)
        self._thread.join(timeout=10)
