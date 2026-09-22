---
artifact: solution-brief
version: "0.1"
created: 2026-09-21
status: implemented-first-slice
---

# Solution Brief: Adaptive Research Plan

## Problem Recap

当前 Plan Revision 能在正式 Stata 副作用之前形成研究计划，也能把 Run 和 Result 绑定到计划节点；但系统尚未清楚区分日常执行调整、研究计划细化和实质研究方向变化。若所有变化都机械生成或阻止 Revision，Agent 要么产生大量噪声，要么失去根据结果自主推进研究的能力。

## Proposed Solution

将 Plan 定义为可演进的“当前工作计划”，而不是执行脚本或方法白名单。Agent 可以根据数据、工具结果、文献和用户讨论调整未来步骤；系统负责说明发生了什么变化、后续执行采用哪个计划，以及变化是否与用户选择的自动化模式相符。

计划变化按影响而不是按研究方法名称处理：

```text
Execution Adjustment
→ 修正命令、补诊断、重试；通常不产生 Plan Revision

Plan Refinement
→ 增删未来步骤、调整顺序、加入稳健性分析；自动生成 Revision

Research Direction Change
→ 显著改变研究问题、核心变量、样本或识别逻辑；提高可见性，
  是否暂停由用户模式、当前风险和 Agent/Evaluator 判断共同决定
```

## Key Features

1. **计划对齐：** 正式 Run 必须关联当前或明确指定的 Plan Node；偏离时 Agent 给出可读说明，而不是静默执行。

2. **变化说明：** 新 Revision 保存基于哪些用户消息、数据观察、文献证据、工具结果或 Evaluator finding 产生，不把模型解释当作唯一事实。

3. **自适应确认：** 自动模式允许 Agent 说明后继续；监督模式对高影响变化倾向于暂停。用户可在 Workspace 级统一切换。

4. **无进展检测：** 综合重复失败、计划往返、相同 Tool Call 和没有新 Evidence 等信号，交由 Turn Driver/Evaluator 决定继续、重试、重规划或等待。

5. **历史完整性：** Replan 只改变未来 current pointer；过去的 Run、Result、Evidence 和 Word Revision 始终保留原计划关系。

## Success Metrics

| Metric | Current | Target | Timeline |
| --- | --- | --- | --- |
| 正式 Run 的 Plan binding | 已有强制路径 | 保持 100% | 持续 |
| 计划变化可解释率 | 尚无统一指标 | 每个非初始 Revision 至少绑定一个可核查触发事实 | 首个 vertical slice |
| 无意义重复计划 | 尚无专项 Case | 语义未变化的重复提案不产生新的 current Revision | 首个 vertical slice |
| 用户决定执行一致性 | Waiting/Context 已存在 | 用户改变核心选择后旧 Dispatch Plan 不继续执行 | 首个 vertical slice |
| 自动模式打扰率 | 未建立基线 | 普通执行调整不触发确认；重大变化在 Trace 中清楚可见 | 真实压力测试 |

## Trade-offs Considered

| What We're Not Doing | Why |
| --- | --- |
| 将研究方法编码为有限状态 Workflow | 会限制新方法和 Agent 自主性 |
| 所有 Plan 变化都要求用户确认 | 会把自动模式退化为逐步审批系统 |
| 用简单 A→B→A 规则强制暂停 | 返回旧方案可能来自新证据，不能单凭形状判断抖动 |
| 现在拆出独立 Planner 模型 | 尚无证据证明额外模型成本带来更好研究结果 |
| 保存隐藏 Chain-of-Thought | 产品只需要计划、依据、动作和决策摘要 |

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Agent 将重大变化描述成普通调整 | Medium | High | 服务端检查受影响的 adopted data/result slots；Evaluator 可提高影响等级 |
| 过度检测抖动阻止合理探索 | Medium | High | 抖动只作为信号；新 Evidence、用户指令或分支会重置信号 |
| Plan Revision 数量过多影响可读性 | High | Medium | UI 默认显示语义变化摘要；执行微调只进入 Trace |
| 自动模式偏离用户原目标 | Medium | High | Context 始终包含研究目标和当前 Plan；候选结束前检查 goal/plan alignment |
| 分支之间错误比较计划 | Low | High | 稳定性检测按 Research Path 隔离 |

## Next Steps

1. 用真实研究过程定义 Execution Adjustment、Plan Refinement 和 Direction Change 的示例集。
2. 设计 Plan Change Summary 和触发事实引用，不先引入大量固定 reason enum。
3. 为自动、监督两种模式定义默认响应，但保留 Agent/Evaluator 判断空间。
4. 建立用户改变量、Stata 报错、加入稳健性、放弃识别策略和分支探索等场景测试。
5. 在真实 auto.dta 研究循环中观察 Revision 数量、打扰率和重复执行率。

## Implementation Record

首个 production slice 已完成。Plan 现在是语义研究计划，不再由当前 Step 的 Stata 命令重建，
也不要求节点保存 exact command。正式 `stata.execute` 使用 `plan_node_key`，主服务在 Tool
Admission 前解析并冻结 Plan Revision/Node 身份；精确命令仍由 Executable Source 和 Stata Run
独立保存并可重跑。任意安全的研究节点类型可以由 Agent 提出，未知类型以开放语义写入
specification，不形成研究方法白名单。

重复的语义 Plan proposal 通过 digest 去重；Revision 保存 `change_kind` 和
`trigger_references`。包含 Waiting 的模型输出不会提前提交 Plan，避免“先采用、后确认”。过去的
Run 继续绑定其执行时 Plan；当前 Plan 演进只影响未来执行与 current eligibility。浏览器 Result
页已增加当前 Plan、节点、依赖和完成 Run 数量展示。

无进展阈值和跨版本效果评测按 Founder 指示暂缓；本 slice 保留 Agent/Skill 的开放判断，不把
研究变化编码成有限状态 Workflow。
