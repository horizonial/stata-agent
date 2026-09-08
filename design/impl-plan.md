# 落实规划（IMPLEMENTATION PLAN）—— 从设计到代码的切片路径

> 目的：把 SPEC v0.5 + DD-01…07 变成**可执行、每片可验收**的开发路径。当前只规划，不动码。
> 前置修订：复现 eval 改用 **Stata/现成评测集**（golden 注册表 PARKED）；下面切片不依赖它。
> 版本：2026-09-07。用户选择：先出规划，审后再写。

---

## 0. 总原则

1. **先"真相源"后"会说话"**：切片 0 先把 events 账本+领域 reducer 立起来（纯离线），LLM/Stata 都是后挂的能力。
2. **每片可独立验收**：有明确验收、可跑测试，不靠"写完了再试"。
3. **当场敲的默认值集中在 §5 表**：实现遇到就按默认，改默认是显式决定不是顺手。
4. **分层不耦合**：数据契约(DD-01) → 控制流(DD-02) → 上下文(DD-03) → 工具(DD-04) → 出稿(DD-05) → 评测(DD-06) → 文献(DD-07)，切片顺序与之对齐。

## 1. 代码布局（默认，可改）

```
D:\work\stata agent\
├── design\           # 文档（本文与 SPEC/DD 在此，不动）
├── research\
├── app\              # ← 代码从这里起
│   ├── pyproject.toml
│   ├── src\stata_agent\
│   │   ├── events\      # DDL/append/不变式/scan/快照/upcaster/reconcile
│   │   ├── domain\      # ResearchState/ResearchSpec/Claim/EvidenceCard reducers
│   │   ├── policy\      # policy 裁决（DD-04，先常值骨架）
│   │   ├── phase\       # PhaseDef/迁移表/run_status
│   │   ├── harness\     # research_turn/build_context/预算/健康探针（切片1）
│   │   ├── providers\   # mock/deepseek + ModelCapabilityProfile（切片1-2）
│   │   ├── tools\       # ToolContract + stata-mcp client（切片3）
│   │   ├── rag\         # ingest/retriever/roles（切片4）
│   │   ├── writer\      # TableModel/esttab 解析/Word（切片5）
│   │   └── cli.py
│   └── tests\           # L0 为主；fixtures\（esttab/mock replay/image stub）
└── samples\ideas\<demo>\ledger.sqlite3   # 每 idea 一个账本（随存档移动）
```
- 技术栈：**Python 3.12（miniconda）· pydantic v2 · sqlite3（stdlib）· pytest**。先只引必要依赖；chromadb/embedding/OTel 等**切片按需再进**。

## 2. 切片定义

| # | 名称 | 目标（验收） | 主要产出 | 依赖 | 明确不做 |
|---|---|---|---|---|---|
| **0** | 无 Agent 事件内核 | **L0 单测全绿 + 恢复确定性**（同输入同事件流） | app 骨架；events(DDL/append/不变式/scan/快照/upcaster/reconcile/恢复)；domain reducer 最小(ResearchState+Claim/Card)；policy 常值；PhaseDef 最小；writer_lease/fence | 无（离线） | LLM/MCP/RAG/UI/网络 |
| **1** | mock 驱动 loop | CLI 一问一答 → 全落 events；事件断言测试过 | mock provider(replay)；harness.research_turn；build_context 最小(L1+L2)；approval/blocked stub | 0 | 真模型/Stata/压缩 |
| **2** | 真 LLM | deepseek + 契约测试(空参/并行/超长/流式中断)过 | providers/deepseek、ModelCapabilityProfile、config、provider 契约测试 | 1 | MCP/记忆 |
| **3** | stata-mcp + skill | 现成 Stata 评测集跑出 L-R（rc=0+结构化+可复现） | tools/10 工具策略映射、运行目录+prepared 纪律、skills/panel_did、结果三层出口 | 2 + stata-mcp + Stata | RAG/writer/图 |
| **4** | 文献最小 | 2 篇 PDF 摄取 + 检索 gold set 有分 | ingest(版本链/chunk_id/roles)、retriever(先词法)、roles 过滤 | 1 | embedding/向量库选型（后置试） |
| **5** | writer/validator | TableModel 解析 + fixture 对抗全抓 + Word 出稿 | esttab parser、writer、validator、Word 链路**双跑**（pandoc-A vs rtf-C） | 0(domain) + fixtures（不阻塞等 3） | UI/图自动化（图接 DD-03 视觉） |

> 依赖原则：0 是一切地基；1 才有"会话"；3 才碰 Stata（你现有 stata-mcp/Stata 就绪后即可）；5 可用 fixture 先行，不必等 3 的真实结果。

