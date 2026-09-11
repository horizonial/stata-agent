/*
 * Stata 研究助手的本地、无依赖前端（打包目录：webui/）。
 *
 * API 是唯一事实来源。这里仅保存工作区、视图、筛选器和打开的临时
 * 溯源 sheet；所有来自用户或事件账本的文字都通过 textContent/DOM API
 * 写入，避免把不可信内容当成标记解析。
 */

const STATUS_LABELS = {
  idle: "空闲",
  busy: "工作中",
  awaiting_user: "等待你",
  validating: "验证中",
  queued: "排队中",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已停止",
  uncertain: "状态不确定",
  paused: "已暂停",
  complete: "完成",
  completed: "已完成",
  cancelling: "正在停止",
  disconnected: "连接中断",
};

// Transport state is intentionally separate from the server's research phase.
// A cancellation request is not terminal: the composer remains locked until a
// durable server snapshot (or the terminal SSE event) confirms the outcome.
const RUN_STATES = new Set(["idle", "running", "cancelling", "disconnected", "failed", "cancelled", "uncertain", "paused", "completed"]);
const ACTIVE_RUN_STATES = new Set(["running", "cancelling", "disconnected"]);
const SETTINGS_PRIVACY_ACK_CODE = "settings_privacy_acknowledgement_required";
const TERMINAL_RUN_STATES = new Set(["idle", "failed", "cancelled", "uncertain", "paused", "completed"]);
const TERMINAL_SERVER_STATUSES = new Set([
  "idle", "complete", "completed", "succeeded", "failed", "cancelled", "canceled", "uncertain", "paused",
]);
const FAILURE_CODES = new Set([
  SETTINGS_PRIVACY_ACK_CODE,
  "request_failed", "approval_failed", "attachment_failed", "attachment_invalid", "attachment_not_ready",
  "http_400", "http_401", "http_403", "http_404", "http_409", "http_413", "http_415", "http_422", "http_429",
  "http_500", "http_502", "http_503", "http_504", "network_error", "stream_disconnected", "run_cancelled",
  "provider_error", "credentials_missing", "provider_disabled", "provider_unavailable", "provider_unsupported", "context_error", "context_budget",
  "privacy_denied", "invalid_tool_calls", "budget_invalid", "budget_limit", "max_steps", "tool_calls",
  "uncertain", "storage_error", "ledger_error", "ledger_writer_conflict", "ledger_writer_stale", "ledger_unavailable",
]);
const FAILURE_MESSAGES = {
  request_failed: "本轮未完成，请查看 Trace 或导出诊断包。",
  approval_failed: "审批操作未完成，请重试或导出诊断包。",
  attachment_failed: "附件处理未完成，请检查附件状态后重试。",
  attachment_invalid: "附件不符合当前工作区的安全要求。",
  attachment_not_ready: "附件尚未准备好，暂不能用于本轮对话。",
  network_error: "暂时无法连接本机服务，请确认服务仍在运行。",
  stream_disconnected: "流式连接中断，正在等待服务端确认本轮状态。",
  run_cancelled: "本轮已停止，研究状态已保存。",
  provider_error: "模型调用失败，请查看 Trace 或导出诊断包。",
  credentials_missing: "尚未配置模型凭据，请在应用设置中填写 API Key。",
  provider_disabled: "已检测到模型凭据，请先在应用设置中开启远端模型调用。",
  provider_unavailable: "模型服务暂时不可用，请稍后重试。",
  provider_unsupported: "当前模型不支持本轮请求，请调整后重试。",
  context_error: "上下文组装失败，本轮未调用模型。",
  context_budget: "上下文超过本轮预算，请缩短输入或从停点继续。",
  privacy_denied: "当前隐私策略不允许这次模型调用。",
  invalid_tool_calls: "模型返回的工具调用格式无效。",
  budget_invalid: "本轮预算参数无效，已安全停止。",
  budget_limit: "已达到单次请求预算上限，可继续处理或重试本轮。",
  max_steps: "已达到单条消息的内部模型调用上限，可继续处理或重试本轮。",
  tool_calls: "已达到单次请求的工具调用上限，可继续处理或重试本轮。",
  uncertain: "外部副作用状态暂不可确认，请先核对运行记录。",
  storage_error: "研究账本写入失败，请刷新状态后重试。",
  ledger_error: "研究账本操作失败，请刷新状态后重试。",
  ledger_writer_conflict: "账本当前由另一运行实例占用，请关闭重复实例后刷新重试。",
  ledger_writer_stale: "本次账本写入已失效，请刷新状态后再重试。",
  ledger_unavailable: "账本暂时不可用，请稍后重试。",
};

const EVENT_GROUPS = {
  approval: new Set(["approval.requested", "approval.granted", "approval.rejected"]),
  run: new Set(["run.requested", "tool.call", "tool.result", "tool.invoked", "tool.done", "run.succeeded", "run.failed", "run.uncertain"]),
  conclusion: new Set(["spec.proposed", "spec.frozen", "spec.locked", "family.main_result_selected", "evidence.card_signed", "claim.signed"]),
  health: new Set(["budget.limit", "health.probe"]),
};
const TRACE_FILTER_VALUES = new Set(["all", "model", "tool", "run", "evidence", "approval", "failure", "conclusion", "health"]);

const NAV_GROUPS = [
  {
    label: "研究",
    items: [
      ["chat", "对话", "message"],
      ["settings", "应用设置", "settings"],
      ["research", "当前研究", "result"],
      ["variables", "变量", "variables"],
      ["data", "数据", "data"],
      ["models", "模型", "model"],
      ["results", "结果", "result"],
      ["charts", "图表", "chart"],
      ["documents", "文档", "file"],
    ],
  },
  {
    label: "治理",
    items: [
      ["approvals", "审批", "approval"],
      ["health", "预算 / 健康", "health"],
      ["trace", "Trace", "trace"],
    ],
  },
];

const ICON_PATHS = {
  menu: ["M3 5.5h12M3 9h12M3 12.5h12"],
  plus: ["M9 3v12M3 9h12"],
  message: ["M3 4.1h12v8.2H8l-3.4 2.4.5-2.4H3z", "M6 7h6M6 9.5h3.5"],
  settings: ["M9 2.7a2 2 0 0 1 2 2v.2a5.1 5.1 0 0 1 1.1.7l.2-.1a2 2 0 0 1 2.7.7l.1.2a2 2 0 0 1-.7 2.7l-.2.1a5.2 5.2 0 0 1 0 1.5l.2.1a2 2 0 0 1 .7 2.7l-.1.2a2 2 0 0 1-2.7.7l-.2-.1a5.1 5.1 0 0 1-1.1.7v.2a2 2 0 0 1-4 0v-.2a5.1 5.1 0 0 1-1.1-.7l-.2.1a2 2 0 0 1-2.7-.7l-.1-.2a2 2 0 0 1 .7-2.7l.2-.1a5.2 5.2 0 0 1 0-1.5l-.2-.1a2 2 0 0 1-.7-2.7l.1-.2a2 2 0 0 1 2.7-.7l.2.1a5.1 5.1 0 0 1 1.1-.7v-.2a2 2 0 0 1 2-2z", "M9 7a2 2 0 1 0 0 4a2 2 0 0 0 0-4"],
  variables: ["M4 3.5v11M7 3.5v11M11 3.5v11M14 3.5v11", "M2.5 6h3M6 11h3M10 7h3M13 12h2"],
  data: ["M3 3.5h12v11H3z", "M3 7h12M6 7v7.5M10 7v7.5", "M4.5 5.2h.1"],
  model: ["M3.5 5.5 9 2.5l5.5 3v7L9 15.5l-5.5-3z", "M9 8v7.5M3.7 5.7 9 8.5l5.3-2.8"],
  result: ["M9 2.5 15 5v4.1c0 3.6-2.5 6.2-6 7.4-3.5-1.2-6-3.8-6-7.4V5z", "m6.3 9.1 1.8 1.8 3.6-3.6"],
  chart: ["M3 14.5V9.5M7 14.5V5.5M11 14.5V7M15 14.5V3.5", "M2.5 15.5h13"],
  file: ["M5 2.5h5l3 3v10H5z", "M10 2.5v3h3", "M7.5 9h3M7.5 12h3"],
  approval: ["M9 2.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 0 0 0-13z", "m5.7 9 2.1 2.1 4.5-4.5"],
  health: ["M9 2.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 0 0 0-13z", "M9 5.2v3.9l2.4 1.4"],
  trace: ["M4 4.5h10M4 9h7M4 13.5h5", "M13 10.5l2.2 2.2L13 15"],
  search: ["M7.8 3a4.8 4.8 0 1 0 0 9.6A4.8 4.8 0 0 0 7.8 3z", "m11.3 11.3 3.4 3.4"],
  chevronDown: ["m4.5 6.5 4.5 4 4.5-4"],
  chevronRight: ["m6.5 4.5 4.5 4.5-4.5 4.5"],
  arrowLeft: ["M14.5 9H3.5", "m8 4.5-4.5 4.5L8 13.5"],
  send: ["M2.5 8.8 16.5 2.5l-3.1 13-4.1-5.4z", "M9.2 10.1 16.5 2.5"],
  paperclip: ["m6.4 9.5 4.2-4.2a2.1 2.1 0 0 1 3 3l-5.3 5.3a3.6 3.6 0 0 1-5.1-5.1l5.5-5.5a4.8 4.8 0 0 1 6.8 6.8l-5.3 5.3"],
  download: ["M9 2.5v9", "M5.8 8.6 9 11.8l3.2-3.2", "M3 14.5v2h12v-2"],
  close: ["M4.5 4.5 13.5 13.5", "m13.5 4.5-9 9"],
  check: ["m4.2 9.2 3.1 3.1 6.5-7"],
  alert: ["M9 2.7 16 15.3H2z", "M9 6.2v4", "M9 12.8h.1"],
  play: ["M6.5 4.5 13.5 9l-7 4.5z"],
  refresh: ["M14.5 7.2A5.8 5.8 0 1 0 15 10", "M14.5 3.8v3.6h-3.6"],
  link: ["M7.1 11.9 5.8 13.2a2.8 2.8 0 0 1-4-4l2.1-2.1a2.8 2.8 0 0 1 4 0", "M10.9 6.1 12.2 4.8a2.8 2.8 0 0 1 4 4l-2.1 2.1a2.8 2.8 0 0 1-4 0", "m5.8 10.2 6.4-6.4"],
  copy: ["M6 6h8v9H6z", "M3.5 12V3.5h8"],
  more: ["M4.5 9h.1M9 9h.1M13.5 9h.1"],
};

const persistedTraceView = readStorage("stata-agent.trace-view", "activity");
const initialTraceView = persistedTraceView === "technical" ? "technical" : "activity";
const persistedTraceFilter = readStorage("stata-agent.trace-filter", "all");
const initialTraceFilters = initialTraceView === "technical"
  ? new Set(["all", "approval", "run", "conclusion", "health"])
  : new Set(["all", "model", "tool", "run", "evidence", "approval", "failure"]);
const initialTraceFilter = initialTraceFilters.has(persistedTraceFilter) ? persistedTraceFilter : "all";

const state = {
  activeWorkspace: readStorage("stata-agent.active-workspace", "ui"),
  sidebarCollapsed: readStorage("stata-agent.sidebar-collapsed", "false") === "true",
  sidebarOpen: false,
  page: readStorage("stata-agent.page", "chat"),
  workspaces: [],
  snapshot: null,
  events: [],
  traceItems: [],
  traceTotal: 0,
  traceCursor: null,
  traceView: initialTraceView,
  traceActivityItems: [],
  traceActivityTotal: 0,
  traceActivityCursor: null,
  traceActivityLegacyCount: 0,
  traceActivityTruncated: false,
  traceFilter: TRACE_FILTER_VALUES.has(initialTraceFilter) ? initialTraceFilter : "all",
  traceQuery: "",
  traceExpandedSeq: null,
  traceExpandedActivity: null,
  traceExpandedTechnical: null,
  goalMode: false,
  draftText: "",
  streamingText: "",
  streamingRendered: "",
  streamingTools: [],
  activeRequestId: null,
  runState: "idle",
  stopPending: false,
  stopStatus: null,
  lastFingerprint: "",
  sending: false,
  refreshing: false,
  refreshController: null,
  chatController: null,
  traceController: null,
  workspaceGeneration: 0,
  pollTimer: null,
  refreshFailed: false,
  selectedEvidence: null,
  decisionContext: null,
  toastTimer: null,
  lastFailure: null,
  lastMessageText: "",
  failureGeneration: 0,
  decisionSubmitting: false,
  overlayFocus: null,
  attachments: [],
  selectedAttachmentIds: [],
  attachmentRole: "style_only",
  attachmentController: null,
  settings: null,
  settingsDraft: {},
  settingsDirty: false,
  settingsLoading: false,
  settingsSaving: false,
  settingsError: null,
  settingsValidationError: null,
  settingsToken: null,
  settingsChecks: {},
  settingsCredentialProvider: null,
};

function readStorage(key, fallback) {
  try {
    const value = window.localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch {
    return fallback;
  }
}

function writeStorage(key, value) {
  try { window.localStorage.setItem(key, value); } catch { /* 本地预览可能禁用 storage */ }
}

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function applyTheme(theme) {
  const normalized = ["system", "light", "dark"].includes(String(theme)) ? String(theme) : "system";
  document.documentElement.dataset.theme = normalized;
}

function applyImmediateSettings() {
  const role = state.settingsDraft?.["attachments.default_role"];
  if (["style_only", "citable_evidence"].includes(String(role))) state.attachmentRole = String(role);
  const mode = state.settingsDraft?.["agent.default_mode"];
  if (["interactive", "goal"].includes(String(mode))) state.goalMode = String(mode) === "goal";
  applyTheme(state.settingsDraft?.["ui.theme"]);
}

function el(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined && options.text !== null) element.textContent = String(options.text);
  if (options.type) element.type = options.type;
  if (options.accept) element.accept = String(options.accept);
  if (options.value !== undefined) element.value = options.value;
  if (options.checked !== undefined) element.checked = Boolean(options.checked);
  if (options.htmlFor) element.htmlFor = String(options.htmlFor);
  if (options.autocomplete) element.autocomplete = String(options.autocomplete);
  if (options.spellcheck !== undefined) element.spellcheck = Boolean(options.spellcheck);
  if (options.disabled !== undefined) element.disabled = Boolean(options.disabled);
  if (options.hidden !== undefined) element.hidden = Boolean(options.hidden);
  if (options.title !== undefined) element.title = String(options.title);
  if (options.role) element.setAttribute("role", options.role);
  if (options.ariaLabel) element.setAttribute("aria-label", String(options.ariaLabel));
  if (options.ariaExpanded !== undefined) element.setAttribute("aria-expanded", String(options.ariaExpanded));
  if (options.ariaControls) element.setAttribute("aria-controls", String(options.ariaControls));
  if (options.ariaDescribedby) element.setAttribute("aria-describedby", String(options.ariaDescribedby));
  if (options.ariaInvalid !== undefined) element.setAttribute("aria-invalid", String(options.ariaInvalid));
  if (options.ariaRequired !== undefined) element.setAttribute("aria-required", String(options.ariaRequired));
  if (options.ariaLive) element.setAttribute("aria-live", String(options.ariaLive));
  if (options.ariaCurrent !== undefined) element.setAttribute("aria-current", String(options.ariaCurrent));
  if (options.tabIndex !== undefined) element.tabIndex = Number(options.tabIndex);
  if (options.colSpan !== undefined) element.colSpan = Number(options.colSpan);
  if (options.dataset) Object.entries(options.dataset).forEach(([key, value]) => { element.dataset[key] = String(value); });
  children.forEach((child) => { if (child !== null && child !== undefined) element.append(child); });
  return element;
}

function clear(element) {
  while (element && element.firstChild) element.removeChild(element.firstChild);
}

function icon(name, className = "") {
  const holder = el("span", { className: `icon ${className}`.trim() });
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 18 18");
  svg.setAttribute("aria-hidden", "true");
  (ICON_PATHS[name] || []).forEach((definition) => {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", definition);
    svg.append(path);
  });
  holder.append(svg);
  return holder;
}

function valueOrFallback(value, fallback = "尚未确定") {
  if (value === null || value === undefined || value === "") return fallback;
  if (Array.isArray(value)) return value.length ? value.map((item) => valueOrFallback(item)).join("、") : fallback;
  if (typeof value === "object") {
    try { return JSON.stringify(value); } catch { return fallback; }
  }
  return String(value);
}

function formatTime(timestamp) {
  if (!timestamp) return "--:--";
  const number = Number(timestamp);
  if (!Number.isFinite(number)) return "--:--";
  const date = new Date(number < 100000000000 ? number * 1000 : number);
  if (Number.isNaN(date.getTime())) return "--:--";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}

