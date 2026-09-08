"""FastAPI UI for the single-idea empirical-research workbench.

The server deliberately exposes read-only projections of the event ledger. UI
actions that change research state continue to go through the runner; the
browser never writes directly to the events table or to a projection.

The page itself lives in ``ui/index.html`` with local CSS and ES module
JavaScript. Keeping those files separate makes the interface easy to inspect
and keeps the FastAPI layer small enough to hand over to the next maintainer.
"""

from __future__ import annotations

import os
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .domain.action import ActionProposal, Act
from .events.schema import (
    EVENT_AGENT_STEP,
    EVENT_APPROVAL_GRANT,
    EVENT_APPROVAL_REJECT,
    EVENT_APPROVAL_REQ,
    EVENT_BUDGET,
    EVENT_HEALTH,
    EVENT_IDEA,
    EVENT_MAIN_RESULT,
    EVENT_PHASE,
    EVENT_RUN_FAILED,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_SPEC_FREEZE,
    EVENT_USER,
    ACTOR_ORCH,
    ACTOR_USER,
    Event,
)
from .harness.research_turn import bootstrap_idea
from .providers.mock import MockFixedProvider, MockReplayProvider
from .runner import approve as runner_approve
from .runner import run_until_gate
from .storage.sqlite_store import SQLiteStore
from .tools.fake_executor import FakeExecutor
from .writer.docx_out import claims_to_docx
from .writer.ground import render_claim_sentence

from .tools.executor import StataExecutor  # noqa: E402  （真 Stata 可选）

DEFAULT_DB = Path(os.environ.get("STATA_AGENT_DB", "samples/ideas/ui/ledger.sqlite3"))
_IDEA = "ui"
_UI_DIR = Path(__file__).with_name("ui")
_INDEX_FILE = _UI_DIR / "index.html"

_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WORKSPACE_REGISTRY_LOCK = threading.RLock()

app = FastAPI(title="stata-agent · research UI")
app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="ui-static")

# SQLiteStore owns a fenced single-writer lease even for projection reads.
# FastAPI runs synchronous routes in a thread pool, while the browser refreshes
# state/events/approvals concurrently. Serialising UI store sessions prevents a
# read request from taking over the lease halfway through a chat write.
_UI_STORE_LOCK = threading.RLock()


class _LockedStore:
    """Delegate to SQLiteStore while holding the process-local UI lease lock."""

    def __init__(self, store: SQLiteStore):
        self._store = store
        self._closed = False

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._store.close()
        finally:
            _UI_STORE_LOCK.release()


def _store() -> _LockedStore:
    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    _UI_STORE_LOCK.acquire()
    try:
        return _LockedStore(SQLiteStore(str(DEFAULT_DB), writer_id="ui", takeover=True))
    except Exception:
        _UI_STORE_LOCK.release()
        raise


DEMO = os.environ.get("STATA_AGENT_DEMO") == "1"


def _workspace_registry_path() -> Path:
    """Return the transparent local workspace registry path.

    Keeping the registry next to the ledger makes a copied local workspace
    self-contained and lets tests redirect it together with ``DEFAULT_DB``.
    ``STATA_AGENT_WORKSPACES`` is an escape hatch for operators who keep the
    ledger and UI metadata in separate local directories.
    """

    configured = os.environ.get("STATA_AGENT_WORKSPACES", "").strip()
    return Path(configured) if configured else DEFAULT_DB.parent / "workspaces.json"


def _validate_workspace_id(value: str | None) -> str:
    raw = str(value or _IDEA).strip()
    if not _WORKSPACE_ID_RE.fullmatch(raw):
        raise HTTPException(status_code=422, detail="工作区标识只允许字母、数字、下划线和连字符。")
    return raw


def _read_workspace_registry() -> list[dict[str, Any]]:
    """Read the small local registry, creating the compatibility ``ui`` row."""

    path = _workspace_registry_path()
    with _WORKSPACE_REGISTRY_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (OSError, ValueError, TypeError):
            data = []
        rows: list[dict[str, Any]] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("id") or "").strip()
                if not _WORKSPACE_ID_RE.fullmatch(ident):
                    continue
                rows.append({
                    "id": ident,
                    "name": str(item.get("name") or ident)[:120],
                    "created_at": int(item.get("created_at") or 0),
                })
        if not any(row["id"] == _IDEA for row in rows):
            rows.insert(0, {"id": _IDEA, "name": "未命名研究", "created_at": int(time.time() * 1000)})
            _write_workspace_registry(rows)
        return rows


def _write_workspace_registry(rows: list[dict[str, Any]]) -> None:
    """Atomically persist workspace metadata without touching the event ledger."""

    path = _workspace_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _workspace_known(idea: str) -> bool:
    return any(row["id"] == idea for row in _read_workspace_registry())


def _resolve_workspace(ws: str | None) -> str:
    idea = _validate_workspace_id(ws)
    if not _workspace_known(idea):
        raise HTTPException(status_code=404, detail="工作区不存在，请先新建或从列表选择。")
    return idea


