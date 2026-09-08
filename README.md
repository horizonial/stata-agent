# 实证研究 Agent —— 项目交接总览（v0.5）

> 本目录 = agent 项目的工作区。**开新 session 研究时在这个目录打开**（会自动读 `CLAUDE.md`），思路都在本 README + `design/`、`research/` 里，不用依赖旧对话。
> 关联：MCP 执行层已完成并发布 —— `https://github.com/horizonial/stata-mcp`（本地 `C:\Users\user\stata-mcp`）。agent 通过 MCP 协议消费它跑 Stata。
> **版本基线：v0.5 = v0.4 + 合并成熟 coding agent 借鉴项（2026-09-07，来源 `research/borrow_from_coding_agents.md`，B+C 全并）**；v0.4 = v0.3 + codex 架构深研（`research/codex_report.md`）。详细变更见 `design/SPEC.md` 顶部"修订记录"；本文档已同步 v0.5。

---

## 0. 一句话定位

**研究伙伴 / 方法顾问型 agent**（不是全自动写论文机）：学者自己想 idea、找文献、找数据放进约定文件夹；agent 与学者**对话式**判断可行性、指出缺什么、反复用 Stata 试 spec，最终产出**实证部分的论文初稿**（中文、Word、数字真实可溯源、引用真实可溯源、文风模仿顶刊）。

**v0.4 一句话（面试叙事）**：把一次实证研究变成**可恢复、可复核、可追责的证据生产流程**——以研究问题为中心、以 Stata 结果为数值真相、以文献原文为引用真相、以事件台账为过程真相。架构 = 确定性骨架（阶段状态机/权限/证据/副作用做软件规则）+ 概率性推理（选题/解释/候选方案留给模型）。

## 1. 已钉死的决策（多轮访谈 + v0.4 修订，见 SPEC §1）

| 维度 | 决策 | 关键理由 |
|---|---|---|
| 角色 | 强交互研究伙伴，对话式判断可行性 | 学者主导 idea/资料，agent 是方法顾问 + 执行者 |
| 形态 | **单 agent 循环起步**，预留多 agent | 先证明单 agent 出东西；多 agent 为解"上下文打架" |
| 编排 | **确定性阶段状态机 + 阶段内受约束单 agent**（v0.4） | 状态迁移只归编排器；多 agent 需评测证明才启用 |
| 终点 | idea→数据→实证→稳健性/机制/异质性→**实证部分初稿** | 引言可浅，方法+结果+解读完整 |
| 大脑 | **LLM 聚合层**，provider 可扩展，先 **deepseek** | 按能力画像+契约测试编程，不按模型名；换模型只改配置 |
| 交互/UI | 强交互 + 聊天 UI（**后置**）；可打断可自动 | 人工确认 = 持久化 approval 事件，不只聊天文本 |
| trace | **结构化全链路**（decision_summary/action/tool_result/结论），UI 可回放；**不存思维链**（v0.4） | 信任+调试+续跑；可解释性靠"为何选它"不靠回放 CoT |
| 状态 | **SQLite events 表（append-only）= 唯一事实源** + 物化视图（v0.4） | jsonl 仅导出；唯一真相源、可回放续跑 |
| 方法论 | **skill 文件化**（DID/机制异质性/稳健性/出表） | 版本化政策包，不写死代码，可插拔扩展 |
| 文献 | **双独立库**：①顶刊库→学文风 ②项目库→变量选取/引用(带 bibtex) | 信任角色类型化（v0.4）：style_only / citable_evidence / background |
| 数值接地 | **EvidenceBundle：先证据包后渲染**（v0.4） | 正文数字/引文只引用已验证 claim，不允许现编 |
| 隐私 | **三档模式**：local_strict / approved_remote / mixed（v0.4） | fallback 不得跨隐私边界（本地 embedding≠全链路本地隐私） |
| 初稿 | **中文 + Word**（rtf 表嵌 Word），LaTeX 次要 | 学者要在 Word 里继续编辑 |
| 数据 | 不限定单目录，agent 需**通用文件读取**（受 MCP 路径审计约束）| 学者指出数据位置 |
| Stata | **走 MCP 协议**连 stata-mcp | 职责清晰、可换后端 |
| 并发 | 一次一个 idea 工作区 | 专注推进 |

