# ARCHITECTURE — 实证研究 Agent 架构总结与交接指南

> 用途：给 codex 接手继续开发用的完整交接文档。覆盖：定位、分层架构、关键设计决策、代码地图、事件契约、已完成/遗留、接手清单。
> 更细的设计在 `design/`（SPEC v0.5 + DD-01…07 + agent-tool-routing.md + rethink-autonomy.md）。
> 运行环境：Python 3.12 · deepseek(OpenAI 兼容) · 本机 Stata 18 + stata-mcp · SQLite · FastAPI + 原生前端。

---

## 0. 一句话定位

**实证研究 agent**：学者给 idea + 文献 + 数据 → agent **自主**（LLM + 工具）判可行性、跑 Stata、做稳健性、写中文实证初稿；数字/引用全可溯源，过程全可回放。它**首先是个正常 LLM**（聊天默认），研究只是它可自主选择的一组工具，不是流程框架。

## 1. 分层架构（核心）

```
用户消息
  ↓
Agent Loop（通用，LLM + function calling，自主多步 + 防循环护栏 + 流式）
  ├─ Skill 层（决策层方法论，注入 system + allowed_tools 约束工具池）
  ├─ Tool 层（11 个原子工具，permission/enabled 动态暴露，结果摘要进上下文）
  └─ 每步落 events（SQLite 事件账本，唯一真相源）
       ├─ 护栏：未知工具拒绝 / 隐私三档 / 写权分离 / 幂等 / 预算 / 健康
       ├─ 证据链：run → card → claim（validator 签，模型不直接写证据）
       └─ Runtime：真 Stata(经 stata-mcp) + RAG(文献) + 记忆 + 事件账本
```

**一句话分工**：`Agent Loop = 决定下一步`；`Skill = 教怎么做一类任务`；`Tool = 执行动作`；`Runtime = 与真实系统交互`。公式：`Agent = Loop + Skill + Tool + State`。

## 2. 关键设计决策（每个一句话 + 在哪）

| 决策 | 要点 | 实现位置 |
|---|---|---|
| **意图路由** | 不做 research/chat 二分开关；LLM 靠 function calling + 工具描述自主决定调不调 | `harness/agent_loop.py` |
| **Tool 契约** | 工具=说明书(name/description/input_schema)+函数(handler)，夹 permission/enabled/timeout/result_to_context | `toolkit.py` |
| **动态工具暴露** | 注册 11 个，单轮只发 enabled(ctx) 且被 Skill.allowed_tools 允许的 | `agent_loop.py` |
| **Skill 在决策层** | 不是工具；causal-inference-mixtape(GitHub) 全文注入 system、约束工具池 | `skills/loader.py` + `skills/causal-inference-mixtape.md` |
| **事件账本** | SQLite append-only 唯一真相；物化视图可重建；不存思维链存可审计决策 | `storage/sqlite_store.py` + `events/` |
| **写权分离** | EvidenceCard/Claim 只能 validator/evidence_builder 签，模型不能直接写 | `tools/evidence_signer.py` + `events/append.py` |
| **证据链** | 每个进稿数字 → result_id → card；正文只能引用已验证 claim | `writer/` + `evidence_signer.py` |
| **隐私三档** | local_strict/approved_remote/mixed_sanitized；fallback 不跨边界 | `privacy/modes.py` + `providers/registry.py` |
| **流式输出** | LLM 原始流 → 统一 AgentEvent(text_delta/tool_started/tool_completed) → SSE → 前端 rAF 节流 | `providers/deepseek.py` + `ui.py` + `ui/app.js` |
| **防循环护栏** | 连续 3 次相同 run_stata 中断（确定性，不靠模型自觉） | `agent_loop.py` |
| **幂等/恢复** | 同 input_hash 复用；断点续跑；writer lease/fence；reconcile | `executor.py` + `storage/` + `harness/recovery.py` |
| **长会话** | token 计量 + 语义压缩(compaction.boundary) + 大结果摘要进账本 | `harness/safety.py` + `compaction.py` |
| **记忆** | 项目约定/决定（约束非证据），与证据库分开 | `memory/memstore.py` |
| **多工作区** | 每工作区=事件账本的 idea_id；`?ws=` 贯穿；registry 存元数据 | `ui.py` |
| **RAG** | 双库+信任角色；词法+哈希向量混合(RRF)；摄取缓存幂等 | `rag/` |
| **评测** | 五层 L0–L4 设计；L0/对抗有实现；复现 golden 自建入口 | `eval/` + `eval_golden/README.md` |

## 3. 代码地图（src/stata_agent/）

