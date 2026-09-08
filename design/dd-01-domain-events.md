# DD-01 详细设计：领域对象与事件账本

> 文档层级：`design/SPEC.md` v0.5 的下位细化。本文把 SPEC §4.3.1（ResearchState）、§4.4（事件账本/物化视图/分支/恢复）、§4.8（Claim/EvidenceCard/ExperimentFamily）落到可实现的契约与算法，并吸收借阅笔记 N1/N9/N10/N11/N13/N14/N15。
> 前提术语与缩写以 SPEC v0.5 为准。M0 实现以本文为数据契约依据；本文**不含**实现时间表（见 §6 落地顺序只给指南）。
> **2026-09-07 审计补丁已并入**（依据 `audit-stata-practice.md` S1）：prep/pipeline 纪律 + 从 prepared 快照重建(A1)、Provenance `env_sig`/`input_data_sig`(A2)、表语义 Locator + `figure` 卡(B1/C1)、reconcile 写工具须重建(A1)、事件目录 + `repro.manifest_built`/`env.snapshot`(A3)。

---

## 0. 设计输入与要回答的问题

**输入**：SPEC v0.5 §4.3.1/§4.4/§4.8、§7 开放点 #1–5；`research/borrow_from_coding_agents.md` B1/B3、C1/C2/C3/C5/C6/C7；`research/codex_report.md` P0-1/P0-5。

**本文必须回答**：
1. canonical（唯一事实源）与 derived（可重建投影）到底怎么切？
2. 每个领域对象长什么样、谁有权写、由哪些事件决定（event-sourced reducer）？
3. 事件目录完整清单（事件类型 / 写者 / 投影更新 / 是否需 reconcile）？
4. events 表 schema（seq/branch/fingerprint/parent）与分支/回滚语义？
5. reconcile、恢复、健康探针的具体算法？
6. 单写者事务 + writer lease/fence 怎么落地？
7. 快照 + upcaster 怎么落地？

---

## 1. 第一性：canonical vs derived

**规则：一个事实只有一个 canonical 出处；其余全是 derived，可由 canonical 重建，丢了 derived 不丢事实。**

| 类别 | 内容 | 可重建？ |
|---|---|---|
| **canonical** | ① events 表（append-only，含不可变 provenance/result/claim 记录）② 不可变工件（do-files、Stata 结构化结果、原始日志、PDF 原件、数据签名清单）③ 用户文件/输入 ④ 由确定性组件**签发后不可变**的 EvidenceCard / Claim 记录 | 否（丢即丢，需备份） |
| **derived** | 物化投影（ResearchState / ExperimentFamily 视图 / result_index / citation_index / claim_index / approval_log）· 统计 · 全文/向量检索索引 · embedding · 导出(jsonl/Word) · UI/trace 视图 | 是（从 events + 工件重建） |

推论：
- **events 只存小 payload + 工件引用，不存大二进制**（原始 Stata 输出/PDF 进工件存储，events 存路径+哈希+结构化摘要）。这是"事件账本"与"artifact store"分离的边界。
- EvidenceCard/Claim 一旦由 validator/evidence_builder **签名**，作为不可变事实追加进 events（或独立 append-only claim 表按 events 重建）。正文引用只认这些记录（写入权分离，SPEC N11）。
- 物化投影**不许被直接修改**——改状态 = 追加对应事件。唯一例外：幂等工具函数内部临时缓存。

## 2. 领域对象模型

所有领域对象都是 **reducer 的输出**：`Projection = fold(events_from(seq0..S), seed)`。构造时只依赖事件与工件，绝不依赖模型输出/UI。

### 2.1 公共 envelope（所有领域对象的审计外壳）

```python
class Audit(BaseModel):
    provenance_event: str        # 本对象最后一次由哪个 event 更新
    last_seq: int                # 折叠到的事件 seq
    created_at / updated_at
```

### 2.2 ResearchState（内容层研究状态，SPEC §4.3.1）

代表"当前研究在用什么样本/变量/识别策略/结论前提"。**压缩/续跑/分支时保留的是它 + 证据索引，不是对话摘要。**

