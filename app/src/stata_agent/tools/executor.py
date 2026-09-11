"""StataExecutor：把一段 Stata 脚本变成不可变 Run（DD-01 执行链 + 三层结果/环境指纹）。

切片 3 最小版：
- 每次 run 自包含（load→估计→echo 机器值+env 标记）→ 单次连接内完成（stata 会话按次重建，
  故状态依赖都在脚本内，正是"从 prepared 快照重建"的精神）。
- 脚本本体写进 run 目录（do_file 可复现）；command_hash=sha256(script)。
- 机器层值靠脚本自身 echo 固定格式解析（等价于未来 validator 的 display→cell 解析）。
"""

from __future__ import annotations

import hashlib
import inspect
import re
import threading
import uuid
from pathlib import Path

from ..events.schema import (
    ACTOR_ORCH,
    EVENT_RUN_FAILED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    Event,
)
from ..harness.cancellation import cancellation_reason, is_cancel_requested
from ..storage.sqlite_store import SQLiteStore
from .stata_client import StataCancelledError, StataSession
from .result_verifier import (
    ResultContract,
    canonical_contract_hash,
    canonical_machine_hash,
    semantic_input_hash,
    validate_contract,
)


class MachineParseError(RuntimeError):
    pass


class TransportUncertainError(MachineParseError):
    """The Stata transport/process outcome cannot be known safely."""

    uncertain = True


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reuse_for(
    store: SQLiteStore,
    idea: str,
    input_hash: str,
    *,
    contract_hash: str | None = None,
) -> dict | None:
    """同输入复用（B2）：账本里已有同 semantic_input_hash 的 succeeded run → 复用其结果，
    不重跑（DD-01 幂等）。"""
    proj = store.project(idea)
    for rid, rec in proj.runs.items():
        if rec.status == "succeeded" and rec.semantic_input_hash == input_hash:
            if contract_hash is not None:
                if not isinstance(rec.result_contract, dict):
                    continue
                try:
                    if canonical_contract_hash(rec.result_contract) != contract_hash:
                        continue
                except Exception:
                    continue
            return {
                "run_id": rid,
                "machine": dict(rec.machine or {}),
                "env": dict((rec.provenance or {}).get("env_sig") or {}),
                "do_file": (rec.provenance or {}).get("do_file"),
                "command_hash": (rec.provenance or {}).get("command_hash"),
                "semantic_input_hash": rec.semantic_input_hash,
                "result_contract": dict(rec.result_contract or {}) if rec.result_contract else None,
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


def _supports_keyword(callable_obj, keyword: str) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return True
    return keyword in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


class StataExecutor:
    def __init__(self, store: SQLiteStore, *, run_root: Path | None = None,
                 share_session: bool = False, stata_mcp_dir: str | Path | None = None):
        self._store = store
        self._run_root = Path(run_root) if run_root else Path(store._path).parent / "runs"
        self._run_root.mkdir(parents=True, exist_ok=True)
        self._share_session = share_session
        self._stata_mcp_dir = Path(stata_mcp_dir) if stata_mcp_dir is not None else None
        self._session = None
        self._state_lock = threading.RLock()

    def close(self) -> None:
        with self._state_lock:
            session = self._session
            self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - repeated cleanup must be safe
                pass

    # ------------------------------------------------------------------ 机器层
    @staticmethod
    def parse_machine(text: str, *, namespace: str | None = None) -> dict:
        """从标记提取机器层数值。非估计命令（无 e(N)，di 输出 '.'）→ 返回空 dict，
        不 raise（探索命令如 describe/list 不该被当回归失败）。"""
        return StataExecutor._parse_machine(text, namespace=namespace)

    @staticmethod
    def _parse_machine(text: str, *, namespace: str | None = None) -> dict:
        prefix = namespace or ""
        b = _find_marker(text, f"{prefix}MACHINE_B=")
        n = _find_marker(text, f"{prefix}MACHINE_N=")
        r2 = _find_marker(text, f"{prefix}MACHINE_R2=")
        if n is None or n in (".", ""):
            return {}  # 非估计命令：无机器层数值
        try:
            out: dict = {"N": int(float(n))}
        except (TypeError, ValueError) as error:
            raise MachineParseError("机器层 N 不是有限整数") from error
        if b is not None and b not in (".", ""):
            try:
                out["coef"] = float(b)
            except (TypeError, ValueError) as error:
                raise MachineParseError("机器层 coef 不是数字") from error
        if r2 is not None and r2 not in (".", ""):
            try:
                out["r2"] = float(r2)
            except (TypeError, ValueError) as error:
                raise MachineParseError("机器层 r2 不是数字") from error
        se = _find_marker(text, f"{prefix}MACHINE_SE=")
        if se is not None and se not in (".", ""):
            try:
                out["se"] = float(se)
            except (TypeError, ValueError) as error:
                raise MachineParseError("机器层 se 不是数字") from error
        if namespace:
            metadata = {
                "target_term": _find_marker(text, f"{namespace}MACHINE_TERM="),
                "estimator": _find_marker(text, f"{namespace}MACHINE_CMD="),
                "dependent_variable": _find_marker(text, f"{namespace}MACHINE_DEPVAR="),
                "vce": _find_marker(text, f"{namespace}MACHINE_VCE="),
                "cluster_variables": _find_marker(text, f"{namespace}MACHINE_CLUSTER="),
                "fixed_effects": _find_marker(text, f"{namespace}MACHINE_FE="),
            }
            if any(value is not None for value in metadata.values()):
                out["model"] = metadata
        return out

    @staticmethod
    def parse_env(text: str, *, namespace: str | None = None) -> dict:
        return StataExecutor._parse_env(text, namespace=namespace)

    @staticmethod
    def _parse_env(text: str, *, namespace: str | None = None) -> dict:
        if namespace:
            ver = _find_marker(text, f"{namespace}STA_ENV_VERSION=")
            flav = _find_marker(text, f"{namespace}STA_ENV_FLAVOR=")
        else:
            ver = _find_marker(text, "STA_ENV version=")
            flav = _find_marker(text, "STA_ENV flavor=")
        if ver is None or flav is None:
            raise MachineParseError("缺少环境标记(STA_ENV version/flavor)")
        return {"stata_version": ver, "stata_flavor": flav}

    @staticmethod
    def _contract_suffix(contract: ResultContract, namespace: str) -> str:
        """Generate trusted extraction commands after user code.

        The namespace is random per run and never exposed to the model.  The
        contract's term has already passed a conservative allowlist, so it is
        safe to use in the Stata ``_b[]``/``_se[]`` selectors.
        """

        term = contract.target_term
        return "\n".join(
            [
                f'di "{namespace}MACHINE_N=" e(N)',
                f'di "{namespace}MACHINE_B=" %21.17g _b[{term}]',
                f'di "{namespace}MACHINE_SE=" %21.17g _se[{term}]',
                f'di "{namespace}MACHINE_R2=" %21.17g e(r2)',
                f'di "{namespace}MACHINE_TERM={term}"',
                f'di "{namespace}MACHINE_CMD=" "`e(cmd)\'"',
                f'di "{namespace}MACHINE_DEPVAR=" "`e(depvar)\'"',
                f'di "{namespace}MACHINE_VCE=" "`e(vce)\'"',
                f'di "{namespace}MACHINE_CLUSTER=" "`e(clustvar)\'"',
                f'di "{namespace}MACHINE_FE=" "`e(absvars)\'"',
                f'di "{namespace}STA_ENV_VERSION=" c(version)',
                f'di "{namespace}STA_ENV_FLAVOR=" c(flavor)',
            ]
        )

    @staticmethod
    def _environment_suffix(namespace: str) -> str:
        return "\n".join(
            [
                f'di "{namespace}STA_ENV_VERSION=" c(version)',
                f'di "{namespace}STA_ENV_FLAVOR=" c(flavor)',
            ]
        )

    # ------------------------------------------------------------------ 执行
    def execute(
        self,
        script: str,
        *,
        idea: str = "i1",
        run_id: str | None = None,
        correlation_id: str | None = None,
        spec_id: str | None = None,
        side_effect: str = "write",
        require_ados: list[str] | None = None,
        cancellation=None,
        cancel_token=None,
        result_contract: ResultContract | dict | None = None,
    ) -> dict:
        """跑一段脚本 → 落 run 链事件 + 返回 {machine, env, do_file, command_hash}。

        require_ados：skill 预检用的必需 ado（如 reghdfe），缺则先失败，不硬跑。
        """
        token = cancellation or cancel_token
        if is_cancel_requested(token):
            raise StataCancelledError(cancellation_reason(token), uncertain=False)
        if require_ados:
            from .ado import missing_ados

            miss = missing_ados(require_ados)
            if miss:
                raise MachineParseError(f"skill 预检失败：缺少 ado {miss}（先安装再跑）")
        contract = validate_contract(result_contract)
        run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
        op = f"op-{run_id}"
        contract_hash = canonical_contract_hash(contract) if contract is not None else None
        input_hash = (
            semantic_input_hash(script, contract)
            if contract is not None
            else sha(script)
        )
        # B2：同输入已在账本 committed → 幂等复用，不重跑（超时≠失败）
        reuse = reuse_for(self._store, idea, input_hash, contract_hash=contract_hash)
        if reuse is not None:
            return reuse
        previous_runs = self._store.project(idea).runs.values()
        attempt = max((rec.attempt_id for rec in previous_runs), default=0) + 1

        # 脚本写进 run 目录 → do_file（可复现）。 关键顺序：requested 必须
        # 先提交，之后的每个真实 MCP 调用才允许开始。
        marker_namespace = f"STA_AGENT_{uuid.uuid4().hex}_"
        user_lines = [ln for ln in script.splitlines() if ln.strip()]
        if contract is not None:
            extraction = self._contract_suffix(contract, marker_namespace)
        elif "STA_ENV version=" not in script or "STA_ENV flavor=" not in script:
            # Exploratory runs still get a trusted environment fingerprint, but
            # never receive coefficient/model extraction commands.
            extraction = self._environment_suffix(marker_namespace)
        else:
            extraction = ""
        executed_script = script if not extraction else f"{script.rstrip()}\n{extraction}\n"
        do_file = self._run_root / f"{run_id}.do"
        do_file.write_text(executed_script, encoding="utf-8", newline="")
        lines = [ln for ln in executed_script.splitlines() if ln.strip()] if user_lines else []
        command_hash = hashlib.sha256(do_file.read_bytes()).hexdigest()

        phase = self._store.project(idea).phase
        self._store.append(Event(
            idea_id=idea, event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            correlation_id=correlation_id, operation_id=op, fingerprint=input_hash, attempt_id=attempt,
            side_effect_state="running", phase=phase,
            payload={"run_id": run_id, "spec_id": spec_id, "side_effect": side_effect,
                     "semantic_input_hash": input_hash,
                     "result_contract": contract.model_dump() if contract is not None else None},
        ))

        terminal_emitted = False
        normal_completion = False

        if not lines:
            reason = "script 为空"
            self._store.append(Event(
                idea_id=idea, event_type=EVENT_RUN_FAILED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                correlation_id=correlation_id, operation_id=op, side_effect_state="failed", phase=phase,
                payload={"run_id": run_id, "reason": reason},
            ))
            raise MachineParseError(reason)

        with self._state_lock:
            sess = self._session if self._share_session else None
        owned_session = False
        texts: list[str] = []
        env: dict = {}
        machine: dict = {}
        external_started = False

        def discard_session() -> None:
            nonlocal sess
            if sess is None:
                return
            with self._state_lock:
                if self._share_session and self._session is sess:
                    self._session = None
            try:
                sess.close()
            except Exception:  # noqa: BLE001 - cleanup must not hide the outcome
                pass
            sess = None

        def call_session(line: str):
            """Invoke old and new session test doubles compatibly."""

            nonlocal external_started
            if is_cancel_requested(token):
                raise StataCancelledError(cancellation_reason(token), uncertain=False)
            external_started = True
            call = getattr(sess, "call", None)
            if callable(call):
                if _supports_keyword(call, "cancellation"):
                    return call(line, cancellation=token)
                return call(line)
            run_batch = sess.run_batch
            if _supports_keyword(run_batch, "cancellation"):
                batch = run_batch([line], cancellation=token)
            else:
                batch = run_batch([line])
            if not batch:
                raise RuntimeError("stata transport returned no result")
            return batch[0]

        def append_call(call_id: str, line: str, index: int) -> None:
            self._store.append(Event(
                idea_id=idea, event_type=EVENT_TOOL_CALL, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                correlation_id=correlation_id, operation_id=op, fingerprint=sha(f"{op}:{index}:{line}"), phase=phase,
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
                    correlation_id=correlation_id, operation_id=op, phase=phase, payload=result_payload,
                ))
                return
            error_payload = {
                "run_id": run_id, "call_id": call_id, "transport_error": True,
                "error_type": type(error).__name__ if error else "UnknownError",
                "error": str(error or "unknown transport failure")[:300],
                "side_effect": side_effect,
            }
            if getattr(error, "cancelled", False):
                error_payload.update({
                    "cancel_requested": True,
                    "terminal_reason": "cancel_requested",
                    "uncertain": bool(getattr(error, "uncertain", True)),
                })
            self._store.append(Event(
                idea_id=idea, event_type=EVENT_TOOL_RESULT, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                correlation_id=correlation_id, operation_id=op, phase=phase,
                payload=error_payload,
            ))

        def append_terminal(kind: str, payload: dict, state: str) -> None:
            nonlocal terminal_emitted
            self._store.append(Event(
                idea_id=idea, event_type=kind, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                correlation_id=correlation_id, operation_id=op, side_effect_state=state, phase=phase,
                payload={"run_id": run_id, **payload},
            ))
            terminal_emitted = True

        def append_cancel_terminal(error: StataCancelledError, *, uncertain: bool) -> None:
            reason = str(error or cancellation_reason(token) or "cancelled")
            payload = {
                "reason": reason[:240],
                "terminal_reason": "cancel_requested",
                "cancel_requested": True,
                "machine": {},
            }
            if uncertain:
                payload["provenance"] = {
                    "kind": "real", "executor": "stata-mcp", "attested": False,
                    "do_file": str(do_file), "command_hash": command_hash,
                    "semantic_input_hash": input_hash,
                    "contract_hash": contract_hash,
                    "side_effect": side_effect,
                    "terminal_reason": "cancel_requested",
                }
            append_terminal(
                EVENT_RUN_UNCERTAIN if uncertain else EVENT_RUN_FAILED,
                payload,
                "uncertain" if uncertain else "cancelled",
            )

        try:
            for index, line in enumerate(lines, start=1):
                call_id = f"{op}:call:{index}"
                if is_cancel_requested(token):
                    cancel_error = StataCancelledError(cancellation_reason(token), uncertain=False)
                    append_cancel_terminal(cancel_error, uncertain=False)
                    raise cancel_error
                append_call(call_id, line, index)
                if is_cancel_requested(token):
                    cancel_error = StataCancelledError(cancellation_reason(token), uncertain=False)
                    append_result(call_id, error=cancel_error)
                    append_cancel_terminal(cancel_error, uncertain=False)
                    raise cancel_error
                try:
                    if sess is None:
                        if self._stata_mcp_dir is None:
                            # Preserve compatibility with injected test
                            # sessions and legacy adapters that expose the
                            # original zero-argument constructor.
                            sess = StataSession()
                        else:
                            sess = StataSession(mcp_dir=self._stata_mcp_dir)
                        owned_session = True
                        if self._share_session:
                            with self._state_lock:
                                self._session = sess
                    result = call_session(line)
                except StataCancelledError as error:
                    append_result(call_id, error=error)
                    uncertain = bool(getattr(error, "uncertain", True))
                    append_cancel_terminal(error, uncertain=uncertain)
                    discard_session()
                    raise
                except Exception as error:  # noqa: BLE001 - classify transport below
                    append_result(call_id, error=error)
                    reason = str(error or "Stata transport failure")
                    if _transport_error(error):
                        append_terminal(
                            EVENT_RUN_UNCERTAIN,
                            {"reason": reason[:240], "machine": {}, "provenance": {
                                "kind": "real", "executor": "stata-mcp", "attested": False,
                                "do_file": str(do_file), "command_hash": command_hash,
                                "semantic_input_hash": input_hash,
                                "contract_hash": contract_hash,
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
                if is_cancel_requested(token):
                    cancel_error = StataCancelledError(cancellation_reason(token), uncertain=True)
                    append_cancel_terminal(cancel_error, uncertain=True)
                    discard_session()
                    raise cancel_error
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
                            "do_file": str(do_file), "command_hash": command_hash,
                            "semantic_input_hash": input_hash,
                            "contract_hash": contract_hash,
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
            if is_cancel_requested(token):
                cancel_error = StataCancelledError(cancellation_reason(token), uncertain=True)
                append_cancel_terminal(cancel_error, uncertain=True)
                discard_session()
                raise cancel_error
            try:
                if contract is None:
                    machine = self.parse_machine(text)
                    try:
                        env = self.parse_env(text)
                    except MachineParseError:
                        try:
                            env = self._parse_env(text, namespace=marker_namespace)
                        except MachineParseError:
                            # No environment response is still a valid
                            # exploratory execution; it is not reportable.
                            env = {}
                else:
                    machine = self._parse_machine(text, namespace=marker_namespace)
                    env = self._parse_env(text, namespace=marker_namespace)
            except MachineParseError as error:
                reason = str(error)
                append_terminal(
                    EVENT_RUN_FAILED,
                    {"reason": reason[:240], "text_head": text[:200]},
                    "failed",
                )
                discard_session()
                raise
            normal_completion = True
        finally:
            if owned_session and not self._share_session and sess is not None:
                try:
                    sess.close()
                except Exception:  # noqa: BLE001 - cleanup must not mask run outcome
                    pass
                finally:
                    sess = None
            if not normal_completion and not terminal_emitted:
                # Last-resort audit closure for an unexpected Python error.
                # If the ledger itself is unavailable, preserve the original
                # exception; the health/recovery layer will report the gap.
                try:
                    append_terminal(
                        EVENT_RUN_UNCERTAIN if external_started else EVENT_RUN_FAILED,
                        {
                            "reason": "unexpected executor error",
                            "terminal_reason": "executor_error",
                            "machine": {},
                            "provenance": {
                                "kind": "real", "executor": "stata-mcp", "attested": False,
                                "do_file": str(do_file), "command_hash": command_hash,
                                "semantic_input_hash": input_hash,
                                "contract_hash": contract_hash,
                                "side_effect": side_effect,
                            },
                        },
                        "uncertain" if external_started else "failed",
                    )
                except Exception:  # noqa: BLE001 - never mask the primary error
                    pass

        prov = {
            "kind": "real",
            "executor": "stata-mcp",
            "attested": True,
            "do_file": str(do_file),
            "command_hash": command_hash,
            "semantic_input_hash": input_hash,
            "contract_hash": contract_hash,
            "data_signature": None,  # 内置数据集 sysuse，无外部文件
            "env_sig": env,
            "side_effect": side_effect,
        }
        prov["machine_hash"] = canonical_machine_hash(machine)
        append_terminal(
            EVENT_RUN_SUCCEEDED,
            {"spec_id": spec_id, "provenance": prov, "machine": machine,
             "output_head": text[:500]},
            "committed",
        )
        return {"run_id": run_id, "machine": machine, "env": env, "do_file": str(do_file),
                "command_hash": command_hash, "semantic_input_hash": input_hash,
                "result_contract": contract.model_dump() if contract is not None else None,
                "reused": False, "output_head": text[:800]}


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
