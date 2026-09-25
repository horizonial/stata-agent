---
artifact: solution-brief
version: "0.2"
created: 2026-09-21
status: founder-review
---

# Solution Brief: Product Evaluation Loop

## Implementation checkpoint — 2026-09-21

产品评测的主线已经改为 **Operational Evaluation**：每个真实 Workspace/Turn 在正常研究
过程中自然产生 Journal、Context、Model、Tool、Run、Result、Evidence、Document、Stop Guard
和用户反馈事实，系统直接从这些权威事实计算 L1/L2/L3 指标。评测不要求用户切换到“测试
模式”，也不依赖先制作专门数据集。

三套公开论文复现和固定 Scenario 继续保留，但降级为辅助的 **Benchmark / Replay**：只用于
发布回归、故障注入、算法对比以及已知答案的校准，不代表日常评测主体。

## 1. Decision Summary

V0.1 不建设通用 LLM Benchmark 平台，也不把现有测试通过率包装成 Agent 质量分。

产品在每次真实使用中持续评价：

```text
Hard Truth Gates
+
Agent Behavior / Control
+
Research Quality
+
Efficiency / Reliability
```

四类结果分开报告，不压成单一总分。

其中：

- 数据保真、正式数字来源、Artifact 完整性、用户控制边界、恢复幂等属于确定性硬门禁；
- 研究方案、解释质量、建议价值和文稿质量属于开放质量评价；
- Agent 可以自由选择研究方法和工具路径，评测不得把某一条命令序列写成唯一正确流程；
- Runtime Evaluator 仍服务于单个 Turn 的继续、等待和停止，不能充当产品评测的独立真值来源。
- 日常任务没有金标准时只报告可观察事实、硬不变量和用户结果信号，不虚构“准确率”；
- 用户的“采用 / 继续修改 / 不采用”是结果有用性信号，不等于客观研究质量；
- 固定 Scenario、论文复现和合成故障只是版本发布前的辅助校准层。

## 2. Research Basis

成熟 Agent 评测的共同做法不是只看最后一句回答，而是把任务、隔离环境、完整轨迹、最终环境状态和多个 Grader 组合起来：

- Anthropic 将 task、trial、grader、transcript、outcome、evaluation harness 分开，并建议组合 code-based、model-based 和 human grader；同时区分 capability eval 与 regression eval。
- OpenAI 的 Trace Grading 对完整决策、工具调用和执行轨迹打结构化标签，用于定位成功或失败发生在哪一层。
- Google ADK 分别评价最终回答、工具使用、单轮/多轮 trajectory、grounding 和 task success。
- PaperBench 使用独立执行环境重新运行研究产物，再用分层 rubric 和经过人工校准的 Judge 评分。
- CORE-Bench、MLAgentBench 和 DSBench 都把真实数据/代码环境、长任务执行和可验证结果作为主体，而不是静态问答。
- tau-bench 通过最终后端状态与多次 trial 检查多轮用户协作和工具行为，并报告稳定性而不是只报告一次成功。

参考：

- https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- https://developers.openai.com/api/docs/guides/evaluation-best-practices
- https://developers.openai.com/api/docs/guides/trace-grading
- https://adk.dev/evaluate/
- https://openai.com/index/paperbench/
- https://github.com/siegelz/core-bench
- https://github.com/snap-stanford/mlagentbench
- https://github.com/LiqiangJing/DSBench
- https://github.com/sierra-research/tau-bench

## 3. What The Evaluation Must Answer

每次候选版本评审必须分别回答：

1. **数据是否保真：** 正式数字是否真实来自可定位、可重跑的 Stata 执行？
2. **研究者是否仍有控制：** Agent 是否在需要研究者拍板时等待，并真实执行了用户决定？
3. **任务是否完成：** 从 idea、数据和可选文献出发，是否形成了可继续研究或可交付的产物？
4. **研究判断是否有价值：** 方案、解释、诊断和建议是否合理，而不是只完成机械步骤？
5. **过程是否可恢复：** 暂停、失败、崩溃、重试和分支后是否能保留历史并安全继续？
6. **知识是否正确使用：** 文献、Stata Help、项目记忆和文风材料是否进入了正确的上下文角色？
7. **代价是否可接受：** 完成同等质量任务用了多少模型调用、工具调用、时间和成本？

## 4. Evaluation Architecture

```text
Ordinary Research Use
    ↓
Workspace → Turn → Step → Model / Tool / Stata → Result / Word
    ↓
Authoritative DB + Journal + Artifacts
    ├── hard invariant observations
    ├── subsystem usage/outcome facts
    ├── agent-loop trajectory facts
    ├── product outcome facts
    ├── explicit user outcome feedback
    └── model / Skill / Tool / policy revisions
    ↓
Read-only Operational Evaluation Query
    ├── L1 subsystem metrics
    ├── L2 loop metrics
    ├── L3 product metrics
    ├── failure/retry/repetition breakdowns
    └── configuration slices
    ↓
Turn view → Workspace view → cross-Workspace trend

Release / Algorithm Change
    ↓
Benchmark + Replay + Failure Injection（辅助）
    ↓
与真实使用趋势共同进入版本决策
```

