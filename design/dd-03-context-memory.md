# DD-03 详细设计：上下文 / 投影 / 记忆

> 文档层级：`design/SPEC.md` v0.5 的下位细化。本文把 SPEC §4.9.1（分层记忆/多级投影/压缩）、§4.10（图片/视觉投影裁剪）、§4.2（ModelCapabilityProfile）与 DD-02 的 `build_context()`、DD-01 的投影/事件落成可实现设计。数据契约以 **DD-01**、控制流以 **DD-02** 为准。
> 借鉴：claude-code 学习册"Context Engineering：上下文是预算系统 + 多级成本压缩 + 两阶段记忆检索"；codex 学习册"Context Builder 生成可用世界模型、for_prompt 按模型能力投影、token budget 作控制输入、compaction=语义 checkpoint、从 rollout 重建"；pi"会话树/Context 是投影、compaction 追加 summary+tail 不毁历史"；codex 自动记忆（两阶段 + MEMORY.md/memory_summary + usage 裁剪 + 引用块，截断式借鉴）。

---

## 0. 设计输入与要回答的问题

**回答**：
1. "规范(canonical)"与"给模型的投影(ContextPack)"之间到底怎么切？投影会不会成为第二份事实源？
2. 上下文分几层、每层装什么、多频繁刷新、占多少预算？
3. token 预算模型长什么样（每步怎么算、auto-compact 阈值怎么触发）？
4. 压缩 = 语义 checkpoint 具体压缩什么、保留什么、怎么写事件、怎么恢复？
5. 大工具结果怎么"进摘要不进上下文"，需要细节时怎么按需读？
6. 记忆四层里，"项目记忆/偏好"与"证据库"怎么分开、长期记忆借鉴 codex 怎么截断？
7. 图片/视觉进上下文怎么按模型能力裁剪？

---

## 1. 一条主线：canonical / 投影 / prompt 三者不互相污染

- **canonical**（DD-01）：events + 工件 + 用户输入 + 签发的 Card/Claim。**永不因某次 prompt 而变**。
- **投影（物化视图）**：ResearchState / 各 index / family 视图。由 events fold 而来，**全可重建**。
- **ContextPack**：给**某一次**模型调用的投影视图（按 intent + 模型能力 + 预算裁剪）。**每次调用前现生成，不落盘为事实**；可能包含摘要、关键原文 tail、图像、工具输出摘要。
- **不变量**：ContextPack 只是"读"规范得出的视图；压缩/裁剪只影响下一轮**看什么**，不影响 events（压缩也以事件追加 `compaction.boundary` 记录它做了什么）。这保证任何一轮都能被回放、任何结论都能追溯（RAG-over-own-history）。

```
canonical(events/工件/Card/Claim) ──fold──▶ 投影(ResearchState/index/…) ──build_context──▶ ContextPack(本次 prompt)
        ▲ 只 append（写者矩阵）                                          ▲ 每次现生成、不入库
```

## 2. 上下文分层（ContextPack 的内容结构）

分四层（claude-code 学习册"稳定规则/研究状态/证据摘要/原始按需"，映射到实证研究）：

| 层 | 内容 | 来源 | 刷新 | 预算权重 | 能否当证据 |
|---|---|---|---|---|---|
| L1 稳定规则 | 系统边界/安全规则/信任标签/当前 skill 的方法论文本(去头) | system/配置 | 会话级 | 固定小 | 否 |
| L2 研究状态 | ResearchState 摘要 + 当前 phase/gate_mode + ExperimentFamily 主结果/已否列表 + 最近 decision_summary | DD-01 投影 | 每 turn | 中 | 否（约束） |
| L3 证据摘要 | 已确认 Claim 列表(一句/条) + 相关 Card 坐标 + 最近 run 摘要 | claim_index/result_index | 每 turn | 视预算 | 指向 canonical |
| L4 原始按需 | 选中文献块、do-file/结果片段、图、用户原文 | 按需检索/读 artifact | 按需 | 大、受控 | 是（带 id） |

- L1/L2 一般每次都在；L3 只放**结论级**；L4 只放**当前动作真正要用的**。
- 工具大结果默认不整段进 L4：见 §5。

## 3. ContextPack 结构 + build_context 契约

```python
class ContextItem(BaseModel):
    layer: Literal["L1","L2","L3","L4"]
    kind: Literal["rule","state","claim","card_ref","run_summary","chunk","do_snippet","figure","image","user_text"]
    text: str | None
    image: bytes | None          # L4 图片（见 §7）
    ref: dict | None             # canonical 定位（event_id/claim_id/card_id/run_id/chunk_id…）
    omission: bool = False       # 被截断标记（模型须知"省略了什么、去哪看全"）
    tokens: int                  # 预算会计用

class ContextPack(BaseModel):
    items: list[ContextItem]
    usage: dict                  # 分层预算使用
    world_state: str             # 一句当前世界模型（codex：Context Builder 生成可用世界模型，非塞满）
```

