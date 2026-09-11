# Product Settings Center V1

## 1. Status and decision

- Priority: **P1**
- Delivery shape: one complete product slice, not a read-only placeholder.
- Architecture change required: **No**. The design adds an application-layer settings service and a
  local operational configuration repository. It does not change the research ledger, agent
  orchestration chain, SQLite lease model, evidence authority, or privacy policy.
- Product boundary: Windows-first, localhost, single-user desktop application.

The existing sidebar entry named “设置” must become a real application settings center. The current
mixed page must be split into:

1. **应用设置** — editable operational configuration.
2. **当前研究设定** — a read-only workspace projection. Research question, variables, FE, cluster,
   identification strategy and main specification continue to change only through conversation,
   approval and ledger events.

## 2. Problem statement

The current button cannot navigate because its delegated click listener is scoped to the workspace
list while the button lives in the sidebar footer. Even after navigation is repaired, the page only
renders a read-only mixture of workspace and process configuration.

Runtime configuration is currently spread across direct `os.environ` reads, eager `.env` loading,
provider construction, Stata execution composition, context-budget creation and UI projection. This
causes five product problems:

- users must edit environment variables or PowerShell commands;
- effective values and their sources are not distinguishable;
- secrets have no supported UI lifecycle;
- hot-apply versus restart-required behavior is undefined;
- provider, Stata and path errors are discovered only after starting real work.

## 3. Goals

The completed P1 slice must:

1. provide a functional, accessible settings route;
2. expose all supported product configuration through one typed effective-settings contract;
3. securely set, replace and delete provider credentials without returning them to the browser;
4. distinguish default, `.env`, saved-user and process-environment sources;
5. validate changes atomically and prevent partial application;
6. make apply timing explicit: immediate, next request or restart required;
7. reuse the existing provider acceptance, Stata doctor, diagnostics and backup services;
8. keep privacy fail-closed and audit privacy relaxation;
9. preserve current environment-variable compatibility;
10. provide enough health information that a non-programmer can finish setup without a terminal.

## 4. Non-goals and hard boundaries

- The settings UI must not edit ResearchState, events, claims, cards, runs, approvals or model
  specifications directly.
- It must not write `.env` files or mutate `os.environ` as its persistence mechanism.
- It must not store API keys in the ledger, application SQLite database, settings JSON, DOM,
  `localStorage`, diagnostics or logs.
- It must not expose arbitrary model identifiers until a matching `ModelCapabilityProfile` exists.
- It must not allow policy bypasses such as “disable validation”, “skip approval”, “trust all files”,
  “auto-rerun uncertain tools” or “allow unrestricted Stata”.
- FakeExecutor remains hidden unless explicit demo/developer mode is active.
- Changing the database, workspace or attachment root is not a normal field edit. These paths are
  displayed in V1; relocation remains a separately verified migration workflow.
- Installer, Authenticode, SmartScreen and cross-version application rollback remain outside this
  feature.

## 5. Architectural placement

```text
webui settings view
        |
        v
FastAPI settings adapter
        |
        v
SettingsService ---------------> Provider/Stata/Library health adapters
        |
        +---- EffectiveSettingsResolver
        |          |
        |          +-- process environment (read-only override)
        |          +-- user settings repository
        |          +-- legacy .env fallback
        |          +-- defaults
        |
        +---- SecretStore ------> Windows Credential Manager
```

Rules:

- `ui.py` owns HTTP composition only.
- `SettingsService` is framework-independent and must not import FastAPI or the UI module.
- Runtime consumers receive an immutable `EffectiveSettings` snapshot. They no longer make
  independent policy decisions from ad-hoc environment reads.
- `ChatService -> agent_loop -> tool/provider/store` remains the sole orchestration chain.
- The research ledger remains the sole truth source for research state. The settings repository is
  explicitly operational application configuration, not a research-state store.

## 6. Configuration scopes