Operational Evaluation 是只读 Query / Projection：它不修改研究事实，也不参与当前 Turn 的
停止决定。原始研究事实仍在每个 Workspace 的权威账本中；指标可随 policy revision 重算。

Benchmark Harness 位于产品 Runtime 之外。它可以读取 trial 产生的权威账本和 Artifact，但
必须通过正常产品入口驱动研究过程，不能直接向数据库伪造一个“成功”的 Workspace。

只有 Benchmark/Replay Trial 必须从干净环境开始。日常 Operational Evaluation 不复制用户
Workspace，不重复执行 Stata，而是评价已经真实发生的研究过程。

### 4.1 Three evaluation levels

完整评测分为三级，缺一不可：

```text
L1 Subsystem Evaluation
   Memory、RAG、Context、Plan、Tool、Evaluator、Gateway 等组件本身
        ↓
L2 Agent Loop / Trajectory Evaluation
   组件在 Turn → Step → Tool → Observation → Replan 循环中能否正确协作
        ↓
L3 Product Scenario Evaluation
   研究者最终是否得到可控、保真、可复现的研究结果和文稿
```

L1 不能替代 L3：Memory precision 很高，不代表它改善了研究任务。

L3 也不能替代 L1：一个端到端任务偶然成功，不能说明 Context、Plan 或 Stop Guard 没有隐藏缺陷。

每个重要 Agent 子系统至少同时接受两类评价：

- **Intrinsic evaluation：** 组件输入输出、状态和局部行为是否正确；
- **Extrinsic evaluation：** 启用该组件是否在真实 Scenario 中改善任务完成、控制、质量或效率，且没有引入新的硬失败。

Extrinsic evaluation 优先使用同一 Scenario/模型配置下的 baseline-candidate 或 ablation 对比，例如：

```text
Memory enabled vs disabled
RAG policy A vs B
old Context compiler vs new compiler
with Plan coordinator vs without Plan guidance
Skill version N vs N+1
```

对比必须冻结其他变量，并记录不可完全控制的 Provider 随机性。

## 5. Auxiliary Benchmark / Replay Contract

本节只适用于发布回归和校准，不是日常评测的前置条件。

一个 Scenario 不是 `prompt + expected_answer`，而是版本化研究环境：

```text
ScenarioRevision
├── identity
│   ├── scenario_id / revision / content hash
│   ├── suite: smoke | regression | capability | adversarial
│   ├── difficulty / tags / split
│   └── reference-solution verification
├── initial environment
│   ├── Workspace fixture
│   ├── .dta / literature / style references
│   ├── Skill and permission policy
│   └── Stata/help/runtime requirements
├── interaction
│   ├── initial idea and user messages
│   ├── autonomous or supervised mode
│   ├── scripted user decisions
│   └── optional user simulator revision
├── disturbances
│   ├── tool/provider failures
│   ├── crash point
│   ├── artifact mutation
│   └── pause/resume event
├── evaluation contract
│   ├── required outcome obligations
│   ├── forbidden outcomes
│   ├── permitted research freedom
│   ├── deterministic grader set
│   ├── quality rubric
│   └── trial count / budgets
└── hidden validators
```

### 5.1 Required outcomes, not golden workflows

Scenario 可以要求：

```text
- 正式结果来自 Stata；
- Word 中每个正式数字可追溯；
- 用户拒绝的变量不得进入 adopted result；
- 缺失问题被识别并向用户说明；
- 最终产物可在干净环境重跑。
```

Scenario 默认不得要求：

```text
- 必须使用 regress 而不是其他合法 Stata 命令；
- 必须按固定顺序调用 summarize → regress → esttab；
- 必须采用某个系统预先认识的研究方法；
- 必须生成与参考答案逐字相同的文章。
```

只有用户或 Scenario 明确声明为 required 的研究约束，才可成为 Hard Block。

### 5.2 Reference solution

每个进入 regression suite 的 Scenario 必须至少有一个可运行参考解，用于证明：

- 输入足够；
- 任务不是自相矛盾；
- 隐藏 validator 可以识别合法结果；
- 环境和依赖确实能完成任务。

参考解不定义唯一研究路径，也不直接暴露给被测 Agent。

## 6. Evaluation Dimensions

### 6.1 Truth and Provenance — Hard Gate

正式数值链必须可闭合：

