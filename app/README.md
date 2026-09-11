# stata-agent · app（切片 0–5b 代码）

> 当前实施与优先级只认 `../IMPLEMENTATION_PLAN.md`；架构契约见 `../ARCHITECTURE.md`，SPEC/DD 作为设计参考。
> 当前里程碑：**LLM 自动多步 → 真 Stata 回归 → 证据入事件链（可复现/可审计）**已跑通。

## 快速跑（在 app/ 下）

发布 wheel 可直接安装，不需要源码目录、`PYTHONPATH` 或额外 extras：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install .\dist\stata_agent-0.1.0-py3-none-any.whl
.\.venv\Scripts\python -m stata_agent.ui
```

`[ui]`、`[rag]`、`[writing]`、`[stata]` 仍保留为空的兼容别名；对应产品入口依赖已经是
wheel 的直接依赖。当前交付物是 Python wheel，不是 Windows installer，不能对 wheel 宣称
Authenticode 验证通过。

```bash
# 离线测试（无需网络/Stata）
python -m pytest -q

# 产品级离线评测（无 Stata、网络或模型 key）
python -m stata_agent.eval --json

# CLI mock loop（你说一句/想一步/回一句，全落 events）
printf '问题\n补料\n' | PYTHONPATH=src python -m stata_agent.cli --db .demo.sqlite3