```python
class Filter(BaseModel):
    rule: str; version: int; reason: str | None; approved_event: str | None

class SampleDefinition(BaseModel):
    source_files: list[str]        # 经 data_refs + 路径审计
    filters: list[Filter]          # 样本筛选（每条带版本与批准，防静默改样本）
    panel_keys: list[str]; time_var: str | None
    signature: str                 # 由来源文件哈希+筛选规则确定

class VariableRole(BaseModel):
    role: Literal["dep","treat","covariate","cluster","weight"]
    code: str                      # Stata 变量名
    construct: str | None          # 构造方式（label/代码摘录）
    construct_do: str | None       # 溯源到 dofile
    provenance: str                # 由哪个 spec/事件引入

class Identification(BaseModel):
    strategy: str                  # DID / IV / event-study / …
    assumptions: list[str]         # 关键假设（可点回 citable_evidence）
    refs: list[str]                # 依据文献 chunk_id

class ResearchState(BaseModel):
    idea_id: str
    phase: str
    sample: SampleDefinition
    variables: dict[str, VariableRole]
    identification: Identification
    current_family_id: str | None      # → ExperimentFamily
    current_spec_id: str | None        # → ResearchSpec（主 spec）
    evidence_refs: list[str]           # 当前结论依赖的 claim_ids
    confirmatory_lock: Lock | None     # 确认性锁定（见 §4.9.5）
    audit: Audit
```
不变量：
- 任何 `sample/filters/variables/identification` 变更都产生一个带 `reason/version` 的事件；confirmatory lock 生效后看结果再改必须走 amendment。
- `evidence_refs` 只许指向 `claim.signed` / `evidence.card_signed` 产生过的 ID。

### 2.3 ResearchSpec（一次可执行的规格 + 输出契约）

模型只能**提议** spec（`spec.proposed`），冻结需 deterministic 组件/编排器；冻结后生成不可变 hash。
**Pipeline 纪律（审计 A1，2026-09-07）**：`prep` 把 raw → prepared dataset（merge/append、panel 设定、变量构造、winsor 等）。估计 run **一律从 prepared 快照启动**，不依赖 stata 会话里递增的内存 .dta（内存顺序可变、非事务——失败重放会双重 append/漂移，MCP reset+replay 只救会话不救内存数据集）。`spec.hash` 覆盖 sample_sig+prep+model ⇒ 同一 prepared+spec 得同一可复用结果。prepared 快照是 **derived/缓存**（raw+prep do 可重建）；raw 文件与 prep do 才是 canonical。

```python
class ResearchSpec(BaseModel):
    spec_id: str
    family_id: str; intent: Literal["exploratory","confirmatory"]
    variant_of: str | None               # 父变体（换控制变量=新变体）
    data_ref: str; sample_sig: str       # 对齐 SampleDefinition.signature（对 raw 定义）
    prep: PrepRef | None                 # 审计A1: raw→prepared 的 do 引用 + prepared 快照
    variable_mapping: dict[str, VariableRole]
    model: dict                          # est_cmd / fe / cluster / weights …
    output_contract: dict                # 机器层要提取的字段（系数表/SE/N/R²…）
    preflight: list[str]                 # 运行前检查项（变量存在/样本非空/依赖 ados 在/…）
    frozen: bool; hash: str              # 冻结即不可变；hash 含 sample_sig+prep.hash
```

### 2.4 ExperimentFamily（防只记录喜欢的规格，SPEC §4.8）

一次研究里按"尝试家族"分组所有运行——预计划 vs 探索、停止规则、多重检验计数、全成员 Run、主结果选择理由。

```python
class RunRef(BaseModel):
    run_id: str; spec_hash: str; status: str
    outcome: str | None; selected: bool = False; selection_reason: str | None

class ExperimentFamily(BaseModel):
    family_id: str; hypothesis_id: str; intent: Literal["exploratory","confirmatory"]
    created_by: str
    plan: dict | None                    # 预计划 specs / stopping rule（可空=纯探索）
    members: list[RunRef]                # 全成员，含被否的
    multiplicity: int = 0                # 该假设下跑的规格数（多重检验）
    main_result: MainResult | None       # 由 human/orchestrator 选择
    audit: Audit
```
不变量：`multiplicity = len(members)`；主结果必须能回答"为何是这个 spec"（selection_reason + approval）。

### 2.5 Run / RunAttempt（一次不可变执行）

对应 codex 的"attempt 记录"，是溯源链最小单位。