| Scope | Examples | Persistence | Application timing |
|---|---|---|---|
| Application | provider, Stata, privacy default, library, memory, context | user settings | next request or restart |
| Secret | Credentials for a catalog provider | Windows Credential Manager | next request |
| Workspace projection | question, phase, spec, variables, FE, cluster | research ledger | read-only in settings |
| Session/UI preference | theme, sidebar state, default interaction mode | user settings | immediate |
| Computed health | provider latency, Stata version, index state, storage usage | not authoritative | refreshed on demand |

V1 uses one application-wide privacy default. Each request captures the effective privacy mode in its
runtime context. A change never mutates an in-flight request.

## 7. Effective settings and precedence

For ordinary values, the resolver uses:

```text
defaults < legacy .env < saved user settings < explicit process environment
```

For secrets:

```text
legacy .env < Windows Credential Manager < explicit process environment
```

An explicit process environment value always wins and is read-only in the UI. The response must show
`source=environment` and `editable=false`; saving a shadow value that cannot become effective is
forbidden.

The existing `.env` file remains a compatibility fallback. The settings center never rewrites it.
The configuration loader must retain source identity rather than flattening every source into
`os.environ` before resolution.

Each exposed setting is represented by a projection equivalent to:

```json
{
  "key": "privacy.mode",
  "value": "local_strict",
  "source": "default",
  "editable": true,
  "sensitive": false,
  "apply_mode": "next_request",
  "pending_restart": false,
  "status": "ready",
  "message": "本机严格模式"
}
```

Allowed `source` values are `default`, `dotenv`, `user`, `credential`, and `environment`.
Allowed `apply_mode` values are `immediate`, `next_request`, and `restart`.

## 8. Settings catalog

### 8.1 Overview

The first screen is a readiness dashboard, not a second copy of every form. It shows:

- model: configured provider, live-enabled state and last safe connectivity result;
- privacy: effective mode and whether remote use is permitted;
- Stata: disabled/ready/error, engine version and doctor timestamp;
- library: path configured, readable state, index count and last refresh;
- memory/context: deterministic/provider/off modes and active budget preset;
- storage: database, workspace and attachment locations plus usage;
- pending changes: count and whether restart is required;
- application version, build, Windows/Python details and Stata license availability.

No license serial, credential, full exception or remote response body is shown.

### 8.2 Model and API

| Key/action | Type | Default | Apply | Notes |
|---|---|---|---|---|
| `provider.primary` | `auto` or a catalog provider id | `auto` | next request | `auto` follows the reviewed catalog order |
| `provider.model` | `auto` or a catalog model id | `auto` | next request | model must resolve to a matching capability profile/provider |
| `provider.live_enabled` | boolean | false | next request | separate explicit network opt-in |
| Provider credential | secret action | absent | next request | one generic provider selector; set/replace/delete; never readable |
| `provider.base_url` | optional HTTPS URL | empty | next request | selected provider override; empty uses catalog default |
| Provider connectivity | action | n/a | n/a | fixed canary only; no research data |

The server owns a closed provider/model catalog. Each selectable model has a
`ModelCapabilityProfile` describing tools, streaming, JSON mode and context/output bounds; a provider
is added only with its credential mapping, endpoint contract and offline/provider contract tests. The
UI renders this catalog and therefore does not hard-code a DeepSeek/Qwen-only list or accept arbitrary
free-text model IDs. The initial catalog includes the historical DeepSeek/Qwen adapters plus several
OpenAI-compatible providers; vendor/model availability still requires a configured endpoint and a
successful canary check.

The initial catalog ids are `deepseek`, `qwen`, `openai`, `moonshot`, `zhipu`, `groq`, `mistral` and
`openrouter`; the exact model list and capability metadata are served by `provider_catalog` so the
browser does not duplicate this registry.

Fallback order is visible through the selected catalog projection. Any fallback continues to obey the
existing privacy boundary and never changes the model/tool orchestration contract.

### 8.3 Privacy and network

| Key | Values | Apply | Required UX |
|---|---|---|---|
| `privacy.mode` | `local_strict`, `mixed_sanitized`, `approved_remote` | next request | explain data movement |

- `local_strict` remains the default and fail-closed fallback.
- Moving to a less strict mode requires a confirmation dialog showing provider, data categories and
  the fact that the change affects future requests only.
