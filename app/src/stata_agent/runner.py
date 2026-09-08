"""runner：把已有件串成一次闭环（你说一句 → 提议 → 允许则执行 → 自动签卡 → 回数字）。

切片演示顺序建议（impl-plan）：1) mock 或真 provider 提议；2) act=request_run 且 policy ALLOW
→ 用 executor（真 Stata 或 FakeExecutor）执行并落 run 事件；3) evidence_signer 把机器层签成卡
+ claim；4) 回话含机器层数字。文件读取/检索(RAG)可另在 turn 前并入 context（后接）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import inspect

import uuid

from .domain.action import ActionProposal
from .events.schema import (
    EVENT_USER,
    EVENT_SPEC_FREEZE,
    EVENT_APPROVAL_REQ,
    EVENT_APPROVAL_GRANT,
    EVENT_APPROVAL_REJECT,
    ACTOR_ORCH,
    ACTOR_USER,
    Event,
)
from .harness.research_turn import research_turn
from .phase.phasedef import GateMode
from .providers.protocol import ProposalProvider
from .storage.sqlite_store import SQLiteStore
from .tools.evidence_signer import sign_run_numeric_cards
from .tools.executor import auto_regress_script

RUN_ACTS = {"request_run", "stata_run"}
SPEC_ACTS = {"propose_spec"}


@dataclass
class CycleResult:
    reply: str
    ran_run_id: str | None = None
    machine: dict = field(default_factory=dict)
    signed_cards: list[str] = field(default_factory=list)
    frozen_specs: list[str] = field(default_factory=list)
    asked: bool = False
    progressed: bool = False
    awaiting_approval: str | None = None  # request_id（policy 判 ASK 时挂起等学者）
    proposal: ActionProposal | None = None


def _request_approval(store, idea, act, dec, *, workspace_id: str | None = None) -> str:
    """ASK → 持久化 approval.requested（DD-01 #4）：学者决定前不执行。"""
    request_id = f"apr-{uuid.uuid4().hex[:8]}"
    store.append(Event(idea_id=idea, event_type=EVENT_APPROVAL_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                       payload={"request_id": request_id, "act": act.act_type,
                                "reason": dec.reason, "note": act.reason or "",
                                "target": act.target or {}, "workspace_id": workspace_id}))
    return request_id


def _remember_approved_note(
    memory,
    note: str,
    *,
    workspace_id: str | None = None,
    source_ids: tuple[str, ...] = (),
) -> None:
    """Write an approved note through either the V2 or legacy memory API.

    Approval is already durable once its ledger event has been appended.  A
    memory validation failure therefore must not turn a successful approval
    into a 500; the helper is intentionally best effort.  Signature
    inspection preserves the old ``remember_decision(memory, note)`` call for
    integrations that still provide the V1 helper.
    """

    if memory is None or not note:
        return
    try:
        from .memory.memstore import remember_decision

        parameters: Mapping[str, inspect.Parameter]
        try:
            parameters = inspect.signature(remember_decision).parameters
        except (TypeError, ValueError):
            parameters = {}
        supports_v2 = "workspace_id" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if supports_v2:
            decision_kwargs: dict[str, object] = {"workspace_id": workspace_id}
            if "source_ids" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                decision_kwargs["source_ids"] = source_ids
            elif "provenance" in parameters:
                decision_kwargs["provenance"] = source_ids
            remember_decision(
                memory,
                note,
                **decision_kwargs,
            )
        else:
            # A V2 store may be paired with an older helper during a rolling
            # upgrade.  Prefer its richer add API when available so the note
            # still carries workspace/provenance rather than silently falling
            # back to a global legacy record.
            add = getattr(memory, "add", None)
            add_parameters: Mapping[str, inspect.Parameter]
            try:
                add_parameters = inspect.signature(add).parameters if callable(add) else {}
            except (TypeError, ValueError):
                add_parameters = {}
            add_supports_v2 = "workspace_id" in add_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in add_parameters.values()
            )
            if add_supports_v2 and callable(add):
                add_kwargs: dict[str, object] = {
                    "kind": "decision",
                    "workspace_id": workspace_id,
                    "confidence": "explicit",
                }
                if "source_ids" in add_parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in add_parameters.values()
                ):
                    add_kwargs["source_ids"] = source_ids
                elif "provenance" in add_parameters:
                    add_kwargs["provenance"] = source_ids
                if "scope" in add_parameters:
                    add_kwargs["scope"] = "project"
                add(f"研究决定：{note}", **add_kwargs)
            else:
                remember_decision(memory, note)
    except Exception:
        # The grant/reject event remains the source of truth.  Memory is a
        # reusable constraint layer and cannot block the auditable decision.
        return