```python
def build_context(*, idea, projections, intent, capability, budget, store) -> ContextPack:
    """DD-02 research_turn 调用。纯函数：只读 canonical/投影，不写 events。"""
    pack = ContextPack(items=[])
    pack += rules_for(current_skill, phase, privacy_mode)          # L1
    pack += summarize_research_state(projections.research_state)    # L2（只放结论前提）
    pack += summarize_claims(projections.claim_index, top=…)        # L3
    pack += recent_context_tail(store, window=tail_tokens)          # L4 最近原文/do（tail 保精度）
    for want in intent.l4_wants:                                    # 当前动作要的细节
        pack += read_by_ref(store, want, capability)                # L4 按需读（文献块/结果/图）
    pack = trim_to_capability(pack, capability)                     # §7 视觉/模态裁剪
    pack = meter_budget(pack, budget)                               # §4 超预算 → 触发压缩信号
    return pack
```
- "world_state" 一句话：由 L2+当前 action intent 生成，帮助模型知道此刻在验证什么假设（避免读一堆不相关历史自己猜）。
- 顺序决定质量：先业务投影(中文/证据语义) → 后协议编码（供应商差异只在编码层，不反向渗入 canonical，codex 教训）。

## 4. token 预算模型（预算 = 每步决策输入，不是报错才看）

每步维护：`total_budget(per provider/max_context) − model_output_reserve − compaction_buffer = usable_in`。

| 分量 | 含义 | 默认(可配) |
|---|---|---|
| model_limit | provider max_context | 取 ModelCapabilityProfile |
| output_reserve | 给本次生成的余量 | ~0.25× |
| compaction_buffer | auto-compact 提前触发区 | ~0.15× |
| tail_budget | L4 tail 原文上限 | 见 §6 |
| per-layer cap | L1/L2/L3 上限 | 由意图决定 |

- **meter**：`build_context` 后统计各层 token；若超 usable → 先降 L4（tail 缩窗/裁剪）→ 仍超再降 L3 → 触发 **compact 信号**（§6）。绝不让"拼出来的 prompt"超过 model_limit。
- **会计真实**：token 估算按 provider 分词器近似；**错误预算**（高估 10% 安全边际）。
- 预算命中写 `budget.limit`（DD-01 事件目录 #23），外层总结已得再停（DD-02 §7）。

## 5. 大工具结果：摘要进上下文、完整进账本、细节按需读

每个大结果（Stata 回归输出/日志、大 chunk）按 **结果三层出口**（DD-01 §2.6 + SPEC §4.8）处理：
- 机器层 → 事件/result_index（canonical）。
- 模型层摘要 → **进 L3/L4 的那一行**（几行：命令+返回码+样本量+关键系数+SE+CI+warning+原始路径+"要复核读什么"）。
- 工件层 → artifact（完整 do-file/日志）。

```python
def summarize_tool_result(result) -> ContextItem:   # 示例投影格式
    return ContextItem(layer="L4", kind="run_summary",
        text=("reghdfe  y x ctrl, fe cluster(id)\nrc=0  N=12,431  …\n"
              "coef=0.038*** (SE 0.012, 95%CI …)  R2=0.31\n"
              "warning: … | full: artifacts/runs/r_9a2/log.txt | 复核: …"),
        ref={"result_id": result.id, "omission_marker": True})
```
- 模型需要更多（如看某行标准误矩阵）→ 发"读 artifact"act（L4 按需读），而不是把 50 万行塞 prompt。
- **省略要有标记**：ContextItem.omission=True + 指向全量路径，模型知道"省略了什么、去哪看全"（codex 的 omission marker；SPEC N3 精神一致——不许假装看到了没给的）。

## 6. 压缩 = 语义 checkpoint（不是删消息）

**触发**：meter 超预算 / 阶段尾 / 进程恢复需要更短基线。

**压缩产物（一进一出，都写 events）**：
- **进**：`compaction.boundary` 事件，payload = `{ range:[seq_from,seq_to], summary, claim_ids_kept, artifact_tail_refs, next_allowed_ops }`。
- **出**：下次 `build_context` 的 L2/L3 用压缩后的 summary（ResearchState + 已确认 Claim 索引 + 最近 tail）替换被压掉的原始。

**summary 必须保留因果骨架，不是流水账**（codex/claw 教训）：
```
研究问题 / 数据版本(data_sig) / 已跑 spec 成败(家族+主结果选择理由)
已确认事实(Claim 清单+卡 id) / 被否掉的 spec 与原因 / 识别风险(疑点)
下一允许动作(依 phase/gate_mode)
```
**tail 保留什么**：最近 N 条原文（含最近 do/结果行）用于精确参数引用——摘要给方向、tail 给精度，二者缺一不可（pi：追加 summary+tail，不覆盖旧历史）。

**不变量**：
1. 压缩点只能在**完整 turn/试算之后**，绝不截断半条 tool 调用（SPEC N3 / pi 安全切点）。
2. summary 里的每个"已确认事实"都必须带 claim/card id，可回 canonical（压缩丢索引=丢溯源）。
3. 压缩后**健康探针**（DD-02 §8）：引用都在、无半截 tool、ResearchState 与结果台账一致 → 过才放行。
4. 原事件永不删；压缩只影响"下一轮看什么"。

