"""Skill 在决策层（不是工具）：加载→匹配→注入 system→allowed_tools 约束工具池。"""

from __future__ import annotations

from pathlib import Path

from stata_agent.events.schema import EVENT_IDEA, ACTOR_AGENT, Event
from stata_agent.harness.agent_loop import run_loop
from stata_agent.skills.loader import Skill, load_skill_dir, match_skills
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.toolkit import ToolContext, default_tools

SKILLS = Path(__file__).resolve().parents[1] / "skills"


def test_load_and_match_external_skill():
    skills = load_skill_dir(SKILLS)
    assert "causal-inference-mixtape" in skills
    sk = skills["causal-inference-mixtape"]
    assert sk.body and len(sk.body) > 1000          # 全文（方法论）加载
    assert "DiD" in sk.description or "DiD" in sk.body
    # 匹配：DiD 关键词命中；skill 少时非因果话题也返回（模型按 description 自判）
    assert any(s.slug == "causal-inference-mixtape" for s in match_skills(skills, "implement a DiD regression"))


class CapturingProvider:
    def __init__(self):
        self.saw_system = ""
        self.saw_tools = []

    def chat(self, messages, tools=None):
        self.saw_system = messages[0]["content"]
        self.saw_tools = [t["function"]["name"] for t in (tools or [])]
        return {"content": "好的", "tool_calls": None}


def _did_skill() -> Skill:
    """fixture：带 allowed_tools 的 skill（测约束工具池能力，不依赖文件）。"""
    return Skill(
        slug="did_fixture",
        description="DID 方法",
        triggers=["did"],
        allowed_tools=["inspect_dataset", "run_stata", "read_artifact"],
        body="先确认处理组与期后，再做平行趋势检验。",
    )


def test_skill_injects_into_system_and_constrains_tools(tmp_path):
    s = SQLiteStore(str(tmp_path / "l.db"), writer_id="t")
    s.append(Event(idea_id="ui", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                   payload={"question": "x"}))
    sk = _did_skill()
    prov = CapturingProvider()
    ctx = ToolContext(idea="ui", store=s, executor=None)
    run_loop(s, prov, default_tools(), ctx, user_text="做 DID", skills=[sk])

    # 1) 全文进了 system
    assert "平行趋势" in prov.saw_system
    # 2) 工具池被约束到 skill.allowed_tools（+ask_user）
    assert set(prov.saw_tools) <= {"inspect_dataset", "run_stata", "read_artifact", "ask_user"}
    assert "fetch_source" not in prov.saw_tools and "write_artifact" not in prov.saw_tools
    s.close()


def test_no_skill_means_full_tool_pool(tmp_path):
    s = SQLiteStore(str(tmp_path / "l.db"), writer_id="t")
    s.append(Event(idea_id="ui", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                   payload={"question": "x"}))
    prov = CapturingProvider()
    ctx = ToolContext(idea="ui", store=s, executor=None)
    run_loop(s, prov, default_tools(), ctx, user_text="普通聊天", skills=[])
    # 无 skill：不按 allowed_tools 约束，暴露所有 enabled 的恒可用工具（read/write artifact）
    assert "read_artifact" in prov.saw_tools and "write_artifact" in prov.saw_tools
    s.close()