```python
class Run(BaseModel):
    run_id: str; spec_id: str; family_id: str
    operation_id: str; attempt_id: int; semantic_input_hash: str
    status: Literal["pending","running","succeeded","failed","uncertain","cancelled"]
    side_effect: Literal["read","write"]   # 读类 vs 写文件/数据类
    closure: ToolClosure | None
    provenance: Provenance | None          # 见下：含 input_data_sig / env_sig（审计 A2）
    result: RunResult | None               # 三层出口（见 2.6）
```
- `semantic_input_hash = H(spec.hash + 数据签名 + 参数)`：重试同一输入优先复用 committed 结果。
- **Provenance 字段（审计 A2）**：`{ do_file, command_hash, data_signature, input_data_signature = H(raw文件集+prep do+spec.prep), env_sig = H(Stata 版本 + 关键 ado 清单[reghdfe/esttab/…] + OS), exec_seq }`。`env_sig` 是复现 eval（换机器/换版本跑作者 do）里"代码错 vs 环境错"的判据，缺它归因不清。stata-mcp 能吐（`about`/`which`）就吐，不能则由上层在 run 前后各取一次。
- `side_effect=read` 可安全重放；`write` 必须走 reconcile（§4）。

### 2.6 结果三层出口 + EvidenceCard + Claim（写入权分离）

每个 Stata 结果保留三层（codex N6）：
- **机器层**（schema 只作用于此，只读）：`exit_code / N / coef / se / checksum` 等结构化字段。
- **模型层**：摘要/警告/下一步（模型可生成，不可改机器层）。
- **工件层**：完整 do-file / 日志 / 环境（落工件存储，事件引用）。

```python
class EvidenceCard(BaseModel):             # 由 validator 签发（模型无权写）
    card_id: str; kind: Literal["numeric","citation","sample","model","figure"]  # figure=审计 C1
    locator: Locator                       # 精确到 页码/表号/行列/result_id/chunk_id/span
    value: dict | None                     # 机器层关键值（带精度）
    machine_hash: str                      # 原始机器层哈希，防改
    signed_by: Literal["validator","evidence_builder"]; verified_at: str
```
`Locator`：
- **数字/表卡（审计 B1：表语义键，不按列序数猜）** → `{run_id, result_id, table_id, panel, row_label, col_group, stat_type(coef|se|p|N|r2|fe_flag|…) }`。`row_label` 匹配 esttab 输出里的变量标签/FE 行/聚类行；`col_group` 匹配列分组（如"处理×组A"）。writer/validator 需要 **esttab→cell 语义解析器**：把 esttab/rtf/csv 排版还原成格子语义，数值接地按语义键比对（不是位置猜）；display 值反查 canonical 值（round-trip，含 `0.038***`→0.0376,p<0.01 与星号、FE 行 "Yes/X"）。
- 引文卡 → `{chunk_id, doc_id, page, section, source_span}`；
- 样本/模型卡 → `{run_id, data_signature, spec_hash, env_sig}`；
- 图卡（审计 C1）→ `{figure_id, run_id, kind(event-study|margins|coefplot|placebo|balance|…), x_var, est_point_ref/ci_ref, caption_ref}`；图本身落 artifact(.gph/.png, hash)，writer 嵌 Word + caption 引 claim，复现=图的 dofile 可跑。
- 描述/平衡表（Table 1，审计 B2）复用数字/表卡语义键；writer 有独立模板。

```python
class Claim(BaseModel):                    # 研究级命题：Run/卡片 → 初稿段落
    claim_id: str; statement: str          # 一句话命题（经提炼，非原文）
    kind: Literal["effect","robustness","mechanism","heterogeneity","design"]
    cards: list[str]                       # 支撑的 EvidenceCard
    status: Literal["draft","supported","retracted"]
    written_by: Literal["evidence_builder","validator"]   # ≠ model
    superseded_by: str | None              # retract 链
```
**写入权分离（N11）硬规则**：`EvidenceCard` 只能由 validator/evidence_builder（确定性代码）从 Stata 机器层签发；`Claim` 只能由 evidence_builder 从已签发的卡片合成。**模型/agent 只能写 `spec.proposed`、决策摘要、结论解读，写不进 card/claim。**

### 2.7 谁写谁读（写者矩阵）

