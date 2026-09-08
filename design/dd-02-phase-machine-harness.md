# DD-02 详细设计：阶段状态机与 Harness（编排器）

> 文档层级：`design/SPEC.md` v0.5 的下位细化。本文把 SPEC §4.3（Harness 与阶段状态机）、§4.9.3（异常/恢复）、§4.9.4（评测 L2 轨迹）、§5（M0–M4）落到可实现的"阶段×属性 + 迁移表 + 门控 + 编排循环"。数据/事件契约以 **DD-01**（`design/dd-01-domain-events.md`）为准。
> 借鉴：codex 学习册"受约束状态转移 + 主循环 run_turn"；claude-code 学习册"query.ts 状态机 + 状态转移表含 blocked + 多条恢复路径"；claw-code 学习册"research_turn 理想循环（validate→load→while not terminal→verify→persist→maybe_compact）"；pi 学习册"双层循环（外层 follow-up / 内层 tool batch）+ 取消不是错误"。

---

## 0. 设计输入与要回答的问题

**回答**：
1. 阶段(phase)与运行态(run_status)如何正交？
2. 每个阶段的属性是什么（进入/退出/工具/人工门/失败策略）？
3. 合法迁移表长什么样（谁有权触发、什么 gate 通过才走）？
4. 门控模型：人工门 vs 机器证据门槛（L-C/L-R/L-D）怎么协同？
5. 模型能提议什么、编排器决定什么（ActionProposal 契约）？
6. 编排主循环（research_turn）怎么跑、两层循环怎么切？
7. 预算/停止/中断（steering）怎么落地成事件与状态？
8. 健康探针在哪些点触发？
9. M0 最小可跑 loop 的最小形态。

---

## 1. 两个正交维度：phase × run_status

**阶段（phase）= 研究内容走到哪**（长期，存 ResearchState/idea_state）。
**运行态（run_status）= 此刻编排器在干嘛**（短期，等谁/在跑/卡住）。

不要把两者混成一个变量（codex/claude-code 的教训：混了就无法表达"ESTIMATION 阶段被审批卡住"这种常态）。

```
phase ∈ {IDEA, LITERATURE, DESIGN, DATA, ESTIMATION, ROBUSTNESS, WRITING, VALIDATION, DONE}
run_status ∈ {idle, busy, awaiting_user, validating, failed}     # blocked 即 awaiting_user
```

- `busy`：编排器在内层 loop 中（可能并行 read-only 工具）。
- `awaiting_user`：卡在人工门/审批/steering（等价于 SPEC 的 blocked，是**正常态**不是异常）。
- `validating`：机器验证中（L-R/L-D），验证事件决定去留。
- `failed`：当前阶段失败策略裁定需回退/停机，见 §6。
- `DONE` 后存档，不再接受 agent 自动动作（只收用户 reopen）。

## 2. 阶段属性表（每阶段一份配置，数据驱动）

每个阶段 = 一份声明式配置 `PhaseDef`（SPEC：不要写死，做成数据/配置驱动）：

```python
class PhaseDef(BaseModel):
    phase: str
    purpose: str
    entry_conditions: list[str]      # 断言（可评估的检查名，如 data.ready / feasibility.ok）
    allowed_ops: list[str]           # 编排器可执行的 op（见 §5）
    allowed_skills: list[str]        # 阶段内可激活的 skill
    exit_gate: Gate | None           # 本阶段出口：机器门槛 + 人工门
    max_attempts: int
    failure_policy: FailurePolicy
    min_context: list[str]           # prompt 至少注入的投影（ResearchState/result_index…）
```

| phase | 目的 | entry_conditions（示例） | 主要 allowed_ops | exit_gate | max_attempts | failure_policy |
|---|---|---|---|---|---|---|
| IDEA | 立题/研究问题/贡献 | idea.declared 存在 | ask_user, read, rag(文献) | 研究问题+贡献写成 idea.md 且 user 确认 | 3 | 重写 |
| LITERATURE | 找定位/缺口/方法 | idea 稳定 | rag(citable), ask_user | 给"已有人做过X/缺口在Y"+引用清单，user 确认 | 3 | 细化检索再回 |
| DESIGN | 可行性+实证设计 | 文献结论 OK | ask_user, file_reader(inspect), spec.proposed, rag | **L-C 证据**（数据可构造+缺什么清单）→ 人工门 | 5 | 回 DESIGN 澄清/回 DATA |
| DATA | 数据就位/变量构造 | 设计过 | file_reader, run(read), data_refs 核对 | 变量可构造证据+样本签名，user 确认 | 3 | 回 DATA 补料 |
| ESTIMATION | 主回归/机制/异质性跑通 | DATA OK | run(write/read), spec.proposed, interpret, evidence 链 | **L-R 证据**（rc=0+结构化+校验过）→ **人工门(主回归)** | 10 | 回 ESTIMATION 调 spec（受 confirmatory lock 约束） |
| ROBUSTNESS | 稳健性/安慰剂/机制 | 主回归批准 | run(read), spec.proposed(variant), rag | 变体家族跑完+对比结论，user 确认 | 8 | 回 ESTIMATION |
| WRITING | 写实证初稿 | 结果定 | rag(style_only), writer, ask_user | 初稿草稿（仅渲染已验证 claim）→ 人工门 | 5 | 回 WRITING |
| VALIDATION | 终验 | 草稿在 | validator(L-D), 复现检查 | **L-D 通过**（对抗用例全抓） | 3 | 回 WRITING/回 ROBUSTNESS |
| DONE | 存档 | L-D 过 | （只读） | 存档清单完成 | – | – |