# live LLM 自动链（必须显式授权隐私模式；无 key 不会伪造结果）
#   真 Stata：python 里有 Stata + stata-mcp 才可
PYTHONUTF8=1 PYTHONPATH=src python - <<'PY'
from pathlib import Path
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.events.schema import EVENT_IDEA, EVENT_PHASE, ACTOR_AGENT, Event
from stata_agent.providers.registry import default_provider
from stata_agent.runner import run_until_gate
from stata_agent.tools.executor import StataExecutor
s = SQLiteStore(str(Path('_x.db')), writer_id='x')
s.append(Event(idea_id='i1', event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT, payload={'question':'q'}))
s.append(Event(idea_id='i1', event_type=EVENT_PHASE, actor=ACTOR_AGENT, source=ACTOR_AGENT, payload={'from':'IDEA','to':'ESTIMATION'}))
ex = StataExecutor(s, run_root=Path('_x_runs'))
res = run_until_gate(s, 'i1', '跑 price 对 mpg 主回归', default_provider(), executor=ex, max_steps=5)
for r in res: print(r.ran_run_id, r.frozen_specs, r.machine)
PY
```

产品评测包含七个稳定 golden scenario，覆盖工具选择/拒绝、run FSM 与 uncertain 恢复、证据数字接地、`消息 → 工具 → Fake → card → claim → draft` 完整链路、长会话上下文/记忆质量、发布信任门和操作治理。命令返回 0（全通过）、1（scenario 失败）、2（golden/配置错误）；可用 `--scenario <ID>` 只跑单项。安装开发依赖后也可使用 `stata-agent-eval --json`。

### 上下文、压缩与记忆质量门

每次 provider attempt 都由 `ContextAssembler` 生成同一份版本化
`ContextBudgetSnapshot`：同时计入 message body、chat/tool framing、active tool
schemas、output reserve 与 compaction buffer，并在 soft/hard limit 前决定是否主动
压缩。超过 soft limit 且账本存在可安全推进的历史时只主动 compact 一次；provider
实际 overflow 另有一次有界 retry，二者都不会重放已经执行的工具。

`compaction.boundary` 写入前会校验连续范围、完整 tool/approval/run semantic unit、
证据引用和 checkpoint projection；`checkpoint_health` 在重启后用 canonical ledger
重新 fold 并比较稳定签名。失败时不追加 boundary，旧账本仍是事实源。

Memory V2 只选择同 workspace、active、未过期、未 quarantine、达到最低相关性且
整条能容纳的约束/working knowledge，并保留 provenance；超长条目会跳过而不是截成
半句，也不会被 touch。JSON 与 SQLite facade 共享同一评分/格式化策略；显式
supersede 链的 orphan/cycle/多 active successor 会失败关闭，系统不会凭关键词猜测
自然语言冲突。L5 场景会重复验证预算合规、完整单元保留、workspace 隔离、重启恢复、
provider under-count retry 和候选重复噪声，输出不含正文、prompt、secret 或路径。

### 操作恢复与 Skill 治理

每次 tool call 都使用 request 内稳定且唯一的 `call_id`，并贯穿 provider 消息、事件账本、SSE 与 UI；
同名工具并发或重放时不得依靠名称/FIFO 猜配。客户端把 `running`、`cancelling`、`disconnected`、
`failed`、`completed` 分开处理：stop 或断流后，只有服务端确认终态才重新开放发送，失败界面只展示
安全错误码、重试和诊断入口。附件历史只投影有界安全 manifest，不显示本机路径或内部异常。

Skill V2 支持有界的 `role/prechecks/steps/rules/examples/disabled_when/evolution`、结构化
`phase/needs` 和 `requires.stata_min`，同时兼容旧 flat Skill。进化候选先写入隔离 staging 和 manifest；
promote 必须携带人工 reviewer、批准决定、时间戳及匹配候选内容的 SHA-256。候选被修改、自动 reviewer、
重复覆盖、路径越界或 symlink 都会失败关闭。active catalog 始终只有一个版本，旧 active 移入历史目录；
模型不能自动批准、执行或提升候选。

### 系数结果合同

需要把数字写入证据链时，`run_stata`/`run_do_file` 必须携带结构化
`result_contract`。最小 `regress` 示例：

```json
{
  "schema_version": 1,
  "target_term": "mpg",
  "estimator": "regress",
  "dependent_variable": "price",
  "vce": "ols",
  "cluster_variables": [],
  "fixed_effects": [],
  "required_stats": ["coef", "se", "N"]
}
```

执行器会在可信边界提取 `_b[]`、`_se[]` 和 `e()` 元数据；模型自行打印的
`MACHINE_*` 行不构成证据。执行成功不代表合同通过，也不代表因果识别有效。
调用 `verify_result`，只有返回 `evidence_ready=true` 才会自动签发 numeric
card/claim；缺合同、term/estimator/VCE/聚类/固定效应不匹配、marker 欺骗或
do-file hash 被篡改都会保持 run 成功但零签卡，并返回稳定失败码。

### 可信交付门（Evidence Production Round-Trip V1）

`write_draft` 先对当前 idea 的 supported claim、numeric table cell、citable
literature chunk 和 figure artifact 做确定性 preflight；任一 card 缺失、role/内容
digest 过期、逐格错绑或图文件 hash 改变都会以 `evidence_not_ready` 失败，不会创建或
覆盖最终文件。通过后同时写出可编辑 DOCX 与 `evidence-manifest.v1.json` sidecar：
manifest 只含安全的 opaque ID、关系、摘要和 delivery digest，不含 PDF 原文、prompt、
secret 或绝对路径；DOCX 中的 claim、表格格和图 caption 保留可反查的 card/chunk 标记。

发布前门禁：

```bash
python -m pytest -q
python -m stata_agent.eval --json
python -m ruff check src tests
python -m mypy src
python -m coverage run --branch -m pytest -q
python -m coverage report
python -m build --wheel
```

真 Stata 发布验收（仅在明确要验证本机运行时时运行；默认测试和产品评测不会启动 Stata）：

```powershell
# 源码 checkout（在 app/ 下）
python -m stata_agent.stata_doctor --json --iterations 20
# 安装 wheel 后的等价入口
stata-agent-stata-check --json --iterations 20
```

门禁会依次验证 `stata-mcp` 工具发现、Stata 引擎、同一持久会话、内置
`auto` 回归和 SQLite 事件链/provenance。`--json` 输出带 `schema_version` 的稳定
报告；返回码 `0` 表示全部通过，`1` 表示运行时或检查失败，`2` 表示参数/配置错误。
报告只保留有限的失败码和脱敏诊断，不输出 API key、许可证序列号、完整 stderr、
用户目录或临时绝对路径。运行前必须安装可用的 Stata 18 与 `stata-mcp`（本机当前
为 perpetual 授权）；许可证或 MCP 不可用时应修复环境后重跑门禁，不能改用
`FakeExecutor` 冒充 live 通过。

当前全量 branch coverage 实测为 77%，CI 门槛为 75%。

golden 只比较结构化稳定字段，不锁 UUID、时间或绝对路径；源文件位于 `eval_golden/scenarios.json`，wheel 内含一份用于安装后评测的副本。当前开发轮次、完整验证命令和后续优先级见仓库根目录 `IMPLEMENTATION_PLAN.md`。

## 环境要求（live 项）
- LLM key：推荐从 UI“应用设置”选择目录 provider 后填写；`DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY`
  等环境变量仍作为兼容的只读来源。
- 隐私（默认 local_strict，**不自动发远端**）：要真用远端 LLM 须显式授权 `STATA_AGENT_PRIVACY=approved_remote`（或 mixed_sanitized）。UI/命令示例：
  `PYTHONUTF8=1 PYTHONPATH=src STATA_AGENT_PRIVACY=approved_remote OPENAI_API_KEY=sk-… python -m stata_agent.ui`
- 若使用环境方式，建议把对应 provider key 放 Windows 用户环境变量；页面管理优先使用 Credential Manager，别明文写进代码/聊天。
- Stata：可用的 Stata 18 + `stata-mcp` 仓库；通过 `STATA_MCP_DIR` 指向仓库（其 `.venv` 中需有 MCP Python）。
- 文献库（RAG）：设置 `STATA_AGENT_LIBRARY`；默认按 `style_only` 摄取，只有显式
  `source_role=citable_evidence` 的索引块可作为证据引用。

## UI（应用设置 + 当前研究只读投影）
```bash
cd app
PYTHONUTF8=1 PYTHONPATH=src python -m stata_agent.ui        # http://127.0.0.1:8001
# 可选 env：STATA_AGENT_DB=<路径>；STATA_AGENT_UI_PORT=端口
# 有目录 provider 凭据且显式授权后才用真 LLM；DeepSeek/DashScope 环境变量仍兼容；真 Stata 也需显式设置
# STATA_AGENT_EXECUTOR=stata。FakeExecutor 仅用于 STATA_AGENT_DEMO=1 或
# STATA_AGENT_EXECUTOR=fake 的演示/测试。
```
端点：`/`(页) · `GET /api/state` · `GET /api/events` · `POST /api/chat {text}` ·
`POST /api/chat/stream`（SSE，含 request_id/heartbeat） · `POST /api/resume`

### 应用设置中心 V1

侧栏“应用设置”提供模型/API、隐私、Stata、文献/附件、Agent/记忆/上下文、数据维护和关于分组；
“当前研究”单独展示账本中的 workspace/spec/phase/runs/claims 等只读信息。设置 API 使用
`GET /api/settings`、`PATCH /api/settings`、`PUT/DELETE /api/settings/secrets/{provider}`，
以及 provider/Stata/library check 和 backup create/verify。页面显示每项的有效值来源、可编辑状态、
生效时机和需重启标记；保存采用 revision 乐观并发控制，失败时保留 dirty 草稿并提示刷新合并。

普通设置优先级为 `default < .env < 用户 settings.json < 显式进程环境`，秘密优先级为
`.env < Windows Credential Manager < 显式进程环境`。环境管理的值在 UI 中锁定，`.env` 不会被页面
改写。Credential Manager 不可用时密钥保存 fail closed；响应只返回 `configured/source/editable`，
不会返回 key 内容。模型/provider 选项来自服务端闭合目录与能力画像；页面不写死某两个 provider，也不
接受任意自由文本模型 ID。放宽隐私模式必须未预选的显式确认，并在下一请求进行 provider I/O 前写入安全
`privacy.mode.changed` 审计；审计失败则不联网。

设置变更的生效时机：主题、默认交互模式和附件默认角色即时生效；provider、隐私、执行器、
上下文/记忆等从下一请求生效；端口、MCP、library/skills 和存储目录只产生 restart pending。
设置页只允许创建/验证有界备份，不提供在线 restore 或数据目录热迁移；恢复必须离线完成。

### Durable memory outbox 运维恢复

失败的 `memory.extraction` intent 可通过本地 operator API 查看和逐项恢复：

```bash
# 只返回当前 workspace 的安全失败摘要（limit 取 1..100）
curl 'http://127.0.0.1:8001/api/operations/memory-outbox/failed?ws=ui&limit=50'