| 对象 | 谁能创建 | 谁能更新 | 谁只读 |
|---|---|---|---|
| ResearchState | —（投影） | 编排器：由事件 fold | agent/UI/评测 |
| ResearchSpec | agent 提议 | 冻结=orchestrator/validator；锁定=confirmatory | 运行时 |
| Run | orchestrator/运行时 | 追加 attempt/result（不可变） | 全 |
| EvidenceCard | validator/evidence_builder | 不可变 | 全 |
| Claim | evidence_builder | retract/supersede（追加新卡） | 全 |
| ExperimentFamily | orchestrator 建档 | human 选 main_result | 全 |

## 3. 事件账本

### 3.1 表结构（SQLite DDL 草案）

```sql
CREATE TABLE events (
  event_id    TEXT PRIMARY KEY,
  idea_id     TEXT NOT NULL,
  seq         INTEGER NOT NULL,             -- 单调；账本内全局排序键
  branch_id   TEXT NOT NULL,                -- 分支
  prev_event_id TEXT,                       -- 本分支内上一条（= parent）
  phase       TEXT,
  event_type  TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  actor       TEXT NOT NULL,                -- user|agent|orchestrator|validator|stata_mcp|system
  source      TEXT NOT NULL,                -- 内容来源（信任/审计用）
  correlation_id TEXT, causation_id TEXT,
  operation_id TEXT, attempt_id INTEGER,
  fingerprint TEXT,                          -- 语义指纹（重试/重放去重）
  confidence TEXT NOT NULL DEFAULT 'judgment',  -- fact|verified|judgment
  side_effect_state TEXT,                    -- prepared|executing|committed|failed|uncertain
  payload     TEXT NOT NULL,                 -- JSON
  created_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX uq_events_seq  ON events(idea_id, seq);
CREATE UNIQUE INDEX uq_events_fp   ON events(idea_id, event_type, fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX idx_events_branch_leaf ON events(idea_id, branch_id, seq);

-- 单写者租约 + revision fence
CREATE TABLE writer_lease (
  writer_id TEXT PRIMARY KEY, token TEXT NOT NULL,
  revision INTEGER NOT NULL,                 -- 每次提交单调递增
  acquired_at INTEGER, expires_at INTEGER
);

-- 快照（可选独立表/文件）
CREATE TABLE snapshots (
  idea_id TEXT, as_of_seq INTEGER, kind TEXT,     -- kind: full|research_state|…
  blob BLOB NOT NULL, created_at INTEGER,
  PRIMARY KEY (idea_id, as_of_seq, kind)
);
```
- `seq` 在账本内全局单调（写入顺序即因果顺序近似）；跨分支只保证本分支有序，树靠 `prev_event_id/branch_id` 表达。
- `fingerprint`：重试/重放去重——对 `tool_call/run` 类 = `semantic_input_hash`，对用户消息类可为空。同 (idea,event_type,fingerprint) 已在账本 → 命中唯一索引，写入被拒或复用（见 §4.2 幂等）。

### 3.2 事件目录（完整清单）

> 升级自 SPEC §4.4 的 6 种 kind 示例。每类给：**event_type｜写者｜何时发生｜payload 核心｜更新哪些投影｜reconcile 关注?** 未列出的 event_type 默认被 reducer 忽略（向前兼容）。