function formatDate(timestamp) {
  if (!timestamp) return "尚未记录";
  const number = Number(timestamp);
  const date = new Date(number < 100000000000 ? number * 1000 : number);
  if (Number.isNaN(date.getTime())) return "尚未记录";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

function statusLabel(status) { return STATUS_LABELS[status] || valueOrFallback(status, "空闲"); }

function statusClass(status) {
  if (["failed", "uncertain", "paused", "disconnected"].includes(status)) return "is-error";
  if (["busy", "validating", "awaiting_user", "running", "cancelling"].includes(status)) return "is-busy";
  if (["complete", "completed", "succeeded", "idle"].includes(status)) return "is-ok";
  return "";
}

function isCurrentWorkspaceGeneration(generation, workspace = state.activeWorkspace) {
  return generation === state.workspaceGeneration && workspace === state.activeWorkspace;
}

function isRunActive() { return ACTIVE_RUN_STATES.has(state.runState); }

function canSend() { return !isRunActive() && !state.stopPending && state.runState !== "cancelling"; }

function normalizeFailure(source, fallbackCode = "request_failed", status = 0) {
  const candidate = source && typeof source === "object"
    ? (source.failure && typeof source.failure === "object" ? source.failure : source.error && typeof source.error === "object" ? source.error : source)
    : {};
  let code = typeof candidate.code === "string" ? candidate.code : "";
  if (!FAILURE_CODES.has(code)) {
    code = /^http_\d+$/.test(code) ? code : fallbackCode;
    if (status >= 400 && status < 600 && FAILURE_CODES.has(`http_${status}`)) code = `http_${status}`;
  }
  const fallbackMessage = FAILURE_MESSAGES[code] || (code.startsWith("http_") && Number(code.slice(5)) < 500
    ? "请求参数或状态不符合当前操作要求。"
    : "请求未完成，请查看 Trace 或导出诊断包。");
  // Only accept the server's message from a known safe envelope. Native
  // Error.message, proxy text and exception details are never projected.
  const message = candidate.safe === true && typeof candidate.message === "string"
    ? candidate.message.slice(0, 240)
    : fallbackMessage;
  const supportAction = candidate.support_action === "retry" || candidate.support_action === "download_diagnostics"
    ? candidate.support_action
    : "download_diagnostics";
  return {
    code,
    message,
    retryable: candidate.retryable === true || [
      "network_error", "stream_disconnected", "request_failed", "approval_failed", "attachment_failed",
      "credentials_missing", "provider_disabled", "provider_unavailable", "context_budget", "budget_invalid", "budget_limit", "max_steps", "tool_calls",
      "ledger_writer_conflict", "ledger_writer_stale", "ledger_unavailable",
    ].includes(code),
    support_action: supportAction,
    request_id: typeof candidate.request_id === "string" ? candidate.request_id.slice(0, 120) : undefined,
  };
}

function safeFailureMessage(error, fallbackCode = "request_failed") {
  return normalizeFailure(error, fallbackCode).message;
}

function setFailure(source, fallbackCode = "request_failed", generation = state.workspaceGeneration) {
  if (generation !== state.workspaceGeneration) return null;
  state.lastFailure = normalizeFailure(source, fallbackCode);
  state.failureGeneration = generation;
  return state.lastFailure;
}

function setRunState(next, generation = state.workspaceGeneration) {
  if (generation !== state.workspaceGeneration || !RUN_STATES.has(next)) return false;
  state.runState = next;
  // Keep the legacy flag as a derived compatibility field. It must stay true
  // through cancelling/disconnected so a stop race cannot reopen the composer.
  state.sending = ACTIVE_RUN_STATES.has(next);
  if (TERMINAL_RUN_STATES.has(next)) state.stopPending = false;
  return true;
}

function serverStatusIsTerminal(status) {
  return typeof status === "string" && TERMINAL_SERVER_STATUSES.has(status);
}

function hasDurableCheckpoint() {
  return state.snapshot?.checkpoint?.available === true;
}

function resumeLabel() {
  return hasDurableCheckpoint() ? "从停点续跑" : "继续处理";
}

function terminalStateFor(status) {
  if (status === "cancelled" || status === "canceled") return "cancelled";
  if (status === "uncertain") return "uncertain";
  if (status === "failed") return "failed";
  if (status === "paused") return "paused";
  return "completed";
}

function phaseLabel(phase) {
  const labels = {
    IDEA: "研究问题", LITERATURE: "可行性", DESIGN: "模型设定", DATA: "数据准备",
    ESTIMATION: "估计", ROBUSTNESS: "稳健性", WRITING: "写作", VALIDATION: "验证", DONE: "完成",
  };
  return labels[phase] || valueOrFallback(phase);
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "尚未确定";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(3) : String(value);
}

function showToast(message, fallbackCode = "request_failed") {
  const toast = $("#toast");
  if (!toast) return;
  const text = typeof message === "string" ? message : safeFailureMessage(message, fallbackCode);
  toast.textContent = String(text);
  toast.hidden = false;
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => { toast.hidden = true; }, 4200);
}

async function jsonResponse(response) {
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const failure = normalizeFailure(body, `http_${response.status}`, response.status);
    const error = new Error(failure.message);
    error.failure = failure;
    error.code = failure.code;
    error.status = response.status;
    throw error;
  }
  return body;
}

function responseErrorMessage(payload, fallback = "请求未完成。") {
  const failure = normalizeFailure(payload, "request_failed");
  return failure.message || fallback;
}

async function getJSON(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { Accept: "application/json", ...(options.headers || {}) } });
  return jsonResponse(response);
}

async function postJSON(url, body) {
  return getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

async function loadSettings({ force = false } = {}) {
  if (state.settingsLoading || (state.settings && !force && !state.settingsDirty)) return;
  state.settingsLoading = true;
  state.settingsError = null;
  state.settingsValidationError = null;
  try {
    const body = await getJSON("/api/settings");
    if (body?.csrf_token) state.settingsToken = String(body.csrf_token);
    state.settings = body;
    const values = body?.values || body?.settings || {};
    state.settingsDraft = {};
    Object.entries(values).forEach(([key, item]) => {
      if (item && item.sensitive) return;
      state.settingsDraft[key] = item?.value;
    });
    state.settingsCredentialProvider = credentialProvider();
    applyImmediateSettings();
    state.settingsDirty = false;
    render();
  } catch (error) {
    state.settingsError = error;
    render();
  } finally {
    state.settingsLoading = false;
  }
}

function settingsProjection(key) {
  return state.settings?.values?.[key] || state.settings?.settings?.[key] || null;
}

function settingsDraftValue(key) {
  const value = state.settingsDraft?.[key];
  return value === null || value === undefined ? "" : value;
}

function providerCatalog() {
  return Array.isArray(state.settings?.provider_catalog) ? state.settings.provider_catalog : [];
}

function providerChoices() {
  return ["auto", ...providerCatalog().map((item) => String(item.id || "")).filter(Boolean)];
}

function modelChoices() {
  const selectedProvider = String(state.settingsDraft?.["provider.primary"] || "auto");
  const models = providerCatalog().flatMap((provider) => (Array.isArray(provider.models) ? provider.models : []).map((model) => ({
    id: String(model.id || ""),
    label: `${provider.label || provider.id} · ${model.id || ""}`,
    provider: String(provider.id || ""),
  }))).filter((item) => item.id);
  const filtered = selectedProvider === "auto" ? models : models.filter((item) => item.provider === selectedProvider);
  return [{ id: "auto", label: "自动选择", provider: "auto" }, ...filtered];
}

function providerLabel(providerId) {
  const found = providerCatalog().find((item) => String(item.id) === String(providerId));
  return found?.label || String(providerId || "");
}

function providerForModel(modelId) {
  for (const provider of providerCatalog()) {
    const models = Array.isArray(provider.models) ? provider.models : [];
    if (models.some((model) => String(model.id) === String(modelId))) return String(provider.id || "");
  }
  return "";
}

function credentialProvider() {
  const current = String(state.settingsCredentialProvider || "");
  if (providerCatalog().some((item) => String(item.id) === current)) return current;
  const selected = String(state.settingsDraft?.["provider.primary"] || "auto");
  if (selected !== "auto" && providerCatalog().some((item) => String(item.id) === selected)) return selected;
  const modelProvider = providerForModel(state.settingsDraft?.["provider.model"]);
  if (modelProvider) return modelProvider;
  return String(providerCatalog()[0]?.id || "");
}

function settingChoices(key) {
  if (key === "provider.primary") return providerChoices();
  if (key === "provider.model") return modelChoices().map((item) => item.id);
  return SETTINGS_ENUMS[key] || null;
}

function pendingSettingsChanges() {
  const changes = {};
  Object.entries(state.settingsDraft || {}).forEach(([key, value]) => {
    const projection = settingsProjection(key);
    if (projection?.editable && !projection.sensitive && value !== projection.value) changes[key] = value;
  });
  return changes;
}

function setSettingsDraft(key, value) {
  state.settingsDraft[key] = value;
  if (key === "provider.primary") {
    const selected = String(value || "auto");
    if (selected !== "auto" && providerCatalog().some((item) => String(item.id) === selected)) {
      // Keep the credential editor aligned with the provider the user just
      // selected; it remains independently switchable for fallback setup.
      state.settingsCredentialProvider = selected;
    }
  }
  state.settingsDirty = true;
  state.settingsValidationError = validateSettingsDraft(pendingSettingsChanges());
  if (key === "ui.theme") applyTheme(value);
  if (key === "attachments.default_role") state.attachmentRole = String(value);
  if (key === "agent.default_mode") state.goalMode = String(value) === "goal";
  render();
}

const SETTINGS_RANGES = {
  "agent.interactive_max_steps": [2, 128],
  "agent.goal_max_steps": [2, 256],
  "agent.max_tool_calls": [1, 512],
  "context.max_input_tokens": [1, 1000000],
  "context.reserve_output_tokens": [0, 1000000],
  "context.recent_tail_tokens": [0, 1000000],
  "context.memory_tokens": [0, 1000000],
  "ui.port": [1024, 65535],
};

function validateSettingsDraft(changes) {
  for (const [key, value] of Object.entries(changes)) {
    const choices = settingChoices(key);
    if (choices && !choices.includes(String(value))) {
      return { key, message: "请选择列表中的有效值。" };
    }
    if (SETTINGS_RANGES[key]) {
      const [minimum, maximum] = SETTINGS_RANGES[key];
      if (!Number.isInteger(value) || value < minimum || value > maximum) {
        return { key, message: "数值超出允许范围。" };
      }
    }
    const endpoint = String(value ?? "").trim();
    const optionalEndpoint = key === "provider.base_url" && endpoint === "";
    if (key.endsWith(".base_url") && !optionalEndpoint && (!/^https:\/\/[^\s/]+(?:\/[^\s]*)?$/i.test(endpoint) || /[@#]/.test(endpoint))) {
      return { key, message: "端点必须是无凭据的 HTTPS 地址。" };
    }
    if (key.endsWith(".root") || key === "stata.mcp_dir") {
      const text = String(value || "");
      if (text.includes("\0") || /(^|[\\/])\.\.([\\/]|$)/.test(text) || text.startsWith("\\\\") || text.startsWith("//")) {
        return { key, message: "目录路径不能包含越界、UNC 或重解析路径。" };
      }
    }
  }
  const maxInput = changes["context.max_input_tokens"] ?? settingsProjection("context.max_input_tokens")?.value;
  const reserve = changes["context.reserve_output_tokens"] ?? settingsProjection("context.reserve_output_tokens")?.value;
  if (Number.isInteger(maxInput) && Number.isInteger(reserve) && reserve > maxInput) {
    return { key: "context.reserve_output_tokens", message: "输出预留不能超过输入预算。" };
  }
  const provider = String(changes["provider.primary"] ?? settingsProjection("provider.primary")?.value ?? "auto");
  const model = String(changes["provider.model"] ?? settingsProjection("provider.model")?.value ?? "auto");
  if (model !== "auto") {
    const modelProvider = providerForModel(model);
    if (!modelProvider || (provider !== "auto" && provider !== modelProvider)) {
      return { key: "provider.model", message: "该模型不属于当前 provider，请重新选择。" };
    }
  }
  return null;
}

async function saveSettings() {
  if (!state.settings || state.settingsSaving || !state.settingsDirty) return;
  const changes = pendingSettingsChanges();
  if (!Object.keys(changes).length) { state.settingsDirty = false; render(); return; }
  state.settingsValidationError = validateSettingsDraft(changes);
  if (state.settingsValidationError) { render(); return; }
  const currentPrivacy = settingsProjection("privacy.mode")?.value || "local_strict";
  const nextPrivacy = changes["privacy.mode"];
  const relaxes = (currentPrivacy === "local_strict" && nextPrivacy && nextPrivacy !== "local_strict")
    || (currentPrivacy === "mixed_sanitized" && nextPrivacy === "approved_remote");
  if (relaxes && !window.confirm("放宽隐私模式会将未来请求中的部分研究内容发送到远端 provider。继续吗？")) return;
  state.settingsSaving = true;
  render();
  try {
    const body = await getJSON("/api/settings", {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-Settings-CSRF": state.settingsToken || "",
        "Origin": window.location.origin,
      },
      body: JSON.stringify({ expected_revision: state.settings.revision, changes, privacy_acknowledgement: Boolean(relaxes) }),
    });
    state.settings = { ...state.settings, ...body.effective, revision: body.revision };
    const values = body.effective?.values || {};
    state.settings.values = values;
    state.settings.settings = values;
    state.settingsDraft = {};
    Object.entries(values).forEach(([key, item]) => { if (!item?.sensitive) state.settingsDraft[key] = item?.value; });
    applyImmediateSettings();
    state.settingsDirty = false;
    state.settingsValidationError = null;
    showToast("设置已保存。");
  } catch (error) {
    state.settingsError = error;
    if (error?.code === "settings_revision_conflict") showToast("设置已被其他实例更新，请刷新后合并。", "request_failed");
    else showToast("设置保存失败，请检查表单后重试。", "request_failed");
  } finally {
    state.settingsSaving = false;
    render();
  }
}

function discardSettings() {
  if (!state.settings) return;
  const values = state.settings.values || state.settings.settings || {};
  state.settingsDraft = {};
  Object.entries(values).forEach(([key, item]) => { if (!item?.sensitive) state.settingsDraft[key] = item?.value; });
  state.settingsDirty = false;
  state.settingsError = null;
  state.settingsValidationError = null;
  applyImmediateSettings();
  render();
}

function resetContextSettings() {
  const recommended = {
    "context.max_input_tokens": 16000,
    "context.reserve_output_tokens": 4000,
    "context.recent_tail_tokens": 4000,
    "context.memory_tokens": 1500,
  };
  Object.entries(recommended).forEach(([key, value]) => {
    const projection = settingsProjection(key);
    if (projection?.editable && !projection.sensitive) state.settingsDraft[key] = value;
  });
  state.settingsDirty = true;
  state.settingsValidationError = validateSettingsDraft(pendingSettingsChanges());
  render();
}

async function settingsSecretAction(provider, input, remove = false) {
  if (!state.settingsToken) await loadSettings({ force: true });
  try {
    const options = {
      method: remove ? "DELETE" : "PUT",
      headers: { "Content-Type": "application/json", "X-Settings-CSRF": state.settingsToken || "", "Origin": window.location.origin },
    };
    if (!remove) options.body = JSON.stringify({ value: input?.value || "" });
    await getJSON(`/api/settings/secrets/${encodeURIComponent(provider)}`, options);
    if (input) input.value = "";
    await loadSettings({ force: true });
    showToast(remove ? "凭据已删除。" : "凭据已保存。", "request_failed");
  } catch { if (input) input.value = ""; showToast("凭据操作失败，密钥未回显。", "request_failed"); }
}

async function runSettingsCheck(kind) {
  state.settingsChecks[kind] = { status: "checking" };
  render();
  try {
    const body = await getJSON(`/api/settings/check/${kind}`, {
      method: "POST",
      headers: { "X-Settings-CSRF": state.settingsToken || "", "Origin": window.location.origin },
    });
    state.settingsChecks[kind] = body.health || { status: "error", code: "unknown" };
  } catch (error) { state.settingsChecks[kind] = { status: "error", code: error?.code || "check_failed" }; }
  render();
}

async function downloadSettingsBackup() {
  try {
    const response = await fetch("/api/settings/backup", { method: "POST", headers: { "X-Settings-CSRF": state.settingsToken || "", "Origin": window.location.origin } });
    if (!response.ok) throw new Error("backup failed");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = el("a");
    link.href = url;
    link.download = "stata-agent-backup.zip";
    document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
  } catch { showToast("备份未完成，请稍后重试。", "request_failed"); }
}

async function verifySettingsBackup(file) {
  if (!file) return;
  if (file.size > 64 * 1024 * 1024) { showToast("备份超过允许大小，未上传。", "request_failed"); return; }
  if (!state.settingsToken) await loadSettings({ force: true });
  const form = new FormData();
  form.append("bundle", file, file.name || "backup.zip");
  try {
    const body = await getJSON("/api/settings/backup/verify", {
      method: "POST",
      headers: { "X-Settings-CSRF": state.settingsToken || "", "Origin": window.location.origin },
      body: form,
    });
    showToast(body?.verified ? "备份校验通过，未执行恢复。" : "备份校验未通过。", body?.verified ? "request_failed" : "request_failed");
  } catch { showToast("备份校验失败，未替换任何数据。", "request_failed"); }
}

function workspaceQuery(extra = "") {
  const joiner = extra ? "&" : "";
  return `ws=${encodeURIComponent(state.activeWorkspace)}${joiner}${extra}`;
}

function workspaceURL(path, extra = "") { return `${path}?${workspaceQuery(extra)}`; }

function cancelWorkspaceRequests() {
  state.workspaceGeneration += 1;
  state.chatController?.abort();
  state.traceController?.abort();
  state.refreshController?.abort();
  state.attachmentController?.abort();
  state.chatController = null;
  state.traceController = null;
  state.refreshController = null;
  state.attachmentController = null;
  state.runState = "idle";
  state.sending = false;
  state.streamingText = "";
  state.streamingTools = [];
  state.activeRequestId = null;
  state.stopPending = false;
  state.lastFailure = null;
  state.lastMessageText = "";
  state.failureGeneration = state.workspaceGeneration;
  state.decisionSubmitting = false;
  state.attachments = [];
  state.selectedAttachmentIds = [];
  state.stopStatus = null;
  state.events = [];
  state.traceItems = [];
  state.traceCursor = null;
  state.traceActivityItems = [];
  state.traceActivityCursor = null;
  state.traceActivityTotal = 0;
  state.traceActivityLegacyCount = 0;
  state.traceActivityTruncated = false;
  state.traceExpandedActivity = null;
  state.traceExpandedTechnical = null;
  state.snapshot = null;
  state.lastFingerprint = "";
}

function switchWorkspace(id) {
  if (!id || id === state.activeWorkspace) return;
  cancelWorkspaceRequests();
  state.activeWorkspace = id;
  writeStorage("stata-agent.active-workspace", id);
  state.page = "chat";
  render();
  refresh({ silent: true });
}

function activeWorkspaceRecord() {
  return state.workspaces.find((item) => item.id === state.activeWorkspace) || null;
}

function resourceCount(key, snapshot = state.snapshot || {}) {
  const rs = snapshot.research_state || {};
  const claims = Array.isArray(snapshot.claim_records) ? snapshot.claim_records.length : 0;
  const cards = Array.isArray(snapshot.card_records) ? snapshot.card_records.length : 0;
  const runs = Array.isArray(snapshot.run_records) ? snapshot.run_records.length : 0;
  if (key === "variables") return (Array.isArray(rs.controls) ? rs.controls.length : 0) + [rs.dependent_variable, rs.core_explanatory_variable].filter(Boolean).length;
  if (key === "data") return rs.sample_sig ? 1 : 0;
  if (key === "models") return runs;
  if (key === "results") return claims || cards;
  if (key === "documents") return snapshot.draft_ready ? 1 : 0;
  if (key === "approvals") return Array.isArray(snapshot.approvals) ? snapshot.approvals.filter((item) => item.status === "pending").length : 0;
  if (key === "trace") return Number(snapshot.events || 0);
  return 0;
}

function updateSidebarMode() {
  const shell = $("#app-shell");
  const button = $("#menu-button");
  if (!shell || !button) return;
  shell.classList.toggle("is-sidebar-collapsed", state.sidebarCollapsed);
  shell.classList.toggle("is-sidebar-open", state.sidebarOpen);
  button.setAttribute("aria-expanded", String(!state.sidebarCollapsed));
  button.setAttribute("aria-label", state.sidebarCollapsed ? "展开工作区导航" : "收起工作区导航");
}

function setSidebarCollapsed(collapsed) {
  state.sidebarCollapsed = Boolean(collapsed);
  writeStorage("stata-agent.sidebar-collapsed", String(state.sidebarCollapsed));
  updateSidebarMode();
}

function setSidebarOpen(open) {
  state.sidebarOpen = Boolean(open);
  updateSidebarMode();
}

function mergeEvents(rows) {
  const bySeq = new Map(state.events.map((row) => [String(row.seq), row]));
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    if (row && row.seq !== null && row.seq !== undefined) bySeq.set(String(row.seq), row);
  });
  state.events = Array.from(bySeq.values()).sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0));
}

