# 意图识别与工具路由设计（agent 的决策中枢）

> 触发：使用中发现"research/chat 二分的意图开关"把 agent 框死了。本文重做**意图识别→工具调用**这一层。
> 定位：这是 agent 的决策中枢——决定"要不要动工具、动哪个、怎么组合"；护栏在工具层，不在对话外壳。

## 0. 一句话结论

**意图识别不该是外层 if/else 开关（research? chat?），而应交给 function calling**：把**所有能力（含研究）注册成工具**，模型根据工具描述自主决定"要不要调、调哪个、调几步"。**研究是一组工具，不是一个模式。**

## 1. 调研依据（业界优秀设计）

- **Tool-First 执行（Open-Rosalind）**：LLM 从不做"知识 oracle"——最终答案里每个事实/计算/标注都来自一次显式工具调用；LLM 退化为"reader 而非 source"，负责把结构化工具输出综合成自然语言，并带证据内联引用。[Open-Rosalind](https://www.biorxiv.org/content/10.64898/2026.05.06.722404.full.pdf)
- **function calling 天然即意图识别**：模型根据 `tool.description` 里的"何时用"自行决定调不调；不需要再套一个分类器。[OpenAI 函数调用最佳实践](https://blog.gitcode.com/6975e9127fc90a5d2ec440ea22a36a6d.html)
- **描述写"when to use"，不写业务规则**：工具描述是模型做决策的输入，要写触发场景与副作用，不是塞几百行规则。[Anthropic tool use](https://raw.githubusercontent.com/EGAdams/planner/main/.claude/skills/meta-skill/docs/blog_equipping_agents_with_skills.md)
- **分层：atomic tools → skills → 护栏**：原子工具（thin wrapper，单个操作、结构化返回）组合成 skill（确定性 pipeline）；护栏（schema 校验/前置条件/审批/幂等）在调用时判。[PRAXIS](https://export.arxiv.org/pdf/2605.23169)
- **确定性 validator 包裹 function calling**：LLM 会幻觉工具名/槽值/调用顺序，用 O(1) 的符号校验器拦（非 LLM-as-judge），非法调用以结构化错误回给模型自纠。[ReacTOD](https://aclanthology.org/2026.trustnlp-main.pdf)
- **staged routing 只为省钱，不为框行为**：正则/embedding 预过滤可省 token，但那是"先筛工具候选集"，不是"决定它只能做研究"。[Google AI Agents Challenge 工程模式](https://jobirun.com/google-ai-agents-challenge-4-engineering-patterns/)
- **动态工具选择**：工具多了（几十个）先 top-k 过滤 schema 减 payload 与认知负担；有 `discover` 兜底。[Dynamic Tool Selection](https://dzone.com/articles/dynamic-tool-selection)

## 2. 目标架构：通用 agent loop + 工具注册表 + 工具层护栏

```
[用户消息]
   ↓
通用 Agent Loop（LLM + function calling）
   ├─ 模型可只回文本（聊天，默认，不是特例）
   ├─ 模型可决定调一个/多个工具（并行），组合成它自己规划的多步
   └─ 模型自判何时停/问用户/交付
   ↓ 每次 tool call → 护栏层（调用时判）
       validator(schema/前置/防幻觉) → policy(隐私/阶段) → approval(危险) → 幂等/恢复
   ↓ 每步落 events（Trace = 它到底干了什么）
```

**关键**：外层不再有"研究驾驶舱"。模型每轮的自由度 = 通用对话 + 任意工具组合；确定性只存在于"工具能否被安全、可审计地执行"。

## 3. 工具注册表（把能力收成工具，替换 8 种研究 act）

| 工具 | 何时用（description 要点） | 副作用 | 对应现有 |
|---|---|---|---|
| `search_literature` | 查/引用文献（变量选取、方法、是否做过） | read | rag |
| `inspect_data` | 看数据结构/变量/样本 | read | stata inspect |
| `run_analysis` | 跑一段估计/稳健性（内嵌 skill，参数=方法+spec） | write | engine+executor |
| `write_draft` | 出实证初稿（只渲染已验证证据） | write | writer |
| `ask_user` | 需要澄清/批准/要数据时问学者 | — | approval |
| `remember` | 记/查研究约定与进度 | write | memory |
| （聊天） | 无需工具，直接回文本 | — | chat |

> 研究从此是 `run_analysis`/`search_literature` 等**工具**，由模型自主触发；不设"研究模式"。

## 4. 工具描述规范（决策质量的来源）

每个工具一个 **窄契约**：
- `name`：动词+对象，如 `run_analysis`、`search_literature`（不叫 `func1`）。
- `description`：**两件事**——① 做什么 ② **什么情况下调它**（trigger）。写模型判断要用的信息，不写给人看的文案，不塞业务规则。
- `parameters`：JSON schema，每个参数给**格式+例子**；能用 enum 就 enum；`required` 列必填；固定值不进参数（后端硬编码）。
- 返回：**结构化 JSON**（数据/结果），非自然语言描述；统一 `{ok, data?, error?}`。

## 5. 动态工具选择（可选层，工具多了才加）

- 工具 ≤10 个：全量给模型（我们当前就这规模）。
- 工具变多：`tool_selector` 先 top-k（lexical/embedding）过滤 schema，再给模型；`discover` 工具兜底漏选。
- 这不是"意图分类"，是"减 payload"。

## 6. 护栏层（保留的确定性，别砍，但位置在工具层）

| 护栏 | 位置 | 现状 |
|---|---|---|
| validator：schema/前置条件/防幻觉工具调用 | 每个 tool call 前 | 需新做（O(1) 符号校验，非 LLM） |
| policy：隐私三档/阶段允许 | 工具执行前 | 已有 `policy.check`，改造为按工具 |
| approval：危险/研究闸门 | 写类/删/联网 | 已有，改造触发点 |
| 幂等/断点续跑 | executor | 已有 |
| 写权分离 | 证据/claim 只 validator 签 | 已有 |
| 预算/健康刹车 | loop | 已有 |

## 7. 迁移映射（现有代码 → 新架构）

1. **删 `_classify_intent` 二分**（research/chat），换成通用 loop：模型直接进 function calling，聊天只是"不调工具"。
2. **provider 加 tool calling**：deepseek `chat(..., tools=[...])` → 返回 tool_calls；写通用 loop（call→执行→append result→再 call→直到无 tool_call 或需问用户）。
3. **能力注册成 §3 工具**：现有 `skills/engine`、`executor`、`rag`、`writer`、`memory` 原样当工具的 handler；写 tool schema。
4. **护栏接进工具执行前**：validator + policy + approval 在 loop 里每次 tool call 触发。
5. **阶段/ResearchState 降级为只读上下文**（注入 prompt 供模型参考，不再自动推进对话）。
6. UI 不变：聊天 + 工具产物（表/卡/Word）+ 一条条 Trace。

## 8. 开放问题（实现前定）

1. **并行工具调用**：模型可一次调多个（read 类并行，write 类串行）——loop 怎么编排？
2. **工具结果回填格式**：结构化结果 + 摘要（大结果只回摘要，全文存 artifact，模型要再读）。
3. **ask_user 的形态**：是"工具"（模型主动请求批准/澄清）还是 loop 内 stop reason？——倾向做成工具，统一。
4. **skill 触发**：模型自选 run_analysis 时，spec 从 skill variants 来，还是模型现写 spec 再过 validator？
5. **审批 UI**：模型调 ask_user → 对话里内联"待你决定"卡（现有 approval 机制可复用）。

## 9. 面试叙事（一句话）

> 我的 agent 不用"意图开关"框死——它把研究、查文献、写稿都注册成工具，模型靠工具描述自主决定每步做什么；确定性只在工具执行前守门（防幻觉/审批/隐私/可溯源）。所以它既会聊天，也会做研究，而不是"只会研究的聊天框"。
