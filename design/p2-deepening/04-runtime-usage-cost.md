---
artifact: solution-brief
version: "0.1"
created: 2026-09-21
status: implemented-first-slice
---

# Solution Brief: Runtime Usage & Cost

## Problem Recap

系统已经记录 Provider Attempt、输入输出 token、缓存 token、Tool/Operation 和 Turn Budget，但用户还不能直接看出一次研究为什么耗时、是否发生无效重试，以及哪些环节消耗最多。若直接建设精确计费系统，会把本地研究工具带向不必要的财务复杂度。

## Proposed Solution

提供以“防失控和解释运行”为目标的 Turn Usage Report。运行预算负责限制循环和资源消耗；使用报告负责解释模型、RAG、Evaluator、Tool 和 Stata 的消耗；金额只作为带质量标记的估算。默认不因估算金额自动停止研究，但用户未来可以配置提醒或硬预算。

```text
Provider / Tool / Runtime facts
        ↓
Usage Aggregation
        ↓
Turn Usage Report
├── tokens and cache
├── attempts and retries
├── tool/stata duration
├── estimated monetary cost
└── no-progress indicators
```

## Key Features

1. **多层聚合：** Attempt、Invocation、Step、Turn 和 Workspace 均可汇总，同时保留向下钻取到具体调用的能力。

2. **成本质量标记：** token 和金额分别显示 `exact / estimated / unknown`；缺失价格不是零成本。

3. **重试与浪费可见：** 单独显示 Provider retry、fallback、失败 Tool、重复调用和未产生新研究进展的消耗。

4. **预算联动：** 展示剩余 Step、Tool、Provider Attempt、token 和 wall-clock budget；到达硬限制后按既有 Turn 语义收敛或暂停。

5. **研究效率解释：** 报告 RAG、Evaluator、正文生成和 Stata 执行占比，供用户和开发评测使用，不对研究方法本身打分。

## Success Metrics

| Metric | Current | Target | Timeline |
| --- | --- | --- | --- |
| Provider Attempt token 覆盖 | 已保存 exact/estimated/unknown | 所有终态 Attempt 都有明确 usage quality | 首个 vertical slice |
| Retry/fallback 归因 | Trace 可查但未聚合 | Turn Summary 可分别显示次数、token 和估算金额 | 首个 vertical slice |
| Tool/Stata 耗时覆盖 | Diagnostics 分散记录 | 正式运行的主要 Tool 都能提供 observed duration 或 unknown | 第二轮 |
| 成本估算诚实性 | 尚无统一报告 | 无价格或 usage 时显示 unknown，不显示 0 | 首个 vertical slice |
| 预算可解释性 | 有 runtime ledger | 每次预算终止能够说明消耗项和剩余额度 | 首个 vertical slice |

## Trade-offs Considered

| What We're Not Doing | Why |
| --- | --- |
| 建设精确账单或支付系统 | Provider Usage 可能延迟或估算，本产品也不负责结算 |
| 默认按金额自动终止 Turn | 研究价值和调用成本不能只用固定金额判断；先提供配置能力 |
| 给本地 Stata/Python 虚构美元价格 | 默认只记录耗时和资源使用，用户可未来配置本地成本模型 |
| 使用实时网络价格作为唯一事实 | 网络价格会变化且无法解释历史；使用应用内版本化价格配置 |
| 把成本写入 Research Evidence | 成本是运营事实，不是统计结论或文章证据 |

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| 估算金额看起来过于精确 | High | Medium | 显示 usage/pricing quality、币种和价格版本；适当舍入 |
| Provider 计费规则变化导致历史漂移 | Medium | Medium | 估算绑定当时的价格 revision；不使用新价格重算旧报告 |
| 为记录耗时侵入权威执行链 | Medium | High | 原始执行事实保持不变；耗时属于可缺失的运营观察 |
| 用户误把成本低理解为研究质量高 | Medium | Medium | Usage Report 与 Evaluation Score 分开，仅并排展示 |
| 日志意外包含密钥或完整敏感输入 | Low | High | 只保存 identity、usage 和 hash；复用 Sensitive Output/credential 边界 |

## Next Steps

1. 列出现有 Provider、Tool、Stata、RAG 和 Evaluator 可取得的 usage/timing 字段。
2. 定义 Usage Fact、Price Revision 和 Cost Projection 的最小边界，避免新建通用计费领域。
3. 先完成 Turn 级 JSON/API Summary，再决定 UI 展示。
4. 为 retry、fallback、cache、unknown usage 和预算耗尽建立测试 Case。
5. 将 Usage 指标接入 Product Evaluation Report，用于识别“质量不变但成本显著上升”的回归。

## Implemented First Slice

V0.1 已提供 Turn 级只读 API 与 Conversation 内按需展开的 Usage 卡片。聚合直接读取
Workspace SQLite 中的 Provider Attempt、Operation 和 Turn Budget 事实；价格来自进程启动时
加载的本地版本化配置。价格缺失、失败 Attempt 未返回 usage，或缓存费率不兼容时，完整金额
保持 `unknown`，同时可以返回已知 subtotal。当前价格结果是带 `pricing_revision` 和
`generated_at` 的运营投影，不是账单或研究证据；若未来要求历史金额永久不随配置变化，再为
Attempt 增加 price binding，而不修改现有运行事实。

首个 slice 有意只完成 Turn 汇总和 Attempt/Operation 下钻。Workspace 跨 Turn 趋势、RAG 与
Evaluator 阶段占比、无进展效率回归继续留在 Product Evaluation 集成阶段，因为当前权威事实
还没有稳定的 invocation-purpose 分类，不应从 prompt 文本猜测。