def _workspace_slug(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", name.lower()).strip("-_")[:48]
    return base or f"workspace-{uuid.uuid4().hex[:8]}"


def _workspace_entries(store: SQLiteStore) -> list[dict[str, Any]]:
    """Combine registry metadata with current read-only ledger counters."""

    rows = _read_workspace_registry()
    result: list[dict[str, Any]] = []
    for row in rows:
        idea = row["id"]
        events = list(store.scan(idea))
        first_idea = next((event for event in events if event.event_type == EVENT_IDEA), None)
        question = str((first_idea.payload if first_idea else {}).get("question") or "").strip()
        approvals = _approval_records(events)
        status, status_detail = _run_state(events, approvals)
        result.append({
            "id": idea,
            "name": (question or row.get("name") or idea)[:120],
            "created_at": row.get("created_at") or (events[0].created_at if events else None),
            "updated_at": events[-1].created_at if events else row.get("created_at"),
            "events": len(events),
            "last_seq": events[-1].seq if events else None,
            "run_status": status,
            "status_detail": status_detail,
            "pending_approvals": len([item for item in approvals if item["status"] == "pending"]),
        })
    return result


def _touch_workspace_name(idea: str, text: str) -> None:
    """Persist a useful display name after the first idea is declared."""

    clean = " ".join(str(text or "").split())[:120]
    if not clean:
        return
    with _WORKSPACE_REGISTRY_LOCK:
        rows = _read_workspace_registry()
        for row in rows:
            if row["id"] == idea and (not row.get("name") or row.get("name") == "未命名研究"):
                row["name"] = clean
                _write_workspace_registry(rows)
                return


def _demo_seed(store: SQLiteStore, idea: str = _IDEA) -> None:
    """DEMO 模式：首次进入把 idea 推进到 ESTIMATION（供演示 spec→run→证据→Word）。

    仅当开了 STATA_AGENT_DEMO=1 且还没有 phase 事件时生效；正常模式不做任何越权推进。
    """
    if not DEMO:
        return
    if any(ev.event_type == EVENT_PHASE for ev in store.scan(idea)):
        return
    store.append(Event(idea_id=idea, event_type=EVENT_PHASE, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                       payload={"from": "IDEA", "to": "ESTIMATION"}))


def _provider():
    from .providers.registry import live_available

    if live_available():
        from .providers.registry import default_provider

        return default_provider()
    if DEMO:
        # 无 LLM key 的离线演示：剧本走完 定 spec → 跑(fake) → 问是否出稿
        return MockReplayProvider([
            ActionProposal(decision_summary="先定主 spec",
                           acts=[Act(act_type="propose_spec", target={"spec_id": "s1"}, reason="主 spec")]),
            ActionProposal(decision_summary="跑主回归",
                           acts=[Act(act_type="request_run", target={"spec_id": "s1"}, reason="跑主回归")]),
            ActionProposal(decision_summary="出结果", ask_user="是否生成 Word 初稿？"),
        ])
    return MockFixedProvider(
        ActionProposal(
            decision_summary="记录消息",
            ask_user="（未配置 LLM key）已记下。设 DEEPSEEK_API_KEY 或 DASHSCOPE_API_KEY 后启用真模型。",
        )
    )


def _executor(store: SQLiteStore):
    """执行器可选：env STATA_AGENT_EXECUTOR=stata 用真 Stata；默认 Fake（离线安全）。"""
    if os.environ.get("STATA_AGENT_EXECUTOR") == "stata":
        run_root = DEFAULT_DB.parent / "runs"
        run_root.mkdir(parents=True, exist_ok=True)
        return StataExecutor(store, run_root=run_root, share_session=True)
    return FakeExecutor(store)


def _privacy_mode() -> str:
    from .providers.registry import privacy_mode

    return privacy_mode()


def _config_info() -> dict:
    """给设置页展示的真实运行时配置（只读）。"""
    from .providers.registry import live_available

    executor = "真 Stata" if os.environ.get("STATA_AGENT_EXECUTOR") == "stata" else "演示(Fake)"
    provider = "未配置"
    if live_available():
        provider = "deepseek" if os.environ.get("DEEPSEEK_API_KEY") else "qwen"
    lib = os.environ.get("STATA_AGENT_LIBRARY", "")
    skills = list(_skills().keys())
    return {
        "provider": provider,
        "executor": executor,
        "privacy_mode": _privacy_mode(),
        "library": lib or "(未配置文献库)",
        "skills": skills,
        "workspace_db": str(DEFAULT_DB),
    }


def _memory():
    """项目记忆（约束，非证据）；没有就 None，工具会优雅降级。"""
    path = DEFAULT_DB.parent / "memory.json"
    try:
        from .memory.memstore import MemoryStore

        return MemoryStore(path)
    except Exception:  # noqa: BLE001
        return None


def _rag():
    """文献混合检索；目录缺失则 None。"""
    lib = os.environ.get("STATA_AGENT_LIBRARY", "").strip()
    if not lib or not Path(lib).exists():
        return None
    try:
        from .rag.index import build_hybrid

        return build_hybrid(lib, cache_path=DEFAULT_DB.parent / "rag_cache.json")
    except Exception:  # noqa: BLE001
        return None


def _skills() -> dict:
    """加载 skills 目录（决策层方法论）。"""
    from .skills.loader import load_skill_dir

    skills_dir = os.environ.get("STATA_AGENT_SKILLS", "skills")
    if not Path(skills_dir).exists():
        return {}
    try:
        return load_skill_dir(skills_dir)
    except Exception:  # noqa: BLE001
        return {}


def _matched_skills(text: str) -> list:
    from .skills.loader import match_skills

    return match_skills(_skills(), text)


_PHASE_LABELS = {
    "IDEA": "研究问题",
    "LITERATURE": "可行性",
    "DESIGN": "模型设定",
    "DATA": "数据准备",
    "ESTIMATION": "估计",
    "ROBUSTNESS": "稳健性",
    "WRITING": "写作",
    "VALIDATION": "验证",
    "DONE": "完成",
}
_PERMISSION_ACTS = frozenset({"delete_file", "web_search", "web_fetch", "download"})


def _event_list(store: SQLiteStore, idea: str = _IDEA) -> list[Event]:
    return list(store.scan(idea))


def _event_payload_preview(event: Event) -> dict[str, Any]:
    """Return enough payload for a trace/replay view without dumping raw JSON.

    Text is still treated as untrusted by the browser, which renders it with
    ``textContent``. Long research material is capped before it leaves the
    read-only API, while evidence and run endpoints provide their own detail
    views when the researcher explicitly opens them.
    """

    payload = event.payload or {}
    kind = event.event_type
    if kind == EVENT_USER:
        return {"text": str(payload.get("text", ""))[:2000]}
    if kind == EVENT_AGENT_STEP:
        acts = payload.get("acts") or []
        return {
            "decision_summary": str(payload.get("decision_summary") or "")[:500],
            "ask": str(payload.get("ask") or "")[:1000],
            "stop_reason": payload.get("stop_reason"),
            "acts": [str(a.get("act_type", "")) for a in acts if isinstance(a, dict)][:12],
        }
    keys = {
        "request_id",
        "act",
        "reason",
        "note",
        "run_id",
        "result_id",
        "spec_id",
        "operation_id",
        "issues",
        "status",
        "ok",
    }
    out: dict[str, Any] = {key: payload[key] for key in keys if key in payload}
    if kind in {EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
        machine = payload.get("machine")
        if isinstance(machine, dict):
            out["machine"] = {str(k): machine[k] for k in list(machine)[:12]}
    if kind == EVENT_APPROVAL_REQ and isinstance(payload.get("target"), dict):
        out["target_keys"] = sorted(str(k) for k in payload["target"].keys())[:12]
    return out


def _public_event(event: Event) -> dict[str, Any]:
    """Stable, compact event shape retained for old clients and trace UI."""

    payload = event.payload or {}
    object_value = next(
        (
            payload.get(key)
            for key in ("object", "claim_id", "card_id", "run_id", "result_id", "spec_id", "request_id")
            if payload.get(key) is not None
        ),
        None,
    )
    if object_value is None and isinstance(payload.get("target"), dict):
        object_value = payload.get("target")
    summary = next(
        (
            str(payload.get(key))
            for key in ("summary", "decision_summary", "reason", "note", "message", "text")
            if payload.get(key)
        ),
        "",
    )[:240]

    return {
        "seq": event.seq,
        "type": event.event_type,
        "event_type": event.event_type,
        "actor": event.actor,
        "phase": event.phase,
        "op": event.operation_id,
        "operation_id": event.operation_id,
        "created_at": event.created_at,
        "object": object_value,
        "summary": summary,
        "payload_keys": sorted((event.payload or {}).keys())[:12],
        "payload_preview": _event_payload_preview(event),
    }


def _approval_records(events: list[Event]) -> list[dict[str, Any]]:
    """Fold approval request/decision events into a read-only UI list."""

    records: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.payload or {}
        request_id = str(payload.get("request_id") or "")
        if not request_id:
            continue
        if event.event_type == EVENT_APPROVAL_REQ:
            act = str(payload.get("act") or "")
            records[request_id] = {
                "request_id": request_id,
                "act": act,
                "kind": "permission_gate" if act in _PERMISSION_ACTS else "research_gate",
                "reason": str(payload.get("reason") or ""),
                "note": str(payload.get("note") or ""),
                "target": payload.get("target") if isinstance(payload.get("target"), dict) else {},
                "status": "pending",
                "requested_seq": event.seq,
                "requested_at": event.created_at,
                "decided_note": "",
                "decision_seq": None,
                "decided_at": None,
            }
        elif event.event_type in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT} and request_id in records:
            records[request_id]["status"] = "approved" if event.event_type == EVENT_APPROVAL_GRANT else "rejected"
            records[request_id]["decided_note"] = str(payload.get("note") or "")
            records[request_id]["decision_seq"] = event.seq
            records[request_id]["decided_at"] = event.created_at
    return list(records.values())


def _phase_history(events: list[Event], current: str | None) -> list[str]:
    history: list[str] = []
    for event in events:
        if event.event_type != EVENT_PHASE:
            continue
        phase = (event.payload or {}).get("to") or (event.payload or {}).get("phase") or event.phase
        if phase and str(phase) not in history:
            history.append(str(phase))
    if current and current not in history:
        history.append(current)
    return history


def _run_state(events: list[Event], approvals: list[dict[str, Any]]) -> tuple[str, str]:
    pending = [item for item in approvals if item["status"] == "pending"]
    if pending:
        return "awaiting_user", f"等待你决定 {pending[-1]['act'] or '一个研究闸门'}"
    for event in reversed(events):
        payload = event.payload or {}
        if event.event_type == EVENT_BUDGET:
            return "paused", "已达到本轮预算上限，可从停点续跑"
        if event.event_type == EVENT_HEALTH and payload.get("ok") is False:
            return "paused", "健康检查未通过，已停在最近一致状态"
        if event.event_type == EVENT_RUN_FAILED:
            return "failed", "最近一次运行未完成，请查看运行记录"
        if event.event_type == EVENT_RUN_UNCERTAIN:
            return "failed", "最近一次运行状态不确定，请先核对运行记录"
        if event.event_type == EVENT_AGENT_STEP:
            if payload.get("ask"):
                return "awaiting_user", "等待你补充研究信息"
            break
    if any(event.event_type == EVENT_RUN_SUCCEEDED for event in events):
        return "idle", "最近一次运行已完成"
    if events:
        return "idle", "等待你的下一条研究指示"
    return "idle", "等待你的第一条研究问题"


def _conversation(events: list[Event]) -> list[dict[str, Any]]:
    """Project durable conversation/replay blocks from append-only events."""

    messages: list[dict[str, Any]] = []
    for event in events:
        payload = event.payload or {}
        base = {"seq": event.seq, "phase": event.phase, "created_at": event.created_at}
        if event.event_type == EVENT_USER:
            messages.append({**base, "role": "user", "kind": "text", "text": str(payload.get("text") or "")})
        elif event.event_type == EVENT_AGENT_STEP:
            ask = str(payload.get("ask") or "")
            reply = str(payload.get("reply") or "")
            summary = str(payload.get("decision_summary") or "")
            acts = payload.get("acts") if isinstance(payload.get("acts"), list) else []
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "agent",
                    "text": reply or ask or summary or "Agent 已完成一轮判断。",
                    "reply": reply,
                    "summary": summary,
                    "ask": ask,
                    "stop_reason": payload.get("stop_reason"),
                    "acts": [
                        {"act_type": str(a.get("act_type") or ""), "reason": str(a.get("reason") or "")}
                        for a in acts
                        if isinstance(a, dict)
                    ][:12],
                }
            )
        elif event.event_type == EVENT_APPROVAL_REQ:
            act = str(payload.get("act") or "")
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "approval",
                    "request_id": str(payload.get("request_id") or ""),
                    "act": act,
                    "gate_kind": "permission_gate" if act in _PERMISSION_ACTS else "research_gate",
                    "reason": str(payload.get("reason") or ""),
                    "note": str(payload.get("note") or ""),
                    "target": payload.get("target") if isinstance(payload.get("target"), dict) else {},
                }
            )
        elif event.event_type in {EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REJECT}:
            messages.append(
                {
                    **base,
                    "role": "system",
                    "kind": "approval_decision",
                    "request_id": str(payload.get("request_id") or ""),
                    "decision": "approved" if event.event_type == EVENT_APPROVAL_GRANT else "rejected",
                    "note": str(payload.get("note") or ""),
                }
            )
        elif event.event_type == EVENT_RUN_SUCCEEDED:
            provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
            machine = payload.get("machine") if isinstance(payload.get("machine"), dict) else {}
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "run",
                    "status": "succeeded",
                    "run_id": str(payload.get("run_id") or payload.get("result_id") or ""),
                    "machine": machine,
                    "provenance": provenance,
                    "text": "运行完成，机器层结果已签入证据链。",
                }
            )
        elif event.event_type in {EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN}:
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "error",
                    "status": "uncertain" if event.event_type == EVENT_RUN_UNCERTAIN else "failed",
                    "run_id": str(payload.get("run_id") or payload.get("result_id") or ""),
                    "text": "主回归未完成；请查看最近运行记录，再决定从停点续跑还是调整口径。",
                    "detail": str(payload.get("error") or payload.get("reason") or "")[:800],
                }
            )
        elif event.event_type == EVENT_BUDGET:
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "error",
                    "status": "paused",
                    "text": "本轮已达到预算上限，研究状态停在最近一致状态。",
                    "detail": "可从停点续跑，或发送新的研究口径。",
                }
            )
        elif event.event_type == EVENT_HEALTH and payload.get("ok") is False:
            messages.append(
                {
                    **base,
                    "role": "assistant",
                    "kind": "error",
                    "status": "paused",
                    "text": "健康检查未通过，自动推进已暂停。",
                    "detail": str(payload.get("issues") or "请查看 Trace 中的健康事件。"),
                }
            )
    return messages