> 说明：IDEA/LITERATURE/DESIGN/DATA 的顺序在 SPEC v0.5 §2 里是 DESIGN 之后才 DATA；此处 DATA 单列是"数据就位"门，ESTIMATION 前的变量构造/样本签核查属于 DATA。实际可把 DATA 并入 DESIGN 之后；**迁移表允许多条路径**（见 §3），不必每个 idea 都走满九宫。

> **审计 D1（2026-09-07）：清洗+试 spec 是紧循环，别让门控卡死探索。** 真实流程=快速回归→发现变量问题→回去改构造→再跑，反复几十轮、先于任何正式 gate。`PhaseDef` 加 **`gate_mode ∈ {explore, formal}`**：进 ESTIMATION 初期 / 纯探索默认 `explore`（轻门控：机器门槛只查"能读结果/语法对"，不请求人工门、不上 confirmatory lock、允许 DATA↔ESTIMATION 来回），只有**声明主 spec 或转入 ROBUSTNESS** 才切 `formal`（L-R + 人工门 + lock）。confirmatory lock（SPEC §4.9.5）**不得成为探索期默认**。

## 3. 合法迁移表

迁移**只由编排器提交**（模型只能请求）。每条边标注触发条件。

**前进边（默认）**：
```
IDEA → LITERATURE      user 确认 idea.md
LITERATURE → DESIGN    文献结论确认（引用清单 OK）
DESIGN → DATA          L-C 证据出（可行/缺数据/缺文献 的下一步被 user 采纳）
DATA → ESTIMATION      样本签名+变量可构造 确认
ESTIMATION → ROBUSTNESS  L-R 主结果通过 + 人工批准（研究闸门）
ROBUSTNESS → WRITING     稳健性结论 user 确认
WRITING → VALIDATION     草稿产出（writer 只渲染已验证 claim）
VALIDATION → DONE        L-D 全过 + 存档
```

**回退/重做边（允许子集，其余进 failed 走失败策略）**：
```
VALIDATION(fail) → WRITING        validator L-D 抓出 → 修
VALIDATION(fail·复现) → ESTIMATION  复现不过=结果不可靠 → 回跑/修 spec
ROBUSTNESS → DESIGN              robust 发现识别策略根本问题 → 重设计（须 user + amendment）
ESTIMATION(fail·data) → DATA     数据/变量问题 → 回数据（补料或改构造）
任意 → IDEA                      user steering（彻底换题）→ 新 idea/branch（DD-01 fork）
```

**拦截/兜底**：
- 越权边（如 ESTIMATION 没 L-R 就要进 WRITING）由 `exit_gate` 未过 → 拒绝迁移，留在原阶段。
- `run_status=failed` 时按该 phase 的 failure_policy 决定自动回退边或 `awaiting_user`。

## 4. 门控模型：机器门槛 × 人工门 × 研究闸门

每阶段出口 = `机器门槛(证据等级) AND (有则)人工门(approval)`。

| 门 | 由谁判 | 依据 | 事件 | 不过怎么办 |
|---|---|---|---|---|
| 机器证据门槛 L-C/L-R/L-D（SPEC §4.8） | validator（确定性） | dofile/rc/结构化结果/复现/对抗 | evidence.card_signed / claim.signed / run.succeeded… | 回本阶段/前阶段重做，**不硬出** |
| 人工门（设计结论/主回归/稳健性/初稿前） | user | approval 请求（带"帮用户做决定"的上下文） | approval.requested → granted/rejected/deferred | rejected→回重做；deferred→awaiting_user |
| 研究闸门（改样本/识别/FE/聚类/主结果选择） | 编排器强制 | 变更检测（与 ResearchState diff） | spec.proposed(带变更理由)→ 需 approval 或 amendment.recorded | 阻止静默变更；confirmatory lock 生效时看结果后改必须 amendment |
| 隐私/工具闸门（数据安全） | policy（DD-04） | 信任标签+工具副作用分级 | deny/ask/allow | deny→tell 模型；ask→approval |

