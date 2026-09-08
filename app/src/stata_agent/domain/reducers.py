"""reducer：把事件流 fold 成 Projection（DD-01 §3.3/§5）。

纯函数、只读事件；非法事件序列在这里抛错（reducer 是守门员）。
切片 0 覆盖：idea/phase/spec/card/claim/run 执行链。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..events.append import validate_writer
from ..events.schema import (
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_RETRACT,
    EVENT_CLAIM_SIGNED,
    EVENT_FILES_DEL,
    EVENT_IDEA,
    EVENT_MAIN_RESULT,
    EVENT_PHASE,
    EVENT_RUN_FAILED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_SPEC_FREEZE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    Event,
)
from .models import Claim, EvidenceCard, ResearchState, RunRecord


class IllegalEventSequence(Exception):
    """非法事件序列（reducer 拒绝，不做静默修复）。"""


@dataclass
class Projection:
    """fold 结果。

    ``pending`` exposes the compact state used by health checks.  The other
    maps are reducer-only bindings rebuilt from the event stream; they prevent
    an operation from being rebound to another run/attempt or call.
    """

    idea_id: str
    phase: Optional[str] = None
    research_state: Optional[ResearchState] = None
    claims: dict[str, Claim] = field(default_factory=dict)
    cards: dict[str, EvidenceCard] = field(default_factory=dict)
    runs: dict[str, RunRecord] = field(default_factory=dict)
    families: dict[str, list[str]] = field(default_factory=dict)
    # op_id -> requested/called/result/uncertain/done
    pending: dict[str, str] = field(default_factory=dict)
    # op_id -> (run_id, attempt_id), fixed by run.requested
    run_bindings: dict[str, tuple[str, int]] = field(default_factory=dict)
    side_effects: dict[str, str] = field(default_factory=dict)
    call_counts: dict[str, int] = field(default_factory=dict)
    last_call_ids: dict[str, str] = field(default_factory=dict)
    call_ids: dict[str, set[str]] = field(default_factory=dict)
    terminal_ops: set[str] = field(default_factory=set)

    def summary(self) -> tuple:
        """稳定摘要：用于确定性重放/审计比较（忽略 seq/created_at/pending）。"""
        claims = tuple(sorted((c.claim_id, c.status) for c in self.claims.values()))
        cards = tuple(sorted(self.cards))
        runs = tuple(sorted((r.run_id, r.status) for r in self.runs.values()))
        refs = tuple(sorted(self.research_state.evidence_refs)) if self.research_state else ()
        return (
            self.idea_id,
            self.phase,
            self.research_state.current_spec_id if self.research_state else None,
            claims,
            cards,
            runs,
            refs,
        )


def empty(idea_id: str) -> Projection:
    return Projection(
        idea_id=idea_id,
        research_state=ResearchState(idea_id=idea_id),
    )


def _copy_projection(proj: Projection, *, rs: ResearchState | None = None) -> Projection:
    """Copy containers before applying an event (important for append preflight)."""
    return Projection(
        idea_id=proj.idea_id,
        phase=proj.phase,
        research_state=rs if rs is not None else (
            proj.research_state.model_copy(deep=True) if proj.research_state else None
        ),
        claims=dict(proj.claims),
        cards=dict(proj.cards),
        runs=dict(proj.runs),
        families={k: list(v) for k, v in proj.families.items()},
        pending=dict(proj.pending),
        run_bindings=dict(proj.run_bindings),
        side_effects=dict(proj.side_effects),
        call_counts=dict(proj.call_counts),
        last_call_ids=dict(proj.last_call_ids),
        call_ids={k: set(v) for k, v in proj.call_ids.items()},
        terminal_ops=set(proj.terminal_ops),
    )


def _operation(ev: Event, payload: dict, kind: str) -> str:
    op = ev.operation_id or payload.get("operation_id")
    if not isinstance(op, str) or not op.strip():
        raise IllegalEventSequence(f"{kind} 缺 operation_id")
    if ev.operation_id and payload.get("operation_id") and payload["operation_id"] != ev.operation_id:
        raise IllegalEventSequence(f"{kind}: event/payload operation_id 不一致")
    return op


def _run_id(payload: dict, kind: str) -> str:
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise IllegalEventSequence(f"{kind} 缺 run_id")
    return run_id


def _assert_binding(proj: Projection, ev: Event, payload: dict, op: str, kind: str) -> tuple[str, int]:
    binding = proj.run_bindings.get(op)
    if binding is None:
        raise IllegalEventSequence(f"{kind}: 无对应 run.requested(op={op!r})")
    run_id = _run_id(payload, kind)
    expected_run, expected_attempt = binding
    if run_id != expected_run:
        raise IllegalEventSequence(
            f"{kind}: op={op!r} 已绑定 run_id={expected_run!r}，收到 {run_id!r}"
        )
    # 0 is Event's historical default meaning "not supplied"; explicit
    # attempts must match the request's immutable attempt.
    if ev.attempt_id not in (0, expected_attempt):
        raise IllegalEventSequence(
            f"{kind}: op={op!r} 已绑定 attempt={expected_attempt}，收到 {ev.attempt_id}"
        )
    return expected_run, expected_attempt


def _assert_not_terminal(proj: Projection, op: str, kind: str) -> str:
    state = proj.pending.get(op)
    if state is None:
        raise IllegalEventSequence(f"{kind}: 无对应 start(op={op!r})")
    if op in proj.terminal_ops or state in {"done", "uncertain"}:
        raise IllegalEventSequence(f"{kind}: op={op!r} 已终结，禁止重开/重复 terminal")
    return state


def apply(proj: Projection, ev: Event) -> Projection:
    """单步 reducer（纯函数）：返回 new Projection。未知事件类型忽略。"""
    if ev.idea_id != proj.idea_id:
        raise IllegalEventSequence(
            f"event idea_id={ev.idea_id!r} 与 projection idea_id={proj.idea_id!r} 不一致"
        )
    kind = ev.event_type
    p = ev.payload if isinstance(ev.payload, dict) else {}
    validate_writer(ev)
    rs = proj.research_state.model_copy(deep=True) if proj.research_state else ResearchState(idea_id=proj.idea_id)
    out = _copy_projection(proj, rs=rs)

    if kind == EVENT_IDEA:
        return out

    if kind == EVENT_PHASE:
        out.phase = p.get("to", p.get("phase"))
        out.research_state = rs.model_copy(update={"phase": out.phase})
        return out

    if kind == EVENT_SPEC_FREEZE:
        out.research_state = rs.model_copy(
            update={"current_spec_id": p.get("spec_id") or rs.current_spec_id}
        )
        return out

    if kind == EVENT_MAIN_RESULT:
        fam = p.get("family_id") or rs.current_family_id
        if fam:
            members = list(out.families.get(fam, []))
            if p.get("run_id") and p["run_id"] not in members:
                members.append(p["run_id"])
            out.families[fam] = members
        return out

    if kind == EVENT_CARD_SIGNED:
        card_payload = p.get("card", p)
        if not isinstance(card_payload, dict):
            raise IllegalEventSequence("card payload 必须是 object")
        card = EvidenceCard.model_validate(card_payload)
        if card.card_id in out.cards:
            raise IllegalEventSequence(f"card {card.card_id!r} 已存在（只 append）")
        out.cards[card.card_id] = card
        return out

    if kind == EVENT_CLAIM_SIGNED:
        claim_payload = p.get("claim", p)
        if not isinstance(claim_payload, dict):
            raise IllegalEventSequence("claim payload 必须是 object")
        claim = Claim.model_validate(claim_payload)
        missing = [card_id for card_id in claim.cards if card_id not in out.cards]
        if missing:
            raise IllegalEventSequence(f"claim 引用不存在的 card: {missing}")
        if not claim.claim_id:
            raise IllegalEventSequence("claim_id 为空")
        if claim.claim_id in out.claims:
            raise IllegalEventSequence(f"claim {claim.claim_id!r} 已存在（只 append）")
        out.claims[claim.claim_id] = claim
        if claim.claim_id not in rs.evidence_refs:
            out.research_state = rs.model_copy(
                update={"evidence_refs": [*rs.evidence_refs, claim.claim_id]}
            )
        return out

    if kind == EVENT_CLAIM_RETRACT:
        cid = p.get("claim_id", "")
        if cid not in out.claims:
            raise IllegalEventSequence(f"retract 不存在的 claim {cid!r}")
        old = out.claims[cid]
        if old.status == "retracted":
            raise IllegalEventSequence(f"claim {cid!r} 已撤回，禁止重复 retract")
        out.claims[cid] = old.model_copy(
            update={"status": "retracted", "superseded_by": p.get("superseded_by")}
        )
        return out

    if kind == EVENT_RUN_REQ:
        op = _operation(ev, p, kind)
        if op in out.run_bindings:
            raise IllegalEventSequence(f"run.requested: op={op!r} 已存在，terminal 后也禁止重开")
        run_id = _run_id(p, kind)
        if run_id in out.runs:
            raise IllegalEventSequence(f"run_id={run_id!r} 已绑定其他 operation")
        attempt = ev.attempt_id if ev.attempt_id > 0 else 1
        side_effect = p.get("side_effect", "read")
        if side_effect not in {"read", "write"}:
            raise IllegalEventSequence(f"run.requested: 非法 side_effect={side_effect!r}")
        semantic_hash = p.get("semantic_input_hash")
        if ev.fingerprint and semantic_hash and ev.fingerprint != semantic_hash:
            raise IllegalEventSequence("run.requested: fingerprint 与 semantic_input_hash 不一致")
        out.run_bindings[op] = (run_id, attempt)
        out.side_effects[op] = side_effect
        out.pending[op] = "requested"
        out.call_counts[op] = 0
        out.call_ids[op] = set()
        out.runs[run_id] = RunRecord(
            run_id=run_id,
            operation_id=op,
            attempt_id=attempt,
            semantic_input_hash=ev.fingerprint or semantic_hash,
            side_effect=side_effect,
            status="running",
        )
        return out

    if kind == EVENT_TOOL_CALL:
        op = _operation(ev, p, kind)
        _assert_binding(out, ev, p, op, kind)
        state = _assert_not_terminal(out, op, kind)
        if state not in {"requested", "result"}:
            raise IllegalEventSequence(
                f"{kind}: op={op!r} 当前状态={state!r}，需要 requested 或 result"
            )
        call_id = p.get("call_id") or f"{op}:call:{out.call_counts.get(op, 0) + 1}"
        if not isinstance(call_id, str) or not call_id.strip():
            raise IllegalEventSequence(f"{kind}: 缺 call_id")
        if call_id in out.call_ids.setdefault(op, set()):
            raise IllegalEventSequence(f"{kind}: op={op!r} 重复 call_id={call_id!r}")
        out.call_ids[op].add(call_id)
        out.last_call_ids[op] = call_id
        out.pending[op] = "called"
        return out

    if kind == EVENT_TOOL_RESULT:
        op = _operation(ev, p, kind)
        _assert_binding(out, ev, p, op, kind)
        state = _assert_not_terminal(out, op, kind)
        if state != "called":
            raise IllegalEventSequence(f"{kind}: op={op!r} 当前状态={state!r}，需要先 tool.call")
        call_id = p.get("call_id") or out.last_call_ids.get(op)
        expected_call = out.last_call_ids.get(op)
        if call_id != expected_call:
            raise IllegalEventSequence(
                f"{kind}: op={op!r} call_id={call_id!r} 与最近 call={expected_call!r} 不一致"
            )
        out.call_counts[op] = out.call_counts.get(op, 0) + 1
        out.pending[op] = "result"
        return out

    if kind in (EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN):
        op = _operation(ev, p, kind)
        run_id, _attempt = _assert_binding(out, ev, p, op, kind)
        state = _assert_not_terminal(out, op, kind)
        if kind in (EVENT_RUN_SUCCEEDED, EVENT_RUN_UNCERTAIN) and state != "result":
            raise IllegalEventSequence(
                f"{kind}: op={op!r} 必须在至少一组 tool.call/tool.result 后终结（当前={state!r}）"
            )
        if kind == EVENT_RUN_FAILED and state not in {"requested", "result"}:
            raise IllegalEventSequence(
                f"{kind}: op={op!r} 不允许在未配对的 tool.call 后终结（当前={state!r}）"
            )
        old = out.runs.get(run_id)
        if old is None:
            raise IllegalEventSequence(f"{kind}: run_id={run_id!r} 无对应 run.requested")
        status = {
            EVENT_RUN_SUCCEEDED: "succeeded",
            EVENT_RUN_FAILED: "failed",
            EVENT_RUN_UNCERTAIN: "uncertain",
        }[kind]
        out.runs[run_id] = old.model_copy(update={
            "status": status,
            "provenance": p.get("provenance", {}),
            "machine": p.get("machine", {}),
        })
        out.terminal_ops.add(op)
        out.pending[op] = "uncertain" if status == "uncertain" else "done"
        return out

    if kind == EVENT_FILES_DEL:
        # 仅登记意图（显式清理），不真正删文件 —— 见 DD-01 rollback 边界
        return out

    # Other event types are forward-compatible.
    return out


def fold(events, idea_id: str | None = None) -> Projection:
    """把事件序列 fold 成投影；任一事件非法则抛 IllegalEventSequence。"""
    iterator = iter(events)
    if idea_id is None:
        try:
            first = next(iterator)
        except StopIteration:
            return empty("?")
        idea_id = first.idea_id
        proj = apply(empty(idea_id), first)
    else:
        proj = empty(idea_id)
    for ev in iterator:
        proj = apply(proj, ev)
    return proj


def unclosed_operations(proj: Projection) -> list[str]:
    """健康探针/恢复用：找出没有 terminal 的 execution（closure 检查，DD-01 §3.3-2）。"""
    return [op for op, st in proj.pending.items() if st != "done"]