async function refresh({ silent = false } = {}) {
  if (document.hidden || state.refreshing) return;
  state.refreshing = true;
  if (state.refreshController) state.refreshController.abort();
  state.refreshController = new AbortController();
  const signal = state.refreshController.signal;
  const workspace = state.activeWorkspace;
  try {
    const query = `ws=${encodeURIComponent(workspace)}`;
    const [workspaceBody, snapshot, eventBody, attachmentBody] = await Promise.all([
      getJSON("/api/workspaces", { signal }),
      getJSON(`/api/state?${query}`, { signal }),
      getJSON(`/api/events?${query}&limit=100`, { signal }),
      getJSON(`/api/attachments?${query}&limit=100`, { signal }),
    ]);
    if (!isCurrentWorkspaceGeneration(state.workspaceGeneration, workspace)) return;
    state.workspaces = Array.isArray(workspaceBody?.items) ? workspaceBody.items : [];
    if (!state.workspaces.some((item) => item.id === state.activeWorkspace)) {
      const fallback = state.workspaces.find((item) => item.id === "ui") || state.workspaces[0];
      if (fallback) {
        state.activeWorkspace = fallback.id;
        writeStorage("stata-agent.active-workspace", state.activeWorkspace);
      }
    }
    snapshot.approvals = Array.isArray(snapshot.approvals) ? snapshot.approvals : [];
    snapshot.pending_approvals = snapshot.approvals.filter((item) => item.status === "pending");
    state.snapshot = snapshot;
    state.attachments = Array.isArray(attachmentBody?.items) ? attachmentBody.items : [];
    const readyIds = new Set(state.attachments.filter((item) => item.status === "ready").map((item) => item.attachment_id));
    state.selectedAttachmentIds = state.selectedAttachmentIds.filter((id) => readyIds.has(id));
    mergeEvents(Array.isArray(eventBody) ? eventBody : eventBody?.items || []);
    state.refreshFailed = false;
    const activeRequest = snapshot?.active_request && typeof snapshot.active_request === "object"
      ? snapshot.active_request : null;
    const activeRequestId = snapshot?.active_request_id || activeRequest?.request_id;
    const activeStatus = activeRequest?.status;
    if (activeRequestId && ["running", "cancelling", "disconnected"].includes(activeStatus)) {
      // A page reload starts with an idle in-memory state.  A durable/API
      // snapshot of an in-flight request must re-lock the composer before the
      // user can submit a duplicate request.
      state.activeRequestId = String(activeRequestId);
      state.stopPending = false;
      setRunState(activeStatus === "cancelling" ? "cancelling" : "running");
    } else {
      const terminalStatus = snapshot?.terminal_status || activeRequest?.status;
      if (terminalStatus && serverStatusIsTerminal(terminalStatus) && terminalStatus !== "idle") {
        const terminal = terminalStateFor(terminalStatus);
        if (!isRunActive() || state.runState === "cancelling" || state.runState === "disconnected") {
          if (terminalStatus === "cancelled" || terminalStatus === "canceled") {
            state.lastFailure = normalizeFailure({ code: "run_cancelled" }, "run_cancelled");
          } else if (terminalStatus === "uncertain") {
            setFailure(snapshot?.terminal_failure || { code: "uncertain" }, "uncertain");
          } else if (terminalStatus === "failed") {
            setFailure(snapshot?.terminal_failure || { code: "request_failed" }, "request_failed");
          } else if (terminalStatus === "paused") {
            setFailure(snapshot?.terminal_failure || { code: "context_budget" }, "context_budget");
          }
          setRunState(terminal);
          state.stopStatus = terminalStatus;
          if (!isRunActive()) state.activeRequestId = null;
        }
      }
    }
    // A snapshot may close a cancellation even if the stream was already
    // aborted. Never infer terminal from a transient busy/idle response while
    // an active request is still being confirmed.
    if (state.runState === "cancelling" && !activeRequestId && serverStatusIsTerminal(snapshot.terminal_status || snapshot.run_status)) {
      const serverStatus = snapshot.terminal_status || snapshot.run_status;
      const terminal = terminalStateFor(serverStatus);
      if (serverStatus === "cancelled" || serverStatus === "canceled") {
        state.lastFailure = normalizeFailure({ code: "run_cancelled" }, "run_cancelled");
      }
      setRunState(terminal);
      state.activeRequestId = null;
    }
    // 数据没变就跳过 render，避免每次轮询 clear+重建导致的闪烁/输入丢失。
    // 关键：sending（流式进行中）期间绝不 render——流式渲染由 sendMessage 的 flush
    // 局部更新负责，这里只更新 state.snapshot 数据；否则全量重建会把流式文本"快进"成整段。
    const fingerprint = _fingerprint();
    if (!isRunActive() && fingerprint !== state.lastFingerprint) {
      render();
      state.lastFingerprint = fingerprint;
    }
    if (state.page === "trace") {
      if (state.traceView === "technical") await loadTrace({ reset: true, renderAfter: true });
      else await loadTraceActivity({ reset: true, renderAfter: true });
    }
  } catch (error) {
    if (error?.name !== "AbortError") {
      state.refreshFailed = true;
      setFailure(error, "network_error");
      if (!silent || !state.snapshot) render();
    }
  } finally {
    state.refreshing = false;
    scheduleRefresh();
  }
}

function _fingerprint() {
  const s = state.snapshot || {};
  return JSON.stringify({
    ws: state.activeWorkspace,
    page: state.page,
    events: state.events.length,
    msgs: (s.messages || []).length,
    runs: Object.keys(s.runs || {}).length,
    pending: (s.pending_approvals || []).length,
    status: s.run_status,
    runState: state.runState,
    goal: state.goalMode,
    attachments: state.attachments.map((item) => `${item.attachment_id}:${item.status}`).join("|"),
    selectedAttachments: state.selectedAttachmentIds.join("|"),
    workspaces: (state.workspaces || []).map((w) => `${w.id}:${w.events}:${w.run_status}`).join("|"),
  });
}

function scheduleRefresh() {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  if (document.hidden) return;
  const active = isRunActive() || ["busy", "validating"].includes(state.snapshot?.run_status);
  state.pollTimer = window.setTimeout(() => refresh({ silent: true }), active ? 1800 : 8000);
}

function render() {
  updateSidebarMode();
  renderTopbar();
  renderSidebar();
  renderView();
  const notice = $("#refresh-notice");
  if (notice) notice.hidden = !state.refreshFailed;
}

function renderTopbar() {
  const snapshot = state.snapshot || {};
  const record = activeWorkspaceRecord();
  const title = snapshot.workspace_name || snapshot.idea_title || record?.name || state.activeWorkspace;
  const titleElement = $("#workspace-context-title");
  if (titleElement) { titleElement.textContent = title; titleElement.title = title; }
  const status = isRunActive() || ["failed", "cancelled", "uncertain", "paused", "completed"].includes(state.runState)
    ? state.runState
    : snapshot.run_status || record?.run_status || "idle";
  const statusLabelElement = $("#run-status-label");
  if (statusLabelElement) statusLabelElement.textContent = statusLabel(status);
  const statusDot = $(".local-status .status-dot");
  if (statusDot) statusDot.className = `status-dot ${statusClass(status)}`;
  const traceButton = $("#trace-button");
  if (traceButton) traceButton.classList.toggle("is-current", state.page === "trace");
  const draftButton = $("#top-draft-button");
  if (draftButton) {
    draftButton.disabled = !snapshot.draft_ready;
    draftButton.title = snapshot.draft_ready ? "生成已确认结论的 Word 初稿" : "尚无已确认结论";
  }
}

function renderSidebar() {
  const list = $("#workspace-list");
  if (!list) return;
  clear(list);
  const rows = state.workspaces.length ? state.workspaces : [{ id: "ui", name: "未命名研究", events: 0, run_status: "idle" }];
  rows.forEach((workspace) => {
    const active = workspace.id === state.activeWorkspace;
    const entry = el("div", { className: "workspace-entry", role: "listitem" });
    const row = el("button", {
      className: `workspace-row${active ? " is-active" : ""}`,
      type: "button",
      ariaExpanded: active,
      dataset: { workspaceId: workspace.id },
      title: active ? "当前工作区" : `切换到 ${workspace.name || workspace.id}`,
    });
    row.append(
      el("span", { className: "workspace-dot", ariaLabel: statusLabel(workspace.run_status || "idle") }),
      el("span", { className: "workspace-name", text: valueOrFallback(workspace.name, workspace.id) }),
      el("span", { className: "workspace-meta", text: workspace.events ? String(workspace.events) : "" }),
      icon("chevronDown", "workspace-chevron"),
    );
    entry.append(row);
    if (active) {
      const resourceGroup = el("div", { className: "workspace-resources", role: "group", ariaLabel: `${workspace.name || workspace.id} 资源` });
      NAV_GROUPS.forEach((group) => {
        group.items.forEach(([key, label, iconName]) => {
          const current = state.page === key;
          const count = resourceCount(key);
          const nav = el("button", {
            className: `resource-nav-button${current ? " is-current" : ""}`,
            type: "button",
            ariaCurrent: current ? "page" : undefined,
            dataset: { navPage: key },
            title: label,
          });
          nav.append(icon(iconName), el("span", { className: "resource-name", text: label }));
          if (count > 0) nav.append(el("span", { className: "resource-count", text: String(count) }));
          if (key === "health" && state.snapshot?.health?.ok !== false) nav.append(el("span", { className: "resource-state", ariaLabel: "正常" }));
          resourceGroup.append(nav);
        });
        if (group === NAV_GROUPS[0]) resourceGroup.append(el("div", { className: "resource-separator" }));
      });
      entry.append(resourceGroup);
    }
    list.append(entry);
  });
}

function renderView() {
  const root = $("#view-root");
  if (!root) return;
  clear(root);
  if (state.page === "chat") root.append(renderChatView());
  else if (state.page === "trace") root.append(renderTraceView());
  else root.append(renderResourceView(state.page));
}

function createViewHeader(title, subtitle, actions = [], className = "") {
  const header = el("header", { className: `view-header ${className}`.trim() });
  const group = el("div", { className: "view-title-group" });
  group.append(el("h1", { text: title }));
  if (subtitle) group.append(el("span", { className: "view-subtitle", text: subtitle }));
  header.append(group);
  if (actions.length) header.append(el("div", { className: "view-actions" }, actions));
  return header;
}

function renderChatView() {
  const view = el("section", { className: "chat-view", ariaLabel: "研究对话" });
  view.append(createViewHeader("对话", `共 ${Number(state.snapshot?.messages?.length || 0)} 条`));
  const scroll = el("div", { className: "chat-scroll" });
  const column = el("div", { className: "chat-column" });
  const messages = Array.isArray(state.snapshot?.messages) ? state.snapshot.messages : [];
  if (!messages.length && !isRunActive()) column.append(renderEmptyChat());
  messages.forEach((message, index) => column.append(renderMessage(message, index === messages.length - 1)));
  if (isRunActive()) column.append(renderBusyMessage());
  scroll.append(column);
  view.append(scroll, renderComposer());
  window.setTimeout(() => { if (isRunActive() || messages.length) scroll.scrollTop = scroll.scrollHeight; }, 0);
  return view;
}

function renderEmptyChat() {
  const empty = el("section", { className: "empty-chat" });
  empty.append(
    el("h2", { text: "从一个研究问题开始" }),
    el("p", { text: "描述你的假设、数据来源或已有模型设定。研究助手会在对话中整理设定、执行 Stata，并在需要你拍板时停下来。" }),
  );
  const prompts = el("div", { className: "empty-prompts" });
  ["帮我梳理这个研究问题", "检查我的数据与识别策略", "比较两种回归设定"].forEach((text) => {
    prompts.append(el("button", { className: "prompt-chip", type: "button", text, dataset: { prompt: text } }));
  });
  empty.append(prompts);
  return empty;
}

const ATTACHMENT_STATUS_LABELS = {
  pending: "处理中",
  ready: "可用",
  quarantine: "待检查",
  quarantined: "待检查",
  rejected: "已拒绝",
  failed: "处理失败",
};

function attachmentStatusLabel(status) { return ATTACHMENT_STATUS_LABELS[status] || "不可用"; }

function safeAttachmentProjection(item) {
  if (!item || typeof item !== "object") return null;
  const attachmentId = typeof item.attachment_id === "string" ? item.attachment_id.slice(0, 120) : "";
  if (!attachmentId) return null;
  const status = typeof item.status === "string" ? item.status.slice(0, 32) : "pending";
  const name = typeof item.display_name === "string" ? item.display_name.slice(0, 180) : "PDF 附件";
  const role = item.source_role === "citable_evidence" ? "可引用证据" : "写作风格";
  const result = { attachment_id: attachmentId, display_name: name || "PDF 附件", status, source_role: role };
  if (typeof item.detected_format === "string") result.detected_format = item.detected_format.slice(0, 40);
  if (Number.isInteger(item.page_count) && item.page_count >= 0) result.page_count = item.page_count;
  if (typeof item.error_code === "string") result.error_code = item.error_code.slice(0, 80);
  result.retryable = item.retryable === true;
  return result;
}

function attachmentManifestFromMessage(message) {
  const source = Array.isArray(message?.attachments)
    ? message.attachments
    : Array.isArray(message?.payload?.attachments) ? message.payload.attachments : [];
  return source.map(safeAttachmentProjection).filter(Boolean).slice(0, 8);
}

function renderAttachmentManifest(items) {
  const safeItems = (Array.isArray(items) ? items : []).map(safeAttachmentProjection).filter(Boolean).slice(0, 8);
  if (!safeItems.length) return null;
  const manifest = el("div", { className: "attachment-manifest", role: "list", ariaLabel: "本轮引用附件" });
  safeItems.forEach((item) => {
    const entry = el("span", { className: "attachment-manifest-item", role: "listitem", dataset: { attachmentId: item.attachment_id, attachmentStatus: item.status } });
    entry.append(
      el("span", { className: "attachment-manifest-name", text: item.display_name }),
      el("span", { className: "attachment-manifest-status", text: attachmentStatusLabel(item.status) }),
    );
    manifest.append(entry);
  });
  return manifest;
}

function renderAttachmentStatusList() {
  const items = (Array.isArray(state.attachments) ? state.attachments : []).map(safeAttachmentProjection).filter(Boolean).slice(0, 8);
  if (!items.length) return null;
  const list = el("div", { className: "attachment-status-list", role: "list", ariaLabel: "附件状态" });
  items.forEach((item) => {
    const busy = ["pending", "quarantine", "quarantined"].includes(item.status);
    const error = ["rejected", "failed"].includes(item.status);
    const row = el("div", { className: `attachment-status-row${busy ? " is-busy" : error ? " is-error" : ""}`, role: "listitem", dataset: { attachmentStatus: item.status, attachmentId: item.attachment_id } });
    row.append(
      el("span", { className: `status-dot ${busy ? "is-busy" : error ? "is-error" : "is-ok"}`, ariaLabel: attachmentStatusLabel(item.status) }),
      el("span", { className: "attachment-status-name", text: item.display_name }),
      el("span", { className: "attachment-status-value", text: item.error_code ? `${attachmentStatusLabel(item.status)} · ${item.error_code}` : attachmentStatusLabel(item.status) }),
    );
    if (busy || error) row.append(el("button", { className: "link-button", type: "button", text: "重试状态", dataset: { action: "refresh-state", attachmentId: item.attachment_id }, ariaLabel: `刷新 ${item.display_name} 状态` }));
    list.append(row);
  });
  return list;
}

function renderComposer() {
  const form = el("form", { className: "composer", dataset: { composer: "true" } });
  if (state.lastFailure) {
    const failure = el("section", { className: "inline-notice is-error" });
    const text = el("div");
    text.append(
      el("p", { className: "notice-title", text: `本轮未完成（${state.lastFailure.code || "request_failed"}）` }),
      el("p", { text: state.lastFailure.message || "本轮未完成。" }),
    );
    const actions = el("div", { className: "notice-actions" });
    if (state.lastFailure.retryable) actions.append(el("button", { className: "link-button", type: "button", text: "重试本轮", dataset: { action: "retry-message" } }));
    actions.append(el("button", { className: "link-button", type: "button", text: "下载诊断包", dataset: { action: "download-diagnostics" } }));
    text.append(actions);
    failure.append(icon("alert"), text);
    form.append(failure);
  }
  if (isRunActive()) {
    const stateText = state.runState === "cancelling"
      ? "正在等待服务端确认停止，确认前不能开启新一轮。"
      : state.runState === "disconnected"
        ? "流式连接已中断，正在等待服务端确认本轮状态。"
        : "正在处理本轮研究指示。";
    form.append(el("div", { className: `run-state-notice ${state.runState === "disconnected" ? "is-error" : "is-busy"}`, role: "status", ariaLive: "polite" }, [
      el("span", { className: `status-dot ${state.runState === "disconnected" ? "is-error" : "is-busy"}`, ariaLabel: statusLabel(state.runState) }),
      el("span", { text: stateText }),
    ]));
  }
  const shell = el("div", { className: "composer-shell" });
  const selected = state.attachments.filter((item) => state.selectedAttachmentIds.includes(item.attachment_id));
  if (selected.length) {
    const chips = el("div", { className: "attachment-chips", ariaLabel: "本轮附件" });
    selected.forEach((item) => {
      const chip = el("span", { className: "attachment-chip" });
      chip.append(
        el("span", { text: item.display_name || "PDF 附件" }),
        el("button", {
          className: "attachment-chip-remove",
          type: "button",
          ariaLabel: `移除 ${item.display_name || "附件"}`,
          text: "×",
          dataset: { action: "remove-attachment", attachmentId: item.attachment_id },
        }),
      );
      chips.append(chip);
    });
    shell.append(chips);
  }
  const statusList = renderAttachmentStatusList();
  if (statusList) shell.append(statusList);
  const textarea = el("textarea", { value: state.draftText, disabled: !canSend(), ariaLabel: "给研究助手发消息", dataset: { composerInput: "true" } });
  textarea.rows = 2;
  textarea.maxLength = 20000;
  textarea.placeholder = "给研究助手发消息…";
  const footer = el("div", { className: "composer-footer" });
  const hint = el("span", { className: `composer-hint${state.sending ? " is-busy" : ""}`, text: state.sending ? state.stopPending ? "正在请求停止…" : "正在处理研究指示…" : "Enter 发送 · Shift+Enter 换行" });
  const actions = el("div", { className: "composer-actions" });
  const mode = el("button", { className: `topbar-button mode-button${state.goalMode ? " is-active" : ""}`, type: "button", text: state.goalMode ? "目标模式" : "交互模式", dataset: { action: "toggle-mode" }, title: state.goalMode ? "目标模式：自动推进到需你决定处" : "交互模式：每轮在需要你决定处停下" });
  const attach = el("button", { className: "icon-button", type: "button", disabled: !canSend(), ariaLabel: "添加 PDF 附件", title: "添加 PDF 附件", dataset: { action: "select-attachments" } });
  attach.append(icon("paperclip"));
  const fileInput = el("input", { type: "file", hidden: true, disabled: !canSend(), dataset: { attachmentInput: "true" } });
  fileInput.accept = ".pdf,application/pdf";
  fileInput.multiple = true;
  const role = el("button", {
    className: "topbar-button attachment-role",
    type: "button",
    disabled: !canSend(),
    text: state.attachmentRole === "citable_evidence" ? "可引用证据" : "写作风格",
    title: "切换新上传 PDF 的用途；可引用证据必须显式选择",
    dataset: { action: "toggle-attachment-role" },
  });
  const send = el("button", { className: "primary-button", type: "submit", disabled: !canSend() });
  send.append(icon("send"), el("span", { text: "发送" }));
  actions.append(mode, role, attach, fileInput);
  if (isRunActive()) {
    actions.append(el("button", {
      className: "secondary-button stop-button",
      type: "button",
      text: state.stopPending ? "正在停止…" : "停止",
      disabled: state.stopPending || state.runState === "disconnected",
      dataset: { action: "stop" },
      ariaLabel: state.stopPending ? "正在请求停止本轮运行" : "停止本轮运行",
    }));
  } else actions.append(send);
  footer.append(hint, actions);
  shell.append(textarea, footer);
  form.append(shell);
  return form;
}