```text
Word/Table Claim
→ Evidence Use
→ Evidence Record
→ Result Element
→ Result
→ Stata Run
→ Command Instance / do-file
→ Data Version
→ source .dta
```

核心检查：

- `formal_numeric_lineage_coverage = 100%`；
- Word/Table 数值与 adopted Result 一致；
- 正式 Result 有有效 Stata execution proof；
- 在干净 Stata session 中依据冻结输入重跑，关键结果在声明容差内一致；
- 非 Stata 探索输出不得静默冒充正式统计结果；
- 不存在结果时不得生成貌似真实的正式数字；
- Artifact identity、hash、size、location 和 availability 满足本次 trust boundary。

任何关键数据保真失败都直接使该 Trial 失败，不由 Model Judge 覆盖。

### 6.2 Researcher Control — Hard Gate + Metrics

检查：

- Waiting 后没有新的 Model Invocation 或 Tool Admission；
- 已 admitted Operation 只收敛，不被错误重复执行；
- 用户回答后旧的 scheduled calls 重新 Admission；
- 用户拒绝、暂停、变量替换或方案选择进入后续 Plan 和执行；
- major plan change 在相应模式下获得确认；
- branch 保留原路径，未发生隐式覆盖或合并；
- 任意采用/交付行为均能定位到用户决定或系统准入依据。

### 6.3 Task and Artifact Completion

检查 Agent 是否形成 Scenario 要求的研究产物，例如：

- 数据状态与变量说明；
- 研究方案及其后续修订；
- 可执行 Stata 研究链；
- adopted Result / Table / Figure；
- 可打开且通过 Delivery Gate 的 Word；
- 用户要求的摘要、解释或下一步建议。

允许按 obligation 给部分完成度，但 release regression case 的 required obligations 必须全部满足。

### 6.4 Research Judgment — Open Quality Rubric

评价：

- 是否真正回应用户的 idea；
- 是否识别关键数据问题和识别假设；
- 是否提出合理替代方案、诊断或稳健性建议；
- Plan 变化是否有事实依据；
- 是否区分确定事实、推断、建议和未知；
- 结果解释是否忠于证据且对成熟研究者有用；
- 是否出现重复、过度谨慎、过度操作或无实质进展。

这部分使用明确 rubric 的 Model Judge，并保留人工专家金标校准。优先采用 pairwise baseline-vs-candidate 或具体 pass/fail criteria，不依赖含糊的 1–10 总体印象分。

### 6.5 Adaptation, Recovery and Reversibility

通过故障注入检查：

- Stata 报错后能查询 help、修复并留下原失败记录；
- Provider retry 仍属于同一 Model Invocation；
- 外部执行完成、Finalization 未提交时不重新执行工具；
- recovery/reconciliation 创建正确的新 Turn 和历史关系；
- Artifact 被替换或缺失时，未来准入失效但历史事实不回写；
- 两个 Workspace 使用不同数据并行时 session、目录和结果互相隔离；
- budget、重复失败和 no-progress 能终止无限循环。

### 6.6 RAG, Context and Memory

直接复用现有 RAG、Context 和 Memory 指标，并增加端到端效果：

- retrieval Recall@K、Precision@K、MRR、evidence-group coverage；
- multi-hop 是否引入新证据；
- citation-node validity 与 claim citation coverage；
- literature/help/style corpus role isolation；
- 无足够证据时的 abstention；
- Stata Help 检索是否真实帮助修复执行；
- 文献检索是否进入方案建议但不越过用户判断；
- 长对话跨窗口后，外部原始记录、用户决定、Research State 和修正后的 Memory 是否仍正确；
- 被纠正或过期的 Memory 不得重新激活为当前事实。

### 6.7 Efficiency and Reliability

单独报告，不与数据真实性加权抵消：

- wall-clock time；
- model invocations / provider attempts；
- input/output/cached tokens 与实际费用；
- tool calls / Stata runs / failed runs；
- 重复 Tool/相同失败/无新 Evidence 的比例；
- 用户被打扰次数；
- Trial 成功率、基础设施失败率和失败分类。

## 7. Agent Subsystem Evaluation Matrix

### 7.1 Intent and Completion Contract

评测 Agent 是否正确理解用户到底要完成什么，而不是只做关键词分类。

Intrinsic：

- explicit requirement precision/recall；
- optional 建议被错误硬化为 required 的比例；
- 多意图、模糊意图、用户纠正和目标改变的识别；
- required obligation 的来源能否定位到用户、Plan 或用户决定；
- 不足以确定时是否请求澄清。

Extrinsic：

- goal coverage；
- 因错误理解目标导致的无用工具调用或错误交付；
- 用户纠正后旧目标是否停止支配后续 Turn。

### 7.2 Context Compiler and Long-context Management

Intrinsic：

