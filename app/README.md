# stata-agent · app（切片 0–5b 代码）

> 依据 `../design/impl-plan.md`；契约见 `../design/SPEC.md` v0.5 与 DD-01…07。
> 当前里程碑：**LLM 自动多步 → 真 Stata 回归 → 证据入事件链（可复现/可审计）**已跑通。

## 快速跑（在 app/ 下）

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

产品评测包含四个稳定 golden scenario：工具选择/拒绝、run FSM 与 uncertain 恢复、证据数字接地，以及 `消息 → 工具 → Fake → card → claim → draft` 完整链路。命令返回 0（全通过）、1（scenario 失败）、2（golden/配置错误）；`--scenario L4_FAKE_TO_DRAFT` 可只跑单项。安装开发依赖后也可使用 `stata-agent-eval --json`。

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

当前全量 branch coverage 实测为 75%，CI 门槛同样为 75%。

golden 只比较结构化稳定字段，不锁 UUID、时间或绝对路径；源文件位于 `eval_golden/scenarios.json`，wheel 内含一份用于安装后评测的副本。真实 Stata、远端模型和 Windows UI 验收步骤见仓库根目录 `PRODUCTIZATION_PLAN.md` 的“发布门禁与验收清单”。

## 环境要求（live 项）
- LLM key：设 `DEEPSEEK_API_KEY`（deepseek，优先）或已有 `DASHSCOPE_API_KEY`（qwen 兜底）。
- 隐私（默认 local_strict，**不自动发远端**）：要真用远端 LLM 须显式授权 `STATA_AGENT_PRIVACY=approved_remote`（或 mixed_sanitized）。UI/命令示例：
  `PYTHONUTF8=1 PYTHONPATH=src STATA_AGENT_PRIVACY=approved_remote DEEPSEEK_API_KEY=sk-… python -m stata_agent.ui`
- 建议把 key 放 Windows 用户环境变量（`setx DEEPSEEK_API_KEY sk-…`），别明文写进代码/聊天。
- Stata：可用的 Stata 18 + `stata-mcp` 仓库；通过 `STATA_MCP_DIR` 指向仓库（其 `.venv` 中需有 MCP Python）。
- 文献库（RAG）：设置 `STATA_AGENT_LIBRARY`；默认按 `style_only` 摄取，只有显式
  `source_role=citable_evidence` 的索引块可作为证据引用。

## UI（只读物化视图 + 发消息，M4 方向）
```bash
cd app
PYTHONUTF8=1 PYTHONPATH=src python -m stata_agent.ui        # http://127.0.0.1:8001
# 可选 env：STATA_AGENT_DB=<路径>；STATA_AGENT_UI_PORT=端口
# 有 DEEPSEEK/DASHSCOPE key 且显式授权后才用真 LLM；真 Stata 也需显式设置
# STATA_AGENT_EXECUTOR=stata。FakeExecutor 仅用于 STATA_AGENT_DEMO=1 或
# STATA_AGENT_EXECUTOR=fake 的演示/测试。
```
端点：`/`(页) · `GET /api/state` · `GET /api/events` · `POST /api/chat {text}` ·
`POST /api/chat/stream`（SSE，含 request_id/heartbeat） · `POST /api/resume`

主链只有一条：`/api/chat`、SSE 和 `/api/resume` 都进入 `harness.agent_loop`；
审批接口只写入审批事件，再通过同一条 resume 链继续。`interactive` 只执行
一个 loop step，`goal` 使用本轮有界多步预算。旧 `runner.run_until_gate` 仅供
兼容测试/库调用，不是 UI 路由。

## 包地图（src/stata_agent/）
- `events/` 事件账本内核：schema/append(写入权)/upcast/reconcile
- `domain/` reducer：models + fold（非法序列拒绝/未闭合探测）+ ActionProposal
- `storage/sqlite_store.py` append/scan/project/快照 + writer lease/fence
- `policy/` 六步裁决（DENY/ASK/ALLOW + 隐私门/研究闸门）
- `phase/` Phase/RunStatus/GateMode + 合法迁移表
- `providers/` capabilities(codec/chat_structured 重试)/deepseek+qwen/registry(按 env 选)/mock
- `harness/` build_context + research_turn（单轮，可带 extra_context）
- `tools/` stata_client(持久会话)/executor(真 Stata→事件)/evidence_signer(机器层→卡)/fake_executor
- `skills/` SKILL.md 政策包加载 + ados 预检
- `rag/` 有界 PDF 摄取（内容哈希 doc/chunk identity、显式 source_role）+
  原子增量缓存 + 词法/哈希向量混合检索
- `writer/` 数字接地 round-trip + claims→.docx
- `runner.py` cycle / run_until_gate（loop-until-gate 自动链）
