# stata-agent · app（切片 0–5b 代码）

> 依据 `../design/impl-plan.md`；契约见 `../design/SPEC.md` v0.5 与 DD-01…07。
> 当前里程碑：**LLM 自动多步 → 真 Stata 回归 → 证据入事件链（可复现/可审计）**已跑通。

## 快速跑（在 app/ 下）

```bash
# 离线测试（无需网络/Stata）
python -m pytest -q

# CLI mock loop（你说一句/想一步/回一句，全落 events）
printf '问题\n补料\n' | PYTHONPATH=src python -m stata_agent.cli --db .demo.sqlite3

# live LLM 自动链（无 DEEPSEEK_API_KEY 时自动用 DASHSCOPE=qwen）
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

## 环境要求（live 项）
- LLM key：设 `DEEPSEEK_API_KEY`（deepseek，优先）或已有 `DASHSCOPE_API_KEY`（qwen 兜底）。
- 隐私（默认 local_strict，**不自动发远端**）：要真用远端 LLM 须显式授权 `STATA_AGENT_PRIVACY=approved_remote`（或 mixed_sanitized）。UI/命令示例：
  `PYTHONUTF8=1 PYTHONPATH=src STATA_AGENT_PRIVACY=approved_remote DEEPSEEK_API_KEY=sk-… python -m stata_agent.ui`
- 建议把 key 放 Windows 用户环境变量（`setx DEEPSEEK_API_KEY sk-…`），别明文写进代码/聊天。
- Stata：`C:\Program Files\Stata18` + stata-mcp 仓库 `C:\Users\user\stata-mcp`（`tools/stata_client.py` 默认指向其 .venv）。
- 文献库（RAG 演示）：`D:\work file\06_学位论文\一区\文献(1)`（见 `tests/test_rag_registry.py`）。

## UI（只读物化视图 + 发消息，M4 方向）
```bash
cd app
PYTHONUTF8=1 PYTHONPATH=src python -m stata_agent.ui        # http://127.0.0.1:8001
# 可选 env：STATA_AGENT_DB=<路径>；STATA_AGENT_UI_PORT=端口
# 有 DEEPSEEK/DASHSCOPE key 自动用真 LLM；默认 FakeExecutor 不碰真 Stata
```
端点：`/`(页) · `GET /api/state` · `GET /api/events` · `POST /api/chat {text}`

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
- `rag/` PDF 摄取(角色/chunk_id) + 词法检索（中文双字）
- `writer/` 数字接地 round-trip + claims→.docx
- `runner.py` cycle / run_until_gate（loop-until-gate 自动链）