| # | event_type | 写者 | 触发 | payload 核心 | 更新投影 | rec. |
|---|---|---|---|---|---|---|
| 1 | `idea.declared` | user/agent | 建档 | 问题/贡献/假设 | idea_state | – |
| 2 | `user.message` | user | 对话 | text | thread | – |
| 3 | `steering` | user | 打断/换方向 | 指令类型 | thread | – |
| 4 | `approval.requested` | orchestrator | 到人工门 | what/为何 | approval_log | ✔ |
| 5 | `approval.granted` / `.rejected` / `.deferred` | user | 人工决定 | what/by/reason | approval_log, research_state | ✔ |
| 6 | `spec.proposed` | agent | 提候选 | spec 草案 | family.pending | – |
| 7 | `spec.frozen` | validator/orch | 冻结不可变 | spec.hash | research_state, family | – |
| 8 | `spec.locked` | orchestrator | confirmatory lock | spec_version | research_state | – |
| 9 | `branch.created` | orchestrator | 换 spec/放弃尝试 | fork_event, reason | branch/leaf | – |
| 10 | `run.requested` | orchestrator | 发起一次执行 | spec_id, operation_id, input_hash | run | – |
| 11 | `tool.call` | orchestrator | 请求 stata_mcp | tool, args_hash, side_effect | run(executing) | ✔ |
| 12 | `tool.result` | stata_mcp/validator | 工具完成/取消/失败 | status, machine层摘要, closure 对齐 | run(result) | ✔ |
| 13 | `run.succeeded` / `.failed` / `.uncertain` | orchestrator | 判定 | provenance, side_effect_state | run, result_index | ✔ |
| 14 | `evidence.card_signed` | validator | 校验通过 | EvidenceCard | card_index | – |
| 15 | `claim.signed` | evidence_builder | 从卡合成 | Claim | claim_index, research_state.evidence_refs | – |
| 16 | `claim.retracted` | evidence_builder | 被新卡推翻 | claim_id, superseded_by | claim_index | – |
| 17 | `family.main_result_selected` | user/orchestrator | 选主结果 | run_id, reason | family, research_state | ✔ |
| 18 | `amendment.recorded` | user/orchestrator | 看结果后改动 | what, before/after, impact | research_state, family | ✔ |
| 19 | `phase.transition` | orchestrator | 阶段迁移 | from,to,entry/exit 校验 | idea_state, research_state | – |
| 20 | `checkpoint.snapshot` | orchestrator | 阶段尾 | as_of_seq | (快照) | – |
| 21 | `compaction.boundary` | orchestrator | 上下文压缩 | 摘要+证据索引范围 | (投影视图) | – |
| 22 | `health.probe.pass` / `.fail` | orchestrator/validator | 恢复/压缩后自检 | checklist | run? | – |
| 23 | `budget.limit` | orchestrator | token/步数超 | 已用/剩余 | thread | – |
| 24 | `privacy.mode.changed` | user | 三档切换/授权 | mode, scope | system | ✔ |
| 25 | `provider.fallback` | orchestrator | 降级 | from,to,reason | system（审计） | ✔ |
| 26 | `system.restored` | orchestrator | 从快照恢复 | snapshot_seq, reconcile 结果 | run? | – |
| 27 | `artifact.stored` | orchestrator/artifact | 大工件落盘 | path, sha256, kind | (工件索引) | – |
| 28 | `files.delete_request` | user/orchestrator | 显式清理文件 | path, run_id 范围 | (工件索引) | ✔ |
| 29 | `repro.manifest_built` | validator/orchestrator | 交付前（L-D） | manifest：raw→prep→估计 do→结果 hash 全链 | repro | – |
| 30 | `env.snapshot` | orchestrator | 复现/换机前 | Stata 版本+关键 ado+OS | provenance | – |

**可读语义**：`run.requested→tool.call→tool.result→run.succeeded/failed/uncertain` 是执行链的最小合法形状（闭合不变量，SPEC N3）；缺任一环即状态不完整，`health.probe` 或恢复时抓出。

### 3.3 事件不变量（写入即校验）

1. **seq 连续单调**：写者拿 revision 分配 seq；重复 seq 拒绝。
2. **闭合**：每个 `tool.call`/`run.requested` 最终有一个 terminal（tool.result 或 run.*）；`result` 带原 `tool_use_id`/`operation_id`，否则模型下一轮会以为动作没发生。
3. **fingerprint 幂等**：同 `(idea,event_type,fingerprint)` 已存在 → 复用而非重复执行（尤其工具重试）。
4. **非法序列拒绝（reducer 是守门员）**：未 started 就 completed、completed 又 append attempt、branch 上追认不属于该分支前缀的因果、claim 引用不存在的 card——一律抛错，不做静默修复。
5. **写入权**：按 §2.7/§3.2 写者列校验（`card_signed` 只接受 validator 身份的 actor）。
6. **只 append**：不允许 UPDATE/DELETE（含投影也不回写 events）。

### 3.4 分支与 leaf 语义

- **每次"换 spec/换样本/放弃最近尝试" = `branch.created`**：新分支继承 fork 点之前的全部事件（不可变过去），之后只 append 自己的新事件；`idea_state.leaf_id` 指向当前分支尾。失败/被否尝试**留在原分支可查**，不覆盖。
- 查询当前状态 = 沿 `leaf_id` 所在分支的 `prev_event_id` 链 fold（+快照）；跨分支对比 = 公共前缀。
- **回滚（rollback）在领域层只做两件事**：① `branch.created` 移到更早 fork 点（隐藏后续尝试但不删除）；② 若确实要清 do-file/结果文件，发 `files.delete_request`（需审批）。**二者永不绑定**（SPEC N13）：隐藏失败尝试 ≠ 磁盘上文件已删。
- fork 后旧分支可以继续并行跑（同一 idea 内多 spec 各占一支，互不污染 ResearchState——这正是多 agent/并行的隔离基础）。