def approve(
    store: SQLiteStore,
    request_id: str,
    *,
    decision: str,
    note: str = "",
    idea: str = "i1",
    memory=None,
    workspace_id: str | None = None,
) -> str:
    """学者决定：approval.granted / rejected（持久化，可审计）。decision ∈ {approve,reject}。

    memory 可选：有 note 时记成"研究决定"记忆（约束后续，不作证据）。
    """
    kind = EVENT_APPROVAL_GRANT if decision == "approve" else EVENT_APPROVAL_REJECT
    if decision not in {"approve", "reject"}:
        raise ValueError("decision 必须是 approve|reject")
    source_ids = (request_id,)
    event_seq = store.append(Event(idea_id=idea, event_type=kind, actor=ACTOR_USER, source=ACTOR_USER,
                                   payload={"request_id": request_id, "note": note,
                                            "workspace_id": workspace_id,
                                            "source_ids": list(source_ids),
                                            "provenance": {"approval_request_id": request_id}}))
    if memory is not None and decision == "approve" and note:
        _remember_approved_note(
            memory,
            note,
            workspace_id=workspace_id,
            source_ids=(*source_ids, str(event_seq)),
        )
    return kind


def _execute_run(store, idea, act, executor):
    """允许的 request_run → 执行（真/fake）→ 自动签卡 → 回填。返回 (run_id, machine, cards)。"""
    script = (act.target or {}).get("script") or auto_regress_script()
    spec_id = (act.target or {}).get("spec_id")
    out = executor.execute(script, idea=idea, spec_id=spec_id)
    cards = sign_run_numeric_cards(
        store, out["run_id"], idea=idea,
        claim_statement=act.reason or f"run {out['run_id']} 的机器层结果",
    )
    return out["run_id"], out["machine"], cards


def _freeze_spec(store, idea, act) -> str:
    """编排器把提议的 spec 冻结成不可变 spec（确定性动作，模型不直接写状态）。"""
    spec_id = (act.target or {}).get("spec_id") or f"spec-{uuid.uuid4().hex[:8]}"
    store.append(Event(
        idea_id=idea, event_type=EVENT_SPEC_FREEZE, actor=ACTOR_ORCH, source=ACTOR_ORCH,
        payload={"spec_id": spec_id, "reason": act.reason or "", "target": act.target or {}},
    ))
    return spec_id


def literature_context(question: str, index, *, top_k: int = 3, char_budget: int = 1200) -> list[str]:
    """把检索命中压缩成给模型的证据块（doc/page/snippet）。无 index 返回空。"""
    if index is None or not question:
        return []
    out: list[str] = []
    used = 0
    for c in index.search(question, top_k=top_k, roles={"citable_evidence"}):
        snip = c.text.replace("\n", " ")[:250]
        line = f"[文献] {c.doc_id[:40]} p{c.page}: {snip}"
        if used + len(line) > char_budget:
            break
        out.append(line)
        used += len(line)
    return out


def cycle(
    store: SQLiteStore,
    idea: str,
    question: str | None,
    provider: ProposalProvider,
    *,
    executor=None,
    index=None,
    memory=None,
    record_user: bool = True,
    gate_mode: GateMode = GateMode.EXPLORE,
    token_cap: int | None = None,
    workspace_id: str | None = None,
) -> CycleResult:
    """一次用户输入闭环。question=None 表示无新输入（纯自动推进）。"""
    if record_user and question:
        store.append(Event(idea_id=idea, event_type=EVENT_USER, actor=ACTOR_USER,
                           source=ACTOR_USER, payload={"text": question}))

    extra = literature_context(question or "", index) if index is not None else []
    if memory is not None:
        try:
            # V2 requires an explicit workspace for automatic injection.  A
            # queryless legacy caller still receives its old projection when
            # the store has no V2 keyword support.
            extra = extra + memory.to_context(workspace_id=workspace_id)
        except TypeError:
            extra = extra + memory.to_context()
    turn = research_turn(store, provider, idea=idea, extra_context=extra, gate_mode=gate_mode,
                         token_cap=token_cap)

    ran_run_id, machine, cards = None, {}, []
    frozen: list[str] = []
    approval_request: str | None = None
    lines: list[str] = [turn.reply]
    if turn.proposal is not None:
        for act, dec in zip(turn.proposal.acts, turn.decisions):
            if dec.verdict == "ask":
                # 人工门：持久化请求并停下，学者决定后才继续
                approval_request = _request_approval(
                    store,
                    idea,
                    act,
                    dec,
                    workspace_id=workspace_id,
                )
                lines.append(f"需你决定：{act.act_type}（{dec.reason}）→ 请求 {approval_request}")
                break
            if dec.verdict != "allow":
                continue
            if act.act_type in RUN_ACTS and executor is not None:
                ran_run_id, machine, cards = _execute_run(store, idea, act, executor)
                lines.append(f"已执行 {ran_run_id}: machine={machine}")
            elif act.act_type in SPEC_ACTS:
                sid = _freeze_spec(store, idea, act)
                frozen.append(sid)
                lines.append(f"已冻结 spec={sid}")
    return CycleResult(
        reply="。".join(l for l in lines if l),
        ran_run_id=ran_run_id, machine=machine, signed_cards=cards,
        frozen_specs=frozen, asked=turn.asked, progressed=bool(frozen or cards or ran_run_id),
        awaiting_approval=approval_request, proposal=turn.proposal,
    )


