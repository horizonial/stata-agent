"""policy 最小裁决（DD-04 §4：固定裁决顺序，DD-02 §4 调用）。

切片 0：常值/集合版，够 L0 测"越权被拒、半截/伪造 act 不在集内、隐私门"。
完整参数校验/研究闸门 diff 后续接入。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..phase.phasedef import GateMode, Phase

DENY = "deny"
ASK = "ask"
ALLOW = "allow"

# 无条件拒绝（模型不能请求的"动作"——写入权分离的体现）
FORBIDDEN_ACTS = frozenset({
    "mark_done",           # 完成 = exit_gate 证据，不是模型说了算
    "delete_file",         # 删除/覆盖：禁止或单次强审批
    "sign_claim",          # EvidenceCard/Claim 只能 validator/evidence_builder 签发
    "phase.transition",    # 状态迁移只归编排器
    "write_research_state",  # 研究状态只随事件 fold
})

# 读类工具：阶段内自动允许
READ_ACTS = frozenset({
    "file_reader", "rag_search", "artifact_read", "inspect_data",
    "data_rows", "get_results", "get_help", "session_history", "task_status",
})

# 写类（会改内存数据/写文件）——需在 数据/估计/稳健 阶段才放行
WRITE_ACTS = frozenset({
    "stata_run", "stata_load_data", "export_graph", "request_run",
})

# 联网类（SPEC §4.10）：local_strict 禁
WEB_ACTS = frozenset({"web_search", "web_fetch", "download"})

WRITE_PHASES = frozenset({Phase.DATA, Phase.ESTIMATION, Phase.ROBUSTNESS})


@dataclass(frozen=True)
class Verdict:
    result: str  # deny|ask|allow
    reason: str


def check_act(
    act: str,
    *,
    phase: Phase | None = None,
    gate_mode: GateMode = GateMode.EXPLORE,
    privacy_mode: str = "local_strict",
    is_research_change: bool = False,
) -> Verdict:
    """六步裁决（DD-04 §4）的切片 0 版：无条件拒 → 策略 → 隐私 → 研究闸门 → ask → allow。"""

    # 1) 无条件拒绝
    if act in FORBIDDEN_ACTS:
        return Verdict(DENY, f"无条件拒绝：{act} 不在模型可请求动作集内")

    # 2) 阶段策略（读/写）
    if act in READ_ACTS:
        return Verdict(ALLOW, "read 白名单")
    if act in WRITE_ACTS:
        if phase is None or phase not in WRITE_PHASES:
            return Verdict(DENY, f"写类 act 仅限 {sorted(p.value for p in WRITE_PHASES)} 阶段")
        return Verdict(ALLOW, "写类 act 于允许阶段内")

    # 3) 隐私模式门（联网/外发）
    if act in WEB_ACTS:
        if privacy_mode == "local_strict":
            return Verdict(DENY, "local_strict 禁联网")
        return Verdict(ALLOW, f"{privacy_mode} 允许联网（内容标 retrieved_untrusted）")

    # 4) 研究闸门（样本/识别/spec 变更）：formal 一律 ask；explore 自动放行但须留痕
    if is_research_change:
        if gate_mode == GateMode.FORMAL:
            return Verdict(ASK, "研究闸门：正式门控下变更需人工批准")
        return Verdict(ALLOW, "研究闸门：explore 放行（须记 amendment）")

    # 5/6) 未知 act：不静默放行
    return Verdict(DENY, f"未知/未登记 act: {act!r}")
