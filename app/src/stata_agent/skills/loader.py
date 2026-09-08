"""Skill 系统（决策层方法论，不是工具）。

Skill = 教 Agent 如何用工具完成一类任务的方法：适用场景/步骤/判断规则/何时停/何时问研究者。
Skill 不执行、无副作用、无 schema；加载后进入上下文引导模型。

- 元数据（name/description/triggers/allowed_tools）→ 匹配阶段，低 token
- 全文 → 匹配后加载进上下文
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Skill:
    slug: str
    description: str
    triggers: list[str] = field(default_factory=list)      # 匹配关键词
    allowed_tools: list[str] = field(default_factory=list)  # 约束工具池
    requires_ados: list[str] = field(default_factory=list)  # 预检：需要的 ado
    path: Path | None = None
    body: str = ""                                          # 全文（方法/判断规则）

    @property
    def metadata(self) -> str:
        """匹配阶段只读元数据（低 token）。"""
        return f"{self.slug}: {self.description}"


def _list_field(fm: str, key: str) -> list[str]:
    m = re.search(rf"^{key}:\s*\[(.*?)\]", fm, re.M | re.S)
    if not m:
        return []
    return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]


def load_skill(path: str | Path) -> Skill:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.S)
    if not m:
        raise ValueError(f"{p} 缺 frontmatter（--- 开头）")
    fm, body = m.group(1), m.group(2).strip()
    name = re.search(r"^name:\s*(\S+)", fm, re.M)
    desc = re.search(r"^description:\s*\"?(.+?)\"?\s*$", fm, re.M)
    return Skill(
        slug=name.group(1) if name else p.stem,
        description=(desc.group(1).strip() if desc else ""),
        triggers=_list_field(fm, "triggers"),
        allowed_tools=_list_field(fm, "allowed_tools"),
        requires_ados=_list_field(fm, "ados"),
        path=p,
        body=body,
    )


def load_skill_dir(directory: str | Path) -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    for f in sorted(Path(directory).glob("*.md")):
        sk = load_skill(f)
        out[sk.slug] = sk
    return out


def match_skills(skills: dict[str, Skill], user_text: str, *, phase: str | None = None) -> list[Skill]:
    """渐进披露：先关键词匹配；skill 少（≤3）时匹配不到就全返回，让模型按 description 自己判断。

    - 有 triggers：命中任一关键词。
    - 无 triggers（外部 SKILL.md 只有 description）：从 description 提取引号内触发短语 + slug 匹配。
    """
    low = user_text.lower()
    matched = []
    for sk in skills.values():
        terms = [t.lower() for t in sk.triggers]
        if not terms:
            terms = [sk.slug.lower()]
            terms += [m.group(1).lower() for m in re.finditer(r'"([^"]+)"', sk.description)]
        if any(t in low for t in terms):
            matched.append(sk)
    if not matched and len(skills) <= 3:
        return list(skills.values())  # skill 少：全加载，模型按 description 自判
    return matched