### 3.5 reconcile（对"不确定"的处置）

**触发**：恢复/续跑时，扫描存在 `side_effect_state='uncertain'` 且无后续 `run.succeeded/failed` 的事件。

| 场景 | 判定 | 动作 |
|---|---|---|
| `read` 工具，超时无结果 | 可安全重试（幂等） | 复用 operation_id，attempt+1 重放；或 query 现状确认 |
| `write` 工具（Stata 写数据/do），超时无结果 | 可能已提交 | 查结果台账/工件：`semantic_input_hash` 已有 committed 结果 → 标记 committed 复用；否则 reset+重跑同 operation_id；仍无法判定 → 人工确认 |
| 审批已发未回 | 非不确定，是等外部 | 保留 pending，不重发 |
| 上一进程的 commit 与本次 revision 冲突 | 旧进程写者 | fence 拒绝（§3.6），以新 lease 为准 |

输出一个 `system.restored` 事件记录 reconcile 决定（reuse/rerun/human/abort），供审计。

**写工具失败/不确定一律假设内存数据集可能被污染（审计 A1）**：对 `write` 型工具的 reconcile/重放**不得依赖 stata 会话内的递增 .dta**，须从 prepared 快照重建再跑；否则重放会把失败 do 的副作用（已 merge/append 的行）再叠一遍。读取类不受此约束。

### 3.6 单写者事务 + writer lease/fence

- **提交协议**（SQLite 事务内原子）：`BEGIN → 校验不变式 → INSERT events（取 next seq） → 更新物化投影（同一事务） → bump writer_lease.revision → COMMIT`。投影与事件同事务保证"事件已落而投影未更新"永不发生。
- **多进程/崩溃恢复 fence**：写者先 `acquire writer_lease (writer_id, token)`（带过期）；每次写带 `revision`；旧进程恢复后若 revision 已前进（被新进程用过）→ 拒绝其提交（stale write）。长跑 Stata 会话跨多次进程启动，靠 lease 保证只有一个活跃写者。
- SQLite 需 `PRAGMA journal_mode=WAL` + 单机单 writer 的显式假设（多 writer 迁移触发条件见 SPEC §7/P2）。

### 3.7 快照 + upcaster + 恢复协议

- **快照**：`checkpoint.snapshot` 事件触发，存 `as_of_seq` 的投影全集（或研究状态+索引）；恢复时**先读最近快照，再对 `seq > as_of_seq` 的事件按序 fold**，不扫全史。
- **upcaster**：注册 `(event_type, schema_version) → (event_type', payload')` 迁移函数表；fold 前逐条 upcast 到当前 schema。schema 演进有迁移测试（L0）。
- **恢复顺序**（健康探针 = checklist，SPEC N2）：
  1. 读最近快照 + 重建 leaf 分支到最新 seq；
  2. **reconcile**：处理所有 `uncertain` 且未闭环的写工具（§3.5）——先于一切自动继续；
  3. **闭合检查**：每个 `tool.call/run.requested` 有 terminal；没有 → 标记并决定补结果/重放/挂起；
  4. **一致性**：`run.succeeded` 必有 provenance；ResearchState.current_spec_id 存在于某 family；main_result（若有）有 approval；
  5. **compaction 基线**：压缩 boundary 与当前研究状态一致（引用都在）；
  6. 全过 → 写 `health.probe.pass` → 放行自动继续；任一 fail → 进 blocked 等人/回退阶段。

### 3.8 存储接口（LedgerStore Protocol）

```python
class LedgerStore(Protocol):
    append(idea_id: str, ev: Event, actor=...) -> int          # 返回 seq；单写者事务+投影
    append_many(idea_id, events) -> int                        # 原子批量
    scan(idea_id, *, after_seq=0, branch=None, types=None) -> Iterator[Event]
    snapshot(idea_id, at_seq=None, kind="full") -> Snapshot
    project(idea_id, *, kinds) -> Projections                  # research_state/families/indexes…
    fork(idea_id, from_seq: int, reason: str) -> branch_id
    resolve_uncertain(idea_id, op_id, decision, note) -> Event
    upcast(ev: Event) -> Event                                  # 注册表驱动
```
reducer 实现为纯函数：`ProjectionState = {research_state, families, indexes…}` + `apply(state, ev) -> state`；`project()` 由 `scan→fold→apply` 完成（先快照后增量）。

