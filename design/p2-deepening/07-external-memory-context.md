# 外部记忆与动态上下文系统

## 决策

Stata Research Agent 不使用有损对话压缩维持长期任务。完整 Memory revision
写入 Workspace 文件系统，SQLite 维护身份、版本、来源、生命周期、替代关系、
使用记录和检索元数据。模型上下文只是一次 Step 的临时工作台。

```text
Canonical Memory Files
        ↓
SQLite identity / source / time / path index
        ↓
Recommendation-style retrieval
        ↓
Exact file open / exact span materialization
        ↓
Dynamic Context Compiler
```

## 不变量

1. 不生成 Context Summary 来替代超出预算的内容。
2. 不把 Memory Summary Projection 自动注入模型。
3. 超预算内容保留原始身份，并记录 `excluded` Context Build Decision。
4. `memory.search` 返回引用、精确前缀预览和排序依据，不返回改写摘要。
5. `memory.open` 从受管的不可变 revision 文件读取精确内容，并核对文件身份。
6. 新决策通过 revision 或 supersession 表达，不覆盖旧历史。
7. Memory 是建议性上下文，不是 Result、Evidence 或当前 Research State。

历史数据库中的 episode、summary projection 和 compaction checkpoint 表暂时保留以避免
破坏已有 Workspace。当前 Runtime 不读取 summary/compaction 内容；兼容 projection 只写
Memory ID、revision ID、类型和标题的引用目录，不再复制或改写 Memory 正文。

## 存储平面

```text
.stata-agent/memory/
├── MEMORY.md                 # 只含 ID、标题、类型和路径的导航目录
├── revisions/               # 不可变完整 revision
└── items/                   # 用户可读、可编辑的 current view
    ├── workspace/
    └── paths/<path-id>/
```

`MEMORY.md` 不是内容摘要。外部修改 current view 时，系统创建新的 imported revision；
不可变 revision 不被覆盖。

## 召回平面

### 自动热路径

每个 Step 根据当前用户消息，从 Hot Memory 中低成本召回。自动路径不增加模型调用，使用：

- 中英文词项和短语重合；
- 当前 Research Path 匹配；
- Memory kind 与问题意图；
- explicit / confirmed / inferred 权威等级；
- pinned、access tier、最近使用；
- 共同 source object；
- supersession 信号转发。

### 显式检索

Agent 通过 `memory.search` 主动检索。默认 `hybrid`：在配置本地 Embedding Gateway 时，
融合本地语义相似度；未配置时自然降级到 lexical + relation。Agent 可以选择 `lexical`
避免额外的向量计算。

召回结果经过相似度惩罚，避免多个高度重复 Memory 占满候选列表。返回：

```text
memory_item_id
memory_revision_id
memory_file
exact-prefix excerpt
retrieval_score
retrieval_reasons
source identities
supersession state
```

Agent 随后通过 `memory.open` 打开需要的精确 revision。

## 更新与时间语义

旧 Memory 命中但已被替代时，系统把匹配信号传递给当前 successor，默认不把旧对象作为
当前答案返回。只有显式历史检查才允许检索 archived/superseded 对象。

```text
old decision --superseded_by--> current decision
      │                              ↑
      └── old query match forwarded ─┘
```

后续需要更丰富关系时，增加 retrieval-only link，不改变 Research Facts。模型推测的关系
不能直接成为权威关系。

## 动态上下文预算

每个 Step 重新计算：

```text
hard input limit
= model context window - reserved output

Context Item budget
= hard input limit
 - actual System Prompt / Main Skill / Tool Schema / runtime envelope
 - configured runtime safety reserve
```

该计算使用当前模型配置和实际固定前缀，不采用固定 96K，也不按固定最近 N 轮裁剪。
Context Compiler 按 P0-P3、recency、group atomicity 和 provider policy 选择精确内容；
可选内容装不下就保留外部引用并记录排除原因，强制内容装不下则失败关闭。

## 评测

Memory/Context 子系统至少持续记录和评估：

- exact current revision recall；
- correction / supersession accuracy；
- cross-conversation 与 cross-path precision；
- source-linked multi-hop recall；
- irrelevant-memory silence；
- abstention under missing memory；
- exact file open 与 ledger identity 一致性；
- Context mandatory retention、optional exclusion 和无有损替换；
- 每次 Step 的 fixed tokens、item budget、实际输入与 cache usage；
- lexical/hybrid 检索的延迟、候选数和最终使用率。

召回只是候选生成。真正的成功指标是 Memory 是否被打开、是否进入 Context、是否被最终
回答或研究动作实际使用，以及用户是否随后纠正。

### 已落地的评测入口

- `memory-correction-v1` 保留对内容纠正、来源和错误激活的检查；
- `memory-retrieval-v2` 使用生产 `memory.search` / `memory.open`，覆盖 lexical、hybrid、
  no-answer、supersession、Research Path 隔离、shared-source 扩展和文件篡改拒绝；
- Layered Evaluation 分开报告 correction 与 retrieval，不把两种不同问题混成一个平均分；
- Operational Evaluation 从普通 Workspace 权威记录持续计算搜索、打开和 Context 使用漏斗。

当前检入基线位于 `verification/memory-retrieval-v2-baseline.json`。完整逐 Trial Bundle 写入
`verification/runs/`，该目录默认不进入版本控制；基线只保留可比较的场景、数据集身份与
聚合指标。