- 当前用户消息、Waiting answer、Research State、Plan、Artifact、Memory 和 RAG slice 的召回率；
- stale revision、未来消息或其他 Conversation/Workspace 内容的泄漏率；
- 用户决定和关键约束在摘要/压缩后的保留率；
- token budget 遵守、优先级排序和截断稳定性；
- 长对话、噪声、重复消息和互相冲突信息下的鲁棒性。

Extrinsic：

- Context 压缩前后 task success、研究决策一致性和成本变化；
- 因 Context 遗漏导致的重复询问、错误 replan、错误工具参数和计划偏离；
- cached context 是否降低成本而不复用过期事实。

### 7.3 Memory System

Intrinsic：

- extraction precision / recall；
- source-message accuracy；
- lifecycle 与 activation precision；
- 用户纠正后旧 Memory 的失活速度；
- Conversation、Workspace 和长期偏好的作用域隔离；
- false memory、duplicate memory、over-generalization 和 stale-memory count；
- 从历史 Trace 提炼 Skill/Memory candidate 时的依据完整性。

Extrinsic：

- 跨 Conversation 是否减少重复说明；
- 是否正确复用研究习惯、变量定义和写作偏好；
- 是否减少重复 Tool/Stata 执行；
- Memory enabled/disabled 对任务质量、成本及错误率的净影响；
- Memory 不相关时是否保持沉默，而不是强行个性化。

现有 `agent_benchmarks.py` 和 Memory lifecycle tests 作为起点，但必须补 live Agent consumption 和 ablation Case。

### 7.4 RAG and Knowledge Use

Intrinsic 分解为：

```text
ingestion quality
→ index quality
→ retrieval quality
→ multi-hop trace quality
→ claim grounding
→ abstention
```

除现有 Recall@K、Precision@K、MRR、evidence groups、citation validity、role isolation 外，还要评：

- PDF 公式/表格选择性解析是否真的提高可检索性；
- lexical/dense/rerank 的增量价值；
- query rewrite 和多跳停止是否带来新证据；
- retrieval miss、source conflict 和 no-answer 时是否明确报告未知。

Extrinsic：

- Stata Help 是否让失败命令成功修复；
- 文献证据是否改善研究建议而不是只增加引用数量；
- style exemplar 是否只影响文风、不污染事实；
- RAG enabled/disabled 对研究质量、grounding、延迟和 token 的净影响。

### 7.5 Research Plan and Replanning

Intrinsic：

- 初始 Plan 是否覆盖用户目标但不过度展开；
- semantic duplicate proposal rate；
- refinement 与 direction change 是否有可核查 trigger；
- major change 是否遵守 autonomous/supervised 策略；
- Plan revision、node、dependency、adoption 和历史关系正确；
- 研究方法保持开放，不因系统不认识方法名而拒绝。

Extrinsic：

- Plan 是否真实约束/指导后续执行，而非仅作为展示文本；
- Plan–Execution semantic alignment；
- 偏离是否被识别、解释并在需要时确认；
- Replan 是否解决失败，还是形成反复改计划但无新 Evidence 的循环；
- 有无 Plan guidance 对长任务完成率和重复劳动的影响。

### 7.6 Tool Selection, Arguments and Admission

Intrinsic：

- tool/no-tool decision accuracy；
- 等价合法 Tool choice；
- tool schema 和 argument contract accuracy；
- Artifact ID、Data Version、Research Path、Plan Node 等引用是否新鲜；
- permission、Waiting、pause、budget、receipt freshness 和 write lane 准入；
- unsafe/irrelevant/unnecessary tool rate。

Extrinsic：

- Tool 选择是否真正推进研究目标；
- 参数错误导致的 Stata 失败和返工；
- 新 Tool description/Skill 是否提高成功率且不增加误调用；
- 并行只读或可并行 Calls 是否降低延迟而不产生副作用冲突。

### 7.7 Turn Driver and Agent Loop

评测完整循环，而不是模型单次输出：

- Observation 是否进入下一 Step；
- 工具失败后是否修订、replan 或等待，而不是重复相同调用；
- multiple tool calls 的 barrier/batch 行为；
- no-progress、同失败、step/tool/provider budget 是否生效；
- 用户在 Running、Waiting、Paused 状态发来的消息是否进入正确 Turn；
- Agent 是否能在长任务中维持目标和已确认约束；
- natural stop 是否只是候选，最终由 Stop Guard 决定。

核心指标包括 loop success、no-progress detection recall、duplicate-action rate、recovery-to-progress rate、steps-to-success 和 unnecessary-interruption count。

### 7.8 Runtime Evaluator and Stop Guard

Evaluator 自身必须有独立金标集，不能用“它输出 PASS”证明它正确。

至少评价：