## 3. 切片 0 详单（先只做它）

产出（约这些文件）：
```
src/stata_agent/events/  schema.py(DDL/Event model) append.py(事务+不变式+seq+revision)
                        scan.py fold/project snapshot.py upcast.py reconcile.py
src/stata_agent/domain/  research_state.py claim_card.py reducers.py(apply(proj,ev))
src/stata_agent/storage/ store.py(LedgerStore Protocol) sqlite_store.py lease.py
src/stata_agent/policy/  policy.py(裁决 6 步，先常值) 
src/stata_agent/phase/   phasedef.py(枚举+配置) transitions.py
tests/  test_append.py test_invariants.py(test_closure_seq_fingerprint_reject)
        test_restore.py test_reconcile.py test_fence.py test_reducers.py
```
切片 0 验收（=L0 用例集合，SPEC §4.9.4 L0）：
1. 同输入 replay ⇒ 同一 events 流（确定性）。
2. closure：每个 tool.call/run.requested 必有 terminal，否则恢复时被抓。
3. 非法序列拒绝：未 started 就 completed / completed 又 append attempt ⇒ 抛错。
4. 取消：cancelled 结果带原 operation_id；写入权：非 validator 写 card ⇒ 拒。
5. reconcile：read 超时可重试 / write 未决查台账决定 reuse|rerun|human。
6. writer lease/fence：旧进程 revision 过期 ⇒ 拒提交。
7. 快照+upcaster：改 schema 版本后老事件可回放。

## 4. 切片 1–5 验收（沿用 DD 各章）

- 1：DD-02 §10 验收（ActionProposal 带 `mark_done` 被拒 = L0 用例；重放确定性）。
- 3：DD-04 §8 验收 + DD-01 A1/A2（prepared 快照纪律、env_sig 记录）；跑现成 Stata 评测集得 L-R。
- 4：DD-07 §4 + DD-06 L1 检索指标起步。
- 5：DD-05 §4 全规则；DD-06 对抗集样本（小数点/假引用/无 id 数字）。
- 每片跑 **L0 + 对应层** 回归；切片 0 全绿前不进 1。

## 5. 当场默认值表（实现遇此即按默认；改 = 显式决定）

| 决策点 | 建议默认 | 状态 |
|---|---|---|
| 代码/数据布局 | §1 的 app/ + samples/ideas/<slug>/ledger.sqlite3 | ✅ 采用 |
| 账本位置 | **每 idea 一个 db 文件**（随工作区存档）；schema 内保留 idea_id | ✅ 采用 |
| seq | 每账本全局单调（单写者）；writer_lease.revision 即版本 | ✅ 采用 |
| payload | events.payload 存 JSON；热字段后按需拆列 | ✅ 采用 |
| fingerprint | 仅 `tool.call/run.requested` 用 `semantic_input_hash`；余为空 | ✅ 采用 |
| 快照 | 每 phase 尾 + 每 N=200 事件兜底；先存 research_state+indexes JSON | ✅ 采用 |
| upcaster | `events/upcast.py` 注册 `(event_type, schema_version)->fn` | ✅ 采用 |
| entry/exit 断言 | PhaseDef.entry_conditions 先用**函数式 python 谓词**，不造 DSL（留 hook） | ✅ 采用 |
| gate_mode | 默认 `explore`；formal 触发点=主 spec 定稿 / 进 ROBUSTNESS / 出初稿 | ⏳ 主题一未拍，切片 0 只建枚举+开关 |
| 写入权 token 规则 | 模型 act 集无 sign_claim/mark_done（DD-01 §2.7）；句级 token 回链留 DD-05 | ⏳ 主题一未拍（切片 0 只保事件级校验） |
| 研究闸门 diff 面 | sample.filters / variables.role / identification.strategy / spec.model 变更即闸 | ⏳ DD-04 待细，切片 0 只留 hook |
| L3 评分默认 | 结构 exact 先 → 数值容差 → 原因分类（DD-06 §5） | ⏳ 主题二未拍（与切片 0 无关） |
| Word 链路 | 切片 5 用 A(pandoc) vs C(rtf) 双跑后定 | ⏳ DD-05 TODO |
| embedding/向量库 | 切片 4 先词法，后试 bge-m3/Chroma | ⏳ DD-07 选型 |

## 6. 风险与门

- 切片 0 风险最低（纯本地 + 可测）；真正不确定性在切片 3（Stata 语义）与 4（中文 PDF/embedding），故放到后面用真环境试。
- 中途如要停：切片 0 交付即"可存档"（账本+测试自成一体），不欠债。
- **下一步门**：用户审本文 → 拍板（可只确认"切片 0 详单 + §5 默认"）→ 开写切片 0。
