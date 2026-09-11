# Release Acceptance Matrix V1

本矩阵是发布证据清单，不是第二份 roadmap。自动门禁全部通过，只代表工件在本机可构建、可验证；
没有对应人工证据时，不得把 Windows 安装包标记为 release-ready。

## Current candidate evidence (2026-09-11)

候选 wheel：`stata_agent-0.1.0-py3-none-any.whl`，SHA-256
`73253254AAE967C9B3A4D7E36AC41D709FA5633B61AA53876B8E5C650CE31D83`；验证环境为
Windows `10.0.26200.0`、Python `3.12.3`。

| Gate | Current status | Evidence / blocker |
|---|---|---|
| Wheel clean-install smoke | passed | 最新候选 wheel 在全新临时 venv 安装；`pip check`、包内 UI/golden、settings/webui 资源导入与 `ui.app` 导入通过，无源码 `PYTHONPATH`；真实浏览器交互另列为 manual pending |
| Full `clean_install_verified` | pending | 尚无 installer，也未在全新 Windows 用户配置文件执行安装/卸载及“卸载保留用户数据”验证 |
| `backup_restore_verified` | passed for this candidate | 在线备份 schema v4、6 events 与 1 个附件；新目标恢复后 SQLite integrity 为 `ok`、event 数一致、附件 SHA-256 一致，并生成且验证 pre-restore rollback bundle |
| `windows_signature_verified` | blocked | 当前只有 `.whl`；Windows trust provider 不支持把 wheel 当 Authenticode 目标，仓库无 installer、签名证书或 `signtool` 流程 |
| `upgrade_verified` | blocked | 仓库无上一支持版本 tag/artifact，无法构造真实旧版本升级证据 |
| `rollback_verified` | partial / pending | 数据级 pre-restore rollback bundle 已验证；缺上一支持版本，尚不能证明旧应用版本可重新启动 |
| Failure-path/manual UX | automated V1 passed / manual pending | 运行期异常 browserless 矩阵已通过；仍需短人工检查提示可理解性、按钮锁定、刷新恢复和键盘操作；安装/卸载继续延期 |

## Settings center evidence (V1)

本轮设置中心的自动证据覆盖：revisioned 非秘密配置、`.env`/用户/显式环境优先级、Credential
Manager 生命周期（测试使用 in-memory adapter）、隐私确认与 provider I/O 前审计、不可变 request
快照、restart pending、localhost Host/Origin/CSRF/body/no-store 防护、健康检查、backup verify、
设置导航与当前研究只读分离。对应测试由 `IMPLEMENTATION_PLAN.md` 的 settings/runtime/API/UI
targeted commands 和全量 pytest 提供；默认不会访问真实 provider 或 Stata。

真实浏览器 smoke 仍需在本机短验：打开“应用设置”、修改并刷新非秘密项、确认环境锁定和 restart
提示、设置/替换/删除凭据但不回显 secret、拒绝未确认的隐私放宽、运行安全健康检查，并确认
“当前研究”没有编辑控件。该 evidence 不改变 installer、Authenticode、clean-install、upgrade
或 rollback 的 pending/blocked 状态；也不把本地测试的 in-memory credential 误报为 Windows
发布签名证明。

上表是本轮候选证据记录，不等同于完整 attestation；在所有必需项完成前，release doctor 必须继续返回
`release_ready=false`。

## Automated gate

| Check | Command | Pass condition |
|---|---|---|
| Unit/integration | `python -m pytest -q` | 全部通过；只允许仓库中已有且有理由的 skip |
| Static | `python -m ruff check src tests` / `python -m mypy src` | 均通过 |
| Product eval | `python -m stata_agent.eval --json` | 所有 golden scenarios 通过 |
| Coverage | `python -m coverage report --fail-under=75` | branch coverage ≥ 75% |
| Wheel | `python -m build --wheel` | 构建成功，包含 UI、golden 和入口点 |
| Release doctor | `python -m stata_agent.release_doctor --offline --json --wheel <wheel>` | `automated_ok=true` |
| Backup | `stata-agent-backup create/verify` | SQLite integrity、manifest size/hash 全通过 |
| Operator governance | `python -m stata_agent.eval --scenario L7_OPERATOR_GOVERNANCE --json` | tool identity、恢复状态与 Skill hash-bound promotion 全通过 |

## Manual Windows gate

