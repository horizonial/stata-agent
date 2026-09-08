"""CLI demo（切片 1）：你说一句 → 一轮 mock research_turn → 回一句 → 全落 events。

用法：
    python -m stata_agent.cli --db samples/ideas/demo/ledger.sqlite3 --idea demo
    （输入若干行，Ctrl+D / 空行退出；看 ledger 里落了哪些事件）

切片 3 换成真模型 + 真工具后，入口不变。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .domain.action import Act, ActionProposal
from .events.schema import EVENT_USER, ACTOR_USER, Event
from .harness.research_turn import bootstrap_idea, research_turn
from .providers.mock import MockReplayProvider
from .storage.sqlite_store import SQLiteStore

# 默认剧本：先反问澄清（可审计地落一条 agent_step）
_DEFAULT_SCRIPT = [
    ActionProposal(
        decision_summary="先澄清数据与样本口径再谈可行性",
        ask_user="请说明：数据文件在哪、研究窗口/样本筛选是什么？",
    ),
    ActionProposal(
        decision_summary="无足够信息推进，停下问用户",
        acts=[Act(act_type="ask_user", reason="需要学者补料")],
        stop_reason="need_input",
    ),
]


def _provider() -> MockReplayProvider:
    return MockReplayProvider([_DEFAULT_SCRIPT[0]])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="切片 1：mock research_turn CLI")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[2] / "samples" / "ideas" / "demo" / "ledger.sqlite3"))
    ap.add_argument("--idea", default="demo")
    args = ap.parse_args(argv)

    # Windows 管道/控制台编码不一致：统一 UTF-8，避免中文变代理字符
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(str(db), writer_id="cli")

    provider = _provider()
    bootstrapped = False
    print("切片 1 mock loop —— 你说一句，agent 想一步回一句（空行/Ctrl+D 退出）。")
    try:
        while True:
            line = input("> ").strip()
            if not line:
                break
            if not bootstrapped:
                bootstrap_idea(store, args.idea, line)
                bootstrapped = True
                print("[idea.declared 已建档]")
            else:
                store.append(Event(
                    idea_id=args.idea, event_type=EVENT_USER, actor=ACTOR_USER, source=ACTOR_USER,
                    payload={"text": line},
                ))
            try:
                res = research_turn(store, provider, idea=args.idea)
                print("agent>", res.reply)
            except StopIteration:
                print("agent> （mock 剧本用尽，等你的指示；切片 3 会换成真模型）")
    except (EOFError, KeyboardInterrupt):
        pass

    n = sum(1 for _ in store.scan(args.idea))
    print(f"\n已落 {n} 条事件 → {db}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