function renderMarkdown(md) {
  // 极简安全 markdown 渲染：纯 DOM 构建（createTextNode/el，天然防 XSS）。
  // 处理：标题、列表、代码块、加粗、行内代码和表格；不解析 HTML/URL。
  const frag = document.createDocumentFragment();
  const lines = String(md || "").split("\n");
  let i = 0;
  let inCode = false;
  let codeBuf = [];
  let listBuf = null;  // { ordered, items[] }

  const inline = (text) => {
    const nodes = [];
    const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0; let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) nodes.push(document.createTextNode(text.slice(last, m.index)));
      const tok = m[0];
      if (tok.startsWith("**")) nodes.push(el("strong", { text: tok.slice(2, -2) }));
      else nodes.push(el("code", { className: "inline-code", text: tok.slice(1, -1) }));
      last = m.index + tok.length;
    }
    if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
    return nodes.length ? nodes : [document.createTextNode(text)];
  };

  const flushList = () => {
    if (!listBuf) return;
    const list = el(listBuf.ordered ? "ol" : "ul", { className: "md-list" });
    listBuf.items.forEach((item) => list.append(el("li", {}, inline(item))));
    frag.append(list);
    listBuf = null;
  };

  const tableCells = (line) => {
    let value = String(line || "").trim();
    if (value.startsWith("|")) value = value.slice(1);
    if (value.endsWith("|")) value = value.slice(0, -1);
    const cells = [];
    let cell = "";
    let escaped = false;
    for (const char of value) {
      if (char === "|" && !escaped) {
        cells.push(cell.trim()); cell = ""; continue;
      }
      if (char === "\\" && !escaped) { escaped = true; cell += char; continue; }
      cell += char; escaped = false;
    }
    cells.push(cell.trim());
    return cells.map((item) => item.replaceAll("\\|", "|"));
  };

  const isTableDivider = (line) => {
    const cells = tableCells(line);
    return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  };

  const appendTable = (headers, rows) => {
    const columns = Math.max(headers.length, ...rows.map((row) => row.length), 1);
    const wrapper = el("div", { className: "md-table-wrap", role: "region", ariaLabel: "Markdown 数据表" });
    const table = el("table", { className: "md-table" });
    table.append(el("caption", { className: "sr-only", text: headers[0] || "数据表" }));
    const headRow = el("tr");
    for (let column = 0; column < columns; column += 1) {
      const header = el("th", {}, inline(headers[column] ?? `列 ${column + 1}`));
      header.setAttribute("scope", "col");
      headRow.append(header);
    }
    table.append(el("thead", {}, [headRow]));
    const body = el("tbody");
    rows.forEach((row) => {
      const tr = el("tr");
      for (let column = 0; column < columns; column += 1) {
        tr.append(el("td", {}, inline(row[column] ?? "")));
      }
      body.append(tr);
    });
    table.append(body);
    wrapper.append(table);
    frag.append(wrapper);
  };

  while (i < lines.length) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      if (inCode) {
        frag.append(el("pre", { className: "md-code" }, [el("code", { text: codeBuf.join("\n") })]));
        codeBuf = []; inCode = false;
      } else { inCode = true; codeBuf = []; }
      i++; continue;
    }
    if (inCode) { codeBuf.push(line); i++; continue; }

    const t = line.trim();
    if (!t) { flushList(); i++; continue; }
    if (t.includes("|") && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
      flushList();
      const headers = tableCells(t);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        rows.push(tableCells(lines[i]));
        i += 1;
      }
      appendTable(headers, rows);
      continue;
    }
    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushList(); frag.append(el(`h${Math.min(h[1].length, 4)}`, { className: "md-heading" }, inline(h[2]))); i++; continue; }
    const ul = t.match(/^[-*]\s+(.*)$/);
    if (ul) { if (!listBuf || listBuf.ordered) { flushList(); listBuf = { ordered: false, items: [] }; } listBuf.items.push(ul[1]); i++; continue; }
    const ol = t.match(/^\d+\.\s+(.*)$/);
    if (ol) { if (!listBuf || !listBuf.ordered) { flushList(); listBuf = { ordered: true, items: [] }; } listBuf.items.push(ol[1]); i++; continue; }
    flushList();
    frag.append(el("p", { className: "message-text" }, inline(t)));
    i++;
  }
  flushList();
  if (inCode) frag.append(el("pre", { className: "md-code" }, [el("code", { text: codeBuf.join("\n") })]));
  return frag;
}

function messageMeta(message, role) {
  const meta = el("div", { className: "message-meta" });
  meta.append(el("span", { className: "message-author", text: role === "user" ? "你" : role === "system" ? "系统" : role === "tool" ? "工具" : "Stata 研究助手" }));
  if (message.created_at) meta.append(el("span", { text: formatTime(message.created_at) }));
  if (message.seq !== null && message.seq !== undefined) meta.append(el("span", { className: "message-seq", text: `#${message.seq}` }));
  return meta;
}

function renderMessage(message, latest = false) {
  const role = message.kind === "tool" ? "tool" : message.role === "user" ? "user" : message.role === "system" ? "system" : "assistant";
  const article = el("article", { className: `message message-${role}${latest ? " is-latest" : ""}`, dataset: { seq: message.seq ?? "" } });
  article.append(el("span", { className: "message-avatar", text: role === "user" ? "R" : role === "system" ? "·" : role === "tool" ? "⌘" : "S" }));
  const body = el("div", { className: "message-body" });
  body.append(messageMeta(message, role));
  if (role === "user") {
    const manifest = renderAttachmentManifest(attachmentManifestFromMessage(message));
    if (manifest) body.append(manifest);
  }
  if (message.kind === "tool") body.append(renderToolCard(message));
  else if (message.kind === "approval") body.append(renderApproval(message));
  else if (message.kind === "run") {
    body.append(el("p", { className: "message-text", text: message.text || "运行完成，执行状态已记录；请查看运行详情确认证据状态。" }));
    body.append(renderResultBlock(message));
  } else if (message.kind === "error") {
    const notice = el("section", { className: `inline-notice ${message.status === "paused" ? "is-warning" : "is-error"}` });
    notice.append(icon(message.status === "paused" ? "health" : "alert"));
    const text = el("div");
    const failure = normalizeFailure(message, message.status === "paused" ? "request_failed" : "request_failed");
    text.append(el("p", { className: "notice-title", text: message.status === "paused" ? "研究已暂停" : `本轮未完成（${failure.code}）` }), el("p", { text: message.status === "paused" ? (hasDurableCheckpoint() ? "研究在当前停点等待继续。" : "本轮已安全结束，可继续处理。") : failure.message }));
    if (message.status === "paused") text.append(el("button", { className: "link-button", type: "button", text: resumeLabel(), dataset: { action: "resume" } }));
    else {
      const actions = el("div", { className: "notice-actions" });
      if (failure.retryable) actions.append(el("button", { className: "link-button", type: "button", text: "重试本轮", dataset: { action: "retry-message" } }));
      actions.append(el("button", { className: "link-button", type: "button", text: "下载诊断包", dataset: { action: "download-diagnostics" } }));
      text.append(actions);
    }
    notice.append(text);
    body.append(notice);
  } else if (message.kind === "approval_decision") {
    body.append(el("p", { className: "message-text", text: `${message.decision === "approved" ? "审批已批准" : "审批已拒绝"}${message.note ? `：${message.note}` : ""}` }));
  } else {
    const summary = message.summary || "";
    const candidate = message.ask && message.ask !== summary ? message.ask : message.text || summary;
    const content = role === "assistant" && summary.trim() === String(candidate).trim() ? "" : candidate;
    if (role === "assistant" && summary) body.append(el("h2", { className: "message-heading", text: summary }));
    if (content) body.append(renderMarkdown(content));
    if (role === "assistant" && Array.isArray(message.acts) && message.acts.length) {
      const details = el("details", { className: "message-details" });
      details.append(el("summary", { text: "查看依据" }));
      const list = el("ul");
      message.acts.forEach((act) => list.append(el("li", { text: `${valueOrFallback(act.act_type)}${act.reason ? `：${act.reason}` : ""}` })));
      details.append(list);
      body.append(details);
    }
  }
  article.append(body);
  return article;
}

function toolStatusLabel(status) {
  if (status === "running") return "执行中";
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "失败";
  return "已停止";
}

function renderToolCard(tool) {
  const status = tool?.status || (tool?.ok === false ? "failed" : tool?.ok === true ? "succeeded" : "running");
  const card = el("section", {
    className: `tool-card tool-card-${status}`,
    dataset: { toolId: tool?.tool_id || "" },
    ariaLabel: `工具 ${valueOrFallback(tool?.tool_name || tool?.name, "未知工具")}`,
  });
  const header = el("div", { className: "tool-card-header" });
  header.append(
    el("span", { className: "tool-card-name", text: valueOrFallback(tool?.tool_name || tool?.name, "未知工具") }),
    el("span", { className: "tool-card-status", text: toolStatusLabel(status) }),
  );
  const meta = [];
  if (tool?.tool_id) meta.push(String(tool.tool_id));
  if (Array.isArray(tool?.args_keys) && tool.args_keys.length) meta.push(`参数：${tool.args_keys.join("、")}`);
  if (meta.length) card.append(el("div", { className: "tool-card-meta", text: meta.join(" · ") }));
  if (status === "failed" && tool?.error) {
    const failure = normalizeFailure(tool.error, "request_failed");
    card.append(el("p", { className: "tool-card-error", text: failure.message }));
  }
  card.prepend(header);
  return card;
}

function renderStreamingTools() {
  const container = $("[data-streaming-tools]");
  if (!container) return;
  clear(container);
  (Array.isArray(state.streamingTools) ? state.streamingTools : []).forEach((tool) => container.append(renderToolCard(tool)));
}

function renderBusyMessage() {
  const article = el("article", { className: `message message-assistant message-busy message-run-${state.runState}`, dataset: { runState: state.runState } });
  article.append(el("span", { className: "message-avatar", text: "S" }));
  const body = el("div", { className: "message-body" });
  body.append(messageMeta({ created_at: Date.now() }, "assistant"));
  // 流式文本放一个固定 data 属性节点，供 delta 到达时局部更新 textContent（不全量重建）
  const p = el("p", { className: "message-text", dataset: { streamingText: "true" } });
  p.textContent = state.streamingText || (state.runState === "cancelling" ? "正在保存停止状态…" : state.runState === "disconnected" ? "连接已中断，正在确认本轮状态…" : "正在处理你的研究指示…");
  body.append(p, el("div", { className: "streaming-tools", dataset: { streamingTools: "true" } }));
  renderStreamingTools();
  article.append(body);
  return article;
}

function approvalSubject(item) {
  const subjects = {
    propose_spec: "冻结主回归设定", request_run: "开始主回归运行", stata_run: "执行 Stata 运行",
    delete_file: "删除工作区文件", web_search: "访问外部网页", web_fetch: "读取外部网页", download: "下载外部文件",
  };
  return subjects[item.act] || item.act || "研究动作";
}

function approvalFor(requestId) {
  return (state.snapshot?.approvals || []).find((item) => item.request_id === requestId) || null;
}

function renderApproval(message) {
  const record = approvalFor(message.request_id);
  const pending = !record || record.status === "pending";
  const permission = message.gate_kind === "permission_gate" || record?.kind === "permission_gate";
  const panel = el("section", { className: `approval-inline${pending ? "" : " is-decided"}`, dataset: { approvalId: message.request_id || "" } });
  const top = el("div", { className: "approval-top" });
  const decidedLabel = record?.status === "modified" ? "已修改" : record?.status === "approved" ? "已批准" : "已拒绝";
  top.append(el("span", { className: "approval-icon" }, [icon(permission ? "health" : "approval")]), el("span", { className: "approval-title", text: `${pending ? "需要你确认" : decidedLabel} · ${approvalSubject(message)}` }));
  if (!pending) top.append(el("span", { className: "approval-status", text: decidedLabel }));
  const content = el("div", { className: "approval-content" });
  content.append(el("p", { text: message.reason || record?.reason || "Agent 需要你确认这一项研究动作。" }));
  if (message.note || record?.note) content.append(el("p", { className: "approval-note", text: message.note || record.note }));
  const details = el("details", { className: "approval-details" });
  details.append(el("summary", { text: "查看依据" }));
  const fields = el("dl");
  const target = message.target || record?.target || {};
  Object.entries(target).slice(0, 8).forEach(([key, value]) => fields.append(el("dt", { text: key }), el("dd", { text: valueOrFallback(value) })));
  if (!Object.keys(target).length) fields.append(el("dt", { text: "类型" }), el("dd", { text: permission ? "权限动作" : "研究设定" }));
  details.append(fields);
  content.append(details);
  if (pending) {
    const actions = el("div", { className: "approval-actions" });
    actions.append(
      el("button", { className: "primary-button", type: "button", text: "批准并继续", dataset: { decision: "approve", requestId: message.request_id || "" } }),
      el("button", { className: "secondary-button", type: "button", text: "提出修改", dataset: { decision: "modify", requestId: message.request_id || "" } }),
      el("button", { className: "secondary-button is-danger", type: "button", text: "拒绝并说明", dataset: { decision: "reject", requestId: message.request_id || "" } }),
    );
    content.append(actions);
  } else if (record?.decided_note) content.append(el("p", { className: "approval-note", text: `决定说明：${record.decided_note}` }));
  panel.append(top, content);
  return panel;
}

function cardValue(card) {
  const value = card?.value;
  if (value && typeof value === "object") return value.value ?? value.estimate ?? value.coef ?? value.effect ?? null;
  return value;
}

function cardsForRun(runId) {
  return (state.snapshot?.card_records || []).filter((card) => card.locator?.run_id === runId || card.run_id === runId);
}

function renderResultBlock(message) {
  const runId = message.run_id || "";
  const run = (state.snapshot?.run_records || []).find((item) => item.run_id === runId);
  const machine = message.machine && typeof message.machine === "object" ? message.machine : run?.machine && typeof run.machine === "object" ? run.machine : {};
  const cards = cardsForRun(runId);
  const block = el("details", { className: "result-block", open: true });
  const summary = el("summary");
  summary.append(el("span", { className: "result-title", text: "回归结果" }));
  if (runId) summary.append(el("span", { className: "result-meta", text: runId }));
  const content = el("div", { className: "result-content" });
  const command = run?.command || run?.script || message.provenance?.command || "";
  if (command) content.append(el("div", { className: "command-line", text: command }));
  const table = el("table", { className: "result-table" });
  const head = el("tr");
  ["指标", "数值", "来源"].forEach((label) => head.append(el("th", { text: label })));
  table.append(el("thead", {}, [head]));
  const rows = el("tbody");
  const entries = Object.entries(machine);
  entries.forEach(([key, value]) => {
    const row = el("tr");
    row.append(el("td", { text: key }), el("td", { text: typeof value === "number" ? formatNumber(value) : valueOrFallback(value) }), el("td", { text: "机器层结果" }));
    rows.append(row);
  });
  cards.forEach((card) => {
    const row = el("tr");
    const value = cardValue(card);
    const button = el("button", { className: "evidence-value", type: "button", text: value === null ? "查看证据" : formatNumber(value), dataset: { cardId: card.card_id } });
    row.append(el("td", { text: card.locator?.stat_type || "证据卡" }), el("td", {}, [button]), el("td", { text: card.card_id || "证据卡" }));
    rows.append(row);
  });
  if (!entries.length && !cards.length) rows.append(el("tr", {}, [el("td", { colSpan: 3, text: "本次运行没有可展示的机器层明细。" })]));
  table.append(rows);
  content.append(table);
  const foot = el("div", { className: "result-foot" });
  if (machine.N !== undefined) foot.append(el("span", { text: `N = ${valueOrFallback(machine.N)}` }));
  if (machine.r2 !== undefined) foot.append(el("span", { text: `R² = ${formatNumber(machine.r2)}` }));
  if (message.provenance?.do_file) foot.append(el("span", { text: valueOrFallback(message.provenance.do_file) }));
  if (foot.childNodes.length) content.append(foot);
  block.append(summary, content);
  return block;
}

function createPageActions(includeBack = true) {
  const actions = [];
  if (includeBack) actions.push(el("button", { className: "secondary-button", type: "button", text: "返回对话", dataset: { navPage: "chat" } }));
  return actions;
}

function renderResourceView(page) {
  if (page === "results") return renderResultsView();
  const titles = {
    settings: ["应用设置", "模型、隐私、Stata 与本机运行偏好"],
    research: ["当前研究", "当前工作区的研究设定（只读投影）"],
    variables: ["变量", "从对话与账本投影出的变量口径"],
    data: ["数据", "样本口径与数据指纹"],
    models: ["模型", "已记录的模型设定与运行"],
    charts: ["图表", "当前工作区生成的可复核图表"],
    documents: ["文档", "由已确认结论生成的可编辑初稿"],
    approvals: ["审批", "研究动作的持久化决定"],
    health: ["预算 / 健康", "运行状态、预算与本机检查"],
  };
  const [title, subtitle] = titles[page] || ["工作区", ""];
  const pageElement = el("section", { className: "resource-page", ariaLabel: title });
  pageElement.append(createViewHeader(title, subtitle, createPageActions()));
  const intro = el("p", { className: "page-intro", text: page === "settings" ? "应用设置只影响运行配置；研究问题、模型与结论仍由对话和账本治理。" : "内容来自当前工作区的只读状态投影；改变研究状态请回到对话中提出指示。" });
  pageElement.append(intro);
  if (page === "settings") pageElement.append(renderSettingsResource());
  else if (page === "research") pageElement.append(renderCurrentResearchResource());
  else if (page === "variables") pageElement.append(renderVariablesResource());
  else if (page === "data") pageElement.append(renderDataResource());
  else if (page === "models") pageElement.append(renderModelsResource());
  else if (page === "charts") pageElement.append(renderEmptyResource("尚未产生可复核图表", "图表将在运行结果或对话明确生成后出现在这里。"));
  else if (page === "documents") pageElement.append(renderDocumentsResource());
  else if (page === "approvals") pageElement.append(renderApprovalsResource());
  else if (page === "health") pageElement.append(renderHealthResource());
  return pageElement;
}