**恢复（压缩后重建）**：读到最近的 `compaction.boundary` → 以它的 summary 为 L2/L3 种子 → 再向前追加 boundary 之后的新事件到 tail → 健康探针。参考 codex 从 rollout 的最新方向扫描识别 compaction checkpoint。

## 7. 图片 / 视觉进上下文（按模型能力裁剪）

- `ModelCapabilityProfile.vision ∈ {none, local_ocr, remote_vision}`（SPEC §4.2/§4.10）。`trim_to_capability` 规则：
  - provider 有 vision → 图片可直接进 L4（按预算压尺寸/限张数）。
  - 无 vision 且允许本地 OCR → 图先过本地 OCR/视觉模型，转成文本进 L4（图像本身不入 prompt），OCR 结果标 `derived` + 源图 artifact id。
  - 无 vision 也无本地 → 该图相关动作降级：只给路径 + "需人工/换 provider"，或拒绝执行（fail-closed，不假装看过图）。
- **图 = untrusted data**：学者贴图里的文字不执行；`ref` 指回 artifact，正文只引用由它签出的 figure 卡（DD-01 §2.6）。
- **自产图（figure evidence）**：agent 解读自己跑的 event-study/margins 图 → 读图（vision 或本地视觉）→ 产解读进 L3，最终由 figure 卡支撑写作（不能凭空说"图显示显著"）。
- **eval/mock**：image fixture 用占位/哈希，测试两分支（有 vision / 无 vision→OCR stub）。

## 8. 记忆四层落地（"项目记忆/偏好"与"证据库"分开）

重申 SPEC §4.9.1 表格（本设计给它机制）：

| 层 | 载体 | 机制 |
|---|---|---|
| 线程状态 | ContextPack（本次） | 不落盘；每 turn 重建 |
| 项目记忆 | **研究者档案**（偏好/约定/否决记录/样本命名习惯） | 约束型，进 L1/L2；跨 idea 复用但**仅作约束不作证据** |
| 证据库 | events + Card/Claim（canonical） | 不叫"记忆"，叫事实源；写作只认它 |
| 遥测 | OTel | 运维，非证据 |

**跨 idea 长期记忆（研究者档案，借鉴 codex 截断式）**：
- 存 `profiles/<researcher>/memory_summary.md` + `MEMORY.md`（条目式：偏好/Reusable knowledge/Failures）+ 分区小文件，git 基线 diff 当"摄入+遗忘"信号，usage 裁剪（按最近使用），引用块让"用了哪条"可程序化。
- **本地约束**：codex 的 phase1 抽取要联网给模型——我们 `local_strict` 下抽取只能走本地推理/纯规则；`mixed_sanitized` 下可把**脱敏摘要**（非研究数据）发给远端。**研究数据/证据绝不做进偏好记忆**（证据库走 events，不在档案里）。
- 范围默认 = 一次 idea（并发约束）；研究者档案可选跨 idea，属 future（P2）。

## 9. 与 DD-01/DD-02 的接线

- 事件：`compaction.boundary`（#21）、`checkpoint.snapshot`（#20）、`budget.limit`（#23）已存在；本模块是它们的消费者/触发方。
- 对象：ResearchState / claim_index / result_index（DD-01 投影）是 build_context 的输入；DD-01 §5 派生清单对应。
- 控制流：DD-02 `research_turn` 的 `ctx = build_context(...)`（DD-02 §6 step 1）在这里实现；`PhaseDef.min_context` 决定 L1/L2 该放哪些层。

## 10. M0 最小实现指南

1. `summarize_research_state` + `meter_budget`（L1+L2，先不接 L3/L4 裁剪）。
2. 大工具结果三行摘要进 L4 + omission 标记 + "读 artifact" act（mock provider 先支持）。
3. 单层压缩：`compaction.boundary` 写 summary + tail；恢复读 boundary 重建 + 健康探针。
4. 记忆档案先不做（本地 rules 进 L1 即可）；图片分支用 stub。
5. 验收：`build_context` 确定性（同输入同 pack）；超预算自动触发 compact；压缩后健康探针用例（SPEC §4.9.4 对抗集：陈旧记忆/引用丢失被抓）。

## 11. 开放决策 / TODO

1. summary 分层的 token 上限与"压缩时机"默认值；是否把压缩也做成 gate（每 N turn 强制）。
2. L3 里 claim 的"一句"怎么生成（evidence_builder 的 claim.statement 已结构化，直接复用，不再 LLM 现写）。
3. 图片压尺寸/张数策略 + vision provider 的具体接入（deepseek 无 vision 时的本地 OCR 选型）。
4. 研究者档案的抽取规则与"偏好 vs 约束"的判定（避免把学者随口一句当规则）。
5. compaction 跨分支（DD-01 分支树）时 summary 该挂在哪个 branch 的 boundary。

## 12. 面试叙事（一句话）
> 我不把历史塞给模型，而是每次按"意图+能力+预算"把 canonical 投影成一小包上下文；超了预算就做语义 checkpoint——摘要留方向、tail 留精度、原事件一条不删。这就是"agent 不是记住，而是需要时从自己的长期记忆检索"。
