"""skill 自进化（沉淀操作为 skill）。

流程（安全第一，绝不静默覆盖）：
  成功 episode（任务 + variants 配置 + 结果稳定）→ 提炼候选 SKILL.md → skills/_staging/
  → 人工 review → promote() 生成「新版本文件」skills/<slug>_e<n>.md（不改已有 skill）
  → 可选写遥测 skill.evolved。

门槛：只有"客观好结果"（主 run 成功、稳健性 stable）才值得沉淀；候选来自引擎的
variants 配置（不是模型自由发挥），保证能被 skills/engine 重新渲染执行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EpisodeVariant:
    id: str
    label: str
    y: str
    cluster: str
    reason: str = ""
    machine: dict = field(default_factory=dict)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "evolved_skill"


def _next_version(existing: list[str], name: str) -> str:
    """对已存在同名前缀文件给递增尾号：skills/<slug>_e<n>.md。"""
    n = 0
    for path in existing:
        m = re.match(rf"{re.escape(name)}_e(\d+)\.md$", path)
        if m:
            n = max(n, int(m.group(1)))
    return f"{name}_e{n + 1}.md"


def skill_candidate_md(*, name: str, task: str, episode: list[EpisodeVariant],
                       stable: bool, source_note: str = "") -> str:
    """把一次稳定 episode 的 variants 配置沉淀成候选 SKILL.md。"""
    vs = [{"id": v.id, "label": v.label, "y": v.y, "cluster": v.cluster, "reason": v.reason}
          for v in episode]
    variants_json = json.dumps(vs, ensure_ascii=False, indent=2)
    method_line = "用 处理×期后 交互识别（DID）；变量映射(treat/post/wave)由调用方注入，本 skill 只声明 y 与 cluster。"
    return f"""---
name: {_slug(name)}
version: 0.1.0
role: methodology
triggers:
  - needs: [did]
requires:
  ados: [reghdfe, esttab]
  stata_min: "17"
---

# {name}

> 由一次稳定 episode 自动沉淀（skill 自进化）。来源：{source_note or '经验沉淀'}。
> 方法学={method_line}　稳健性目标：{'稳定' if stable else '需人工修正后再沉淀'}。

## variants（沉淀自该次跑通配置，可再编辑）
```json
{variants_json}
```

## rules（继承默认）
- forbid: 单期无事件研究却宣称平行趋势
- forbid: 多 spec 只挑显著报（全 family 留痕，选主结果须给理由）
- require: 进稿数字来自 run→card；报告注明聚类口径

## 证据门槛
- L-R：rc=0 + 结构化(coef/SE/N) + 可复现 do + env_sig。
- 主结果入选须带理由（防只记录喜欢规格）。
"""


def stage(candidate_md: str, *, skills_dir: str | Path, name: str | None = None) -> Path:
    """写进 skills/_staging/，等人审；不动 skills/。"""
    from ..skills.loader import load_skill  # noqa: F401  (仅校验结构不必要)

    d = Path(skills_dir) / "_staging"
    d.mkdir(parents=True, exist_ok=True)
    m = re.search(r"^name:\s*(\S+)", candidate_md, re.M)
    slug_name = name or (m.group(1) if m else "evolved_skill")
    p = d / f"{slug_name}.md"
    p.write_text(candidate_md, encoding="utf-8")
    return p


def promote(candidate_md: str, *, skills_dir: str | Path, approved: bool = False,
            telemetry=None, idea: str | None = None) -> Path:
    """批准后生成「新版本文件」，绝不覆盖已有 skill。"""
    if not approved:
        raise PermissionError("promote 需人工批准（approved=True）；未经审的沉淀不得进 skills/")
    d = Path(skills_dir)
    d.mkdir(parents=True, exist_ok=True)
    m = re.search(r"^name:\s*(\S+)", candidate_md, re.M)
    name = (m.group(1) if m else "evolved_skill")
    version = re.search(r"^version:\s*(\S+)", candidate_md, re.M)
    existing = [p.name for p in d.glob("*.md")]
    filename = _next_version(existing, name)
    target = d / filename
    target.write_text(candidate_md, encoding="utf-8")
    if telemetry is not None:
        telemetry.record(kind="skill.evolved", idea=idea,
                         detail=f"promote -> {filename} (v{version.group(1) if version else '?'})")
    return target