- The confirmation must use an explicit acknowledgement, not a prechecked box.
- Saving the application default updates only the revisioned settings repository. At the next request,
  before any provider I/O, the chat bootstrap compares the workspace's last audited mode and appends
  `privacy.mode.changed` when the effective mode differs. The event carries scope, old mode, new mode
  and settings revision, with no secret or research content. If this audit append fails, the request
  fails closed before network access. This avoids pretending that a JSON settings write and a SQLite
  ledger write are one atomic transaction.
- Tightening privacy requires no warning and takes effect for the next request.
- Live-provider enablement and privacy mode are separate controls; both gates must pass.

### 8.4 Stata and execution

| Key/action | Type | Default | Apply | Notes |
|---|---|---|---|---|
| `executor.kind` | `disabled/stata` | disabled | next request | `fake` only in demo/developer mode |
| `stata.mcp_dir` | directory | unset | restart | canonical real directory only |
| Stata environment check | action | n/a | n/a | reuses bounded Stata doctor |
| Engine/license status | computed | n/a | n/a | available/unavailable only |
| Version and required ado | computed | n/a | n/a | safe allow-listed output |
| Run root | computed | n/a | n/a | read-only |

The doctor action runs in an isolated temporary workspace. It may be cancelled, returns stable
check codes and never exposes license identifiers or raw third-party stderr.

### 8.5 Literature library and attachments

| Key/action | Type | Default | Apply | Notes |
|---|---|---|---|---|
| `library.root` | directory | unset | restart/reload | must exist and be readable |
| `skills.root` | directory | packaged skills | restart/reload | safe path validation |
| `attachments.default_role` | `style_only/citable_evidence` | `style_only` | immediate | per-upload override remains |
| Library check | action | n/a | n/a | count/readability, no extraction dump |
| Rebuild index | action | n/a | n/a | explicit bounded operation |
| Limits/usage | computed | n/a | n/a | file/quota/storage status |

Changing a library path invalidates only derived indexes. It does not delete source files. A rebuild
must remain idempotent and preserve `source_role` isolation.

### 8.6 Agent, context and memory

| Key | Values/default | Apply | Validation |
|---|---|---|---|
| `agent.default_mode` | `interactive` | immediate | `interactive/goal` |
| `agent.interactive_max_steps` | 16 | next request | integer 2–128；每条用户消息重置 |
| `agent.goal_max_steps` | 64 | next request | integer 2–256；每个目标任务重置 |
| `agent.max_tool_calls` | 128 | next request | integer 1–512；只计顶层 agent tool call |
| `compaction.summary_mode` | `deterministic` | next request | deterministic/provider |
| `memory.extraction_mode` | `off` | next request | off/provider |
| `context.max_input_tokens` | 16000 | next request | positive integer |
| `context.reserve_output_tokens` | 4000 | next request | <= max input |
| `context.recent_tail_tokens` | 4000 | next request | non-negative |
| `context.memory_tokens` | 1500 | next request | non-negative |

Provider-backed compaction or memory extraction is disabled unless provider, live and privacy gates
all pass. These fields appear under “高级”; the UI offers “恢复推荐值” and shows the resulting usable
input budget before saving.

`max_steps` is not a conversation-message limit. It bounds provider turns inside one submitted user
message or goal run and starts fresh on the next submission. Executor-internal Stata/MCP operations do
not consume this counter. After any tool call, the last available provider turn receives no tool schemas
and is reserved for synthesizing the completed tool result into a user-facing answer.

The remaining internal hard/soft limits and token-character heuristic stay computed unless a later
measured need justifies exposing them.

### 8.7 Data, maintenance and diagnostics

- Show effective database, workspace and attachment roots with source and restart status.
- Provide “打开数据目录” only through a bounded local desktop integration; never accept a path from
  browser-controlled navigation.
- Provide live backup using the existing verified `ReleaseOperations.create_backup` behavior.
- Verify a selected backup before any restore decision.
- Restore remains offline: the settings page may validate and schedule the operation, but cannot
  replace the active database while the UI writer is running.