**同步规则**：机器门槛先过，才请求人工门（人工不背"数值没验证"的锅）；研究闸门在 machine 判定前先拦（人确认的是"要跑这个变体"，不是事后才知道）。

## 5. 模型 vs 编排器：ActionProposal 契约

**模型产出 ActionProposal，编排器执行 ops**（codex"受约束状态转移"：模型想法≠统计事实；claude-code"生成命令≠执行命令"）。

```python
class ActionProposal(BaseModel):
    # 每条 = 一个候选动作；编排器逐个裁决/执行
    acts: list[Act]
    ask_user: bool                  # 需要澄清/等决定
    stop_reason: Literal["gate","budget","need_input","done"] | None

class Act(BaseModel):
    act_type: Literal[
        "propose_spec",        # 提 spec（含 research 变更理由）
        "request_run",         # 请求执行（引用已 frozen spec）
        "interpret",           # 解读某 run（结论摘要，非 claim）
        "select_candidate",    # 从候选里选（最后仍需 user/orchestrator 落）
        "ask_user",            # 提问
        "read_artifact", "rag_search", "inspect_data",
        "request_robustness_variant",
        "draft_section",       # 写作提议（只能引用 claim/card）
    ]
    target: dict | None        # 结构化参数
    reason: str | None
```

**硬约束**：
- 编排器把 `Act` 映射到 op 时**逐条过 policy**（权限/研究闸门/信任标签）。
- 模型**不能**发出这些 act：`sign_claim` / `mark_done` / `delete_file` / `phase.transition` / 直接改 ResearchState。
- `propose_spec` 必须携带对 ResearchState 的 diff 与理由（研究闸门输入）；confirmatory lock 后解读到新结果再改，须先 `amendment.recorded`。
- 模型说"我完成了"≠ 完成：完成 = exit_gate 证据通过（§4）。

## 6. 主循环：research_turn（含两层循环）

借鉴 codex run_turn + claw 的 research_turn 理想循环。分两层：
- **外层（会话/steering 粒度）**：等用户消息或恢复信号；把 `user.message/steering/approval.granted` 转成一次内层调用；可换 phase。
- **内层（一次 agent turn）**：在**当前 phase 内**循环直到命中 gate/budget/need_input/done。

```python
def research_turn(idea, phase_def, store, policy, budget) -> Status:
    """内层：一次 agent turn。外层由事件驱动（见 §7）。"""
    while True:
        # 1) 恢复/压缩后健康探针（见 §8）；2) 构建投影上下文（DD-03 展开）
        ctx = build_context(project(store, idea))          # ResearchState+result_index+skill
        prop = llm.act(ctx, allowed=phase_def.min_context, schema=ActionProposal)
        store.append(agent_step(decision_summary=prop.reasoning, evidence_ids=…))
        if prop.ask_user: approval.requested(…); return AWAIT_USER

        for act in prop.acts:
            verdict = policy.check(phase=idea.phase, act=act, trust=…)
            if verdict is DENY:   # 告诉模型为什么，回下一轮
                store.append(tool.result(closed_to=…, denied_reason=…)); continue
            if verdict is ASK:    # 研究闸门/权限升级
                approval.requested(what=act); return AWAIT_USER
            # ALLOW：
            match act.act_type:
                case "propose_spec": spec.frozen(…); research_state(…)     # orchest. 侧
                case "request_run":  run.execute(…)                        # → tool.call/result/run.*
                case "interpret":    agent_step(interpretation=…)          # 不许 sign claim
                case "read_artifact"|"rag_search"|"inspect_data":
                     parallel_readonly(acts)                               # §9 顺序写回
                case "draft_section": writer.render(verified_only=True)
        # 4) 阶段出口 gate
        if phase_def.exit_gate.machine:                     # L-C/L-R/L-D validator
            status = validate(exit_gate)                    # run_status=validating
            if status is FAIL: apply failure_policy(phase_def); continue/return
        # 5) 人工门
        if phase_def.exit_gate.human:
            approval.requested(…); return AWAIT_USER
        # 6) 预算/停止
        budget.tick(); if budget.exhausted: store.append(budget.limit); return STOP_SUMMARIZE
        if prop.stop_reason == "done" and no pending acts: break
    # 阶段完成 → 编排器提交迁移（DD-01 phase.transition）
    store.append(phase.transition(from=…, to=next(phase_def)))
    return ADVANCE
```

