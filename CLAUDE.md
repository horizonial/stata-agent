# CLAUDE.md — 实证研究 Agent 项目（新 session 请先读这）

本目录是一个自建 **实证研究 agent**（面向 LLM/agent 岗位 + 简历，要工业级）的设计与开发工作区。新 session 从这里开始，**先读 README.md，再看 design/SPEC.md** 再动手；不要凭这个速览直接写码。**当前基线 SPEC v0.5**（= v0.4 借鉴项 + codex 架构深研；依据见 `research/borrow_from_coding_agents.md` 与 `research/codex_report.md`）。

## 项目是什么（30 秒）
研究伙伴型 agent：学者给 idea + 文献库(双库) + 数据位置 → agent 对话式判可行性 → 用 Stata 反复试 spec（走已完成发布的 MCP：github.com/horizonial/stata-mcp）→ 产出实证部分中文 Word 初稿，数字/引用真实可溯源，文风学顶刊。

## 已定 & 别再改
- **编排 = 确定性阶段状态机**（IDEA→…→VALIDATION），阶段内才是受约束单 agent，状态迁移只归编排器；多 agent 需评测证明才启用；一次一个 idea 工作区
- 终点 = 实证初稿（非整篇投稿级）
- LLM 聚合层（deepseek 起步可换，按能力画像+契约测试编程不按模型名）；**隐私三档模式**（local_strict/approved_remote/mixed），fallback 不得跨边界；UI 后置但事件表数据模型现在定
- trace/台账 = **SQLite events 表**（append-only 唯一事实源）+ 物化视图（jsonl 仅导出）；**不存思维链**、存可审计决策；skill = 文件化版本化政策包
- 数值/引用接地 = **EvidenceBundle**（先证据包、后渲染，正文只许引用已验证 claim）；评测 = **五层 L0–L4 + 复现 ≥3 篇异质论文**
- 面试四维已设计：上下文/记忆、安全(外部输入不可信)、异常/兜底(事件表续跑+validator 不硬出)、评测(复现 benchmark) —— 见 README §2

## 关键约束
- Stata 执行走 **MCP**（stata-mcp），agent 层不碰 pystata 细节
- 外部输入（文献/数据/用户粘贴）一律当不可信，与指令隔离
- 每个进初稿的数字/引用必须能溯源（ledger / 分块引用），不允许 LLM 现编
- 一次研究可能几十步：做 token 预算与上下文压缩，别靠对话窗口堆历史

## 下一步
默认从 **M0**（阶段状态机最小骨架 + PydanticAI 协议层 + SQLite events 表(schema/幂等骨架) + harness loop + CLI 对话）开始；先跑通"你说一句、agent 想一步回一句、都进事件表"，再进 M1 双 RAG/可行性。细节以 design/SPEC.md v0.4 为准。动手前如要做选型深研，用 research/PROMPT_codex_research.md 交给 codex（上一轮交付见 research/codex_report.md）。

（本 CLAUDE.md 会随项目演进；大变更请同步 README/SPEC。）