def _auto_advance(store: SQLiteStore, idea: str, *, until: str = "ESTIMATION") -> int | None:
    """B1 最小 gate：一旦"主 spec 已定稿"（研究闸门过的信号），编排器沿合法迁移
    链把 phase 推进到 until（如 ESTIMATION）。不是 seed hack——由 spec.frozen 事件
    触发、逐跳走 can_transition，且不会回退。

    更完整的"可行性→估计"门禁（L-C/L-R 证据判定）留待门禁深化。
    """
    from .phase.phasedef import PHASE_ORDER, Phase, can_transition
    from .events.schema import EVENT_PHASE, ACTOR_ORCH

    if not any(e.event_type == "idea.declared" for e in store.scan(idea, event_types={"idea.declared"})):
        return None
    proj = store.project(idea)
    rs = proj.research_state
    if rs is None or not rs.current_spec_id:
        return None
    cur = proj.phase
    start = Phase(cur) if cur and cur in PHASE_ORDER else Phase.IDEA
    si, ti = PHASE_ORDER.index(start.value), PHASE_ORDER.index(until)
    if si >= ti:
        return None
    last_seq = None
    for step in range(si, ti):
        a, b = Phase(PHASE_ORDER[step]), Phase(PHASE_ORDER[step + 1])
        if not can_transition(a, b):
            continue
        last_seq = store.append(Event(idea_id=idea, event_type=EVENT_PHASE, actor=ACTOR_ORCH,
                                      source=ACTOR_ORCH, payload={"from": a.value, "to": b.value}))
    return last_seq


def run_until_gate(
    store: SQLiteStore,
    idea: str,
    question: str | None,
    provider: ProposalProvider,
    *,
    executor=None,
    index=None,
    memory=None,
    max_steps: int = 6,
    gate_mode: GateMode = GateMode.EXPLORE,
    token_cap: int | None = None,
    auto_compact_every: int | None = None,
    autonomous: bool = False,
    workspace_id: str | None = None,
) -> list[CycleResult]:
    """阶段内自动续轮（DD-02 §6）：spec 冻结后继续问模型，直到 ① 问用户 ② need_input/done
    ③ 一轮无推进 ④ 预算耗尽(budget.limit) ⑤ 健康检查不过。每一步全落 events。"""
    from .harness.safety import Budget, health_check, signal_health

    budget = Budget(max_steps=max_steps)
    results: list[CycleResult] = []
    for step in range(max_steps):
        # 预算刹车：耗尽即落事件并停（总结已得，不硬跑）
        if budget.exhausted:
            budget.signal_exhausted(store, idea)
            break

        # 健康探针：前一步之后状态必须一致，异常不许自动推进
        if step > 0:
            issues = health_check(store.project(idea))
            if issues:
                signal_health(store, idea, issues, ok=False)
                break

        q = question if step == 0 else None
        try:
            res = cycle(
                store,
                idea,
                q,
                provider,
                executor=executor,
                index=index,
                memory=memory,
                record_user=q is not None,
                gate_mode=gate_mode,
                token_cap=token_cap,
                workspace_id=workspace_id,
            )
        except StopIteration:
            break  # 模型剧本用尽 = 该停下问用户（mock 语义）
        results.append(res)
        budget.tick()

        # B1：若这一步定了主 spec（gate 信号），编排器推进 phase（不靠 seed hack）
        if res.frozen_specs:
            _auto_advance(store, idea)

        # C：事件距上次压缩超阈值 → 自动 compact（摘要入账本，原始事件不删）
        if auto_compact_every:
            from .events.schema import EVENT_COMPACTION
            from .harness.compaction import compact as _compact

            last_b = 0
            for e in store.scan(idea, event_types={EVENT_COMPACTION}):
                last_b = max(last_b, e.seq or 0)
            total = sum(1 for _ in store.scan(idea))
            if total - last_b >= auto_compact_every:
                try:
                    _compact(store, idea, reason="auto_compact")
                except ValueError:
                    pass

        if res.awaiting_approval:
            break  # 停等人批准（持久化请求已落事件）
        stop = (not autonomous and res.asked) or (
            res.proposal is not None and res.proposal.stop_reason in {"need_input", "done"})
        # 目标/自主模式：不把"提问"当硬停（除非无任何推进或明确 need_input/done）
        if stop or not res.progressed:
            break
    if budget.exhausted:
        budget.signal_exhausted(store, idea)
    return results
