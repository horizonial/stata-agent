"""工具策略映射表（DD-04 §3，stata-mcp 10 工具全量，数据驱动）。

每项：副作用(read/write/control) + 允许阶段 + 默认策略。policy 与 executor 共用本表。
"""

from __future__ import annotations

from dataclasses import dataclass, field

WRITE_PHASES = ("DATA", "ESTIMATION", "ROBUSTNESS")


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    side_effect: str  # read | write | control
    default_phases: tuple[str, ...] = ()
    note: str = ""


TOOLS: dict[str, ToolPolicy] = {
    "stata_run": ToolPolicy("stata_run", "write", WRITE_PHASES, "核心；restricted 模式"),
    "stata_load_data": ToolPolicy("stata_load_data", "write", ("DATA", "ESTIMATION"), "路径审计；URL→Deferred"),
    "stata_inspect_data": ToolPolicy("stata_inspect_data", "read"),
    "stata_data_rows": ToolPolicy("stata_data_rows", "read"),
    "stata_get_results": ToolPolicy("stata_get_results", "read"),
    "stata_get_help": ToolPolicy("stata_get_help", "read"),
    "stata_session_history": ToolPolicy("stata_session_history", "read", note="恢复/审计"),
    "stata_break": ToolPolicy("stata_break", "control", note="对自有会话"),
    "stata_task_status": ToolPolicy("stata_task_status", "read"),
    "stata_export_graph": ToolPolicy("stata_export_graph", "write", ("DATA", "ESTIMATION", "ROBUSTNESS"),
                                     "写 _mcp_graphs/"),
}


def side_effect_of(name: str) -> str:
    return TOOLS[name].side_effect


def allowed_phases(name: str) -> tuple[str, ...]:
    return TOOLS[name].default_phases


def phase_allows(name: str, phase: str | None) -> bool:
    tp = TOOLS[name]
    return tp.side_effect == "read" or (phase in tp.default_phases)


def write_tools() -> list[str]:
    return [n for n, t in TOOLS.items() if t.side_effect == "write"]


def summary() -> dict[str, dict]:
    return {n: {"side_effect": t.side_effect, "phases": list(t.default_phases)} for n, t in TOOLS.items()}