- false terminate：工作未完成却允许成功；
- false continue：已经完成却继续消耗；
- false wait：不需要用户判断却打扰用户；
- missed wait：需要研究者决定却自动推进；
- required evidence detection；
- plan deviation、loop no-progress、unsupported claim 等 finding 的 precision/recall；
- 相同证据、不同表述下 verdict 的稳定性；
- Judge 与专家标签的 agreement、confusion matrix 和分歧样本。

Stop Guard 的确定性部分用规则测试；开放研究质量 finding 用校准后的模型 grader，不得混成同一准确率。

### 7.9 Model Gateway and Provider Resilience

Intrinsic：

- routing、fallback、retry、circuit breaker 和 delivery-unknown 语义；
- 一个逻辑 Invocation 下 Provider Attempts 的归属；
- timeout、429、网络断开、流式中断和非法结构化输出；
- token/usage/cost/cache accounting 准确性；
- model/provider 配置、Prompt 和 Context Manifest 的可重建性；
- Provider 切换后语义合同是否保持。

Extrinsic：

- 不同模型/路由在同一 Scenario 上的质量、稳定性、成本和延迟 Pareto 对比；
- fallback 是否真正恢复任务，而不是生成低质量但形式成功的结果。

### 7.10 Skills and Self-evolution

Intrinsic：

- Skill trigger precision/recall；
- 主研究 Skill 与按需 Skill 的选择边界；
- Skill 版本、来源和实际注入内容是否进入 Context Manifest；
- 多 Skill 冲突、过度触发和提示污染；
- candidate Skill 是否有足够 Trace/人工依据；
- 未经用户采用的 Skill candidate 不得自动成为全局规则。

Extrinsic：

- Skill N 与 N+1 在固定 Scenario 上的 pairwise 质量变化；
- 是否减少重复错误或改善特定研究任务；
- 是否牺牲研究自由度、增加无意义流程或提高 token 成本；
- 自进化后的变化能否回滚，并能定位到触发它的历史案例。

### 7.11 Research State, Adoption and Branching

检查：

- current pointer、CAS 和 revision correctness；
- Data/Plan/Result/Document slot 的采用对象是否满足当前 eligibility；
- branch 是否冻结父路径快照并独立演进；
- adoption 是否有用户、Agent proposal 和 Operation 来源；
- merge/adopt 不会隐式覆盖原路线；
- UI Research State 与权威事实存在 projection lag 时是否明确展示。

### 7.12 Evidence, Result and Document Generation

除数字 lineage 硬门禁外，还要分别评价：

- Result capture completeness；
- table group/row/cell 与 estimation state 一致；
- 正文 claim 与 EvidenceUse 对齐；
- Word/RTF/DOCX package、样式、表格和可读性；
- 文稿的完整性、逻辑、解释、引用与用户指定文风；
- round-trip 人工编辑后，受控内容和用户内容是否正确合并；
- 文稿质量提高是否以篡改数字或丢失来源为代价。

### 7.13 Security, Permissions and Isolation

虽然研究方法不设白名单，能力边界仍需评测：

- RAG/tool output 中的 prompt injection；
- Provider key、敏感数据和系统提示泄漏；
- partial/full access 切换；
- Workspace 文件和会话隔离；
- Shell/Python/Stata/MCP 的注册、权限与 Trace；
- Worker 无权威写入、无长期凭据、无旁路副作用；
- 恶意或损坏 Skill 不得获得未声明执行能力。

### 7.14 UI and Researcher Inspectability

Agent 系统正确但研究者无法核查，产品仍然失败。应评价：

- 用户能否从数字定位到 Result、命令、do-file 和 dta；
- 能否查看当前数据状态与原始处理 Trace；
- 能否理解 Plan 变化、Waiting 原因和采用关系；
- 能否从变量构造节点创建分支并比较路线；
- projection lag、missing artifact、unreconciled operation 是否被正确展示；
- 摘要是否降低核查成本，而不隐藏原始 Trace。

### 7.15 Agent Decision Quality Without Chain-of-thought Inspection

“评测 Agent 本身”不等于要求保存或逐字评分模型隐藏推理。系统评价可观察的决策边界：

```text
Frozen Context
→ Plan / Tool / Wait / Stop Proposal
→ External Observation
→ Revised Decision
→ Authoritative Outcome
```

使用以下方法评价 Agent 判断：

- 同一事实换一种表述，关键决定是否稳定；
- 加入无关长上下文，是否仍抓住真正约束；
- 改变一个关键事实，决定是否随之合理变化；
- 删除 RAG/Memory/Skill slice 后，Agent 是否知道信息不足；
- 提供冲突证据时，是否暴露不确定性并请求用户判断；
- 工具返回失败或反例后，是否修正原方案；
- 不检查私有 chain-of-thought，只评分结构化 proposal、公开 rationale、tool trajectory 和结果。