# 使用 GET 返回的当前 state_version 明确确认 at-least-once 风险
curl -X POST 'http://127.0.0.1:8001/api/operations/memory-outbox/recover?ws=ui' \
  -H 'content-type: application/json' \
  -d '{"idempotency_key":"sha256:…","expected_state_version":7,"acknowledge_at_least_once":true}'
```

成功响应的 `outcome` 只有两种：`reconciled` 表示账本已经有匹配的 terminal
memory-extraction event，outbox 已在不调用 provider 的情况下完成；`redriven` 表示没有
terminal event，intent 已回到 `pending`，由现有 bounded dispatcher 后续 claim。redrive
不会同步调用 provider、不会增加 `max_attempts`，也不会创建新事件或新 outbox row。

请求必须带上 GET 观察到的 `state_version`；状态已经变化时返回 409，避免旧页面重复操作
新一轮失败。接口只返回 allow-listed metadata 和稳定错误码，不返回 source text、prompt、
provider response 或原始异常。`state_version` 是 freshness fence，不是认证凭据。

若之前的 provider 调用已经产生远端副作用但本地没有 terminal event，redrive 仍可能再次
调用 provider；因此必须显式确认 at-least-once 风险。系统不承诺 remote provider
exactly-once。该接口面向 localhost single-user 部署，不提供远程鉴权或批量重放。

### Durable memory outbox 保留与清理

completed outbox 只作为 dispatch index 保留；只有 ledger 已存在匹配 fingerprint 的终态
memory-extraction event，才允许进入 retention preview。failed、pending、processing、近期
completed、跨 workspace、metadata 不完整或没有终态证明的行都不会被删除。failed 行必须先
通过上面的 recovery 接口 reconcile，不能直接清理。

```bash
# 默认保留 90 天；retention_days 范围 7..3650，limit 范围 1..100
curl 'http://127.0.0.1:8001/api/operations/memory-outbox/retention/preview?ws=ui&retention_days=90&limit=100'