function renderEmptyResource(title, detail) {
  const empty = el("div", { className: "empty-resource" });
  empty.append(el("strong", { text: title }), el("span", { text: detail }));
  return empty;
}

function resourceSection(title, count, content, open = true, status = "") {
  const section = el("details", { className: "resource-section", open });
  const summary = el("summary");
  summary.append(el("span", { className: "section-title", text: title }));
  if (count !== null && count !== undefined) summary.append(el("span", { className: "section-count", text: String(count) }));
  if (status) summary.append(el("span", { className: "section-status", text: status }));
  section.append(summary, el("div", { className: "section-body" }, [content]));
  return section;
}

const SETTINGS_GROUPS = [
  ["模型与 API", ["provider.primary", "provider.model", "provider.live_enabled", "provider.base_url"]],
  ["隐私与网络", ["privacy.mode"]],
  ["Stata", ["executor.kind", "stata.mcp_dir"]],
  ["文献、技能与附件", ["library.root", "skills.root", "attachments.default_role"]],
  ["Agent、记忆与上下文", ["agent.default_mode", "agent.interactive_max_steps", "agent.goal_max_steps", "agent.max_tool_calls", "compaction.summary_mode", "memory.extraction_mode", "context.max_input_tokens", "context.reserve_output_tokens", "context.recent_tail_tokens", "context.memory_tokens"]],
  ["数据目录（只读）", ["storage.database", "storage.workspaces", "storage.attachments"]],
  ["外观与应用", ["ui.theme", "ui.language", "ui.port"]],
];

const SETTINGS_ENUMS = {
  "privacy.mode": ["local_strict", "mixed_sanitized", "approved_remote"],
  "executor.kind": ["disabled", "stata"],
  "attachments.default_role": ["style_only", "citable_evidence"],
  "agent.default_mode": ["interactive", "goal"],
  "compaction.summary_mode": ["deterministic", "provider"],
  "memory.extraction_mode": ["off", "provider"],
  "ui.theme": ["system", "light", "dark"],
  "ui.language": ["zh-CN"],
};

function settingLabel(key) {
  return key.split(".").slice(-1)[0].replaceAll("_", " ");
}

function settingControl(key, projection) {
  const id = `settings-${key.replaceAll(".", "-")}`;
  const value = settingsDraftValue(key);
  const disabled = projection?.editable === false;
  let control;
  const choices = settingChoices(key);
  if (choices) {
    control = el("select", { id, className: "settings-control", dataset: { settingKey: key }, disabled });
    const labels = key === "provider.primary"
      ? new Map([["auto", "自动选择"], ...providerCatalog().map((item) => [String(item.id), item.label || item.id])])
      : key === "provider.model"
        ? new Map(modelChoices().map((item) => [item.id, item.label]))
        : new Map();
    choices.forEach((choice) => {
      const option = el("option", { value: choice, text: labels.get(choice) || choice });
      option.selected = String(value) === choice;
      control.append(option);
    });
  } else if (projection?.value === true || projection?.value === false) {
    control = el("input", { id, className: "settings-checkbox", type: "checkbox", dataset: { settingKey: key }, checked: value === true, disabled });
  } else {
    const numeric = key.startsWith("context.")
      || (key.startsWith("agent.") && key !== "agent.default_mode")
      || key === "ui.port";
    control = el("input", { id, className: "settings-control", type: numeric ? "number" : "text", value, dataset: { settingKey: key }, disabled, spellcheck: false });
  }
  control.setAttribute("aria-describedby", `${id}-meta`);
  return control;
}

function renderSettingRow(key) {
  const projection = settingsProjection(key);
  const row = el("div", { className: "settings-row" });
  const id = `settings-${key.replaceAll(".", "-")}`;
  const label = el("label", { className: "settings-label", htmlFor: id, text: projection?.message || settingLabel(key) });
  const metaBits = [`来源：${projection?.source || "unknown"}`, `生效：${projection?.apply_mode || "—"}`];
  if (projection?.pending_restart) metaBits.push("需要重启");
  if (projection?.editable === false) metaBits.push("由环境/系统管理");
  const meta = el("div", { id: `${id}-meta`, className: "settings-meta", text: metaBits.join(" · ") });
  const cell = el("div", { className: "settings-value" });
  cell.append(settingControl(key, projection), meta);
  if (projection?.status === "error") cell.append(el("p", { className: "settings-error", role: "alert", text: "当前值无效，已使用安全默认值。" }));
  row.append(label, cell);
  return row;
}

function providerSecretStatus(provider) {
  const fromCatalog = providerCatalog().find((item) => String(item.id) === String(provider))?.secret;
  return fromCatalog || state.settings?.secrets?.[provider] || { configured: false, source: "none", editable: true };
}

function renderProviderCredentialEditor() {
  const provider = credentialProvider();
  const status = providerSecretStatus(provider);
  const unavailable = status.editable === false && status.source !== "environment";
  const row = el("div", { className: "settings-secret-row" });
  const providerSelect = el("select", { className: "settings-control", ariaLabel: "选择要管理凭据的 provider", dataset: { settingsCredentialProvider: "true" } });
  providerCatalog().forEach((item) => {
    const id = String(item.id || "");
    if (!id) return;
    const option = el("option", { value: id, text: item.label ? `${item.label}（${id}）` : id });
    option.selected = id === provider;
    providerSelect.append(option);
  });
  const statusText = status.source === "environment"
    ? "由环境管理（清除对应环境变量后可在此填写）"
    : unavailable ? "安全凭据存储不可用（未保存密钥）"
    : status.configured ? "已配置（不会显示密钥）" : "未配置";
  const title = el("div", { className: "settings-secret-title", text: "Provider 凭据" });
  const input = el("input", { className: "settings-control", type: "password", autocomplete: "new-password", ariaLabel: `${provider} 凭据（不会回显）`, spellcheck: false, placeholder: status.configured ? "输入新密钥以替换" : "输入密钥", dataset: { secretInput: provider }, disabled: !provider || status.source === "environment" || unavailable });
  const actions = el("div", { className: "settings-actions" });
  actions.append(el("button", { className: "secondary-button", type: "button", text: status.configured ? "替换凭据" : "保存凭据", dataset: { settingsSecretSave: provider }, disabled: !provider || status.source === "environment" || unavailable }));
  if (status.configured && status.source !== "environment") actions.append(el("button", { className: "link-button", type: "button", text: "删除", dataset: { settingsSecretDelete: provider } }));
  row.append(
    el("div", { className: "settings-secret-main" }, [title, providerSelect, el("span", { className: "settings-meta", text: `${statusText} · 生效：下一请求` })]),
    el("div", { className: "settings-secret-controls" }, [input, actions]),
  );
  return row;
}

function renderSettingsResource() {
  if (state.settingsLoading && !state.settings) return renderEmptyResource("正在读取应用设置", "不会读取或展示任何密钥内容。");
  if (state.settingsError && !state.settings) return el("div", { className: "inline-notice is-error" }, [icon("alert"), el("div", {}, [el("p", { className: "notice-title", text: "设置暂时不可用" }), el("p", { text: "请刷新页面后重试。" }), el("button", { className: "secondary-button", type: "button", text: "重试", dataset: { action: "settings-refresh" } })])]);
  const container = el("div", { className: "settings-center" });
  const overview = el("div", { className: "settings-overview" });
  const health = Array.isArray(state.settings?.health) ? state.settings.health : [];
  overview.append(el("div", { className: "settings-overview-title", text: state.settings?.restart_required ? "有设置需要重启后生效" : "应用设置已就绪" }), el("div", { className: "settings-meta", text: `Revision ${state.settings?.revision ?? 0} · ${health.length ? `${health.length} 项需要关注` : "配置健康"}` }));
  if (providerCatalog().length) container.append(overview, renderProviderCredentialEditor());
  else container.append(overview);
  SETTINGS_GROUPS.forEach(([title, keys]) => {
    const body = el("div", { className: "settings-grid" });
    keys.forEach((key) => body.append(renderSettingRow(key)));
    const advanced = title === "Agent、记忆与上下文";
    if (advanced) body.append(el("button", { className: "link-button settings-reset-button", type: "button", text: "恢复推荐值", dataset: { action: "settings-reset-context" } }));
    const section = resourceSection(title, keys.length, body, !advanced);
    if (advanced) section.classList.add("settings-advanced");
    container.append(section);
  });
  const checks = el("div", { className: "settings-checks" });
  ["provider", "stata", "library"].forEach((kind) => {
    const result = state.settingsChecks[kind];
    checks.append(el("div", { className: "settings-check-row" }, [el("span", { text: `${kind} 检查` }), el("span", { className: "settings-meta", text: result ? (result.status === "checking" ? "检查中…" : result.code || result.status) : "未检查" }), el("button", { className: "secondary-button", type: "button", text: "检查", dataset: { settingsCheck: kind } })]));
  });
  container.append(resourceSection("健康检查", 3, checks, true));
  const maintenance = el("div", { className: "settings-actions" });
  const backupInput = el("input", { type: "file", accept: ".zip,application/zip", hidden: true, ariaLabel: "选择要校验的备份文件", dataset: { settingsBackupInput: "true" } });
  maintenance.append(
    el("button", { className: "secondary-button", type: "button", text: "下载已验证备份", dataset: { action: "settings-backup" } }),
    el("button", { className: "secondary-button", type: "button", text: "校验备份文件", dataset: { action: "settings-verify-backup" } }),
    el("button", { className: "link-button", type: "button", text: "下载诊断包", dataset: { action: "settings-diagnostics" } }),
    el("button", { className: "secondary-button", type: "button", text: "刷新设置", dataset: { action: "settings-refresh" } }),
    backupInput,
  );
  container.append(resourceSection("数据维护", null, maintenance, true));
  const about = fieldList([["版本", "stata-agent 0.1.0", true], ["运行环境", "Windows 本机", false], ["Stata 授权", "永久授权", false], ["界面语言", "zh-CN（当前版本固定）", false]]);
  container.append(resourceSection("关于", 4, about, false));
  const pendingCount = Object.keys(pendingSettingsChanges()).length;
  const bar = el("div", { className: `settings-save-bar${state.settingsDirty ? " is-dirty" : ""}` });
  if (state.settingsValidationError) bar.append(el("p", { className: "settings-error", role: "alert", text: state.settingsValidationError.message }));
  bar.append(el("span", { className: "settings-meta", text: pendingCount ? `${pendingCount} 项未保存更改` : "所有更改均已保存" }), el("button", { className: "secondary-button", type: "button", text: "放弃更改", dataset: { action: "settings-discard" }, disabled: !state.settingsDirty }), el("button", { className: "primary-button", type: "button", text: state.settingsSaving ? "保存中…" : "保存设置", dataset: { action: "settings-save" }, disabled: !state.settingsDirty || state.settingsSaving || Boolean(state.settingsValidationError) }));
  container.append(bar);
  return container;
}

function fieldList(fields) {
  const list = el("dl", { className: "field-list" });
  fields.forEach(([label, value, mono]) => {
    const row = el("div", { className: "field-row" });
    row.append(el("dt", { text: label }), el("dd", { className: mono ? "mono" : "", text: valueOrFallback(value) }));
    list.append(row);
  });
  return list;
}

function renderCurrentResearchResource() {
  const snapshot = state.snapshot || {};
  const rs = snapshot.research_state || {};
  const cfg = snapshot.config || {};
  const settings = fieldList([
    ["研究问题", snapshot.idea_title, true],
    ["当前阶段", phaseLabel(snapshot.display_phase || snapshot.phase)],
    ["主 spec", rs.current_spec_id || snapshot.spec || "（未定）"],
    ["LLM 模型", cfg.provider || "未配置"],
    ["Stata 执行", cfg.executor || "—"],
    ["隐私模式", snapshot.privacy_mode || "local_strict", true],
    ["文献库", cfg.library || "（未配置）", true],
    ["已加载技能", (cfg.skills && cfg.skills.length) ? cfg.skills.join("、") : "无", true],
    ["工作区账本", cfg.workspace_db || "—", true],
  ]);
  return resourceSection("设置", 10, settings, true);
}

function renderVariablesResource() {
  const rs = state.snapshot?.research_state || {};
  const rows = [];
  if (rs.dependent_variable) rows.push(["因变量", rs.dependent_variable]);
  if (rs.core_explanatory_variable) rows.push(["核心解释变量", rs.core_explanatory_variable]);
  if (Array.isArray(rs.controls) && rs.controls.length) rows.push(["控制变量", rs.controls]);
  if (rs.fixed_effects) rows.push(["固定效应", rs.fixed_effects]);
  if (rs.cluster_level) rows.push(["聚类层级", rs.cluster_level]);
  return resourceSection("变量口径", rows.length, rows.length ? fieldList(rows) : renderEmptyResource("尚未形成变量口径", "请在对话中描述研究问题与数据字段。"));
}

function latestRun() { return (state.snapshot?.run_records || []).slice().sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0))[0] || null; }

function renderDataResource() {
  const rs = state.snapshot?.research_state || {};
  const run = latestRun();
  const prov = run?.provenance || {};
  const content = fieldList([["样本口径", rs.sample_sig, true], ["最近运行样本量", run?.machine?.N], ["数据指纹", prov.data_signature, true], ["最后更新", state.snapshot?.updated_at ? formatDate(state.snapshot.updated_at) : null]]);
  return resourceSection("样本与指纹", 4, content, true);
}

function renderModelsResource() {
  const snapshot = state.snapshot || {};
  const rs = snapshot.research_state || {};
  const runs = Array.isArray(snapshot.run_records) ? snapshot.run_records : [];
  const content = el("div", { className: "resource-list" });
  content.append(fieldList([["当前 spec", rs.current_spec_id || snapshot.spec, true], ["模型族", rs.current_family_id, true]]));
  if (!runs.length) content.append(renderEmptyResource("尚未产生运行记录", "模型将在对话中确认并执行后出现在这里。"));
  else runs.slice().reverse().forEach((run) => {
    const line = el("div", { className: "resource-line" });
    const main = el("div", { className: "resource-line-main" });
    main.append(el("div", { className: "resource-line-title", text: run.run_id }), el("div", { className: "resource-line-meta", text: `${statusLabel(run.status)} · ${valueOrFallback(run.provenance?.do_file)}` }));
    line.append(main, el("button", { className: "link-button resource-line-action", type: "button", text: "查看证据", dataset: { runId: run.run_id } }));
    content.append(line);
  });
  return resourceSection("模型与运行", runs.length, content, true);
}

function renderDocumentsResource() {
  const snapshot = state.snapshot || {};
  const claims = Array.isArray(snapshot.claim_records) ? snapshot.claim_records : [];
  const content = el("div", { className: "resource-list" });
  if (snapshot.draft_ready) {
    content.append(el("div", { className: "inline-notice" }, [icon("file"), el("div", {}, [el("p", { className: "notice-title", text: "Word 初稿可生成" }), el("p", { text: "只包含已确认且可溯源的结论。" })])]));
    content.append(el("button", { className: "primary-button", type: "button", text: "生成 Word 初稿", dataset: { action: "generate-draft" } }));
  } else content.append(renderEmptyResource("尚无可写入初稿的结论", "当证据结论获确认后，可从这里生成 Word。"));
  if (claims.length) {
    const list = el("div", { className: "resource-list" });
    claims.forEach((claim) => {
      const line = el("div", { className: "resource-line" });
      const main = el("div", { className: "resource-line-main" });
      main.append(el("div", { className: "resource-line-title", text: claim.statement || claim.claim_id }), el("div", { className: "resource-line-meta", text: `${claim.status || "尚未确定"} · ${claim.claim_id}` }));
      line.append(main);
      list.append(line);
    });
    content.append(resourceSection("可写入结论", claims.length, list, false));
  }
  return resourceSection("文档产物", snapshot.draft_ready ? 1 : 0, content, true);
}

function renderApprovalsResource() {
  const approvals = Array.isArray(state.snapshot?.approvals) ? state.snapshot.approvals : [];
  const content = el("div", { className: "resource-list" });
  if (!approvals.length) content.append(renderEmptyResource("暂无审批记录", "需要人工拍板的研究动作会以内联审批形式出现在对话中。"));
  else approvals.slice().reverse().forEach((record) => {
    const line = el("div", { className: "resource-line" });
    const main = el("div", { className: "resource-line-main" });
    const dot = el("span", { className: `status-dot ${record.status === "approved" || record.status === "modified" ? "is-ok" : record.status === "rejected" ? "is-error" : "is-busy"}` });
    main.append(el("div", { className: "resource-line-title" }, [dot, document.createTextNode(` ${approvalSubject(record)}`)]), el("div", { className: "resource-line-meta", text: `${record.status} · #${valueOrFallback(record.requested_seq)}` }));
    if (record.status === "pending") line.append(main, el("button", { className: "link-button resource-line-action", type: "button", text: "回到对话", dataset: { approvalId: record.request_id } }));
    else line.append(main);
    content.append(line);
  });
  return resourceSection("审批记录", approvals.length, content, true);
}

function renderHealthResource() {
  const snapshot = state.snapshot || {};
  const health = snapshot.health || {};
  const status = snapshot.run_status || "idle";
  const content = el("div", { className: "resource-list" });
  content.append(fieldList([["运行状态", statusLabel(status)], ["状态说明", snapshot.status_detail], ["本机模式", snapshot.privacy_mode || "local_strict", true], ["最后更新", snapshot.updated_at ? formatDate(snapshot.updated_at) : null]]));
  const notice = el("div", { className: `inline-notice ${health.ok === false ? "is-error" : ""}` });
  notice.append(icon(health.ok === false ? "alert" : "check"), el("div", {}, [el("p", { className: "notice-title", text: health.ok === false ? "健康检查未通过" : "本机环境正常" }), el("p", { text: health.detail || "没有新的健康问题。" })]));
  content.append(notice);
  if (status === "paused" || status === "failed") content.append(el("button", { className: "secondary-button", type: "button", text: resumeLabel(), dataset: { action: "resume" } }));
  return resourceSection("状态与预算", 1, content, true, statusLabel(status));
}

