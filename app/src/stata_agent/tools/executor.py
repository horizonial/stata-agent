"""StataExecutor：把一段 Stata 脚本变成不可变 Run（DD-01 执行链 + 三层结果/环境指纹）。

切片 3 最小版：
- 每次 run 自包含（load→估计→echo 机器值+env 标记）→ 单次连接内完成（stata 会话按次重建，
  故状态依赖都在脚本内，正是"从 prepared 快照重建"的精神）。
- 脚本本体写进 run 目录（do_file 可复现）；command_hash=sha256(script)。
- 机器层值靠脚本自身 echo 固定格式解析（等价于未来 validator 的 display→cell 解析）。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from ..domain.reducers import Projection
from ..events.schema import (
    EVENT_RUN_FAILED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    ACTOR_ORCH,
    Event,
)
from ..storage.sqlite_store import SQLiteStore
from .stata_client import StataSession


class MachineParseError(RuntimeError):
    pass


class TransportUncertainError(MachineParseError):
    """The Stata transport/process outcome cannot be known safely."""

    uncertain = True


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reuse_for(store: SQLiteStore, idea: str, input_hash: str) -> dict | None:
    """同输入复用（B2）：账本里已有同 semantic_input_hash 的 succeeded run → 复用其结果，
    不重跑（DD-01 幂等）。"""
    proj = store.project(idea)
    for rid, rec in proj.runs.items():
        if rec.status == "succeeded" and rec.semantic_input_hash == input_hash:
            return {
                "run_id": rid,
                "machine": dict(rec.machine or {}),
                "env": dict((rec.provenance or {}).get("env_sig") or {}),
                "do_file": (rec.provenance or {}).get("do_file"),
                "command_hash": input_hash,
                "reused": True,
            }
    return None


def _find_marker(text: str, token: str) -> str | None:
    """解析形如 `token<value>` 的一行，value 到行尾。"""
    m = re.search(rf"^{re.escape(token)}(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def _transport_error(error: BaseException | None = None, text: str = "") -> bool:
    """Classify connection/timeout failures separately from Stata rc errors."""
    if isinstance(error, (TimeoutError, ConnectionError, BrokenPipeError, EOFError, OSError)):
        return True
    haystack = f"{error or ''} {text}".lower()
    return any(token in haystack for token in (
        "timeout", "timed out", "disconnect", "connection reset", "broken pipe",
        "eof", "process exited", "session closed", "transport", "超时", "断开", "关闭",
    ))


class StataExecutor:
    def __init__(self, store: SQLiteStore, *, run_root: Path | None = None,
                 share_session: bool = False):
        self._store = store
        self._run_root = Path(run_root) if run_root else Path(store._path).parent / "runs"
        self._run_root.mkdir(parents=True, exist_ok=True)
        self._share_session = share_session
        self._session = None

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    # ------------------------------------------------------------------ 机器层
    @staticmethod
    def parse_machine(text: str) -> dict:
        """从标记提取机器层数值。非估计命令（无 e(N)，di 输出 '.'）→ 返回空 dict，
        不 raise（探索命令如 describe/list 不该被当回归失败）。"""
        b = _find_marker(text, "MACHINE_B=")
        n = _find_marker(text, "MACHINE_N=")
        r2 = _find_marker(text, "MACHINE_R2=")
        if n is None or n in (".", ""):
            return {}  # 非估计命令：无机器层数值
        out: dict = {"N": int(float(n))}
        if b is not None and b not in (".", ""):
            out["coef"] = float(b)
        if r2 is not None and r2 not in (".", ""):
            out["r2"] = float(r2)
        se = _find_marker(text, "MACHINE_SE=")
        if se is not None and se not in (".", ""):
            out["se"] = float(se)
        return out

    @staticmethod
    def parse_env(text: str) -> dict:
        ver = _find_marker(text, "STA_ENV version=")
        flav = _find_marker(text, "STA_ENV flavor=")
        if ver is None or flav is None:
            raise MachineParseError("缺少环境标记(STA_ENV version/flavor)")
        return {"stata_version": ver, "stata_flavor": flav}

    # ------------------------------------------------------------------ 执行
    def execute(
        self,
        script: str,
        *,
        idea: str = "i1",
        run_id: str | None = None,
        spec_id: str | None = None,
        side_effect: str = "write",
        require_ados: list[str] | None = None,
    ) -> dict:
        """跑一段脚本 → 落 run 链事件 + 返回 {machine, env, do_file, command_hash}。

        require_ados：skill 预检用的必需 ado（如 reghdfe），缺则先失败，不硬跑。
        """
        if require_ados:
            from .ado import missing_ados

            miss = missing_ados(require_ados)
            if miss:
                raise MachineParseError(f"skill 预检失败：缺少 ado {miss}（先安装再跑）")
        run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
        op = f"op-{run_id}"
        input_hash = sha(script)
        # B2：同输入已在账本 committed → 幂等复用，不重跑（超时≠失败）
        reuse = reuse_for(self._store, idea, input_hash)
        if reuse is not None:
            return reuse
        previous_runs = self._store.project(idea).runs.values()
        attempt = max((rec.attempt_id for rec in previous_runs), default=0) + 1

        # 脚本写进 run 目录 → do_file（可复现）。 关键顺序：requested 必须
        # 先提交，之后的每个真实 MCP 调用才允许开始。
        do_file = self._run_root / f"{run_id}.do"
        do_file.write_text(script, encoding="utf-8")
        lines = [ln for ln in script.splitlines() if ln.strip()]

        phase = self._store.project(idea).phase
        self._store.append(Event(
            idea_id=idea, event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            operation_id=op, fingerprint=input_hash, attempt_id=attempt,
            side_effect_state="running", phase=phase,
            payload={"run_id": run_id, "spec_id": spec_id, "side_effect": side_effect,
                     "semantic_input_hash": input_hash},
        ))

        if not lines:
            reason = "script 为空"
            self._store.append(Event(
                idea_id=idea, event_type=EVENT_RUN_FAILED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                operation_id=op, side_effect_state="failed", phase=phase,
                payload={"run_id": run_id, "reason": reason},
            ))
            raise MachineParseError(reason)

        sess = self._session if self._share_session else None
        owned_session = False
        texts: list[str] = []
        env: dict = {}
        machine: dict = {}

        def discard_session() -> None:
            nonlocal sess
            if sess is None:
                return
            if self._share_session and self._session is sess:
                self._session = None
            try:
                sess.close()
            except Exception:  # noqa: BLE001 - cleanup must not hide the outcome
                pass
            sess = None

        def append_call(call_id: str, line: str, index: int) -> None:
            self._store.append(Event(
                idea_id=idea, event_type=EVENT_TOOL_CALL, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                operation_id=op, fingerprint=sha(f"{op}:{index}:{line}"), phase=phase,
                payload={"run_id": run_id, "call_id": call_id, "call_index": index,
                         "code_hash": sha(line), "code_head": line[:160],
                         "side_effect": side_effect, "executor": "stata-mcp"},
            ))

        def append_result(call_id: str, result=None, *, error: BaseException | None = None) -> None:
            if result is not None:
                result_text = str(getattr(result, "text", "") or "")
                structured = getattr(result, "structured", {}) or {}
                rc = getattr(result, "rc", None)
                if callable(rc):
                    rc = rc()
                is_error = bool(getattr(result, "is_error", False))
                result_payload = {
                    "run_id": run_id, "call_id": call_id, "rc": rc,
                    "is_error": is_error, "text_head": result_text[:500],
                    "structured": structured if isinstance(structured, dict) else {},
                    "side_effect": side_effect,
                }
                self._store.append(Event(
                    idea_id=idea, event_type=EVENT_TOOL_RESULT, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                    operation_id=op, phase=phase, payload=result_payload,
                ))
                return
            self._store.append(Event(
                idea_id=idea, event_type=EVENT_TOOL_RESULT, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                operation_id=op, phase=phase,
                payload={"run_id": run_id, "call_id": call_id, "transport_error": True,
                         "error_type": type(error).__name__ if error else "UnknownError",
                         "error": str(error or "unknown transport failure")[:300],
                         "side_effect": side_effect},
            ))

        def append_terminal(kind: str, payload: dict, state: str) -> None:
            self._store.append(Event(
                idea_id=idea, event_type=kind, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                operation_id=op, side_effect_state=state, phase=phase,
                payload={"run_id": run_id, **payload},
            ))

        try:
            for index, line in enumerate(lines, start=1):
                call_id = f"{op}:call:{index}"
                append_call(call_id, line, index)
                try:
                    if sess is None:
                        sess = StataSession()
                        owned_session = True
                        if self._share_session:
                            self._session = sess
                    call = getattr(sess, "call", None)
                    if callable(call):
                        result = call(line)
                    else:
                        # Compatibility with small test doubles/older clients;
                        # one-line batches still represent one external call.
                        batch = sess.run_batch([line])
                        if not batch:
                            raise RuntimeError("stata transport returned no result")
                        result = batch[0]
                except Exception as error:  # noqa: BLE001 - classify transport below
                    append_result(call_id, error=error)
                    reason = str(error or "Stata transport failure")
                    if _transport_error(error):
                        append_terminal(
                            EVENT_RUN_UNCERTAIN,
                            {"reason": reason[:240], "machine": {}, "provenance": {
                                "kind": "real", "executor": "stata-mcp", "attested": False,
                                "do_file": str(do_file), "command_hash": input_hash,
                                "side_effect": side_effect,
                            }},
                            "uncertain",
                        )
                        discard_session()
                        raise TransportUncertainError(reason) from error
                    append_terminal(
                        EVENT_RUN_FAILED,
                        {"reason": reason[:240], "machine": {}, "text_head": reason[:200]},
                        "failed",
                    )
                    discard_session()
                    raise MachineParseError(reason) from error

                append_result(call_id, result)
                result_text = str(getattr(result, "text", "") or "")
                texts.append(result_text)
                result_rc = getattr(result, "rc", None)
                if callable(result_rc):
                    result_rc = result_rc()
                is_error = bool(getattr(result, "is_error", False))
                transport = _transport_error(text=result_text)
                if transport:
                    reason = result_text or "Stata transport returned an uncertain response"
                    append_terminal(
                        EVENT_RUN_UNCERTAIN,
                        {"reason": reason[:240], "machine": {}, "provenance": {
                            "kind": "real", "executor": "stata-mcp", "attested": False,
                            "do_file": str(do_file), "command_hash": input_hash,
                            "side_effect": side_effect,
                        }},
                        "uncertain",
                    )
                    discard_session()
                    raise TransportUncertainError(reason)
                if is_error or (result_rc is not None and result_rc != 0):
                    reason = f"stata rc!=0: {result_text[:300]}"
                    append_terminal(
                        EVENT_RUN_FAILED,
                        {"reason": reason[:240], "text_head": result_text[:200]},
                        "failed",
                    )
                    discard_session()
                    raise MachineParseError(reason)

            text = "\n".join(texts)
            try:
                machine = self.parse_machine(text)
                env = self.parse_env(text)
            except MachineParseError as error:
                reason = str(error)
                append_terminal(
                    EVENT_RUN_FAILED,
                    {"reason": reason[:240], "text_head": text[:200]},
                    "failed",
                )
                discard_session()
                raise
        finally:
            if owned_session and not self._share_session and sess is not None:
                try:
                    sess.close()
                finally:
                    sess = None

        prov = {
            "kind": "real",
            "executor": "stata-mcp",
            "attested": True,
            "do_file": str(do_file),
            "command_hash": input_hash,
            "data_signature": None,  # 内置数据集 sysuse，无外部文件
            "env_sig": env,
            "side_effect": side_effect,
        }
        append_terminal(
            EVENT_RUN_SUCCEEDED,
            {"spec_id": spec_id, "provenance": prov, "machine": machine,
             "output_head": text[:500]},
            "committed",
        )
        return {"run_id": run_id, "machine": machine, "env": env, "do_file": str(do_file),
                "command_hash": input_hash, "output_head": text[:800]}


# 复现/自检用：内置 auto 回归（load→估计→echo 机器+env）
def auto_regress_script() -> str:
    return (
        "sysuse auto, clear\n"
        "reg price mpg\n"
        'di "MACHINE_B=" %9.6f _b[mpg]\n'
        'di "MACHINE_N=" e(N)\n'
        'di "MACHINE_R2=" %9.6f e(r2)\n'
        'di "STA_ENV version=" c(version)\n'
        'di "STA_ENV flavor=" c(flavor)\n'
    )