这类 consistency、counterfactual 和 perturbation Case 比“让另一个模型读长篇思维链打分”更稳定，也更符合产品真正可观察、可审计的边界。

## 8. Grader Policy

| Grader | 适用问题 | 不得决定 |
| --- | --- | --- |
| Deterministic invariant | schema、权限、状态机、budget、Artifact、lineage | 研究方法是否学术上最佳 |
| Outcome/state | 最终 Workspace/DB/Word 是否达到目标 | Agent 是否必须采用某条路径 |
| Reproducibility | 干净环境重跑、数值容差、产物一致性 | 开放式解释是否优秀 |
| Trace/behavior | 重复、偏离、等待、恢复、无进展、工具选择 | 仅因路径不同就判错 |
| Model rubric | 方案、解释、建议、grounding、文稿质量 | 数字是否真实、工具是否真实执行 |
| Human expert | 校准 Judge、复杂研究判断、版本 pairwise | 替代可自动检查的硬事实 |

被测 Agent 的 Runtime Evaluator、最终自述和 natural stop 只能作为被评对象，不能作为独立 Grader 的答案。

## 9. Non-determinism and Metrics

每个真实模型 Case 是 task；同一配置的一次运行是 trial。

至少分别报告：

- `pass@1`：首次完成能力；
- `pass@k`：多次尝试至少一次成功，适合探索性 capability；
- `pass^k`：连续 k 次全部成功，适合数据保真、控制和正式交付稳定性；
- 各 Hard Gate 违规次数；
- 各 slice 的 task success 和质量分布；
- 成本、延迟和失败类型分布；
- baseline/candidate 的逐 Case 变化。

Hard Truth Gate 不因平均分较高而被放行。候选版本即使研究质量平均分提高，只要新增不可追溯数字、越过用户决定或产生重复副作用，仍然不能通过正式门禁。

## 10. Auxiliary Release Suite

以下 Suite 不承担日常评测。它们只在发布、重大模型/Skill/RAG/Runtime 变更、已知失败复现和
故障注入时运行，用于回答“新版本是否破坏了已知能力”。

### 10.1 Smoke / deterministic regression

每次相关变更运行：

1. `auto.dta`：Stata OLS → Result → esttab/Word → numeric lineage。
2. Waiting → 用户回答 → revision 更新 → 剩余 Calls 重新 Admission。
3. Pause → convergence → continuation Turn。
4. 外部执行完成后崩溃 → recovery → reconciliation，不重新执行。
5. Python 探索输出不能未经相应采用规则进入正式 Word。
6. Artifact missing/changed 后未来准入失败，历史 Run 不回写。
7. 两个 Workspace 加载不同 `.dta` 并行，结果和 Session 隔离。
8. RAG 无答案时 abstain，style exemplar 不作为事实证据。

### 10.2 Real model regression

用冻结模型/Provider 配置重复运行：

1. idea + `auto.dta`，无人干预完成可追溯 Word；
2. 人为缺失/异常值场景，Agent 解释问题并请求研究者决定；
3. 用户否决变量或模型，旧 Dispatch Plan 失效；
4. Stata 命令失败，检索 help 后修复；
5. 变量构造节点开分支，主路径保留；
6. 文献多跳检索形成有引用的建议；
7. 长对话中纠正研究偏好，后续 Context/Memory 使用新事实。

### 10.3 Capability suite

`auto.dta` 只作为执行链 smoke fixture，不代表研究质量。能力集逐步增加：

- 脏的横截面数据：缺失、异常、编码错误和 merge；
- 面板/固定效应/DiD；
- IV 与识别假设；
- 复杂变量构造和替代定义；
- 用户指定的新方法论文与本地数据；
- 数据不足或无法识别，正确拒绝给出确定结论；
- 长时间、多轮、多个研究分支的真实研究任务。

方法名称不进入系统白名单；每个 Scenario 只冻结用户要求、数据事实、必要约束和可验证产物。

## 11. Existing Assets and Actual Gaps

现有可直接复用：

- `SqliteOperationalEvaluationQuery`：从真实 Turn 权威事实计算 L1/L2/L3；
- Turn / Workspace Evaluation API：日常查看，不需要运行独立 Harness；
- `turn_outcome_feedback`：追加式记录用户采用、继续修改或不采用的真实结果信号；
- provider、operation、Evaluator、Stop Guard 和重复 Tool Call breakdown；
- model / Skill / Tool catalog / policy revision configuration slice；
- `ReleaseEvaluationGate`：确定性 release regression 入口；
- Founder autonomous/controlled scenario：产品核心 journey 的初始 contract；
- `agent_benchmarks.py`：版本化 dataset、Memory/Tool/Judge 指标；
- `rag_evaluation.py` 与真实 RAG runs：检索、multi-hop、grounding、role isolation；
- Workspace Journal、Trace、Run、Result、Evidence、Document 和 usage facts：Trial 原始证据；
- Runtime Evaluator 与 Stop Guard：被测的运行时行为。