- Provide the existing privacy-safe diagnostic bundle action.
- Data relocation is displayed as unavailable until a backup-copy-verify-swap migration workflow is
  separately implemented; direct editing of storage roots is prohibited.

### 8.8 Appearance and about

- `ui.theme`: `system/light/dark`, applied immediately.
- Interface language is `zh-CN` in V1 and displayed as read-only until localization exists.
- Show package version, build identity, Python/Windows version and release-gate status.
- Show Stata authorization as `available/unavailable`; the known perpetual license may be labelled
  “永久授权” only from trusted local metadata, never from raw banner text.
- No serial number or machine identifier is exposed.

## 9. Persistence and concurrency

### 9.1 Non-secret repository

Store non-secret user configuration at:

```text
%LOCALAPPDATA%\StataAgent\settings.json
```

The document contains only:

```json
{
  "schema": "stata-agent.settings.v1",
  "revision": 1,
  "updated_at": 0,
  "values": {}
}
```

Requirements:

- explicit allow-list of keys;
- JSON schema/version validation;
- bounded file size and nesting;
- write to a sibling temporary file, flush, atomically replace;
- Windows inter-process lock around compare-and-write;
- optimistic `expected_revision`; stale callers receive `409 settings_revision_conflict`;
- future schema fails closed and leaves the file untouched;
- invalid/corrupt configuration falls back to safe defaults and reports a health failure instead of
  silently accepting partial values.

### 9.2 Secret repository

Use a `SecretStore` protocol with:

- `WindowsCredentialSecretStore` for production;
- `InMemorySecretStore` for tests;
- environment credentials as higher-priority read-only overrides.

Credential target names are fixed application constants derived from the reviewed provider catalog,
such as `StataAgent/provider/deepseek/default`. The browser can only query `configured`, `source` and
`editable`; it cannot read secret text, length, prefix or last characters. The page exposes one generic
provider selector rather than one permanent form row per vendor.

If Windows Credential Manager is unavailable, saving fails closed with
`secure_store_unavailable`. Environment-based credentials continue to work.

## 10. Application contracts

Recommended framework-independent types:

- `SettingsDocument(schema, revision, updated_at, values)`
- `EffectiveSetting(key, value, source, editable, sensitive, apply_mode, status, message)`
- `EffectiveSettings(revision, values, restart_required, pending_keys)`
- `SettingsPatch(expected_revision, changes)`
- `SettingsApplyResult(revision, changed_keys, restart_required, effective)`
- `SettingsHealthResult(component, status, code, message, checked_at, metrics)`
- `SecretStatus(provider, configured, source, editable)`

`SettingsService` responsibilities:

1. resolve and serialize effective settings;
2. validate a whole patch before writing;
3. reject environment-managed keys;
4. enforce idle/next-request/restart rules;
5. coordinate secret lifecycle without reading secrets back;
6. call bounded provider/Stata/library checks;
7. expose safe restart and health projections;
8. produce privacy-change audit metadata.

It must not construct prompts, invoke the agent loop or write research projections.

## 11. HTTP API

All responses use allow-listed fields and the existing safe error envelope.

| Endpoint | Purpose |
|---|---|
| `GET /api/settings` | full masked effective projection, revision and health summary |
| `PATCH /api/settings` | atomic non-secret patch with `expected_revision` |
| `PUT /api/settings/secrets/{provider}` | set or replace one credential |
| `DELETE /api/settings/secrets/{provider}` | delete one credential |
| `POST /api/settings/check/provider` | fixed provider canary |
| `POST /api/settings/check/stata` | isolated Stata doctor |
| `POST /api/settings/check/library` | bounded path/index readiness check |
| `POST /api/settings/backup` | create verified live backup |
| `POST /api/settings/backup/verify` | verify bundle without restore |

Patch example:

```json
{
  "expected_revision": 4,
  "changes": {
    "privacy.mode": "mixed_sanitized",
    "provider.live_enabled": true
  },
  "privacy_acknowledgement": true
}
```

The API must reject unknown keys, wrong types, invalid enum values, invalid paths, non-HTTPS remote
URLs, conflicting revisions and writes while a prohibited active operation exists.