## 2. 面试/工业级四维（已定设计，见 SPEC §4.9）

- **上下文与记忆**：V2 由 `ContextAssembler` 按固定层级、token budget 和 manifest 构造 projection；`compaction.boundary` 只追加 checkpoint、保留完整工具尾部；MemoryStore 按 canonical workspace 隔离并附 provenance（项目约定/决定是约束，不是证据）。工具大结果进事件表/证据库、上下文只放摘要。金句：*agent 不靠记住，靠随时能查——RAG-over-own-history*。
- **安全**：外部输入（文献/数据/粘贴）不可信、与指令隔离（信任标签）；Stata 全走 MCP 受限模式不裸执行；**隐私三档模式**（v0.4）。金句：*最危险的是文献里的注入被当成你的指令，最隐蔽的是 fallback 偷偷变成数据出境*。
- **异常/兜底**：每层失败有明确降级 + 幂等键（operation/attempt/语义哈希）+ 恢复协议（稳定阶段续跑、先核未决副作用再决定）+ validator 不硬出 + 预算超停。金句：*超时是"不知道成功没"，不是"肯定没成功"*。
- **评测（杀手锏）**：实证有 ground truth —— **复现 ≥3 篇异质论文当 eval + 五层 L0–L4**（契约/组件/轨迹/单元格数值/专家盲评），遮蔽答案、防污染。金句：*通用 agent 苦于没标准答案；实证 agent 有——复现论文就是 eval，数字对不上就是失败*。

**深做的 2-3 亮点**（v0.4 收敛，别夸大）：
① **数字来源闭环**——每个正文数字绑定 Stata result_id + command_hash + data_signature + do_file，写作器只渲染已验证 claim；
② **研究决策可查询**——规格选择/人工确认/拒绝理由/反事实分支 = 一等对象，能答"为什么是这个模型"；
③ **实证社会科学单元格级复现协议**——五层评测的 L3。混合 RAG/事件溯源/OTel/Chroma 等是成熟构件，做扎实即可不当卖点。

## 3. 技术选型（v0.4，见 SPEC §1.5）

- **协议层**：PydanticAI（工具循环/结构化输出/schema 校验）——定位收窄为类型化模型/工具协议层，**不垄断业务状态与恢复语义**
- **编排**：**自写确定性阶段状态机**（阶段迁移/权限/人工门/失败策略）+ PydanticAI 跑阶段内 agent 循环（LangGraph 现不引入，触发条件见 SPEC §1.5）
- **状态**：SQLite **events 表**（append-only，唯一事实源）+ **物化视图**（当前状态）+ 快照/upcaster —— 可查询/回放/审计/续跑；jsonl 是导出玩具
- **契约**：agent→LLM 关键产物全 Pydantic model，不过重试；schema 保"形状正确"，EvidenceBundle/validator 保"事实正确"
- **模型适配**（v0.4 P1）：ModelCapabilityProfile + provider 契约测试；mock LLM provider（录 replay），harness 确定性单测
- **数值/引用接地**（v0.4）：EvidenceBundle（NumericClaim/CitationClaim/ResearchDecision + render_policy），写作只渲染已验证 claim
- **双 RAG（重点 + 保留 grep）**：向量检索 + grep/BM25 精确并查（混合）+ 可选 rerank；分块级引用溯源（块带 doc_id+页/章节/span）；**信任角色类型化**（style_only/citable/background）；Embedder/VectorDB 接口抽象（Chroma 起步，换型看指标不看篇数）
- **可观测**（v0.4 P1）：SQLite 业务账本 + OTel→现成后端（Langfuse/LangSmith/Logfire），两本账稳定 ID 关联，默认不采内容
- 一次一个 idea 工作区；UI 后置但**事件表数据模型现在定**（UI 只读物化视图+发消息）