Operational Evaluation 当前缺口：

1. 跨 Workspace、跨软件版本的时间趋势聚合和 UI；
2. 将用户后续继续研究、创建分支、采用 Result 等自然行为转成明确的弱信号，且不冒充显式反馈；
3. 失败原因从稳定 error/reason code 进一步聚类到可行动建议；
4. 长期使用中 policy revision 变化后的可比性标记；
5. 可选择的模型 Judge / 人工抽查仍需校准，不能自动成为权威质量分。

辅助 Benchmark/Replay 缺口：

1. 没有统一、版本化的 Product Scenario schema；
2. 没有从干净 fixture 驱动正常产品入口的多 Trial runner；
3. Release Gate 主要是 pytest selector 汇总，尚无 scenario/trial/grader 对象；
4. 没有独立重跑 Stata 产物的 reproducibility grader；
5. 没有 Word numeric lineage、Plan/Execution、用户决定执行情况的统一 grader bundle；
6. 没有 dataset/grader/model/skill/runtime 全量冻结的 Experiment Manifest；
7. 没有可比较性判定和逐 Case baseline/candidate diff；
8. Judge 稳定性已有单测，但尚无人工金标 calibration set；
9. 真实失败 Trace 还不能一键转为脱敏 Scenario candidate。

## 12. Implementation Slices

### Implementation Status — 2026-09-21

Operational Evaluation 主链已经完成第一条可运行纵切：

- 普通真实 Turn 自动形成 L1 Memory/RAG/Context/Plan/Tool/Evaluator/Gateway 指标；
- 普通真实 Turn 自动形成 L2 Step/Failure recovery/Waiting/Stop Guard 指标；
- 普通真实 Turn 自动形成 L3 Stata provenance/Word gate/replay readiness/control 指标；
- 零分母返回 `not_applicable`，开放质量信号返回 `observed`，只有硬不变量产生 `pass/fail`；
- 指标保留 numerator、denominator、source tables 和 policy revision；
- Turn/Workspace 查询均返回模型、主 Skill、Tool catalog 和 System Prompt 版本切片；
- 用户可以在不打断研究的前提下主动登记 outcome feedback；
- 真实工作区扫描器可落盘版本化 JSON，用于日常趋势和发布前对比。

辅助 Benchmark/Replay 第一条可运行纵切已经完成：

- `EvaluationScenario v1` 严格 loader 与 canonical hash；
- `ExperimentManifest v1`、SUT snapshot、comparison contract；
- `.staging → artifact integrity verification → atomic publish` Trial Store；
- deterministic grader registry、hard gate、quality grader；
- Trial、Case、Experiment 三层结果，其中 Experiment 保留逐 Case 指标而不生成总分；
- `pass@1`、`pass@k`、`pass^k` 与 infrastructure error 分离；
- Project Memory intrinsic adapter；
- canonical RAG intrinsic adapter，覆盖单跳、真实两跳 Retrieval Session、corpus-role
  isolation、固定 no-answer abstention 和报告防篡改；
- Context Compiler intrinsic adapter，覆盖去重、优先级、动态预算、外部原文装载、remote/local
  privacy boundary 和 mandatory fail-closed；
- 当前冻结的三子系统 Experiment 共运行 9 个隔离 Trial。
- 真实公开论文复现集已建立三层合同：certified execution baseline、Agent
  replication task、stress/controlled-research variants；不以精确命令文本作为金标。
- Acemoglu et al. (AER 2011) Table 3 Column 1 已接入统一 Product Evaluation：官方
  do/data 哈希、Stata 18 `version 9.2` 实跑、数值 oracle、`esttab` RTF 与独立
  reproducibility grader 全部通过。
- McKenzie (World Bank 2025) 完整复现包已形成第二个真实 baseline：官方 master do、
  两个 `.dta`、描述统计、power commands、两个子图、合并图、PNG/PDF 在 Stata 18
  实跑通过；下一步将其迁移到通用 package adapter。
- Baker et al. JEL DiD 作者仓库已按 commit 冻结；Table 2 短链路真实重建分析
  `.dta`，复现加权/未加权 2x2 DiD、聚类回归等价结果和 RTF。执行时只操作 Trial
  副本，覆盖“作者脚本会覆盖包内数据文件”的 source immutability 风险。
- Agent Loop 第一条统一 trajectory Scenario 已落地并重复 3 个隔离 Trial：真实
  Turn Worker、Model Gateway、JIT Admission、Operation、Tool Result、Goal Coverage 与
  Stop Guard 因果链由 Workspace SQLite hard grader 独立核验。

