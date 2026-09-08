"""多工作区 · 完整对话窗口演示（无网络，确定性）。

先造一个完整历史（idea→定 spec→真跑(fake)→签证据→一次审批请求与决定），
再打印每个"窗口"的数据（工作区列表 / 对话 / Trace / 审批 / 证据 / Word），
说明这些数据分别喂给 UI 的哪个区域。
用法：cd app && PYTHONUTF8=1 PYTHONPATH=src python examples/ws_demo.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="wsdemo_")
os.environ["STATA_AGENT_DB"] = os.path.join(_TMP, "ledger.sqlite3")
os.environ["STATA_AGENT_WORKSPACES"] = os.path.join(_TMP, "workspaces.json")

from stata_agent.domain.action import Act, ActionProposal  # noqa: E402
from stata_agent.events.schema import EVENT_IDEA, ACTOR_USER, Event  # noqa: E402
from stata_agent.phase.phasedef import GateMode  # noqa: E402
from stata_agent.providers.mock import MockReplayProvider  # noqa: E402
from stata_agent.runner import approve as runner_approve  # noqa: E402
from stata_agent.runner import run_until_gate  # noqa: E402
from stata_agent.tools.fake_executor import FakeExecutor  # noqa: E402
from stata_agent import ui  # noqa: E402

IDEAS = {
    "ui": "（默认区，空着用来对照）",
    "ck": "最低工资对快餐店就业的影响（DID）",
}


def seed(store, idea: str):
    """在 idea 内灌入一条"完整研究对话"的历史。"""
    store.append(Event(idea_id=idea, event_type=EVENT_IDEA, actor=ACTOR_USER, source=ACTOR_USER,
                       payload={"question": IDEAS[idea]}))
    store.append(Event(idea_id=idea, event_type="user.message", actor=ACTOR_USER, source=ACTOR_USER,
                       payload={"text": "用双重差分跑主回归，样本是 NJ/PA 快餐店"}))

    # 阶段1：agent 自动定 spec → 跑(fake) → 出证据 → 问是否出稿
    run_until_gate(store, idea, None, MockReplayProvider([
        ActionProposal(decision_summary="定主 spec",
                       acts=[Act(act_type="propose_spec", target={"spec_id": "s-main"}, reason="主回归 fte")]),
        ActionProposal(decision_summary="跑主回归",
                       acts=[Act(act_type="request_run", target={"spec_id": "s-main"}, reason="跑主回归")]),
        ActionProposal(decision_summary="出结果", ask_user="要展示并出 Word 初稿吗？"),
    ]), executor=FakeExecutor(store), max_steps=5)

    # 阶段2：一次正式门控的审批请求（换口径），再由学者批准带说明
    gated = run_until_gate(store, idea, None, MockReplayProvider([
        ActionProposal(decision_summary="想换不含管理者的 FTE 再验证",
                       acts=[Act(act_type="propose_spec", target={"spec_id": "s-alt"},
                                 reason="稳健：不含管理者的 FTE")]),
    ]), executor=FakeExecutor(store), max_steps=2, gate_mode=GateMode.FORMAL)
    request_id = gated[-1].awaiting_approval
    runner_approve(store, request_id, decision="approve", note="可以，用不含管理者的 FTE 再看一次",
                   idea=idea)


def main() -> int:
    # 注册两个工作区
    s = ui._store()
    try:
        for idea in IDEAS:
            if not ui._workspace_known(idea):
                ui._read_workspace_registry()  # 确保默认 ui 行存在
            ui._touch_workspace_name(idea, IDEAS[idea])
        ui._write_workspace_registry([
            {"id": idea, "name": IDEAS[idea], "created_at": int(1e12)} for idea in ("ui", "ck")
        ])
        seed(s, "ck")
    finally:
        s.close()

    print("\n=== 窗口① 工作区列表（左侧导航） ===")
    for w in ui.workspaces()["items"]:
        print(f"  [{w['id']}]  {w['name']}  | 事件 {w['events']}  {w['run_status']}")

    print("\n=== 窗口② 对话（中央，renderMessages 的数据） ===")
    for m in ui.state(ws="ck")["messages"]:
        head = (m.get("text") or "").replace("\n", " ")[:90]
        print(f"  {m['role']:>9}/{m.get('kind','')[:12]:<12} #{m['seq']:<3} {head}")

    print("\n=== 窗口③ Trace 页（/api/trace，事件流） ===")
    for e in ui.trace(ws="ck", limit=20)["items"]:
        prev = e.get("payload_preview") or {}
        brief = str(prev.get("decision_summary") or prev.get("text") or prev.get("reason")
                    or prev.get("machine") or "")[:60]
        print(f"  #{e['seq']:<3} {e['type']:<24} {e['actor']:<5} {e.get('phase') or '':<4} {brief}")

    print("\n=== 窗口④ 审批（/api/approvals?status=all） ===")
    for a in ui.approvals(ws="ck", status="all")["items"]:
        print(f"  {a['request_id']:<10} {a['act']:<12} {a['status']:<9} 注:{a.get('note') or a.get('decided_note') or ''}")

    print("\n=== 窗口⑤ 证据（/api/cards + /api/claims） ===")
    for c in ui.cards(ws="ck")["items"][:5]:
        loc = c.get("locator") or {}
        print(f"  card {c['card_id']:<22} kind={c['kind']:<8} loc={str(loc)[:60]}")
    for cl in ui.claims(ws="ck")["items"]:
        print(f"  claim {cl['claim_id']:<16} {cl['status']:<9} {str(cl['statement'])[:50]}")

    print("\n=== 窗口⑥ Word（/api/draft.docx 内容） ===")
    from stata_agent.writer.draft_multi import draft_from_ledger

    store = ui._store()
    try:
        data = draft_from_ledger(store.project("ck"), method="DID：fte 对 处理×期后 交互（聚类到店）。",
                                 limits="双期数据，平行趋势用稳健口径替代。")
        out = Path(_TMP) / "ck_draft.docx"
        out.write_bytes(data)
        print(f"  docx {len(data)} bytes -> {out}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