def _summary(store: SQLiteStore, idea: str = _IDEA) -> dict[str, Any]:
    events = _event_list(store, idea)
    proj = store.project(idea)
    rs = proj.research_state
    approvals = _approval_records(events)
    run_status, status_detail = _run_state(events, approvals)
    first_idea = next((e for e in events if e.event_type == EVENT_IDEA), None)
    idea_title = str((first_idea.payload if first_idea else {}).get("question") or "未命名研究")
    current_phase = proj.phase
    if not current_phase:
        phase_events = _phase_history(events, None)
        current_phase = phase_events[-1] if phase_events else ("IDEA" if events else None)
    phase_history = _phase_history(events, proj.phase)
    claims = sorted((c.claim_id, c.status, c.statement[:80]) for c in proj.claims.values())
    claim_records = [
        {**claim.model_dump(), "citation_count": len(claim.cards), "card_ids": list(claim.cards)}
        for claim in sorted(proj.claims.values(), key=lambda item: item.claim_id)
    ]
    card_records = [card.model_dump() for card in sorted(proj.cards.values(), key=lambda item: item.card_id)]
    run_records = [{"run_id": run_id, **record.model_dump()} for run_id, record in sorted(proj.runs.items())]
    checkpoint_event = next((e for e in reversed(events) if e.event_type == "checkpoint.snapshot"), None)
    workspace = next((item for item in _workspace_entries(store) if item["id"] == idea), None)
    workspace = workspace or {
        "id": idea,
        "name": idea,
        "events": len(events),
        "run_status": run_status,
        "pending_approvals": len([item for item in approvals if item["status"] == "pending"]),
    }
    return {
        # Existing v0 fields; keep their shape for older clients.
        "phase": proj.phase,
        "spec": rs.current_spec_id if rs else None,
        "claims": claims,
        "cards": sorted(proj.cards),
        "runs": {k: v.status for k, v in sorted(proj.runs.items())},
        "events": len(events),
        "workspace_id": idea,
        "workspace_name": workspace["name"],
        "workspace": workspace,
        # Read-only UI projection fields.
        "idea_title": idea_title,
        "privacy_mode": _privacy_mode(),
        "local_strict": _privacy_mode() == "local_strict",
        "config": _config_info(),
        "display_phase": current_phase,
        "phase_history": phase_history,
        "phase_labels": _PHASE_LABELS,
        "run_status": run_status,
        "status_detail": status_detail,
        "research_state": {
            "sample_sig": rs.sample_sig if rs else None,
            "dependent_variable": None,
            "core_explanatory_variable": None,
            "controls": [],
            "fixed_effects": None,
            "cluster_level": None,
            "identification_strategy": None,
            "current_spec_id": rs.current_spec_id if rs else None,
            "current_family_id": rs.current_family_id if rs else None,
        },
        "claim_records": claim_records,
        "card_records": card_records,
        "run_records": run_records,
        "pending_approvals": [item for item in approvals if item["status"] == "pending"],
        "approvals": approvals,
        "messages": _conversation(events),
        "checkpoint": {"available": checkpoint_event is not None, "seq": checkpoint_event.seq if checkpoint_event else None},
        "draft_ready": bool(any(c.status == "supported" for c in proj.claims.values())),
        "capabilities": {
            "approvals": True,
            "approval_revision": False,
            "resume": True,
            "goal_mode": True,
            "stop": False,
            "draft": True,
            "cursor_events": True,
        },
        "health": {"ok": run_status not in {"failed", "paused"}, "detail": status_detail},
        "updated_at": events[-1].created_at if events else None,
    }