## 12. Local HTTP security

Settings mutation materially increases the risk of a malicious webpage targeting localhost.
Therefore state-changing endpoints require all of the following:

- exact localhost Host validation;
- same-origin `Origin`/`Referer` validation;
- an unguessable session CSRF token issued by the UI bootstrap and sent in a custom header;
- JSON content type and bounded request body;
- no permissive CORS;
- no secrets in query strings;
- `Cache-Control: no-store` for settings and secret responses;
- stable errors with no raw exception, filesystem internals or credential metadata.

The token is process/session control, not user authentication. Remote multi-user access remains out of
scope.

## 13. Apply and restart semantics

- **Immediate:** theme, default interaction mode and default attachment role.
- **Next request:** provider choice, live enablement, privacy, compaction, memory extraction and context
  budgets. An in-flight request retains its immutable starting snapshot.
- **Restart:** UI port, Stata MCP path and components with process-level caches such as library/skills
  root until explicit reload is proven safe.
- **Never direct:** active database/workspace/attachment root relocation.

The UI maintains a sticky save bar with changed-key count. Save validates and commits the complete
patch once. It then shows either “已生效”, “下次请求生效” or “需要重启”. There is no optimistic local
pretence before the server confirms the new revision.

## 14. User experience

### 14.1 Page structure

- Desktop: narrow internal navigation on the left, one form section on the right.
- Mobile/narrow: single-column sections with the same order.
- Overview appears first; status cards link to the corresponding section.
- Advanced context fields and custom endpoints are collapsed by default.
- Research settings are a separate read-only page labelled “当前研究”, not “应用设置”.

### 14.2 Form behavior

- Dirty fields are visibly marked; leaving the page prompts to discard or stay.
- Save is disabled when validation fails or nothing changed.
- Environment-managed fields show a lock and their source.
- Secret fields show only configured/not configured and offer replace/delete.
- Connectivity checks are independent of Save and show timestamp, safe code and latency bucket.
- Keyboard navigation, visible focus, labels, descriptions and error association are mandatory.
- No `innerHTML`; all dynamic output continues through safe DOM construction.

### 14.3 Failure behavior

Stable codes include:

- `settings_revision_conflict`
- `settings_invalid_value`
- `settings_environment_managed`
- `settings_restart_required`
- `settings_busy`
- `secure_store_unavailable`
- `provider_check_failed`
- `stata_check_failed`
- `library_check_failed`
- existing safe storage/ledger conflict codes

Failures keep the form values locally in memory, never in `localStorage` when a secret was involved.
Users can refresh the effective projection or download diagnostics.

## 15. Runtime integration and compatibility

The implementation must replace direct runtime reads with a single resolver at composition
boundaries. Compatibility environment names remain supported for the historical providers and for
operator-managed deployments; page-managed credentials and provider/model choices come from the
closed catalog:

- `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`
- `DEEPSEEK_BASE_URL`, `DASHSCOPE_BASE_URL`
- `STATA_AGENT_LIVE`, `STATA_AGENT_PRIVACY`, `STATA_AGENT_EXECUTOR`
- `STATA_MCP_DIR`, `STATA_AGENT_LIBRARY`, `STATA_AGENT_SKILLS`
- `STATA_AGENT_DB`, `STATA_AGENT_WORKSPACES`, `STATA_AGENT_ATTACHMENTS`
- `STATA_AGENT_UI_PORT`, `STATA_AGENT_DEMO`
- compaction, memory-extraction and context-budget variables

Existing command-line and test environments must keep working without a settings file. The absence
of Windows Credential Manager must not break offline tests, mock/demo runs or environment-based keys.

## 16. Implementation sequence

This is one P1 objective delivered in order; completion is assessed only after the whole slice passes.

1. **Settings domain/application foundation**
   - typed catalog, repository, resolver, source tracking, revision and atomic validation;
   - migrate runtime composition away from independent environment reads.
2. **Secure credential lifecycle**
   - SecretStore protocol, Windows Credential Manager adapter and test double;
   - provider construction from immutable effective settings.
