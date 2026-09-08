"""L0：policy 六步裁决最小版 + 阶段迁移合法表。"""

from __future__ import annotations

from stata_agent.policy.policy import ALLOW, ASK, DENY, check_act
from stata_agent.phase.phasedef import GateMode, Phase, can_transition


def test_forbidden_acts_always_denied():
    for act in ["mark_done", "delete_file", "sign_claim", "phase.transition"]:
        assert check_act(act).result == DENY


def test_read_allowed_any_phase():
    assert check_act("inspect_data", phase=Phase.IDEA).result == ALLOW
    assert check_act("rag_search", phase=Phase.WRITING).result == ALLOW


def test_write_requires_estimation_like_phase():
    assert check_act("stata_run", phase=Phase.IDEA).result == DENY
    assert check_act("stata_run", phase=Phase.DESIGN).result == DENY
    assert check_act("stata_run", phase=Phase.ESTIMATION).result == ALLOW
    assert check_act("stata_run", phase=Phase.DATA).result == ALLOW


def test_web_gated_by_privacy_mode():
    assert check_act("web_search", privacy_mode="local_strict").result == DENY
    assert check_act("web_search", privacy_mode="approved_remote").result == ALLOW
    assert check_act("download", privacy_mode="mixed_sanitized").result == ALLOW


def test_research_gate_depends_on_gate_mode():
    # 正式门控：研究变更要人工批准
    assert check_act("propose_spec", phase=Phase.ESTIMATION,
                     gate_mode=GateMode.FORMAL, is_research_change=True).result == ASK
    # 探索环：自动放行（但须记 amendment）
    assert check_act("propose_spec", phase=Phase.ESTIMATION,
                     gate_mode=GateMode.EXPLORE, is_research_change=True).result == ALLOW


def test_unknown_act_not_silently_allowed():
    assert check_act("totally_new_thing").result == DENY


def test_phase_transition_edges():
    assert can_transition(Phase.IDEA, Phase.LITERATURE)
    assert can_transition(Phase.ESTIMATION, Phase.ROBUSTNESS)
    assert not can_transition(Phase.IDEA, Phase.DONE)
    assert can_transition(Phase.VALIDATION, Phase.ESTIMATION)  # 受控回退
    assert not can_transition(Phase.ROBUSTNESS, Phase.IDEA)