## 4. 关键场景走查（trace 示例）

### 4.1 一次 spec 试算（ESTIMATION）
```
spec.proposed(agent, spec A)
approval.requested → approval.granted(user)          # 研究闸门：主回归前停等
spec.frozen(A.hash)
run.requested(A, op=op1, input_hash=h) 
tool.call(stata_run, side_effect=read|write)
tool.result(rc=0, machine{...}, 工件引用)             # 闭合
run.succeeded(provenance{do_file,data_signature})
evidence.card_signed(validator, locator=表3-列2)      # 机器层只读 → 卡
claim.signed(evidence_builder, 引用 card)
```

### 4.2 换 spec 即分支
```
branch.created(fork_at=seq_k, reason="换 FE: 双向FE→单向")  # 旧 spec 结果留在原分支
spec.proposed(spec B, variant_of=A) → …（同 4.1）
# family.members 同时看到 A 的 run 与 B 的 run；A 若未选中则只作探索记录
```

### 4.3 崩溃恢复（含 uncertain）
```
进程死前：run.requested + tool.call(write, executing)
恢复：读快照 → 扫描 → tool.call 无 terminal、side_effect_state=uncertain
reconcile：查工件/result_index 无 committed → reset+重跑(op1, attempt2) → tool.result → run.succeeded
写 system.restored(reconcile=rerun) → health.probe.pass → 放行
```

### 4.4 上下文压缩（checkpoint）
```
compaction.boundary(summary=ResearchState+已确认 Claim 索引, 保留最近 N do 文件原文为 tail)
# 只 append；模型下一轮读的是投影（summary+tail），原始历史仍在 events 可随时回放
```

## 5. 派生投影清单（derived，全可重建）

| 投影 | 由哪些事件 fold | 用途 |
|---|---|---|
| research_state | idea/spec/branch/run/claim/family/approval/amendment | 模型 prompt、研究状态展示 |
| experiment_families | spec/run/main_result | 防选择报告、L4 方法学审计 |
| result_index | run.* + evidence.card_signed | 数值接地、写作渲染、复现 L3 |
| citation_index | (RAG) claim/card | 引用接地 |
| claim_index | claim.signed/retracted | writer 输入 |
| approval_log | approval.*/amendment | 审计、审批续跑 |
| trace/UI 视图 | 任意 | 回放/调试 |
| 检索/embedding | 工件+事件 | RAG（derived，坏可重建） |

## 6. 落地顺序指南（不排期）

1. **无 Agent 事件内核**（SPEC 阶段 0 精神）：events 表 + append 事务 + 不变式校验 + scan + 快照/恢复骨架——用纯脚本/测试驱动，先不加 LLM。
2. **最小 domain reducer**：ResearchState + run/result + evidence.card_signed/claim.signed 三个 reducer + 单写者 lease/fence。
3. **分支与 reconcile**：fork + leaf + reconcile 决策表 + 健康探针 checklist。
4. **事件目录补全**：按 §3.2 补 approvals/amendments/budget/privacy 等运营事件。
5. 之后才是接模型（spec.proposed / agent_step 由 agent 写）与 stata-mcp（tool.* 由 MCP 适配写）。

## 7. 开放决策 / TODO（进实现前要钉死）

1. `seq` 全局单调 vs 每 branch 单调 + 合并排序——先按全局单调（单写者下等价），多分支并行写时评估。
2. `fingerprint` 精确算法与哪些 event_type 需要它（先只对 tool.call/run.requested）。
3. payload 存 JSON 文本 vs 拆列——先 JSON（SQLite json_extract 查询），性能不达标再物化热字段。
4. upcaster 注册表组织（模块内 dict vs 声明式文件）。
5. 工件存储布局（`outputs/runs/<run_id>/…`）与 hash 校验时机。
6. 快照粒度（full vs research_state only）与触发点（每 phase 尾 vs 每 N 事件）。
7. 多分支并行的展示/合并（UI 后置问题，现在只定数据语义）。

## 8. 面试叙事（一句话）
> 所有状态都是事件的投影，所有正文数字都是一张签名卡片的坐标，模型只能提议、不能署名——这就是把"研究过程"变成"可追责的证据链"的机制。