要点：
- **机器证据没过 → 不请求人工门**；gate 决定"去留/回退"，不是模型自己说了算。
- **单次 turn 内串行执行有副作用 op**；只读 op 可并行但按原序写回 events（pi source-order，§9）。
- **半截 act（LLM stopReason=length）不执行**（SPEC N3），整轮丢弃进重试计数。

## 7. 外层：事件驱动的会话与中断（steering / follow-up / 打断）

外层是一个**事件消费者**，不是 while(true) 聊天：每次有 `user.message / steering / approval.granted / deferred / phase gate 触发 / 恢复信号` 时，决定"调一次 research_turn / 换 phase / 终止"。

- `steering`（换方向/换题）：若仍在同一 idea → 视情况 `branch.created`（DD-01 §3.4）；若彻底换题 → 结束当前 idea，另开（并发约束：一次一个 idea 工作区）。
- **取消 ≠ 错误**（pi）：`cancel` 时若内层在跑工具 → 发 `tool.result(cancelled)`（带原 operation_id）+ run 标 cancelled；**模型下一轮必须知道"没得到结果"**（SPEC N3）。
- **审批回模型**：`approval.granted/rejected/deferred` 都作为工具结果回给模型（rejected 带理由），模型据此改动作——审批是状态机的正常分支，不是旁路。
- 预算/健康探针命中时，外层**先总结已得（decision_summary + 已确认 Claim）再停**，不留半截。

## 8. 健康探针放置（触发点）

`health_probe` 在四处跑（结果写 `health.probe.pass/fail` 事件）：
1. **进程恢复后**（DD-01 §3.7 顺序 2–6 之后）：reconcile 未决 + 闭合 + 一致性检查，过才放行自动继续。
2. **压缩 boundary 后**：重建的投影上下文与 events 一致（引用都在、无半截 tool）。
3. **跨昂贵阶段前**（DATA→ESTIMATION、WRITING→VALIDATION）：机器证据链完整才允许进。
4. **进入 confirmatory 的 ROBUSTNESS 前**：确认 ResearchState 已锁定、无未决探索性改动。

fail 时 → `run_status=failed` + 按 failure_policy 回退，而不是带病继续。

## 9. 并行与顺序规则（M0–M3 内仍是单 agent）

- 默认：有副作用 op（stata run/write）**串行**；同 phase 内只读 op（rag_search/inspect_data/read_artifact）可小批量并行。
- 写回 events **按模型原始 act 顺序**，不是完成顺序（pi：语义层保输入序）。
- 同一 idea 内多 spec 分支（DD-01）允许**各自隔离地读**，但写 ResearchState 仍只有一个活分支（leaf）。真正跨分支并行执行 = P2 多 agent 的门槛（先评测证明）。

## 10. M0 最小可跑 loop（指南，不排期）

最小切片（阶段 0 + M0 前半，无 LLM 也可跑通一半）：
1. `LedgerStore` + events DDL + append 事务/不变式/scan（DD-01 §3.1/§3.8）。
2. `PhaseDef` 配置（九阶段表，data-driven）+ `policy`（研究闸门/信任最小实现）+ `build_context`（先只拼 ResearchState+最近 events）。
3. mock LLM provider（replay 一个 `ActionProposal`）驱动 `research_turn`；CLI：`你说一句 → 一个 turn → 回一句 → 全落 events`。
4. 验收：**确定性重放**——同输入 replay 得同 events 序列（单测）；`phase.transition` 只由编排器提交（L0 用例：模型 proposal 里带 `mark_done` 被拒）。

## 11. 开放决策 / TODO

1. `PhaseDef` 的 entry/exit 条件断言语言（查重名/简单规则 vs Python 谓词注册表）。
2. 每阶段的 `allowed_ops` 与 skill 的粒度：先列白名单枚举，成熟后抽成策略文件。
3. `max_attempts` 的计数口径（模型重试 vs op 重试 vs 阶段重做是否共用）。
4. 人工门请求的"帮用户做决定"上下文格式（claude-code 学习册建议审批附上下文——细节放 DD-04 权限）。
5. ROBUSTNESS 与 WRITING 之间的"结论选定"是否要独立子门。
6. steering 换题时是 fork 还是 close+new idea（先 close+new；fork 留给"换 spec"）。

## 12. 面试叙事（一句话）
> 阶段管"研究到哪一步"，运行态管"在等谁"；模型只能提议、编排器才提交迁移——把"越权"从 prompt 提醒变成不可违反的状态机不变量，这就是可信实证 agent 的控制面。