尚未完成的辅助 Benchmark/Replay 主体：

- Intent/Completion Contract；
- Plan/Replan；
- Tool Selection/Admission；
- Turn Loop/Stop Guard；
- Model Gateway provider retry/cost/cache；
- Research State/Provenance 与 Word consistency；独立 Stata reproducibility grader 已有
  第一条真实论文纵切，尚需通用化到多 package；
- Founder、真实 Stata 和 release-gate 场景迁移到统一 Experiment runner；
- baseline/candidate Experiment 报告落盘与失败 Trace 转 Scenario candidate。

### P0 — Operational Evaluation Kernel

日常主线：

- 所有指标从普通产品入口已提交的权威事实计算；
- 完成 Turn 即可查看，不要求创建 Scenario 或 gold dataset；
- 按 Turn、Workspace、模型、Skill、Tool catalog、policy 和软件版本切片；
- 自动记录 failure/retry/repetition/stop reason，显式用户反馈单独追加；
- 指标策略可版本化重算，原研究事实不被评测回写；
- Dashboard 只展示 hard gate、observed signal、N/A 和 unknown，不生成单一总分。

发布辅助线：

先完成：

- `EvaluationScenario v1` 严格 schema 和 loader；
- `ExperimentManifest v1`，冻结代码、Scenario、模型、Prompt/Skill、Tool catalog、RAG、Stata 和环境 revision；
- 隔离 Trial runner，复用正常 Product API；
- Run bundle：Workspace DB snapshot、Journal/Trace、Artifact manifest、usage 和 final state；
- deterministic grader registry；
- provenance、Word consistency、user-control、recovery、reproducibility graders；
- case-level report、failure taxonomy、`pass@1/pass^k`；
- 与当前正式 baseline 的逐 Case comparison。

同时定义统一的 `SubsystemEvalCase / SubsystemEvalResult` 接口，并接入第一批组件包：

- Intent/Completion Contract；
- Context Compiler；
- Memory；
- RAG；
- Plan/Replan；
- Tool Selection/Admission；
- Turn Loop/Stop Guard；
- Model Gateway；
- Research State/Provenance。

组件包既支持固定输入的 intrinsic test，也支持调用真实 Agent 的 extrinsic/ablation trial。组件指标不得各自另建不兼容的数据集、运行和报告格式。

P0 首先接入已有 Founder、real Stata、RAG 和 release-gate 场景，不重复建设第二套测试。

### P1 — Research Quality and Calibration

- research-quality rubric grader；
- baseline/candidate pairwise Judge；
- 人工金标和 Judge agreement/confusion report；
- user simulator 与多轮 controlled scenarios；
- 真实研究 capability suite；
- failure trace → scenario candidate 工作流；
- cost/latency/quality 联合对比，但不形成总分。

### P2 — Production Feedback Loop

- 本地真实使用的确定性指标默认计算，模型/人工内容抽查才需要用户选择；
- anomaly、drift 和 recurring failure cluster；
- 用户反馈先关联真实 Turn，只有需要复现时才转为脱敏 Scenario candidate；
- 模型/Prompt/Skill A/B；
- 周期性长任务与压力评测。

## 13. Release Policy

候选版本的评审顺序：

```text
Hard Truth Gates
    fail → reject
    pass ↓
Regression suite
    critical regression → reject or explicit founder override
    pass ↓
Capability and quality comparison
    ↓
Cost/latency trade-off review
    ↓
Founder decision
```

报告必须优先展示：

1. 新增硬失败；
2. 曾通过但现在失败的具体 Case；
3. 不可比较的 Case 及原因；
4. 研究质量的 pairwise 变化；
5. 成本和时间变化；
6. 需要人工查看的原始 Trace 和产物。

## 14. Non-goals

V0.1 不做：

- 公共排行榜；
- 通用 Benchmark SaaS；
- 固定研究方法或 Result Profile 白名单；
- 依赖某个云端评测平台作为权威记录；
- 用 Runtime Evaluator 自评结果作为 release 真值；
- 用单一综合分自动替代研究者或 Founder 的版本判断。

## 15. Acceptance Criteria For This Design

设计进入实现前应满足：

- 数据保真和用户控制被定义为不可被平均分抵消的硬门禁；
- Scenario 允许多种合法研究路径；
- Product Eval 与 Runtime Eval 的权威边界清楚；
- 每个真实 Trial 可重建其模型、Skill、Tool、RAG、数据和环境；
- 确定性、模型和人工 Grader 的职责不重叠；
- 首批 Case 可直接复用现有产品测试与真实 Stata/RAG 证据；
- `auto.dta` 被明确限制为 smoke fixture，而不是研究能力的代表。