def _draft_response(store: SQLiteStore, *, require_ready: bool = False, idea: str = _IDEA) -> StreamingResponse:
    from .writer.draft_multi import draft_from_ledger

    proj = store.project(idea)
    if require_ready and not any(c.status == "supported" for c in proj.claims.values()):
        raise HTTPException(status_code=409, detail="尚未有已确认的证据结论，暂不能生成 Word 初稿。")
    method = ("本研究用双重差分（DID）识别：结果变量对 处理组×期后 交互回归，"
              "标准误聚类到处理相关层级；样本为双期店面（长表）。")
    limits = ("数据仅两期，无法做事件研究/动态效应；平行趋势改用基线特征平衡与多个"
              "稳健性口径替代，并在解读时保留因果表述的审慎。")
    if proj.runs:
        data = draft_from_ledger(proj, method=method, limits=limits)
    else:
        paragraphs: list[tuple[str, object]] = []
        for claim in sorted(proj.claims.values(), key=lambda c: c.claim_id):
            paragraphs.append((render_claim_sentence(claim, proj.cards), claim))
        data = claims_to_docx("实证研究初稿（自动渲染）", paragraphs).getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": "attachment; filename=draft.docx"},
    )


def _last_seq(store: SQLiteStore, idea: str = _IDEA) -> int:
    return max((event.seq or 0 for event in store.scan(idea)), default=0)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Serve the local, dependency-free UI document."""

    return _INDEX_FILE.read_text(encoding="utf-8")


class WorkspaceIn(BaseModel):
    """Payload for creating a local workspace; no ledger event is written."""

    name: str = "新研究工作区"
    id: str | None = None


@app.get("/api/workspaces")
def workspaces():
    """List the local workspace registry plus read-only ledger counters."""

    s = _store()
    try:
        return {"items": _workspace_entries(s), "active": _IDEA}
    finally:
        s.close()


@app.post("/api/workspaces")
def create_workspace(body: WorkspaceIn):
    """Create a workspace in the transparent local registry.

    The registry is metadata only. Research state still enters the event
    ledger through ``/api/chat`` and runner/approval routes.
    """

    name = " ".join(str(body.name or "").split())
    if not name:
        raise HTTPException(status_code=422, detail="工作区名称不能为空。")
    if len(name) > 120:
        raise HTTPException(status_code=422, detail="工作区名称不能超过 120 个字符。")
    requested_id = body.id.strip() if isinstance(body.id, str) else ""
    if requested_id:
        idea = _validate_workspace_id(requested_id)
    else:
        idea = _workspace_slug(name)
    with _WORKSPACE_REGISTRY_LOCK:
        rows = _read_workspace_registry()
        used = {row["id"] for row in rows}
        if idea in used:
            if requested_id:
                raise HTTPException(status_code=409, detail="该工作区标识已经存在。")
            stem = idea[:52]
            suffix = 2
            while f"{stem}-{suffix}" in used:
                suffix += 1
            idea = f"{stem}-{suffix}"
        row = {"id": idea, "name": name, "created_at": int(time.time() * 1000)}
        rows.append(row)
        _write_workspace_registry(rows)
    return {"workspace": {**row, "events": 0, "last_seq": None, "run_status": "idle", "pending_approvals": 0},
            "items": rows,
            "active": idea}


@app.get("/api/state")
def state(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return _summary(s, idea)
    finally:
        s.close()


@app.get("/api/events")
def events(limit: int = 300, before_seq: int | None = None, ws: str = _IDEA):
    """Return compact trace rows, with optional backward cursor pagination.

    The original v0 response is a list. Supplying ``before_seq`` opts into
    the richer ``{items, next_before_seq}`` shape so old clients remain valid.
    """
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        rows = [_public_event(event) for event in _event_list(s, idea)]
        limit = max(1, min(int(limit), 500))
        if before_seq is None:
            return rows[-limit:]
        older = [row for row in rows if row.get("seq") is not None and int(row["seq"]) < before_seq]
        page = older[-limit:]
        next_before = page[0]["seq"] if len(page) == limit else None
        return {"items": page, "next_before_seq": next_before}
    finally:
        s.close()


@app.get("/api/trace")
def trace(
    limit: int = 50,
    before_seq: int | None = None,
    search: str | None = None,
    type: str | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    phase: str | None = None,
    ws: str = _IDEA,
):
    """Return a single-page trace projection with search/filter/cursor support."""

    idea = _resolve_workspace(ws)
    s = _store()
    try:
        rows = [_public_event(event) for event in _event_list(s, idea)]
        requested_type = (event_type or type or "").strip().lower()
        requested_actor = (actor or "").strip().lower()
        requested_phase = (phase or "").strip().lower()
        requested_search = (search or "").strip().lower()

        def matches(row: dict[str, Any]) -> bool:
            if requested_type and str(row.get("type") or "").lower() != requested_type:
                return False
            if requested_actor and str(row.get("actor") or "").lower() != requested_actor:
                return False
            if requested_phase and str(row.get("phase") or "").lower() != requested_phase:
                return False
            if requested_search:
                haystack = " ".join(
                    str(row.get(key) or "")
                    for key in ("seq", "type", "actor", "phase", "object", "summary", "op", "payload_preview")
                ).lower()
                if requested_search not in haystack:
                    return False
            return True

        filtered = [row for row in rows if matches(row)]
        ordered = list(reversed(filtered))
        if before_seq is not None:
            ordered = [row for row in ordered if row.get("seq") is not None and int(row["seq"]) < int(before_seq)]
        safe_limit = max(1, min(int(limit), 500))
        page = ordered[:safe_limit]
        next_before = page[-1]["seq"] if len(page) == safe_limit and page else None
        return {
            "items": page,
            "next_before_seq": next_before,
            "total": len(filtered),
            "workspace": idea,
        }
    finally:
        s.close()


class ChatIn(BaseModel):
    text: str
    mode: str = "interactive"  # interactive=提问即停；goal=自动跑到需你决定/硬停才停


_GREETINGS = {"你好", "您好", "hello", "hi", "hey", "在吗", "在么", "喂", "早上好", "晚上好", "哈喽"}


def _clean(text: str) -> str:
    return "".join(ch for ch in text.strip().lower() if ch.isalnum())


def _is_greeting(text: str) -> bool:
    c = _clean(text)
    return c in _GREETINGS or (len(c) <= 4 and any(g in c for g in ("你好", "hello", "hi")))


_RESEARCH_HINTS = ("研究", "回归", "双重差分", "did", "验证", "检验", "估计", "机制",
                   "稳健", "异质性", "异质", "面板", "文献", "假设", "样本", "数据",
                   "主回归", "跑一下", "帮我跑", "spec", "论文", "初稿", "可行")


def _is_research(text: str) -> bool:
    """离线兜底：只有明确提到研究动词/对象才算 research（有 LLM 时用意图识别替代）。"""
    low = text.lower()
    return any(h in low for h in _RESEARCH_HINTS)


def _classify_intent(provider, text: str, history: list[dict]) -> str:
    """意图识别（先于一切路由）：research=要用数据/文献做实证分析/跑回归/出初稿；
    其余（闲聊/解释/写东西/问问题…）一律 chat。无 LLM 时退回关键词兜底。"""
    if hasattr(provider, "chat"):
        sys_msg = (
            "你是路由器。判断下一条用户消息意图，只输出一个词：research 或 chat。\n"
            "research = 用户明确要你针对数据做实证分析/跑回归/写论文初稿/验证某个假设/做研究计划。\n"
            "chat = 其余一切：寒暄、提问、解释概念、写代码/文字、闲聊，哪怕提到数据或研究相关词。\n"
            "只输出 research 或 chat，不要解释。"
        )
        msgs = [{"role": "system", "content": sys_msg}]
        msgs.extend(history[-6:])
        if not msgs or msgs[-1].get("role") != "user":
            msgs.append({"role": "user", "content": text})
        try:
            out = provider.chat(msgs).strip().lower()
            if "research" in out:
                return "research"
            return "chat"
        except Exception:  # noqa: BLE001
            pass
    return "research" if _is_research(text) else "chat"


def _chat_reply(provider, messages: list[dict]) -> str:
    """普通聊天回复：直接调 LLM.chat；无 chat 能力的 provider 走演示文案。"""
    if hasattr(provider, "chat"):
        try:
            reply = provider.chat(messages)
            if isinstance(reply, str) and reply.strip():
                return reply.strip()
        except Exception as e:  # noqa: BLE001
            return f"（这次没接上模型，稍后再试。细节：{str(e)[:120]}）"
    return "（演示模式：这里会是我的自然回复。配好 LLM key 后就是正常对话。）"


def _history_messages(store, idea: str, limit: int = 12) -> list[dict]:
    """把账本里的对话投影成 openai 消息（普通聊天用；研究动作不进历史太多）。"""
    out: list[dict] = []
    for e in store.scan(idea):
        p = e.payload or {}
        if e.event_type == "user.message":
            out.append({"role": "user", "content": str(p.get("text") or "")})
        elif e.event_type == "agent_step" and not p.get("research"):
            content = str(p.get("ask") or p.get("decision_summary") or "")
            if content:
                out.append({"role": "assistant", "content": content})
    return out[-limit:]


def _onboarding(*, has_idea: bool, mode: str) -> str:
    base = ("你好，我是你的研究 agent——先当普通对话用，随便聊都行；"
            "想让我做研究时再给研究问题 + 数据（例如：验证最低工资对就业的影响，数据见…）。")
    tip = ("\n请这样给一句，例如：\n"
           "· 想验证『最低工资提高会减少快餐店就业吗』，数据在 …（长表：店×期，含处理组/期后/结果列）\n"
           "· 或先问我能做什么 / 看右侧状态。")
    if has_idea:
        base += ("\n注意：当前研究问题还是空的（可能之前只说了寒暄）。"
                 "请重开一个新 idea 或直接给出真实研究问题与数据，我会从建档开始。")
    mode_note = f"\n当前为「{('目标' if mode=='goal' else '交互')}」模式。" if mode else ""
    return base + tip + mode_note


class ApproveIn(BaseModel):
    request_id: str
    decision: str  # approve | reject
    note: str = ""


class ApprovalDecisionIn(BaseModel):
    decision: str
    note: str = ""


def _decision(store: SQLiteStore, request_id: str, decision: str, note: str, idea: str = _IDEA) -> dict[str, Any]:
    records = {item["request_id"]: item for item in _approval_records(_event_list(store, idea))}
    record = records.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="审批请求不存在或已被清理。")
    if record["status"] != "pending":
        raise HTTPException(status_code=409, detail="该审批请求已经有决定，不能重复提交。")
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=501, detail="当前后端只支持批准或拒绝；请通过对话提出修改要求。")
    event_kind = runner_approve(store, request_id, decision=decision, note=note, idea=idea)
    return {
        "decision": "approved" if decision == "approve" else "rejected",
        "event": event_kind,
        "event_seq": _last_seq(store, idea),
        "state": _summary(store, idea),
    }


@app.get("/api/approvals")
def approvals(status: str = "pending", ws: str = _IDEA):
    """List durable approval requests; ``status=all`` restores the audit trail."""

    idea = _resolve_workspace(ws)
    s = _store()
    try:
        records = _approval_records(_event_list(s, idea))
        if status != "all":
            records = [item for item in records if item["status"] == "pending"]
        return {"pending": records} if status != "all" else {"items": records, "pending": [i for i in records if i["status"] == "pending"]}
    finally:
        s.close()


@app.post("/api/approve")
def approve(body: ApproveIn, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        result = _decision(s, body.request_id, body.decision, body.note, idea)
        # Keep the original small response fields while adding the richer state.
        return {"ok": True, "event": result["event"], "event_seq": result["event_seq"], "state": result["state"]}
    finally:
        s.close()


@app.post("/api/approvals/{request_id}/decision")
def approval_decision(request_id: str, body: ApprovalDecisionIn, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return _decision(s, request_id, body.decision, body.note, idea)
    finally:
        s.close()


def _resume_result(idea: str = _IDEA) -> dict[str, Any]:
    s = _store()
    try:
        _demo_seed(s, idea)
        res = run_until_gate(s, idea, None, _provider(), executor=_executor(s), max_steps=4)
        snapshot = _summary(s, idea)
        return {"reply": res[-1].reply if res else "", "status": snapshot["run_status"], "state": snapshot}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="续跑未完成：请查看 Trace 中的最近错误。") from e
    finally:
        s.close()


@app.post("/api/resume")
def resume(ws: str = _IDEA):
    """Continue from the current durable projection without new input."""

    return _resume_result(_resolve_workspace(ws))


@app.post("/api/control/resume")
def control_resume(ws: str = _IDEA):
    return _resume_result(_resolve_workspace(ws))


@app.post("/api/control/stop")
def control_stop(ws: str = _IDEA):
    """Explicitly report the missing cancellation primitive; never fake success."""

    _resolve_workspace(ws)
    raise HTTPException(status_code=501, detail="当前后端尚未提供安全的运行中断控制；请等待当前请求返回。")


@app.get("/api/draft")
def draft(ws: str = _IDEA):
    """Legacy download endpoint; retains the v0 empty-draft behavior."""

    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return _draft_response(s, require_ready=False, idea=idea)
    finally:
        s.close()


@app.get("/api/draft.docx")
def draft_docx(ws: str = _IDEA):
    """Strict download endpoint used by the new UI."""

    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return _draft_response(s, require_ready=True, idea=idea)
    finally:
        s.close()


@app.get("/api/claims")
def claims(status: str | None = None, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        records = _summary(s, idea)["claim_records"]
        if status:
            records = [record for record in records if record.get("status") == status]
        return {"items": records}
    finally:
        s.close()


@app.get("/api/cards")
def cards(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return {"items": _summary(s, idea)["card_records"]}
    finally:
        s.close()


@app.get("/api/cards/{card_id}")
def card(card_id: str, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        proj = s.project(idea)
        item = proj.cards.get(card_id)
        if item is None:
            raise HTTPException(status_code=404, detail="证据卡不存在。")
        record = item.model_dump()
        run_id = item.locator.get("run_id") if isinstance(item.locator, dict) else None
        run = proj.runs.get(run_id) if run_id else None
        provenance = (run.provenance if run else {}) or {}
        record.update(
            {
                "run_id": run_id,
                "run": run.model_dump() if run else None,
                "provenance": provenance,
                "traceability_complete": bool(run_id and run),
            }
        )
        return record
    finally:
        s.close()


@app.get("/api/runs")
def runs(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        return {"items": _summary(s, idea)["run_records"]}
    finally:
        s.close()


@app.get("/api/runs/{run_id}")
def run(run_id: str, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        record = s.project(idea).runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="运行记录不存在。")
        return {"run_id": run_id, **record.model_dump()}
    finally:
        s.close()


@app.get("/api/health")
def health(ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    s = _store()
    try:
        summary = _summary(s, idea)
        return {
            "ok": bool(summary["health"]["ok"]),
            "local_strict": True,
            "status": summary["run_status"],
            "detail": summary["status_detail"],
        }
    finally:
        s.close()


def _run_chat_sync(idea: str, text: str, mode: str, on_event=None):
    """跑一轮 chat 的核心（同步、阻塞），返回 (reply, ask, state_summary)。"""
    s = _store()
    try:
        bootstrap_idea(s, idea, text)
        _touch_workspace_name(idea, text)
        s.append(Event(idea_id=idea, event_type=EVENT_USER, actor=ACTOR_USER, source=ACTOR_USER,
                       payload={"text": text}))

        from .harness.agent_loop import run_loop
        from .toolkit import ToolContext, default_tools

        provider = _provider()
        pm = _privacy_mode()
        ctx = ToolContext(
            idea=idea, store=s, executor=_executor(s),
            rag=_rag(), memory=_memory(),
            run_root=(DEFAULT_DB.parent / "runs"),
            privacy_mode=pm,
            network_available=(pm != "local_strict"),
            phase=s.project(idea).phase,
        )
        res = run_loop(s, provider, default_tools(), ctx, user_text=text,
                       privacy_mode=pm, skills=_matched_skills(text), on_event=on_event)
        reply = res.ask or res.reply or ""
        return reply, res.ask, _summary(s, idea)
    finally:
        s.close()


@app.post("/api/chat")
def chat(body: ChatIn, ws: str = _IDEA):
    idea = _resolve_workspace(ws)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="消息不能为空。")
    if len(text) > 20000:
        raise HTTPException(status_code=413, detail="消息过长，请拆成几条研究指示。")
    if body.mode not in {"interactive", "goal"}:
        raise HTTPException(status_code=422, detail="mode 必须是 interactive|goal。")
    try:
        reply, ask, state = _run_chat_sync(idea, text, body.mode)
        return {"reply": reply, "mode": body.mode, "ask": ask, "state": state}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="本轮未完成：请查看 Trace 中的最近事件。") from e


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn, ws: str = _IDEA):
    """SSE 真流式：LLM 边生成 token 边转发，工具调用发 tool_started/tool_completed。"""
    import asyncio
    import json as _json
    import queue as _queue

    idea = _resolve_workspace(ws)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="消息不能为空。")
    if len(text) > 20000:
        raise HTTPException(status_code=413, detail="消息过长，请拆成几条研究指示。")
    if body.mode not in {"interactive", "goal"}:
        raise HTTPException(status_code=422, detail="mode 必须是 interactive|goal。")

    def sse(obj: dict) -> str:
        return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"

    async def gen():
        yield sse({"type": "start"})
        evq: _queue.Queue = _queue.Queue()

        def on_event(ev):
            evq.put(ev)

        def run():
            try:
                return _run_chat_sync(idea, text, body.mode, on_event=on_event)
            except Exception as e:  # noqa: BLE001
                return None, None, {"__error__": str(e)[:200]}

        task = asyncio.create_task(asyncio.to_thread(run))
        # 消费统一事件直到 loop 完成
        while True:
            if task.done():
                reply, ask, state = task.result()
                if state and "__error__" in state:
                    yield sse({"type": "error", "detail": state["__error__"]})
                else:
                    yield sse({"type": "done", "ask": ask, "state": state})
                break
            drained = False
            while True:
                try:
                    ev = evq.get_nowait()
                except _queue.Empty:
                    break
                drained = True
                if ev["type"] == "text_delta":
                    yield sse({"type": "token", "text": ev["text"]})
                elif ev["type"] == "tool_started":
                    yield sse({"type": "tool_started", "name": ev["name"]})
                elif ev["type"] == "tool_completed":
                    yield sse({"type": "tool_completed", "name": ev["name"], "ok": ev.get("ok")})
            if not drained:
                await asyncio.sleep(0.02)

    return StreamingResponse(gen(), media_type="text/event-stream")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STATA_AGENT_UI_PORT", "8001")))


_HTML = _INDEX_FILE.read_text(encoding="utf-8") if _INDEX_FILE.exists() else ""


if __name__ == "__main__":
    main()