每项记录日期、操作者、OS build、Python/Stata 版本、artifact SHA-256 和诊断 bundle ID。秘密、原始研究
文本和绝对用户路径不得进入证据文件。

| Attestation key | Procedure | Pass condition |
|---|---|---|
| `windows_signature_verified` | 对最终 installer/artifact 验证 Authenticode 发布者、时间戳和 SHA-256 | 签名有效，发布者匹配，下载后仍有效 |
| `clean_install_verified` | 全新 Windows 用户目录安装并启动 UI | 无源码/PYTHONPATH 依赖；health 正常；卸载不删除用户数据 |
| `upgrade_verified` | 从上一支持版本带真实副本 DB 升级 | migration 一次完成；workspace/events/memory/attachments 可读 |
| `rollback_verified` | 升级失败后用受支持旧版本和 rollback bundle 恢复 | 原 DB 未损坏；旧版本可启动；失败可诊断 |
| `backup_restore_verified` | 含附件的 backup→新目录 restore | event/memory/attachment hash 一致；篡改 bundle 被拒绝 |

## Failure-path UX gate

- 断网、DNS/TLS、429、provider stream 中断：不泄露 raw exception；明确是否可重试和诊断入口。
- 用户 stop、浏览器断连、workspace 切换：旧请求停止，附件上传取消，状态不串 workspace。
- oversized/disguised/encrypted/scanned/malformed PDF、路径穿越/UNC/ADS 名称：稳定拒绝或 quarantine，
  外部 sentinel 不变，非-ready 文件不能进入 RAG。
- SQLite 锁、损坏/篡改 bundle、未来 schema：restore 在替换目标前失败，原 DB 保持可读。
- 同名 tool call 的开始/完成节点保持一一对应；stop、断流和刷新期间未确认终态前不能重复发送。
- Skill candidate 篡改、非人工 reviewer、错误 digest、重复 promote、路径越界和 symlink：全部拒绝，原 active 不变。

## Runtime Failure & Recovery UX V1 (automated)

本表只记录本轮可以在 browserless 环境证明的运行期安全契约；`manual pending` 不得被解释为
release-ready。每一项均要求稳定错误码/终态、无内部细节泄漏，并在新状态读取中保持一致。

| Scenario | Automated evidence | Terminal/state contract | Retry action | Refresh result | Manual follow-up |
|---|---|---|---|---|---|
| Stata/provider interruption or failure | `test_product_ux_coverage.py` terminal SSE/replay cases + runtime cancellation tests | `failed` / `uncertain` with `provider_error` or `uncertain` | provider unavailable only; uncertain never auto-runs | durable reason preserved | pending |
| User stop / transport disconnect | stream worker start→stop→ledger→fresh `/api/state` integration cases | `cancelled` before side effect; `uncertain` after unknown side effect | cancelled may start a new request after confirmation; uncertain diagnostics only | no phantom running request | pending |
| SQLite writer conflict | safe `LeaseConflict`/`StaleWrite` HTTP envelope + fencing regression | `409`, `ledger_writer_conflict` or `ledger_writer_stale` | refresh then retry; no takeover loop | never projected as completed/idle | pending |
| Attachment rejection/quarantine | attachment service/storage/UI matrix (encrypted, truncated, timeout, crash, oversize) | stable `status` + `error_code`; non-ready is not model-visible | deterministic reject: reselect; transient parser: safe re-upload | quarantine/rejected survives listing | pending |
| Browser refresh after active/terminal run | active request projection + fresh-state integration and browserless contract tests | active request remains locked; terminal state is explicit | only after terminal confirmation | cancelled/failed/uncertain remains visible | pending |
| Duplicate approval submission | concurrent approve/reject/modify race tests | one terminal decision; loser `409 http_409` | refresh winner; no duplicate side effect | winner is durable and unique | pending |

Automated commands for this table are the targeted runtime/attachment/UI tests in `IMPLEMENTATION_PLAN.md`;
the full pytest, Ruff, Mypy, eval and coverage gates remain mandatory before release packaging.

## Attestation file

完成所有人工项后创建本地（不要伪造或提交未经验证的值）JSON：

```json
{
  "schema": "stata-agent.release-attestation.v1",
  "windows_signature_verified": true,
  "clean_install_verified": true,
  "upgrade_verified": true,
  "rollback_verified": true,
  "backup_restore_verified": true
}
```

最终检查：

```powershell
python -m stata_agent.release_doctor --offline --json --wheel <wheel> --attestation <json> --require-release-ready
```