3. **Runtime and safety integration**
   - apply timing, active-request fencing, privacy acknowledgement/audit and restart projection;
   - retain existing provider/privacy/tool/ledger invariants.
4. **Health and maintenance adapters**
   - provider fixed canary, Stata doctor, library readiness, backup and diagnostics reuse;
   - safe error catalog and bounded results.
5. **Settings HTTP API and localhost mutation protection**
   - masked read model, atomic patch, secret endpoints, CSRF/origin/host enforcement.
6. **Complete UI**
   - repair navigation; separate current-research projection; implement overview and every settings
     section, source/restart badges, save bar, dialogs, checks and accessibility.
7. **Regression and product acceptance**
   - unit, integration, concurrency, security, browserless interaction and short manual browser UX;
   - full pytest, Ruff, Mypy, eval, coverage, wheel and release doctor.

## 17. Test design

### Unit

- precedence matrix for every source and environment lock behavior;
- schema migration, corrupt/future settings, atomic write and stale revision;
- complete validation matrix for enums, URLs, paths and context-budget relationships;
- secret set/replace/delete/status with zero secret serialization;
- privacy relaxation acknowledgement and active-request fencing;
- apply-mode and restart-required calculation.

### Integration

- saved provider/privacy/Stata/library settings affect the next real application composition;
- environment variables override saved values without shadow-save confusion;
- provider check sends only the fixed canary;
- Stata check uses isolation and returns sanitized diagnostics;
- settings survive restart and concurrent writers yield one winner/one conflict;
- backup action reuses verified release operations;
- privacy changes create safe audit metadata and do not alter an active request;
- no settings endpoint writes the research state directly.

### Security

- secret values never appear in GET, error body, trace, ledger, logs, diagnostics or DOM snapshots;
- cross-origin, missing-CSRF, wrong-host and non-JSON mutation requests fail;
- custom endpoint validation rejects unsafe schemes and credential-bearing URLs;
- path traversal, UNC/ADS edge cases, symlinks/reparse points and oversized config files fail closed;
- unknown keys cannot become runtime settings.

### UI/browserless

- sidebar Settings button navigates correctly;
- dirty/save/discard and revision-conflict behavior;
- environment-managed lock state;
- privacy confirmation and restart banner;
- secret replace/delete without value echo;
- provider/Stata/library check states;
- keyboard and focus behavior;
- research settings remain read-only and separate.

## 18. Definition of done

The P1 settings center is complete only when:

1. a clean user can configure model credentials, live enablement, privacy, Stata and library without
   editing PowerShell or `.env`;
2. the browser cannot retrieve a saved credential;
3. every displayed value identifies its effective source and apply timing;
4. changes are atomic, revisioned, concurrency-safe and survive restart;
5. environment overrides remain compatible and visibly locked;
6. provider/Stata/library readiness can be checked before research begins;
7. research settings remain ledger-governed and cannot be directly edited;
8. in-flight requests keep their starting configuration and are never silently reconfigured;
9. privacy relaxation is explicit, fail-closed and auditable;
10. navigation, keyboard, refresh and error recovery pass product UX tests;
11. existing architecture invariants and all full validation gates pass;
12. documentation describes both UI configuration and environment-based operator overrides.

## 19. Expected implementation surface

Primary new application modules should be limited to settings contracts/service, local repository and
secret-store adapters. Expected integration files include:

- `app/src/stata_agent/config.py`
- `app/src/stata_agent/providers/catalog.py`
- `app/src/stata_agent/providers/openai_compatible.py`
- `app/src/stata_agent/providers/registry.py`
- `app/src/stata_agent/providers/deepseek.py`
- `app/src/stata_agent/ui.py`
- `app/src/stata_agent/webui/index.html`
- `app/src/stata_agent/webui/app.js`
- `app/src/stata_agent/webui/styles.css`
- focused new settings tests and existing provider/privacy/UI/release regression tests

Low-level ledger schema, agent loop, evidence/Writer, RAG retrieval quality, memory schema and SQLite
lease implementation should not change unless a concrete contract defect is demonstrated.