## 4. 架构分层（见 SPEC §3）

```
UI(后置: 聊天/进度/trace 回放, 只读物化视图+发消息)
   ↓
确定性阶段状态机(自写: IDEA→…→VALIDATION; 阶段迁移/权限/人工门/失败策略 只归它)
   ↓
受约束的单 agent 循环 PydanticAI(阶段内 plan→act→observe→reflect + skill 加载 + 预算, 每步写事件)
   ↓
能力层(注册式): stata_mcp_client(策略映射) / file_reader / ledger(events+物化视图)
   │            / rag(双库, 信任角色) / evidence_builder / writer(只渲染已验证) / validator(claim-to-source)
   ↓
RAG 内部: Retriever(混合: 向量+grep/BM25+可选 rerank) → VectorDB(Chroma 起步,可换) + Embedder(可换)
   ↓
事件账本 SQLite(events append-only+物化视图+快照)   遥测 OTel→现成后端
   ↓
LLM 聚合: ModelCapabilityProfile + provider 接口(deepseek 起步+mock); 隐私三档门(local_strict/approved_remote/mixed)
```

## 5. 一个 idea 的工作流（见 SPEC §2，跑在阶段状态机上）

阶段机：`IDEA→LITERATURE→DESIGN→DATA→ESTIMATION→ROBUSTNESS→WRITING→VALIDATION→DONE`；每阶段有 allowed_tools/human_gate/失败策略，**只有编排器能提交迁移**。

学者想 idea → 找文献放库 → 找数据 → 起工作区告诉 agent → [0 建档: idea 声明+建 events 账本+载 skill] → [1 对话式可行性: 反问→查项目库(citable)→查数据→给"可行/缺数据/缺文献"，approval 事件持久化] → [2 实证设计与跑: 按 skill 提 spec→MCP 跑→结构化结果+provenance 进证据库→numeric_claims→讨论迭代] → [3 稳健性/机制/异质性] → [4 实证初稿: 学顶刊文风(style_only)→引项目库真实文献(citable)→EvidenceBundle 渲染→validator 过数字/引用/复现→中文 Word] → 学者改、迭代 → 存档(events 导出+do-files+初稿+引用清单)。

## 6. 路线（给 codex/新 session 的切法，v0.4 合并 P1/P2，见 SPEC §5）

- **M0 骨架**：阶段状态机(最小) + PydanticAI 协议层 + LLM 聚合(deepseek+mock) + **SQLite events 表(schema/版本/幂等骨架)** + 物化视图 + 工作区脚手架 + CLI 对话（一句一问、都进表、不存思维链）
- **M1 可行性**：file_reader + 双 RAG 落库(摄取版本链/稳定 chunk_id/source_role) + 混合检索 + 50–100 条 gold set 消融 + 对话式可行性 + **隐私三档默认 local_strict** + OTel 埋点；验收=真实 idea 跑通
- **M2 实证**：连 stata-mcp + 策略映射/错误分类 + skills(先 panel_did) + approval 门控事件 + **恢复协议(幂等+未决副作用核对)**；验收=复现 1–2 篇基准主表
- **M3 初稿**：EvidenceBundle + writer + validator + Word/rtf + 顶刊文风(style_only 隔离)；验收=对抗注入 validator 全抓 + 复现集扩 ≥3 篇异质，跑 L0–L3
- **M4 观测/UI**：聊天+进度+trace 回放 + OTel 接现成后端
- **P2（评测证明需要再做）**：多 Agent 文献广搜 / Qdrant / Temporal-DBOS / 自研 Dashboard

## 7. 待深化/待试（见 SPEC §7 与 research 提示词）

