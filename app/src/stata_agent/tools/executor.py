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
    ACTOR_ORCH,
    Event,
)
from ..storage.sqlite_store import SQLiteStore
from .stata_client import StataSession


class MachineParseError(RuntimeError):
    pass


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
        attempt = len(self._store.project(idea).runs) + 1

        # 脚本写进 run 目录 → do_file（可复现）；再发给 stata-mcp
        do_file = self._run_root / f"{run_id}.do"
        do_file.write_text(script, encoding="utf-8")
        lines = [ln for ln in script.splitlines() if ln.strip()]

        # 执行（最多 2 次；share_session 时复用会话省启动，失败则丢弃共享会话降级全新）
        ok: tuple[dict, dict, str] | None = None
        last_err: Exception | None = None
        for _try in range(1, 3):
            close_sess = False
            if self._share_session and self._session is not None:
                sess = self._session
            else:
                sess = StataSession()
                close_sess = not self._share_session
                if self._share_session:
                    self._session = sess
            try:
                results = sess.run_batch(lines)
            finally:
                if close_sess:
                    sess.close()
            text = "\n".join(r.text for r in results)
            if any(r.is_error for r in results) or "crashed" in text.lower():
                # 会话可能崩了：丢弃共享会话，下次用全新
                if self._share_session:
                    self._session = None
                last_err = MachineParseError(f"stata rc!=0: {text[:300]}")
                continue
            try:
                machine = self.parse_machine(text)
                env = self.parse_env(text)
                ok = (machine, env, text)
                break
            except MachineParseError as e:
                last_err = e
                if "crashed" not in text.lower():
                    break  # 非瞬时会话问题 → 不再重试
        # 事件只在最终成败时写一次（避免重试产生重复指纹/半截状态）
        self._store.append(Event(
            idea_id=idea, event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            operation_id=op, fingerprint=input_hash, attempt_id=attempt,
            side_effect_state="running", phase=self._store.project(idea).phase,
            payload={"run_id": run_id, "spec_id": spec_id, "side_effect": side_effect,
                     "semantic_input_hash": input_hash},
        ))
        if ok is None:
            reason = str(last_err or "执行失败")
            self._store.append(Event(
                idea_id=idea, event_type=EVENT_RUN_FAILED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                operation_id=op, side_effect_state="failed", phase=self._store.project(idea).phase,
                payload={"run_id": run_id, "reason": reason[:200], "text_head": text[:200]},
            ))
            raise MachineParseError(reason) from last_err
        machine, env, text = ok
        prov = {
            "do_file": str(do_file),
            "command_hash": input_hash,
            "data_signature": None,  # 内置数据集 sysuse，无外部文件
            "env_sig": env,
            "side_effect": side_effect,
        }
        self._store.append(Event(
            idea_id=idea, event_type=EVENT_RUN_SUCCEEDED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            operation_id=op, side_effect_state="committed", phase=self._store.project(idea).phase,
            payload={"run_id": run_id, "spec_id": spec_id, "provenance": prov, "machine": machine,
                     "output_head": text[:500]},  # 大输出摘要进账本；do_file 存全文可按需读
        ))
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