function renderResultsView() {
  const snapshot = state.snapshot || {};
  const claims = Array.isArray(snapshot.claim_records) ? snapshot.claim_records : [];
  const runs = Array.isArray(snapshot.run_records) ? snapshot.run_records : [];
  const page = el("section", { className: "resource-page results-page", ariaLabel: "结果" });
  const sync = el("span", { className: "sync-status" });
  sync.append(icon("refresh"), el("span", { text: state.refreshFailed ? "同步失败" : "刚刚同步 · 自动更新中" }));
  const actions = createPageActions();
  const draft = el("button", { className: "primary-button", type: "button", text: "生成 Word 初稿", disabled: !snapshot.draft_ready, dataset: { action: "generate-draft" } });
  actions.splice(0, 0, draft);
  page.append(createViewHeader("结果", "随对话与运行实时更新", actions), el("p", { className: "page-intro" }, [el("span", { text: "这里汇总当前工作区已生成的结论与模型；" }), sync]));
  const conclusionBody = el("div", { className: "resource-list" });
  if (!claims.length) conclusionBody.append(renderEmptyResource("尚未形成已确认结论", "结果会在运行完成并通过证据核对后出现在这里。"));
  else claims.forEach((claim) => {
    const line = el("div", { className: "resource-line" });
    const main = el("div", { className: "resource-line-main" });
    main.append(el("div", { className: "resource-line-title", text: claim.statement || claim.claim_id }));
    const card = (snapshot.card_records || []).find((item) => claim.cards?.includes(item.card_id));
    if (card) {
      const value = el("button", { className: "resource-value resource-value-button", type: "button", text: cardValue(card) === null ? "查看证据" : formatNumber(cardValue(card)), dataset: { cardId: card.card_id } });
      main.append(el("div", { className: "resource-line-meta" }, [value, document.createTextNode(` · ${claim.status || "尚未确定"} · ${card.card_id}`)]));
    } else main.append(el("div", { className: "resource-line-meta", text: `${claim.status || "尚未确定"} · ${claim.claim_id}` }));
    line.append(main);
    if (card) line.append(el("button", { className: "link-button resource-line-action", type: "button", text: "查看证据", dataset: { cardId: card.card_id, claimId: claim.claim_id } }));
    conclusionBody.append(line);
  });
  page.append(resourceSection("已确认结论", claims.length, conclusionBody, true, claims.length ? "可追溯" : "待生成"));

  const comparison = el("div", { className: "section-table-wrap" });
  if (!runs.length) comparison.append(renderEmptyResource("尚未产生模型运行", "完成一次 Stata 运行后，模型比较会实时更新。"));
  else {
    const table = el("table", { className: "results-table" });
    const head = el("tr");
    ["运行标识", "状态", "样本量", "代码位置"].forEach((label) => head.append(el("th", { text: label })));
    table.append(el("thead", {}, [head]));
    const body = el("tbody");
    runs.slice().reverse().forEach((run) => {
      const row = el("tr");
      row.append(el("td", { className: "mono", text: run.run_id }), el("td", { text: statusLabel(run.status) }), el("td", { text: valueOrFallback(run.machine?.N) }), el("td", { className: "mono", text: valueOrFallback(run.provenance?.do_file) }));
      body.append(row);
    });
    table.append(body); comparison.append(table);
  }
  page.append(resourceSection("模型比较", runs.length, comparison, true));

  const recent = el("div", { className: "resource-list" });
  if (!runs.length) recent.append(renderEmptyResource("暂无最近运行", "运行状态会在这里持续更新。"));
  else runs.slice().reverse().slice(0, 5).forEach((run) => {
    const line = el("div", { className: "resource-line" });
    line.append(el("div", { className: "resource-line-main" }, [el("div", { className: "resource-line-title" }, [el("span", { className: `status-dot ${run.status === "succeeded" ? "is-ok" : "is-error"}` }), document.createTextNode(` ${run.run_id}`)]), el("div", { className: "resource-line-meta", text: `${statusLabel(run.status)} · ${formatDate(run.created_at)}` })]));
    const card = cardsForRun(run.run_id)[0];
    if (card) line.append(el("button", { className: "link-button resource-line-action", type: "button", text: "查看详情", dataset: { cardId: card.card_id } }));
    recent.append(line);
  });
  page.append(resourceSection("最近运行", runs.length, recent, true));

  const pending = el("div", { className: "resource-list" });
  const pendingClaims = claims.filter((claim) => claim.status !== "supported");
  if (!pendingClaims.length) pending.append(renderEmptyResource("没有待写入结论", "已确认结论可从上方生成 Word 初稿。"));
  else pendingClaims.forEach((claim) => pending.append(el("div", { className: "doc-pending" }, [el("span", { className: "status-dot is-busy" }), el("span", { text: claim.statement || claim.claim_id })])));
  page.append(resourceSection("待写入文档", pendingClaims.length, pending, true));
  return page;
}

async function loadTrace({ reset = false, renderAfter = false } = {}) {
  state.traceController?.abort();
  if (reset) {
    state.traceItems = [];
    state.traceCursor = null;
  }
  const controller = new AbortController();
  state.traceController = controller;
  const generation = state.workspaceGeneration;
  const workspace = state.activeWorkspace;
  const params = [`limit=50`, `ws=${encodeURIComponent(workspace)}`];
  if (state.traceQuery) params.push(`search=${encodeURIComponent(state.traceQuery)}`);
  if (state.traceCursor) params.push(`before_seq=${encodeURIComponent(state.traceCursor)}`);
  const group = state.traceFilter;
  if (group !== "all") {
    const types = Array.from(EVENT_GROUPS[group] || []);
    if (types.length === 1) params.push(`type=${encodeURIComponent(types[0])}`);
    else if (state.traceQuery) params[1] = `ws=${encodeURIComponent(state.activeWorkspace)}`;
  }
  try {
    const body = await getJSON(`/api/trace?${params.join("&")}`, { signal: controller.signal });
    if (generation !== state.workspaceGeneration || workspace !== state.activeWorkspace) return;
    const rows = Array.isArray(body?.items) ? body.items : [];
    state.traceItems = reset ? rows : state.traceItems.concat(rows);
    state.traceTotal = Number(body?.total || state.traceItems.length);
    state.traceCursor = body?.next_before_seq ?? null;
    if (renderAfter) render();
  } catch (error) {
    if (error?.name !== "AbortError") {
      setFailure(error, "network_error");
      showToast("Trace 暂时不可用，请重试或下载诊断包。", "network_error");
    }
  } finally {
    if (state.traceController === controller) state.traceController = null;
  }
}

function eventEvidenceCard(event) {
  const preview = event.payload_preview || {};
  const cardId = preview.card_id || (typeof event.object === "string" && event.object.startsWith("card") ? event.object : null);
  if (cardId && (state.snapshot?.card_records || []).some((card) => card.card_id === cardId)) return cardId;
  return null;
}

function visibleTraceItems() {
  if (state.traceFilter === "all") return state.traceItems;
  const kinds = EVENT_GROUPS[state.traceFilter] || new Set();
  return state.traceItems.filter((event) => kinds.has(event.type || event.event_type || ""));
}

function renderTechnicalTraceView() {
  const view = el("section", { className: "trace-page", ariaLabel: "Trace 事件回放" });
  const actions = [el("button", { className: "secondary-button", type: "button", text: "返回对话", dataset: { navPage: "chat" } })];
  view.append(createViewHeader("Trace", `${state.traceTotal || state.snapshot?.events || 0} 个事件`, actions, "trace-page-header"));
  const toolbar = el("div", { className: "trace-toolbar" });
  ["all", "approval", "run", "conclusion", "health"].forEach((group) => toolbar.append(el("button", { className: `trace-filter${state.traceFilter === group ? " is-current" : ""}`, type: "button", text: group === "all" ? "全部" : group === "approval" ? "审批" : group === "run" ? "运行" : group === "conclusion" ? "结论" : "预算 / 健康", dataset: { traceFilter: group } })));
  const search = el("label", { className: "trace-search" });
  search.append(icon("search"));
  const input = el("input", { value: state.traceQuery, ariaLabel: "搜索 Trace 事件" });
  input.type = "search"; input.placeholder = "搜索事件、参与者或对象"; input.dataset.traceSearch = "true";
  search.append(input); toolbar.append(search);
  view.append(toolbar);
  const wrap = el("div", { className: "trace-table-wrap" });
  const table = el("table", { className: "trace-table" });
  const header = el("tr");
  ["序号", "时间", "事件", "参与者", "阶段", "对象", "摘要"].forEach((label) => header.append(el("th", { text: label })));
  table.append(el("thead", {}, [header]));
  const body = el("tbody");
  const visible = visibleTraceItems();
  if (!visible.length) body.append(el("tr", {}, [el("td", { colSpan: 7, text: "暂无符合条件的事件。" })]));
  visible.forEach((event) => {
    const expanded = String(state.traceExpandedSeq) === String(event.seq);
    const row = el("tr", { className: "trace-row", dataset: { traceSeq: event.seq ?? "" }, ariaExpanded: expanded });
    row.tabIndex = 0;
    row.append(el("td", { className: "trace-seq", text: `#${valueOrFallback(event.seq, "?")}` }), el("td", { text: formatTime(event.created_at) }), el("td", { className: "trace-type", text: event.type || event.event_type || "未知事件" }), el("td", { text: valueOrFallback(event.actor) }), el("td", { text: phaseLabel(event.phase) }), el("td", { className: "mono", text: valueOrFallback(event.object) }), el("td", { className: "trace-summary-cell", text: valueOrFallback(event.summary || event.payload_preview?.decision_summary || event.payload_preview?.reason, "展开查看") }));
    body.append(row);
    if (expanded) {
      const detail = el("tr", { className: "trace-detail-row" });
      const cell = el("td", { colSpan: 7 });
      const preview = event.payload_preview || {};
      const text = Object.keys(preview).length ? Object.entries(preview).map(([key, value]) => `${key}: ${valueOrFallback(value)}`).join("\n") : `payload: ${(event.payload_keys || []).join(", ") || "无"}`;
      const content = el("div", { className: "trace-detail-content", text });
      cell.append(content);
      const cardId = eventEvidenceCard(event);
      if (cardId) cell.append(el("button", { className: "link-button", type: "button", text: "查看证据溯源", dataset: { cardId } }));
      detail.append(cell); body.append(detail);
    }
  });
  table.append(body); wrap.append(table);
  const footer = el("div", { className: "trace-load" });
  if (state.traceCursor) footer.append(el("button", { className: "secondary-button", type: "button", text: "加载更早事件", dataset: { action: "load-trace" } }));
  wrap.append(footer); view.append(wrap);
  return view;
}

function formatTraceDuration(value) {
  if (value === null || value === undefined || value === "") return "—";
  const duration = Number(value);
  if (!Number.isFinite(duration) || duration < 0) return "—";
  if (duration < 1000) return `${Math.round(duration)} ms`;
  return `${(duration / 1000).toFixed(duration >= 10000 ? 0 : 1)} 秒`;
}

function activityStatusClass(status) {
  return status === "completed" ? "is-ok" : ["failed", "uncertain", "cancelled"].includes(status) ? "is-error" : "is-busy";
}

function appendCopyableTechnical(parent, technical) {
  if (!technical || typeof technical !== "object") return;
  const fields = [
    ["请求标识", technical.request_id || technical.correlation_id],
    ["操作标识", technical.operation_id],
    ["运行标识", technical.run_id],
    ["调用标识", technical.call_id],
    ["证据卡", technical.card_id],
    ["结论", technical.claim_id],
  ];
  const rows = fields.filter(([, value]) => value);
  const eventSeqs = Array.isArray(technical.event_seqs) ? technical.event_seqs.filter((value) => value !== null && value !== undefined) : [];
  if (!rows.length && !eventSeqs.length) return;
  const detail = el("div", { className: "trace-technical-details" });
  detail.append(el("div", { className: "trace-technical-title", text: "技术详情" }));
  rows.forEach(([label, value]) => {
    const row = el("div", { className: "trace-technical-row" });
    row.append(el("span", { text: `${label}：` }), el("code", { text: value }));
    row.append(el("button", { className: "link-button", type: "button", text: "复制", dataset: { traceCopy: value } }));
    detail.append(row);
  });
  if (eventSeqs.length) {
    const row = el("div", { className: "trace-technical-row" });
    row.append(el("span", { text: "事件序号：" }), el("code", { text: eventSeqs.map((value) => `#${value}`).join(", ") }));
    detail.append(row);
  }
  parent.append(detail);
}

function renderActivityStep(step, groupIndex) {
  const expandedKey = `${groupIndex}:${step.ordinal}`;
  const expanded = state.traceExpandedActivity === expandedKey;
  const wrapper = el("div", { className: `trace-activity-step ${expanded ? "is-expanded" : ""}` });
  const button = el("button", { className: "trace-activity-step-toggle", type: "button", ariaExpanded: expanded, dataset: { traceActivityStep: expandedKey } });
  const marker = el("span", { className: `status-dot ${activityStatusClass(step.status)}`, ariaLabel: step.status_label || step.status });
  const body = el("span", { className: "trace-activity-step-body" });
  body.append(el("span", { className: "trace-activity-step-label", text: step.label || "未命名步骤" }), el("span", { className: "trace-activity-step-summary", text: step.summary || step.status_label || "" }));
  const meta = el("span", { className: "trace-activity-step-meta", text: formatTraceDuration(step.duration_ms) });
  button.append(marker, body, meta, icon(expanded ? "chevronDown" : "chevronRight"));
  wrapper.append(button);
  if (expanded) {
    const details = el("div", { className: "trace-activity-step-details" });
    if (step.warning) details.append(el("p", { className: "trace-activity-warning", text: step.warning }));
    if (Array.isArray(step.children) && step.children.length) {
      const children = el("ul", { className: "trace-activity-children" });
      step.children.forEach((child) => {
        const childStatus = child.status === "failed" ? "失败" : child.status === "running" ? "进行中" : child.warning ? "状态未知" : "已完成";
        children.append(el("li", { text: `${child.label || "内部步骤"} · ${childStatus}` }));
      });
      details.append(children);
    }
    appendCopyableTechnical(details, step.technical);
    wrapper.append(details);
  }
  return wrapper;
}

function renderActivityCard(group, index) {
  const card = el("article", { className: "trace-activity-card" });
  const header = el("header", { className: "trace-activity-card-header" });
  const title = el("div", { className: "trace-activity-card-title" });
  title.append(el("span", { className: "trace-activity-request", text: `请求 ${valueOrFallback(group.display_ordinal, "?")}` }), el("span", { className: "trace-activity-request-text", text: group.title || "未命名请求" }));
  const stateLine = el("div", { className: "trace-activity-card-state" });
  stateLine.append(el("span", { className: `status-dot ${activityStatusClass(group.status)}` }), el("span", { text: `${group.status_label || group.status} · ${formatTraceDuration(group.duration_ms)}` }));
  const technicalExpanded = state.traceExpandedTechnical === String(index);
  const headerActions = el("div", { className: "trace-activity-card-actions" });
  headerActions.append(stateLine, el("button", {
    className: "link-button trace-technical-toggle",
    type: "button",
    text: technicalExpanded ? "隐藏技术详情" : "技术详情",
    ariaExpanded: technicalExpanded,
    dataset: { traceTechnical: String(index) },
  }));
  header.append(title, headerActions); card.append(header);
  const chips = el("div", { className: "trace-activity-chips" });
  const counts = group.counts || {};
  const evidenceLabels = { none: "无", executed_only: "仅执行", structured: "结构化结果", verified: "已验证" };
  const contextPeak = group.context?.peak_tokens === null || group.context?.peak_tokens === undefined
    ? "—"
    : `${group.context.peak_tokens} tokens`;
  [["模型", counts.provider_turns], ["工具", counts.tools], ["Stata", counts.runs], ["证据", evidenceLabels[group.evidence_status] || "未知"], ["上下文", contextPeak]].forEach(([label, value]) => chips.append(el("span", { className: "trace-activity-chip", text: `${label} ${valueOrFallback(value, "0")}` })));
  card.append(chips);
  const steps = el("div", { className: "trace-activity-steps" });
  (Array.isArray(group.steps) ? group.steps : []).forEach((step) => steps.append(renderActivityStep(step, index)));
  if (!group.steps?.length) steps.append(el("p", { className: "empty-resource", text: "暂无可展示的活动步骤。" }));
  card.append(steps);
  if (technicalExpanded) appendCopyableTechnical(card, group.technical_ids);
  return card;
}

function renderActivityTimelineView() {
  const view = el("section", { className: "trace-page", ariaLabel: "Trace 活动时间线" });
  const actions = [el("button", { className: "secondary-button", type: "button", text: "返回对话", dataset: { navPage: "chat" } })];
  view.append(createViewHeader("活动时间线", `${state.traceActivityTotal || 0} 个请求`, actions, "trace-page-header"));
  const toolbar = el("div", { className: "trace-toolbar" });
  const tabs = el("div", { className: "trace-view-tabs", role: "tablist", ariaLabel: "Trace 视图" });
  [["activity", "活动时间线"], ["technical", "技术审计"]].forEach(([value, label]) => tabs.append(el("button", { className: `trace-view-tab${state.traceView === value ? " is-current" : ""}`, type: "button", text: label, dataset: { traceView: value }, ariaCurrent: state.traceView === value ? "page" : undefined })));
  toolbar.append(tabs);
  const filterLabels = { all: "全部", model: "模型", tool: "工具与 Stata", run: "运行", evidence: "证据", approval: "审批", failure: "异常" };
  ["all", "model", "tool", "run", "evidence", "approval", "failure"].forEach((group) => toolbar.append(el("button", { className: `trace-filter${state.traceFilter === group ? " is-current" : ""}`, type: "button", text: filterLabels[group], dataset: { traceFilter: group } })));
  const search = el("label", { className: "trace-search" });
  search.append(icon("search"));
  const input = el("input", { value: state.traceQuery, ariaLabel: "搜索 Trace 活动" });
  input.type = "search"; input.placeholder = "搜索请求、工具、运行或错误"; input.dataset.traceSearch = "true";
  search.append(input); toolbar.append(search); view.append(toolbar);
  const wrap = el("div", { className: "trace-activity-list" });
  if (!state.traceActivityItems.length) wrap.append(el("div", { className: "trace-empty-state" }, [el("p", { className: "empty-resource", text: "暂无活动记录。" }), state.traceActivityLegacyCount ? el("p", { className: "trace-muted", text: `${state.traceActivityLegacyCount} 条历史事件未关联到请求，可切换技术审计查看。` }) : null]));
  else state.traceActivityItems.forEach((group, index) => wrap.append(renderActivityCard(group, index)));
  const footer = el("div", { className: "trace-load" });
  if (state.traceActivityCursor) footer.append(el("button", { className: "secondary-button", type: "button", text: "加载更早请求", dataset: { action: "load-trace-activity" } }));
  if (state.traceActivityTruncated) footer.append(el("span", { className: "trace-muted", text: "部分历史事件按安全上限截断。" }));
  wrap.append(footer); view.append(wrap);
  return view;
}

function renderTraceView() {
  return state.traceView === "technical" ? renderTechnicalTraceView() : renderActivityTimelineView();
}