v0.4 已关闭：ledger.jsonl vs SQLite 冲突、thought 持久化、本地隐私 vs DeepSeek 矛盾、双库只靠 collection 隔离、validator 事后校验。待深化集中在：阶段状态机细节、provider 契约测试、事件 schema 演进(upcaster/快照)、恢复协议落地、中文 PDF 扫描版 OCR、Word 产出链路、检索 gold set 与 hybrid 消融、评测集 ≥3 篇异质论文+专家容差。**深研任务书在 `research/PROMPT_codex_research.md`；codex 交付报告在 `research/codex_report.md`（已完成一轮，P0 已并入 SPEC）**。

## 8. 文件导航

- `CLAUDE.md` —— 新 session 自动读的速览
- `README.md` —— 本文档，交接总览（v0.5）
- `design/SPEC.md` —— 完整设计规格 **v0.5**（决策/架构/四维/路线/开放点；顶部有 v0.4/v0.5 修订记录）
- `design/dd-01-domain-events.md` —— 详细设计 01：领域对象（ResearchState/ResearchSpec/ExperimentFamily/Run/Claim/EvidenceCard）与事件账本（事件目录/schema/reconcile/分支/fence/恢复/健康探针）
- `design/dd-02-phase-machine-harness.md` —— 详细设计 02：阶段状态机 + harness（phase×run_status、阶段属性表、迁移表、门控模型、ActionProposal 契约、research_turn 循环、预算/steering/健康探针、M0 loop 指南）
- `design/dd-03-context-memory.md` —— 详细设计 03：上下文/投影/记忆（ContextPack 分层、canonical→投影→prompt 不互相污染、token 预算、压缩=语义 checkpoint、大结果摘要+按需读、项目记忆 vs 证据库、图片/vision 裁剪）
- `design/dd-04-tool-permission.md` —— 详细设计 04：工具契约与权限（ToolContract 四段、stata-mcp 10 工具真实策略映射、Policy 固定裁决顺序、权限维度矩阵、author do-file/联网高风险入口、M0 指南）
- `design/dd-05-writer-validator.md` —— 详细设计 05：Writer + Validator（证据→初稿管线、esttab 表语义解析器、表/图/Table1 渲染、Validator 规则集、分级证据门槛 L-C/L-R/L-D、Word 链路）
- `design/dd-06-eval.md` —— 详细设计 06：评测（五层 L0–L4 实现、复现 benchmark ≥3 篇异质、遮蔽答案/防污染、mock 确定性替身、对抗集、LLM judge 校准）
- `design/dd-07-rag-skills.md` —— 详细设计 07：文献管线 + skill 规范（双库摄取版本链/抽取 QA/OCR 接线、混合检索+rerank、citable 纪律、skill 文件 schema 与激活 preflight）
- `design/audit-stata-practice.md` —— Stata 实证需求审计（2026-09-07）：prep/pipeline、env_sig、表语义 locator、图证据、门控探索环、联网/图片能力决策（S1 已修入 DD-01，S2 修入 DD-02/SPEC）
- `design/golden-replication-registry.md` —— Golden 复现论文注册表（eval L3 用，草稿）：CK1994/AL1999/NSW 起步三篇 + ADH held-out，含数据源/遮蔽数字/容差登记模板
- `design/impl-plan.md` —— **落实规划**（切片 0–5、代码布局、当场默认值表、切片 0 详单与验收）；审后据此写码
- `design/ui-requirements-codex.md` —— **UI 需求**（给 codex 做界面/交互设计，我据此写码；契约：只读物化视图+发消息）
- `research/PROMPT_codex_research.md` —— 交给 codex 的深度调研任务书
- `research/codex_report.md` —— codex 调研交付报告（2026-09-03，SPEC v0.4 的修订依据，从 docx 转存）
- `research/borrow_from_coding_agents.md` —— 借鉴引入笔记（2026-09-07，从 pi/codex/claude-code/claw-code 学习册提取可借鉴项 → SPEC 映射 + codex 记忆系统复查；v0.5 候选清单见其 §6）
- MCP 执行层：`C:\Users\user\stata-mcp`（DESIGN.md 记录 7 轮审计；README 中英优劣势）