# 将 preview 返回的 cutoff/limit/selection_token 原样回传，并明确确认不可逆删除
curl -X POST 'http://127.0.0.1:8001/api/operations/memory-outbox/retention/prune?ws=ui' \
  -H 'content-type: application/json' \
  -d '{"cutoff":1786200000,"limit":100,"selection_token":"v1:…","acknowledge_irreversible_delete":true}'
```

preview 是只读且有界的：单次最多扫描 1,000 行，按 `(completed_at, outbox_id)` 稳定分页，
`scan_truncated=true` 时使用返回的 `next_cursor` 继续。prune 会重新计算选择并检查
selection token 与每行 `state_version`；选择变化返回 409，整批不会部分删除。成功响应的
`outcome` 为 `pruned` 或空选择的 `noop`，不表示 provider 已执行。

DELETE 只释放 SQLite 内部页面供后续复用，不承诺数据库文件缩小；本轮不执行自动清理、
VACUUM、WAL checkpoint、ledger event retention、audit log 或浏览器 UI。selection token 和
`state_version` 是 freshness fence，不是认证凭据。清理后重启仍会因为 terminal ledger event
而跳过 startup backfill，不会复活已删除的 completed intent。

### 本地诊断包（只读、隐私安全）

支持定位单轮请求时，可导出当前 workspace 的 bounded `diagnostic.bundle.v1`：

```bash
# 默认 event_limit=200、outbox_limit=100；范围分别为 1..500 和 1..200
curl -OJ 'http://127.0.0.1:8001/api/operations/diagnostics/bundle?ws=ui&event_limit=200&outbox_limit=100&request_id=req-…'
```

bundle 使用 request/operation/run 与 outbox `event_seq` 关联，只返回事件类型、序号、状态、
时间、计数和 opaque IDs 等 allow-listed metadata；默认不包含用户/模型文本、工具参数/结果、
Stata 代码/输出/机器结果、prompt、memory/provider 内容、路径、lease 凭据或原始错误。
`request_id` 是可选的精确过滤器，不能当作认证或授权凭据。导出是逐个组件读取的
point-in-time 支持快照，不保证跨 ledger/outbox/queue 的原子一致性；queue 统计明确是
process-local/non-durable。接口只读，不启动 provider、executor、dispatcher 或 queue callback，
local_strict 和 provider 不可用时仍可导出。它不是完整历史审计、远程安全边界或 SLO 指标；
OTel、远程上传和告警另行设计。

### 备份、恢复与发布门禁

Provider 策略门禁默认纯本地运行；只有显式 `--live` 且已有 live/privacy 环境门同时放行时，才发送固定、
不含研究内容的 canary：

```powershell
stata-agent-provider-check --json
stata-agent-provider-check --live --json
```

备份使用 SQLite online-backup API 生成一致快照，并以 manifest 中的 size/SHA-256 校验数据库和
可选私有附件目录。verify/restore 会拒绝 zip-slip、符号链接、重复或未声明成员、超限、hash 不符、
损坏数据库和未来 schema。restore 只允许在 UI 已停止时显式执行，必须确认数据替换，并在替换前生成
可验证的 pre-restore rollback bundle：

```powershell
stata-agent-backup create --database .\agent.sqlite3 --output .\backup.zip --assets .\.attachments
stata-agent-backup verify --bundle .\backup.zip
stata-agent-backup restore --bundle .\backup.zip --database .\agent.sqlite3 --assets .\.attachments --acknowledge-data-replacement
```

默认 release doctor 纯本地、零网络，只证明自动门禁；没有真实签名/安装/升级/回滚证据时
`release_ready=false` 是预期结果：

```powershell
python -m stata_agent.release_doctor --offline --json
python -m stata_agent.release_doctor --offline --json --wheel .\dist\stata_agent-*.whl
```

Windows Authenticode、clean install、upgrade/rollback 与含附件恢复必须按
`RELEASE_ACCEPTANCE_MATRIX.md` 人工执行并生成 attestation；代码不会伪造签名或安装验收。

主链只有一条：`/api/chat`、SSE 和 `/api/resume` 都进入 `harness.agent_loop`；
审批接口只写入审批事件，再通过同一条 resume 链继续。`interactive` 默认允许单条消息内部
16 个 provider 回合，`goal` 默认 64 个，下一条提交重新计数；顶层工具调用默认上限 128。
发生工具调用后，最后一个 provider 回合只用于汇总结果。这些预算可在设置页调整并从下一请求生效。
旧 `runner.run_until_gate` 仅供
兼容测试/库调用，不是 UI 路由。

## 包地图（src/stata_agent/）
- `events/` 事件账本内核：schema/append(写入权)/upcast/reconcile
- `domain/` reducer：models + fold（非法序列拒绝/未闭合探测）+ ActionProposal
- `storage/sqlite_store.py` append/scan/project/快照 + writer lease/fence
- `policy/` 六步裁决（DENY/ASK/ALLOW + 隐私门/研究闸门）
- `phase/` Phase/RunStatus/GateMode + 合法迁移表
- `providers/` capability profiles + closed provider/model catalog + OpenAI-compatible transport；DeepSeek/Qwen 历史适配器、registry（按 settings/env 选）和 mock
- `harness/` build_context + research_turn（单轮，可带 extra_context）
- `tools/` stata_client(持久会话)/executor(真 Stata→事件+合同提取)/result_verifier(12 项失败关闭验收)/evidence_signer(机器层→卡)/fake_executor(显式 test 合同)
- `skills/` SKILL.md 政策包加载 + ados 预检
- `rag/` 有界 PDF 摄取（内容哈希 doc/chunk identity、显式 source_role）+
  原子增量缓存 + 词法/哈希向量混合检索
- `writer/` 数字接地 round-trip + claims→.docx
- `runner.py` cycle / run_until_gate（loop-until-gate 自动链）