async function openEvidence(cardId, claim = null) {
  const overlay = $("#evidence-overlay");
  const content = $("#evidence-content");
  if (!overlay || !content || !cardId) return;
  activateOverlay(overlay, "evidence-overlay", "#close-evidence-button");
  clear(content); content.append(el("p", { className: "empty-resource", text: "正在读取证据链…" }));
  state.selectedEvidence = { cardId, claim };
  try {
    const detail = await getJSON(workspaceURL(`/api/cards/${encodeURIComponent(cardId)}`));
    state.selectedEvidence.detail = detail;
    renderEvidenceSheet(detail, claim);
  } catch (error) { clear(content); content.append(el("p", { className: "traceability-warning", text: safeFailureMessage(error, "request_failed") })); }
}

function appendSheetField(parent, label, value, mono = false) {
  const row = el("div", { className: "sheet-field" });
  row.append(el("dt", { text: label }), el("dd", { className: mono ? "mono" : "", text: valueOrFallback(value) }));
  parent.append(row);
}

function renderEvidenceSheet(detail, claim) {
  const content = $("#evidence-content");
  if (!content) return;
  clear(content);
  const value = cardValue(detail);
  if (value !== null && value !== undefined) content.append(el("p", { className: "sheet-value", text: formatNumber(value) }));
  if (claim?.statement) content.append(el("p", { className: "sheet-statement", text: claim.statement }));
  const runId = detail.run_id || detail.locator?.run_id;
  const chain = [claim?.claim_id, detail.card_id, runId].filter(Boolean).join(" → ");
  if (chain) content.append(el("p", { className: "provenance-chain", text: chain }));
  if (detail.traceability_complete === false) content.append(el("p", { className: "traceability-warning", text: "当前证据卡没有可核对的运行记录。" }));
  const fields = el("dl", { className: "sheet-fields" });
  const provenance = detail.provenance && typeof detail.provenance === "object" ? detail.provenance : {};
  appendSheetField(fields, "代码位置", provenance.do_file ? `${provenance.do_file} · 行号尚未确定` : null, true);
  appendSheetField(fields, "数据指纹", provenance.data_signature || detail.machine_hash, true);
  appendSheetField(fields, "环境", provenance.env_sig);
  appendSheetField(fields, "签发者", detail.signed_by);
  content.append(fields);
  const viewRun = $("#view-run-button");
  const copy = $("#copy-citation-button");
  if (viewRun) { viewRun.disabled = !runId; viewRun.dataset.runId = runId || ""; }
  if (copy) copy.disabled = !chain;
}

function overlayFocusable(overlay) {
  return Array.from(overlay?.querySelectorAll("button:not([disabled]), [href], textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])") || [])
    .filter((node) => !node.hidden && node.getAttribute("aria-hidden") !== "true");
}

function activateOverlay(overlay, id, initialSelector = null) {
  if (!overlay) return;
  if (!state.overlayFocus) state.overlayFocus = {};
  if (!state.overlayFocus[id]) state.overlayFocus[id] = document.activeElement;
  overlay.hidden = false;
  const target = initialSelector ? $(initialSelector, overlay) : overlayFocusable(overlay)[0];
  window.setTimeout(() => (target || overlay).focus?.(), 0);
}

function trapOverlayFocus(event) {
  if (event.key !== "Tab") return;
  const selector = ["#evidence-overlay", "#decision-overlay"].find((candidateSelector) => {
    const candidate = $(candidateSelector);
    return candidate && !candidate.hidden;
  });
  const overlay = selector ? $(selector) : null;
  if (!overlay) return;
  const focusable = overlayFocusable(overlay);
  if (!focusable.length) { event.preventDefault(); overlay.focus?.(); return; }
  const first = focusable[0]; const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

function closeOverlay(id) {
  const overlay = $(`#${id}`);
  if (overlay) overlay.hidden = true;
  if (id === "evidence-overlay") state.selectedEvidence = null;
  if (id === "decision-overlay") state.decisionContext = null;
  const previous = state.overlayFocus?.[id];
  if (previous && typeof previous.focus === "function" && previous.isConnected) window.setTimeout(() => previous.focus(), 0);
  if (state.overlayFocus) delete state.overlayFocus[id];
}

function openDecision(requestId, decision = "reject") {
  const overlay = $("#decision-overlay");
  if (!overlay) return;
  const record = approvalFor(requestId);
  state.decisionContext = { requestId, decision };
  const title = $("#decision-title");
  const eyebrow = overlay.querySelector(".eyebrow");
  const label = $(".dialog-label", overlay);
  const submit = $("#submit-decision-button");
  const modifying = decision === "modify";
  if (title) title.textContent = modifying ? "提出修改要求" : "说明你的拒绝决定";
  if (eyebrow) eyebrow.textContent = modifying ? "审批修订" : "研究闸门";
  if (label) label.textContent = modifying ? "具体修改要求（必填）" : "给 Agent 的说明（可选）";
  if (submit) submit.textContent = modifying ? "提交修改" : "提交拒绝";
  const description = $("#decision-description");
  if (description) description.textContent = modifying
    ? record ? `请写清楚对“${approvalSubject(record)}”需要怎样调整；要求会写入用户事件和审批审计，并从同一停点继续。` : "请写清楚需要调整的研究动作。"
    : record ? `你将拒绝“${approvalSubject(record)}”。研究材料与 Trace 不会丢失。` : "你将拒绝这项研究动作。";
  const note = $("#decision-note");
  const error = $("#decision-note-error");
  const status = $("#decision-status");
  const dialog = $(".dialog", overlay);
  if (note) {
    note.value = "";
    note.required = modifying;
    note.setAttribute("aria-required", String(modifying));
    note.setAttribute("aria-invalid", "false");
  }
  if (error) error.textContent = "";
  if (status) status.textContent = "";
  dialog?.classList.remove("is-submitting");
  activateOverlay(overlay, "decision-overlay", "#decision-note");
}

async function submitDecision(requestId, decision, note = "") {
  const modifying = decision === "modify";
  const noteElement = $("#decision-note");
  const errorElement = $("#decision-note-error");
  const statusElement = $("#decision-status");
  const dialog = $("#decision-overlay .dialog");
  const actions = $$("#decision-overlay .dialog-actions button");
  if (modifying && !String(note || "").trim()) {
    if (noteElement) {
      noteElement.required = true;
      noteElement.setAttribute("aria-required", "true");
      noteElement.setAttribute("aria-invalid", "true");
      noteElement.focus();
    }
    if (errorElement) errorElement.textContent = "请填写具体修改要求后再提交。";
    return false;
  }
  state.decisionSubmitting = true;
  actions.forEach((button) => { button.disabled = true; });
  if (dialog) dialog.classList.add("is-submitting");
  if (statusElement) statusElement.textContent = "正在保存审批决定…";
  if (errorElement) errorElement.textContent = "";
  try {
    const result = await postJSON(workspaceURL(`/api/approvals/${encodeURIComponent(requestId)}/decision`), { decision, note });
    if (result?.state) state.snapshot = result.state;
    if (decision === "approve" || decision === "modify") {
      try { await postJSON(workspaceURL("/api/control/resume")); }
      catch (error) {
        setFailure(error, "approval_failed");
        showToast("审批已记录，但续跑请求未完成。", "approval_failed");
      }
    }
    closeOverlay("decision-overlay"); await refresh({ silent: true });
    showToast(decision === "approve" ? "审批已批准，已尝试从当前停点继续。" : decision === "modify" ? "修改要求已记录，已尝试从当前停点继续。" : "审批已拒绝，决定已持久化。");
    return true;
  } catch (error) {
    setFailure(error, "approval_failed");
    if (statusElement) statusElement.textContent = "";
    if (errorElement) errorElement.textContent = "审批操作未完成，请重试或下载诊断包。";
    if (noteElement && modifying) noteElement.setAttribute("aria-invalid", "true");
    showToast("审批操作未完成，请重试或下载诊断包。", "approval_failed");
    return false;
  } finally {
    state.decisionSubmitting = false;
    if (dialog) dialog.classList.remove("is-submitting");
    actions.forEach((button) => { button.disabled = false; });
  }
}

async function loadTraceActivity({ reset = false, renderAfter = false } = {}) {
  state.traceController?.abort();
  if (reset) {
    state.traceActivityItems = [];
    state.traceActivityCursor = null;
    state.traceExpandedActivity = null;
    state.traceExpandedTechnical = null;
  }
  const controller = new AbortController();
  state.traceController = controller;
  const generation = state.workspaceGeneration;
  const workspace = state.activeWorkspace;
  const params = [`limit=20`, `ws=${encodeURIComponent(workspace)}`];
  if (state.traceQuery) params.push(`search=${encodeURIComponent(state.traceQuery)}`);
  if (state.traceActivityCursor) params.push(`before_seq=${encodeURIComponent(state.traceActivityCursor)}`);
  if (state.traceFilter && state.traceFilter !== "all") params.push(`category=${encodeURIComponent(state.traceFilter)}`);
  try {
    const body = await getJSON(`/api/trace/activity?${params.join("&")}`, { signal: controller.signal });
    if (generation !== state.workspaceGeneration || workspace !== state.activeWorkspace) return;
    const rows = Array.isArray(body?.items) ? body.items : [];
    state.traceActivityItems = reset ? rows : state.traceActivityItems.concat(rows);
    state.traceActivityTotal = Number(body?.total_groups ?? state.traceActivityItems.length);
    state.traceActivityCursor = body?.next_before_seq ?? null;
    state.traceActivityLegacyCount = Number(body?.legacy_uncorrelated_count || 0);
    state.traceActivityTruncated = Boolean(body?.truncated);
    if (renderAfter) render();
  } catch (error) {
    if (error?.name !== "AbortError") {
      setFailure(error, "network_error");
      showToast("活动时间线暂时不可用，请重试或切换技术审计。", "network_error");
    }
  } finally {
    if (state.traceController === controller) state.traceController = null;
  }
}

function updateStopControls() {
  const button = $("[data-action='stop']");
  if (button) {
    button.disabled = Boolean(state.stopPending) || state.runState === "disconnected";
    button.textContent = state.stopPending ? "正在停止…" : state.runState === "cancelling" ? "等待停止…" : "停止";
    button.setAttribute("aria-label", state.stopPending ? "正在请求停止本轮运行" : state.runState === "cancelling" ? "等待服务端确认停止" : "停止本轮运行");
  }
  const hint = $(".composer-hint");
  if (hint && isRunActive()) {
    hint.textContent = state.stopPending ? "正在请求停止…" : state.runState === "cancelling" ? "等待服务端确认停止…" : state.runState === "disconnected" ? "连接中断，正在确认状态…" : "正在处理研究指示…";
  }
}

function wait(ms) { return new Promise((resolve) => window.setTimeout(resolve, ms)); }

function finalizeRun(status, generation = state.workspaceGeneration) {
  if (!isCurrentWorkspaceGeneration(generation)) return false;
  const terminal = terminalStateFor(status);
  if (status === "cancelled" || status === "canceled") {
    state.lastFailure = normalizeFailure({ code: "run_cancelled" }, "run_cancelled");
  } else if (status === "uncertain") {
    state.lastFailure = normalizeFailure({ code: "uncertain" }, "uncertain");
  } else if (status === "paused") {
    state.lastFailure = normalizeFailure({ code: "context_budget" }, "context_budget");
  } else if (status === "failed") {
    state.lastFailure = normalizeFailure({ code: "request_failed" }, "request_failed");
  }
  setRunState(terminal, generation);
  state.stopStatus = status;
  state.stopPending = false;
  state.activeRequestId = null;
  render();
  state.streamingText = "";
  state.streamingTools = [];
  return true;
}

async function waitForTerminalConfirmation(requestId, generation, workspace, maxAttempts = 24) {
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (!isCurrentWorkspaceGeneration(generation, workspace)) return false;
    try {
      const snapshot = await getJSON(`/api/state?ws=${encodeURIComponent(workspace)}`);
      if (!isCurrentWorkspaceGeneration(generation, workspace)) return false;
      state.snapshot = snapshot;
      const status = snapshot?.run_status;
      const serverRequestId = snapshot?.request_id || snapshot?.active_request_id;
      if (serverRequestId && requestId && String(serverRequestId) !== String(requestId)) {
        await wait(250);
        continue;
      }
      // Do not treat the default idle projection as proof that a request has
      // stopped. A non-idle terminal status is the server acknowledgement.
      if (serverStatusIsTerminal(status) && status !== "idle") {
        finalizeRun(status, generation);
        return true;
      }
    } catch (error) {
      if (error?.name !== "AbortError" && isCurrentWorkspaceGeneration(generation, workspace)) setFailure(error, "network_error", generation);
    }
    await wait(250);
  }
  return false;
}

async function stopMessage() {
  if (!isRunActive() || state.stopPending) return;
  const requestId = state.activeRequestId;
  if (!requestId) {
    setRunState("disconnected");
    setFailure({ code: "stream_disconnected" }, "stream_disconnected");
    state.chatController?.abort();
    render();
    return;
  }
  const generation = state.workspaceGeneration;
  const workspace = state.activeWorkspace;
  state.stopPending = true;
  setRunState("cancelling", generation);
  updateStopControls();
  try {
    const result = await postJSON(workspaceURL("/api/control/stop"), { request_id: requestId });
    state.stopStatus = result?.status || "cancelling";
    state.stopPending = false;
    updateStopControls();
    if (["cancelled", "canceled", "completed", "failed", "uncertain"].includes(result?.status)) {
      finalizeRun(result.status, generation);
      showToast(result.status === "cancelled" || result.status === "canceled" ? "本轮已停止。" : "本轮已结束。", result.status === "failed" ? "request_failed" : "run_cancelled");
      return;
    }
    showToast("已请求停止本轮运行。正在收尾并保存可审计状态。", "run_cancelled");
    // The server has received the cancellation request.  Closing the stream
    // now is safe only because the client remains in `cancelling` until the
    // server snapshot confirms a terminal state.
    state.chatController?.abort();
    void waitForTerminalConfirmation(requestId, generation, workspace);
  } catch (error) {
    state.stopPending = false;
    updateStopControls();
    setFailure(error, "request_failed", generation);
    showToast("停止请求未完成，请重试。", "request_failed");
  }
}