```
harness/agent_loop.py     通用 loop + 防循环 + 流式(on_event)
harness/safety.py         预算 / 健康探针 / estimate_tokens
harness/compaction.py     语义压缩(compaction.boundary)
harness/recovery.py       reconcile 未决执行链
harness/telemetry.py      两本账遥测(jsonl)
harness/branch.py         分支 fork/切片/父链
harness/research_turn.py  旧 research_turn(已弃用，UI 走 agent_loop)
toolkit.py                11 工具 + ToolContext + Tool 契约
skills/loader.py          Skill 加载/匹配(渐进披露)
skills/evolve.py          自进化(stage→人工批准→promote 新版本)
events/schema.py          事件类型常量 + Event 模型
events/append.py          写权校验(只 append)
events/upcast.py          schema 升级
events/reconcile.py       不确定写工具决策表
domain/models.py          EvidenceCard/Claim/RunRecord/ResearchState
domain/reducers.py        fold(事件→投影) + 非法序列拒绝
domain/family.py          ExperimentFamily(成员/选主结果)
storage/sqlite_store.py   事件账本 DDL + append/scan/project + writer lease/fence + 快照
providers/deepseek.py     deepseek(chat/stream_chat) + qwen 兜底
providers/registry.py     按 env 选 provider + 隐私门
providers/capabilities.py ModelCapabilityProfile
providers/codec.py        ActionProposal 编解码(旧)
providers/llm.py          chat_structured 重试(旧)
providers/mock.py         MockReplay/MockFixed
tools/executor.py         真 Stata 执行(逐行会话+机器层提取+幂等复用+重试)
tools/stata_client.py     MCP stdio 客户端(StataSession 持久)
tools/evidence_signer.py  run 机器层→card→claim 签名(幂等)
tools/fake_executor.py    离线假 Stata(测试/demo)
tools/robustness.py       稳健性一致检查(stars/check)
tools/strategy.py         stata-mcp 10 工具策略映射表
tools/ado.py              真 ado 预检(which)
rag/ingest.py             PDF 摄取(角色/chunk_id/扫描探测)
rag/retriever.py          词法检索(中文双字)
rag/hybrid.py             向量索引 + RRF 混合
rag/embed.py              HashEmbedder(本地确定性)
rag/library.py            chunk_id→块 可查
rag/index.py              摄取缓存幂等 + build_hybrid
writer/ground.py          数字接地 round-trip
writer/table.py           TableModel(esttab/TSV 语义 + 回归表)
writer/citation.py        引文接地(marker 反查)
writer/figure.py          figure 证据卡 + Word 插图
writer/docx_out.py        claims/表格 → docx
writer/draft_multi.py     多表初稿(draft_from_ledger)
memory/memstore.py        项目记忆(usage/prune)
privacy/modes.py          隐私三档 + 边界
ui.py                     FastAPI 全部端点 + SSE 流式 + 多工作区 + config
ui/index.html·app.js·styles.css  前端(纯原生，无框架)
```

## 4. 事件契约（关键事件类型）

- 执行链（Stata）：`run.requested → run.succeeded/failed/uncertain`（含 machine/provenance/env_sig）
- agent loop 工具调用：`tool.invoked → tool.done`（区别于 Stata 执行链）
- 证据：`evidence.card_signed → claim.signed → claim.retracted`
- 审批：`approval.requested → approval.granted/rejected`
- 决策：`agent_step`（payload: reply 或 ask 或 decision_summary；**不存思维链**）
- 其它：`idea.declared / user.message / spec.frozen / phase.transition / compaction.boundary / budget.limit / health.probe / system.restored / branch.created`

## 5. 已完成 vs 遗留

**已通并验证**：自主 loop（真 deepseek+真 Stata 端到端 DID 系数 2.81）· 11 工具 · skill 决策层 · 证据链写权分离 · 流式输出 · 多工作区 UI · RAG 真检索 · 防循环 · 断点续跑 · 压缩/记忆/分支 · 隐私三档 · L0 测试(109 个)。

**遗留（打磨/扩展，非阻塞）**：
1. **模型代码准确性**：deepseek 偶发把 reg 命令跑偏（N/r2 错），需 skill 细化或 verify_result 主动复核或换更强模型。
2. **Markdown 渲染**：前端目前纯文本，模型输出的 markdown 表格/代码块未渲染。
3. **工具节点**：流式里 tool_started/completed 目前只是文本提示，未做独立节点卡片。
4. **会话复用稳定性**：真 Stata 连续跑偶发崩溃（已加失败降级，仍需观察）。
5. **评测 harness**：L1–L4 未系统化；复现 golden 待建。
6. **UI 附件/图片输入**、**stop 中断**、**审批"改要求"** 未接。
7. **自进化 skill**：evolve.py 还在 staging（promote 需人工），且 skill_candidate_md 生成的是旧 variants 格式，需对齐新 Skill 语义。

## 6. 给 codex 的接手清单

1. **先跑通**：`cd app && python -m pytest`（109 过 4 skip）；`python -m stata_agent.ui`（8001）看 UI。
2. **读设计**：`design/agent-tool-routing.md`（意图/工具/Skill 分层，最新方向）+ `design/rethink-autonomy.md`（为什么从"研究驾驶舱"改到"自主 agent"）。
3. **别破坏的契约**：写权分离（模型不能签 card/claim）· 事件账本 append-only · 隐私门 · 工具 permission/enabled。
4. **建议下一步**（按价值）：① Markdown 渲染前端；② 工具调用独立节点；③ 模型准确性(skill 细化/verify_result)；④ 评测 harness L1–L4；⑤ UI 附件/stop/审批改要求。
5. **外部依赖路径**：stata-mcp 在 `C:\Users\user\stata-mcp`；文献库 `D:\work file\06_学位论文\一区\文献(1)`；key 在 `app/.env`（gitignored）。

## 7. 关键环境变量（app/.env）

```
DEEPSEEK_API_KEY          deepseek key（优先）
STATA_AGENT_PRIVACY       local_strict / approved_remote / mixed_sanitized
STATA_AGENT_EXECUTOR      stata(真) / 缺省(Fake)
STATA_AGENT_LIBRARY       文献库目录（RAG）
STATA_AGENT_SKILLS        skills 目录
STATA_AGENT_DEMO          1=离线演示
STATA_AGENT_UI_PORT       端口（默认 8001）
```