async function uploadAttachments(fileList) {
  const files = Array.from(fileList || []).slice(0, 8);
  if (!files.length) return;
  if (Array.from(fileList || []).length > 8) showToast("一次最多选择 8 个 PDF；已处理前 8 个。");
  state.attachmentController?.abort();
  const controller = new AbortController();
  state.attachmentController = controller;
  const generation = state.workspaceGeneration;
  for (const file of files) {
    if (controller.signal.aborted || generation !== state.workspaceGeneration) break;
    if (!String(file.name || "").toLowerCase().endsWith(".pdf")) {
      showToast(`${file.name || "文件"} 不是 PDF，已跳过。`);
      continue;
    }
    const idempotency = globalThis.crypto?.randomUUID?.() || `upload-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    try {
      const response = await fetch(workspaceURL("/api/attachments"), {
        method: "POST",
        headers: {
          "Content-Type": file.type || "application/pdf",
          "X-Attachment-Filename": encodeURIComponent(file.name),
          "X-Attachment-Role": state.attachmentRole,
          "X-Idempotency-Key": idempotency,
        },
        body: file,
        signal: controller.signal,
      });
      const payload = await jsonResponse(response);
      if (!isCurrentWorkspaceGeneration(generation)) break;
      const attachment = payload?.attachment;
      if (attachment) {
        const index = state.attachments.findIndex((item) => item.attachment_id === attachment.attachment_id);
        if (index >= 0) state.attachments[index] = attachment;
        else state.attachments.unshift(attachment);
        if (attachment.status === "ready" && !state.selectedAttachmentIds.includes(attachment.attachment_id)) {
          state.selectedAttachmentIds.push(attachment.attachment_id);
        }
        if (attachment.status !== "ready") showToast(`${attachment.display_name || "PDF 附件"}：${attachment.status || "不可用"}（${attachment.error_code || "不可用"}）`, "attachment_failed");
        render();
      }
    } catch (error) {
      if (error?.name !== "AbortError" && isCurrentWorkspaceGeneration(generation)) {
        setFailure(error, "attachment_failed", generation);
        showToast("附件上传未完成，请重试或刷新状态。", "attachment_failed");
        render();
      }
    }
  }
  if (state.attachmentController === controller) state.attachmentController = null;
}

function retryLastMessage() {
  if (!state.lastMessageText) return;
  if (!canSend()) {
    showToast("当前运行尚未进入终态，请等待服务端确认后再重试。", "request_failed");
    return;
  }
  state.draftText = state.lastMessageText;
  state.lastFailure = null;
  render();
  void sendMessage();
}

async function sendMessage() {
  const input = $("[data-composer-input]");
  const text = (input?.value || state.draftText).trim();
  if (!text) return;
  if (!canSend()) {
    showToast("当前运行尚未进入终态，请等待服务端确认后再发送。", "request_failed");
    return;
  }
  const generation = state.workspaceGeneration;
  const workspace = state.activeWorkspace;
  const controller = new AbortController();
  state.chatController?.abort();
  state.chatController = controller;
  state.lastMessageText = text;
  state.draftText = "";
  setRunState("running", generation);
  state.streamingText = "";
  state.streamingRendered = "";
  state.streamingTools = [];
  state.activeRequestId = null;
  state.stopPending = false;
  state.stopStatus = null;
  state.lastFailure = null;
  render();
  let streamTerminal = false;
  let rafId = null;
  try {
    const res = await fetch(workspaceURL("/api/chat/stream"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        mode: state.goalMode ? "goal" : "interactive",
        attachment_ids: state.selectedAttachmentIds.slice(0, 8),
      }),
      signal: controller.signal,
    });
    if (!res.ok) {
      let payload = null;
      try { payload = await res.json(); } catch { /* non-JSON proxy failure */ }
      const failure = normalizeFailure(payload, `http_${res.status}`, res.status);
      const error = new Error(failure.message);
      error.failure = failure;
      throw error;
    }
    if (!res.body) {
      const failure = normalizeFailure({ code: "stream_disconnected" }, "stream_disconnected");
      const error = new Error(failure.message);
      error.failure = failure;
      throw error;
    }
    state.activeRequestId = res.headers.get("x-request-id") || null;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    // 流式渲染：token 只进入单一 buffer；DOM 更新始终使用 textContent。
    const streamLoop = () => {
      if (state.streamingText !== state.streamingRendered) {
        state.streamingRendered = state.streamingText;
        const node = $("[data-streaming-text]");
        if (node) {
          node.textContent = state.streamingText + "▌";
          node.style.color = "var(--text)";
          const scroll = $(".chat-scroll");
          if (scroll) scroll.scrollTop = scroll.scrollHeight;
        } else render();
      }
      rafId = isRunActive() ? requestAnimationFrame(streamLoop) : null;
    };
    rafId = requestAnimationFrame(streamLoop);
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = raw.split("\n").find((entry) => entry.startsWith("data: "));
        if (!line) continue;
        let data; try { data = JSON.parse(line.slice(6)); } catch { continue; }
        if (!isCurrentWorkspaceGeneration(generation, workspace)) return;
        if (data.request_id) state.activeRequestId = String(data.request_id);
        if (data.type === "token") {
          state.streamingText += String(data.text || "");
        } else if (data.type === "tool_started") {
          state.streamingTools.push({
            tool_id: String(data.tool_id || data.call_id || `${data.request_id || state.activeRequestId || "request"}:tool:${state.streamingTools.length + 1}`),
            tool_name: String(data.tool || data.name || "未知工具"),
            status: "running",
          });
          renderStreamingTools();
        } else if (data.type === "tool_completed" || data.type === "tool_failed") {
          const toolId = data.tool_id || data.call_id ? String(data.tool_id || data.call_id) : "";
          let item = toolId ? state.streamingTools.find((tool) => tool.tool_id === toolId) : null;
          if (!item) item = state.streamingTools.slice().reverse().find((tool) => tool.tool_name === String(data.tool || data.name || "未知工具") && tool.status === "running");
          if (!item) {
            item = { tool_id: toolId || `${data.request_id || state.activeRequestId || "request"}:tool:${state.streamingTools.length + 1}`, tool_name: String(data.tool || data.name || "未知工具") };
            state.streamingTools.push(item);
          }
          item.status = data.status || (data.ok === false ? "failed" : "succeeded");
          item.ok = data.ok !== undefined ? Boolean(data.ok) : item.status === "succeeded";
          item.error = data.error ? normalizeFailure(data.error, "request_failed") : null;
          renderStreamingTools();
        } else if (data.type === "done") {
          if (data.state) state.snapshot = data.state;
          state.stopStatus = data.status || "completed";
          state.selectedAttachmentIds = [];
          streamTerminal = true;
          if (["failed", "uncertain", "paused"].includes(data.status)) {
            const fallback = data.status === "uncertain" ? "uncertain" : data.status === "paused" ? "context_budget" : "request_failed";
            setFailure(data.error || data, fallback, generation);
            setRunState(data.status, generation);
          } else if (data.status === "cancelled" || data.status === "canceled") {
            state.lastFailure = normalizeFailure({ code: "run_cancelled" }, "run_cancelled");
            setRunState("cancelled", generation);
          } else setRunState("completed", generation);
        } else if (data.type === "error") {
          streamTerminal = true;
          setFailure(data.error || data, "request_failed", generation);
          setRunState("failed", generation);
        }
      }
    }
    if (!streamTerminal) {
      setRunState("disconnected", generation);
      setFailure({ code: "stream_disconnected" }, "stream_disconnected", generation);
      void waitForTerminalConfirmation(state.activeRequestId, generation, workspace);
    } else await refresh({ silent: true });
  } catch (error) {
    if (!isCurrentWorkspaceGeneration(generation, workspace)) return;
    if (error?.name === "AbortError" && ["cancelling", "disconnected"].includes(state.runState)) return;
    if (error?.name === "AbortError") {
      setRunState("disconnected", generation);
      setFailure({ code: "stream_disconnected" }, "stream_disconnected", generation);
      void waitForTerminalConfirmation(state.activeRequestId, generation, workspace);
      return;
    }
    setFailure(error, error?.failure?.code === "stream_disconnected" ? "stream_disconnected" : "request_failed", generation);
    setRunState("failed", generation);
    state.draftText = text;
    showToast("本轮未完成，请重试或下载诊断包。", "request_failed");
  } finally {
    if (rafId) cancelAnimationFrame(rafId);
    if (state.chatController === controller) state.chatController = null;
    if (!isCurrentWorkspaceGeneration(generation, workspace)) return;
    // Cancelling/disconnected are deliberately non-terminal. The busy card,
    // tail text and retry/diagnostics actions remain until confirmation.
    if (isRunActive()) {
      render();
      return;
    }
    const node = $("[data-streaming-text]");
    if (node) {
      node.textContent = state.streamingText;
      const article = node.closest(".message");
      if (article) article.classList.remove("message-busy");
    }
    state.stopPending = false;
    state.activeRequestId = null;
    render();
    state.streamingText = "";
    state.streamingTools = [];
  }
}

async function resumeResearch() {
  $$('[data-action="resume"]').forEach((button) => { button.disabled = true; });
  const checkpoint = hasDurableCheckpoint();
  try { await postJSON(workspaceURL("/api/control/resume")); await refresh({ silent: true }); showToast(checkpoint ? "已从当前停点请求续跑。" : "已请求继续处理。"); }
  catch (error) { setFailure(error, "request_failed"); showToast("继续处理未完成，请重试或下载诊断包。", "request_failed"); render(); }
  finally { $$('[data-action="resume"]').forEach((button) => { button.disabled = false; }); }
}

function setPage(page) {
  const next = page || "chat";
  if (state.page === "settings" && next !== "settings" && state.settingsDirty) {
    if (!window.confirm("设置尚有未保存更改，离开后将放弃这些更改。继续吗？")) return;
    discardSettings();
  }
  state.page = next;
  writeStorage("stata-agent.page", state.page);
  setSidebarOpen(false);
  render();
  if (state.page === "trace") {
    if (state.traceView === "technical") loadTrace({ reset: true, renderAfter: true });
    else loadTraceActivity({ reset: true, renderAfter: true });
  }
  if (state.page === "settings") loadSettings();
}

async function createWorkspace() {
  const name = window.prompt("给新工作区起一个名字", "新研究工作区");
  if (name === null) return;
  const clean = name.trim();
  if (!clean) { showToast("工作区名称不能为空。"); return; }
  try {
    const result = await postJSON("/api/workspaces", { name: clean });
    const id = result?.workspace?.id;
    if (id) switchWorkspace(id);
    showToast("已创建新工作区。");
  } catch (error) { showToast("新建工作区失败，请重试。", "request_failed"); }
}

function bindEvents() {
  $("#menu-button")?.addEventListener("click", () => {
    if (window.matchMedia("(max-width: 700px)").matches) setSidebarOpen(!state.sidebarOpen);
    else setSidebarCollapsed(!state.sidebarCollapsed);
  });
  $("#new-workspace-button")?.addEventListener("click", createWorkspace);
  $("#trace-button")?.addEventListener("click", () => setPage("trace"));
  $("#top-draft-button")?.addEventListener("click", () => {
    if (!state.snapshot?.draft_ready) { showToast("尚无已确认结论，暂不能生成 Word 初稿。"); return; }
    window.location.assign(workspaceURL("/api/draft.docx"));
  });
  $("#workspace-context")?.addEventListener("click", () => { setSidebarCollapsed(false); setSidebarOpen(true); });
  $("#user-button")?.addEventListener("click", () => setPage("settings"));
  $("#workspace-sidebar")?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target || target.closest("#workspace-list")) return;
    const nav = target.closest("[data-nav-page]");
    if (nav) { setPage(nav.dataset.navPage); }
  });
  $("#workspace-list")?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const nav = target.closest("[data-nav-page]");
    if (nav) { setPage(nav.dataset.navPage); return; }
    const row = target.closest("[data-workspace-id]");
    if (!row) return;
    const id = row.dataset.workspaceId;
    if (!id || id === state.activeWorkspace) { setSidebarCollapsed(false); return; }
    switchWorkspace(id);
  });
  $("#view-root")?.addEventListener("input", (event) => {
    const target = event.target instanceof HTMLTextAreaElement ? event.target : null;
    if (target?.dataset.composerInput !== undefined) state.draftText = target.value;
    const setting = event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement ? event.target : null;
    if (setting?.dataset.settingKey) {
      const value = setting instanceof HTMLInputElement && setting.type === "checkbox" ? setting.checked : setting.type === "number" ? (setting.value === "" ? "" : Number(setting.value)) : setting.value;
      setSettingsDraft(setting.dataset.settingKey, value);
      return;
    }
    if (event.target?.dataset.traceSearch !== undefined) {
      state.traceQuery = event.target.value.trim();
      window.clearTimeout(state.traceSearchTimer);
      state.traceSearchTimer = window.setTimeout(() => {
        if (state.traceView === "technical") loadTrace({ reset: true, renderAfter: true });
        else loadTraceActivity({ reset: true, renderAfter: true });
      }, 260);
    }
  });
  $("#view-root")?.addEventListener("change", (event) => {
    const target = event.target instanceof HTMLInputElement ? event.target : null;
    const setting = event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement ? event.target : null;
    if (setting?.dataset.settingsCredentialProvider !== undefined) {
      state.settingsCredentialProvider = setting.value;
      render();
      return;
    }
    if (setting?.dataset.settingKey) {
      const value = setting instanceof HTMLInputElement && setting.type === "checkbox" ? setting.checked : setting.type === "number" ? (setting.value === "" ? "" : Number(setting.value)) : setting.value;
      setSettingsDraft(setting.dataset.settingKey, value);
      return;
    }
    if (target?.dataset.settingsBackupInput !== undefined) {
      const file = target.files?.[0] || null;
      target.value = "";
      verifySettingsBackup(file);
      return;
    }
    if (target?.dataset.attachmentInput !== undefined) {
      uploadAttachments(target.files);
      target.value = "";
    }
  });
  $("#view-root")?.addEventListener("keydown", (event) => {
    const target = event.target instanceof HTMLElement ? event.target : null;
    if (target?.dataset.composerInput !== undefined && event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); return; }
    if (target?.dataset.traceSearch !== undefined && event.key === "Enter") {
      event.preventDefault();
      if (state.traceView === "technical") loadTrace({ reset: true, renderAfter: true });
      else loadTraceActivity({ reset: true, renderAfter: true });
    }
    const activityStep = target?.closest("[data-trace-activity-step]");
    if (activityStep && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); activityStep.click(); }
    const row = target?.closest("[data-trace-seq]");
    if (row && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); row.click(); }
  });
  $("#view-root")?.addEventListener("submit", (event) => { if (event.target?.dataset.composer) { event.preventDefault(); sendMessage(); } });
  $("#view-root")?.addEventListener("click", async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const prompt = target.closest("[data-prompt]");
    if (prompt) { state.draftText = prompt.dataset.prompt || ""; render(); $("[data-composer-input]")?.focus(); return; }
    const nav = target.closest("[data-nav-page]");
    if (nav) { setPage(nav.dataset.navPage); return; }
    const traceView = target.closest("[data-trace-view]");
    if (traceView) {
      state.traceView = traceView.dataset.traceView === "technical" ? "technical" : "activity";
      writeStorage("stata-agent.trace-view", state.traceView);
      const allowedFilters = state.traceView === "technical"
        ? new Set(["all", "approval", "run", "conclusion", "health"])
        : new Set(["all", "model", "tool", "run", "evidence", "approval", "failure"]);
      if (!allowedFilters.has(state.traceFilter)) {
        state.traceFilter = "all";
        writeStorage("stata-agent.trace-filter", state.traceFilter);
      }
      state.traceExpandedActivity = null;
      state.traceExpandedTechnical = null;
      if (state.traceView === "technical") loadTrace({ reset: true, renderAfter: true });
      else loadTraceActivity({ reset: true, renderAfter: true });
      return;
    }
    const filter = target.closest("[data-trace-filter]");
    if (filter) {
      state.traceFilter = filter.dataset.traceFilter || "all";
      writeStorage("stata-agent.trace-filter", state.traceFilter);
      state.traceExpandedSeq = null;
      state.traceExpandedActivity = null;
      state.traceExpandedTechnical = null;
      if (state.traceView === "technical") loadTrace({ reset: true, renderAfter: true });
      else loadTraceActivity({ reset: true, renderAfter: true });
      return;
    }
    const activityStep = target.closest("[data-trace-activity-step]");
    if (activityStep) {
      state.traceExpandedActivity = state.traceExpandedActivity === activityStep.dataset.traceActivityStep ? null : activityStep.dataset.traceActivityStep;
      render();
      return;
    }
    const technicalToggle = target.closest("[data-trace-technical]");
    if (technicalToggle) {
      const key = technicalToggle.dataset.traceTechnical || "";
      state.traceExpandedTechnical = state.traceExpandedTechnical === key ? null : key;
      render();
      return;
    }
    const copyButton = target.closest("[data-trace-copy]");
    if (copyButton) {
      const value = copyButton.dataset.traceCopy || "";
      if (!value) return;
      try { await navigator.clipboard.writeText(value); showToast("已复制技术标识。"); }
      catch { showToast("当前浏览器不允许访问剪贴板，请展开技术详情手动记录。"); }
      return;
    }
    const approvalLink = target.closest("[data-approval-id]");
    if (approvalLink && !target.closest("[data-decision]")) {
      setPage("chat");
      window.setTimeout(() => {
        const targetMessage = Array.from($$("[data-approval-id]"), (item) => item).find((item) => item.dataset.approvalId === approvalLink.dataset.approvalId);
        targetMessage?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 0);
      return;
    }
    const cardButton = target.closest("[data-card-id]");
    if (cardButton) {
      const cardId = cardButton.dataset.cardId;
      const claim = (state.snapshot?.claim_records || []).find((item) => item.claim_id === cardButton.dataset.claimId || item.cards?.includes(cardId));
      openEvidence(cardId, claim); return;
    }
    const decision = target.closest("[data-decision]");
    if (decision) {
      const requestId = decision.dataset.requestId;
      if (decision.dataset.decision === "approve") submitDecision(requestId, "approve");
      else if (decision.dataset.decision === "reject") openDecision(requestId, "reject");
      else if (decision.dataset.decision === "modify") openDecision(requestId, "modify");
      return;
    }
    const runButton = target.closest("[data-run-id]");
    if (runButton) {
      const card = cardsForRun(runButton.dataset.runId)[0];
      if (card) openEvidence(card.card_id); else showToast("该运行暂时没有可打开的证据卡。");
      return;
    }
    const action = target.closest("[data-action]");
    if (action) {
      const name = action.dataset.action;
      if (name === "toggle-mode") { state.goalMode = !state.goalMode; render(); }
      else if (name === "toggle-attachment-role") {
        state.attachmentRole = state.attachmentRole === "citable_evidence" ? "style_only" : "citable_evidence";
        render();
      }
      else if (name === "select-attachments") $(`[data-attachment-input]`)?.click();
      else if (name === "remove-attachment") {
        state.selectedAttachmentIds = state.selectedAttachmentIds.filter((id) => id !== action.dataset.attachmentId);
        render();
      }
      else if (name === "stop") stopMessage();
      else if (name === "retry-message") retryLastMessage();
      else if (name === "refresh-state") refresh({ silent: false });
      else if (name === "resume") resumeResearch();
      else if (name === "generate-draft") { if (state.snapshot?.draft_ready) window.location.assign(workspaceURL("/api/draft.docx")); else showToast("尚无已确认结论。"); }
      else if (name === "load-trace") { loadTrace({ reset: false, renderAfter: true }); }
      else if (name === "load-trace-activity") { loadTraceActivity({ reset: false, renderAfter: true }); }
      else if (name === "download-diagnostics") {
        const request = state.activeRequestId ? `request_id=${encodeURIComponent(state.activeRequestId)}` : "";
        window.location.assign(workspaceURL("/api/operations/diagnostics/bundle", request));
      }
      else if (name === "settings-refresh") loadSettings({ force: true });
      else if (name === "settings-save") saveSettings();
      else if (name === "settings-discard") discardSettings();
      else if (name === "settings-reset-context") resetContextSettings();
      else if (name === "settings-backup") downloadSettingsBackup();
      else if (name === "settings-verify-backup") { $("[data-settings-backup-input]")?.click(); }
      else if (name === "settings-diagnostics") {
        const request = state.activeRequestId ? `request_id=${encodeURIComponent(state.activeRequestId)}` : "";
        window.location.assign(workspaceURL("/api/operations/diagnostics/bundle", request));
      }
      return;
    }
    const secretSave = target.closest("[data-settings-secret-save]");
    if (secretSave) {
      const provider = secretSave.dataset.settingsSecretSave;
      settingsSecretAction(provider, $(`[data-secret-input="${provider}"]`), false);
      return;
    }
    const secretDelete = target.closest("[data-settings-secret-delete]");
    if (secretDelete) {
      const provider = secretDelete.dataset.settingsSecretDelete;
      if (window.confirm(`删除 ${provider} 凭据？`)) settingsSecretAction(provider, null, true);
      return;
    }
    const check = target.closest("[data-settings-check]");
    if (check) { runSettingsCheck(check.dataset.settingsCheck); return; }
    const traceRow = target.closest("[data-trace-seq]");
    if (traceRow) { state.traceExpandedSeq = String(state.traceExpandedSeq) === String(traceRow.dataset.traceSeq) ? null : traceRow.dataset.traceSeq; render(); }
  });
  $("#close-evidence-button")?.addEventListener("click", () => closeOverlay("evidence-overlay"));
  $("#close-decision-button")?.addEventListener("click", () => closeOverlay("decision-overlay"));
  $("#cancel-decision-button")?.addEventListener("click", () => closeOverlay("decision-overlay"));
  $("#submit-decision-button")?.addEventListener("click", () => {
    const context = state.decisionContext;
    if (context) submitDecision(context.requestId, context.decision || "reject", $("#decision-note")?.value.trim() || "");
  });
  $("#view-run-button")?.addEventListener("click", () => { closeOverlay("evidence-overlay"); setPage("models"); });
  $("#copy-citation-button")?.addEventListener("click", async () => {
    const detail = state.selectedEvidence?.detail; const claim = state.selectedEvidence?.claim;
    const citation = [claim?.claim_id, detail?.card_id, detail?.run_id || detail?.locator?.run_id].filter(Boolean).join(" → ");
    if (!citation) return;
    try { await navigator.clipboard.writeText(citation); showToast("已复制引用标识。"); } catch { showToast("当前浏览器不允许访问剪贴板，请手动记录引用标识。"); }
  });
  $$('[data-close-overlay]')?.forEach((button) => button.addEventListener("click", () => closeOverlay(button.dataset.closeOverlay)));
  $("#refresh-notice")?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-action]") : null;
    if (!target) return;
    if (target.dataset.action === "refresh-state") refresh({ silent: false });
    else if (target.dataset.action === "download-diagnostics") {
      const request = state.activeRequestId ? `request_id=${encodeURIComponent(state.activeRequestId)}` : "";
      window.location.assign(workspaceURL("/api/operations/diagnostics/bundle", request));
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Tab") { trapOverlayFocus(event); return; }
    if (event.key !== "Escape") return;
    closeOverlay("evidence-overlay"); closeOverlay("decision-overlay"); setSidebarOpen(false);
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh({ silent: true }); });
}

bindEvents();
applyImmediateSettings();
setSidebarCollapsed(state.sidebarCollapsed);
refresh();
loadSettings();
window.addEventListener("beforeunload", (event) => {
  if (!state.settingsDirty) return;
  event.preventDefault();
  event.returnValue = "有未保存的设置更改。";
});
