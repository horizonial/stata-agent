/*
 * Stata 研究助手的本地、无依赖前端。
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
  uncertain: "状态不确定",
  paused: "已暂停",
  complete: "完成",
};

const EVENT_GROUPS = {
  approval: new Set(["approval.requested", "approval.granted", "approval.rejected"]),
  run: new Set(["run.requested", "tool.call", "tool.result", "run.succeeded", "run.failed", "run.uncertain"]),
  conclusion: new Set(["spec.proposed", "spec.frozen", "spec.locked", "family.main_result_selected", "evidence.card_signed", "claim.signed"]),
  health: new Set(["budget.limit", "health.probe"]),
};

const NAV_GROUPS = [
  {
    label: "研究",
    items: [
      ["chat", "对话", "message"],
      ["settings", "设定", "settings"],
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

const state = {
  activeWorkspace: readStorage("stata-agent.active-workspace", "ui"),
  sidebarCollapsed: readStorage("stata-agent.sidebar-collapsed", "false") === "true",
  sidebarOpen: false,
  page: "chat",
  workspaces: [],
  snapshot: null,
  events: [],
  traceItems: [],
  traceTotal: 0,
  traceCursor: null,
  traceFilter: "all",
  traceQuery: "",
  traceExpandedSeq: null,
  goalMode: false,
  draftText: "",
  streamingText: "",
  streamingRendered: "",
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

function el(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined && options.text !== null) element.textContent = String(options.text);
  if (options.type) element.type = options.type;
  if (options.value !== undefined) element.value = options.value;
  if (options.disabled !== undefined) element.disabled = Boolean(options.disabled);
  if (options.hidden !== undefined) element.hidden = Boolean(options.hidden);
  if (options.title !== undefined) element.title = String(options.title);
  if (options.role) element.setAttribute("role", options.role);
  if (options.ariaLabel) element.setAttribute("aria-label", String(options.ariaLabel));
  if (options.ariaExpanded !== undefined) element.setAttribute("aria-expanded", String(options.ariaExpanded));
  if (options.ariaControls) element.setAttribute("aria-controls", String(options.ariaControls));
  if (options.ariaCurrent !== undefined) element.setAttribute("aria-current", String(options.ariaCurrent));
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
  if (["failed", "paused"].includes(status)) return "is-error";
  if (["busy", "validating", "awaiting_user"].includes(status)) return "is-busy";
  if (["complete", "idle"].includes(status)) return "is-ok";
  return "";
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

function showToast(message) {
  const toast = $("#toast");
  if (!toast) return;
  toast.textContent = String(message);
  toast.hidden = false;
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => { toast.hidden = true; }, 4200);
}

async function jsonResponse(response) {
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const detail = body && body.detail ? body.detail : `请求失败（${response.status}）`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

async function getJSON(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { Accept: "application/json", ...(options.headers || {}) } });
  return jsonResponse(response);
}

async function postJSON(url, body) {
  return getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
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
  state.chatController = null;
  state.traceController = null;
  state.refreshController = null;
  state.sending = false;
  state.streamingText = "";
  state.events = [];
  state.traceItems = [];
  state.traceCursor = null;
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
    const [workspaceBody, snapshot, eventBody] = await Promise.all([
      getJSON("/api/workspaces", { signal }),
      getJSON(`/api/state?${query}`, { signal }),
      getJSON(`/api/events?${query}&limit=100`, { signal }),
    ]);
    if (workspace !== state.activeWorkspace) return;
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
    mergeEvents(Array.isArray(eventBody) ? eventBody : eventBody?.items || []);
    state.refreshFailed = false;
    // 数据没变就跳过 render，避免每次轮询 clear+重建导致的闪烁/输入丢失。
    // 关键：sending（流式进行中）期间绝不 render——流式渲染由 sendMessage 的 flush
    // 局部更新负责，这里只更新 state.snapshot 数据；否则全量重建会把流式文本"快进"成整段。
    const fingerprint = _fingerprint();
    if (!state.sending && fingerprint !== state.lastFingerprint) {
      render();
      state.lastFingerprint = fingerprint;
    }
    if (state.page === "trace") await loadTrace({ reset: true, renderAfter: true });
  } catch (error) {
    if (error?.name !== "AbortError") {
      state.refreshFailed = true;
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
    goal: state.goalMode,
    workspaces: (state.workspaces || []).map((w) => `${w.id}:${w.events}:${w.run_status}`).join("|"),
  });
}

function scheduleRefresh() {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  if (document.hidden) return;
  const active = state.sending || ["busy", "validating"].includes(state.snapshot?.run_status);
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
  const status = snapshot.run_status || record?.run_status || "idle";
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
  if (!messages.length && !state.sending) column.append(renderEmptyChat());
  messages.forEach((message, index) => column.append(renderMessage(message, index === messages.length - 1)));
  if (state.sending) column.append(renderBusyMessage());
  scroll.append(column);
  view.append(scroll, renderComposer());
  window.setTimeout(() => { if (state.sending || messages.length) scroll.scrollTop = scroll.scrollHeight; }, 0);
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

function renderComposer() {
  const form = el("form", { className: "composer", dataset: { composer: "true" } });
  const shell = el("div", { className: "composer-shell" });
  const textarea = el("textarea", { value: state.draftText, ariaLabel: "给研究助手发消息", dataset: { composerInput: "true" } });
  textarea.rows = 2;
  textarea.maxLength = 20000;
  textarea.placeholder = "给研究助手发消息…";
  const footer = el("div", { className: "composer-footer" });
  const hint = el("span", { className: `composer-hint${state.sending ? " is-busy" : ""}`, text: state.sending ? "正在提交研究指示…" : "Enter 发送 · Shift+Enter 换行" });
  const actions = el("div", { className: "composer-actions" });
  const mode = el("button", { className: `topbar-button mode-button${state.goalMode ? " is-active" : ""}`, type: "button", text: state.goalMode ? "目标模式" : "交互模式", dataset: { action: "toggle-mode" }, title: state.goalMode ? "目标模式：自动推进到需你决定处" : "交互模式：每轮在需要你决定处停下" });
  const attach = el("button", { className: "icon-button", type: "button", ariaLabel: "附件暂不可用", disabled: true, title: "当前后端暂不支持附件" });
  attach.append(icon("paperclip"));
  const send = el("button", { className: "primary-button", type: "submit", disabled: state.sending });
  send.append(icon("send"), el("span", { text: "发送" }));
  actions.append(mode, attach, send);
  footer.append(hint, actions);
  shell.append(textarea, footer);
  form.append(shell);
  return form;
}

function renderMarkdown(md) {
  // 极简安全 markdown 渲染：纯 DOM 构建（createTextNode/el，天然防 XSS）。
  // 处理：标题 #/##/###、无序列表 -/*、有序列表 1.、代码块 ```、加粗 **、行内代码 `。
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
  meta.append(el("span", { className: "message-author", text: role === "user" ? "你" : role === "system" ? "系统" : "Stata 研究助手" }));
  if (message.created_at) meta.append(el("span", { text: formatTime(message.created_at) }));
  if (message.seq !== null && message.seq !== undefined) meta.append(el("span", { className: "message-seq", text: `#${message.seq}` }));
  return meta;
}

function renderMessage(message, latest = false) {
  const role = message.role === "user" ? "user" : message.role === "system" ? "system" : "assistant";
  const article = el("article", { className: `message message-${role}${latest ? " is-latest" : ""}`, dataset: { seq: message.seq ?? "" } });
  article.append(el("span", { className: "message-avatar", text: role === "user" ? "R" : role === "system" ? "·" : "S" }));
  const body = el("div", { className: "message-body" });
  body.append(messageMeta(message, role));
  if (message.kind === "approval") body.append(renderApproval(message));
  else if (message.kind === "run") {
    body.append(el("p", { className: "message-text", text: message.text || "运行完成，机器层结果已签入证据链。" }));
    body.append(renderResultBlock(message));
  } else if (message.kind === "error") {
    const notice = el("section", { className: `inline-notice ${message.status === "paused" ? "is-warning" : "is-error"}` });
    notice.append(icon(message.status === "paused" ? "health" : "alert"));
    const text = el("div");
    text.append(el("p", { className: "notice-title", text: message.status === "paused" ? "研究已暂停" : "主回归未完成" }), el("p", { text: message.text || "请查看最近运行记录。" }));
    if (message.detail) text.append(el("p", { className: "message-details-note", text: message.detail }));
    if (message.status === "paused") text.append(el("button", { className: "link-button", type: "button", text: "从停点续跑", dataset: { action: "resume" } }));
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

function renderBusyMessage() {
  const article = el("article", { className: "message message-assistant message-busy" });
  article.append(el("span", { className: "message-avatar", text: "S" }));
  const body = el("div", { className: "message-body" });
  body.append(messageMeta({ created_at: Date.now() }, "assistant"));
  // 流式文本放一个固定 data 属性节点，供 delta 到达时局部更新 textContent（不全量重建）
  const p = el("p", { className: "message-text", dataset: { streamingText: "true" } });
  p.textContent = state.streamingText || "正在处理你的研究指示…";
  body.append(p);
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
  top.append(el("span", { className: "approval-icon" }, [icon(permission ? "health" : "approval")]), el("span", { className: "approval-title", text: `${pending ? "需要你确认" : record.status === "approved" ? "已批准" : "已拒绝"} · ${approvalSubject(message)}` }));
  if (!pending) top.append(el("span", { className: "approval-status", text: record.status === "approved" ? "已记录" : "已记录" }));
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
    settings: ["设定", "当前工作区的研究设定（只读投影）"],
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
  const intro = el("p", { className: "page-intro", text: "内容来自当前工作区的只读状态投影；改变研究状态请回到对话中提出指示。" });
  pageElement.append(intro);
  if (page === "settings") pageElement.append(renderSettingsResource());
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

function fieldList(fields) {
  const list = el("dl", { className: "field-list" });
  fields.forEach(([label, value, mono]) => {
    const row = el("div", { className: "field-row" });
    row.append(el("dt", { text: label }), el("dd", { className: mono ? "mono" : "", text: valueOrFallback(value) }));
    list.append(row);
  });
  return list;
}

function renderSettingsResource() {
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
    const dot = el("span", { className: `status-dot ${record.status === "approved" ? "is-ok" : record.status === "rejected" ? "is-error" : "is-busy"}` });
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
  if (status === "paused" || status === "failed") content.append(el("button", { className: "secondary-button", type: "button", text: "从停点续跑", dataset: { action: "resume" } }));
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
    if (error?.name !== "AbortError") showToast(`Trace 暂时不可用：${error.message}`);
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

function renderTraceView() {
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

async function openEvidence(cardId, claim = null) {
  const overlay = $("#evidence-overlay");
  const content = $("#evidence-content");
  if (!overlay || !content || !cardId) return;
  overlay.hidden = false; clear(content); content.append(el("p", { className: "empty-resource", text: "正在读取证据链…" }));
  state.selectedEvidence = { cardId, claim };
  try {
    const detail = await getJSON(workspaceURL(`/api/cards/${encodeURIComponent(cardId)}`));
    state.selectedEvidence.detail = detail;
    renderEvidenceSheet(detail, claim);
  } catch (error) { clear(content); content.append(el("p", { className: "traceability-warning", text: `证据详情暂时不可用：${error.message}` })); }
  $("#close-evidence-button")?.focus();
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

function closeOverlay(id) {
  const overlay = $(`#${id}`);
  if (overlay) overlay.hidden = true;
  if (id === "evidence-overlay") state.selectedEvidence = null;
  if (id === "decision-overlay") state.decisionContext = null;
}

function openDecision(requestId) {
  const overlay = $("#decision-overlay");
  if (!overlay) return;
  const record = approvalFor(requestId);
  state.decisionContext = { requestId };
  const description = $("#decision-description");
  if (description) description.textContent = record ? `你将拒绝“${approvalSubject(record)}”。研究材料与 Trace 不会丢失。` : "你将拒绝这项研究动作。";
  const note = $("#decision-note");
  if (note) note.value = "";
  overlay.hidden = false; note?.focus();
}

async function submitDecision(requestId, decision, note = "") {
  $$('[data-decision]').forEach((button) => { button.disabled = true; });
  try {
    const result = await postJSON(workspaceURL(`/api/approvals/${encodeURIComponent(requestId)}/decision`), { decision, note });
    if (result?.state) state.snapshot = result.state;
    if (decision === "approve") {
      try { await postJSON(workspaceURL("/api/control/resume")); } catch (error) { showToast(`审批已批准，但续跑尚未完成：${error.message}`); }
    }
    closeOverlay("decision-overlay"); await refresh({ silent: true });
    showToast(decision === "approve" ? "审批已批准，已尝试从当前停点继续。" : "审批已拒绝，决定已持久化。");
  } catch (error) {
    showToast(`提交审批失败：${error.message}`);
    $$('[data-decision]').forEach((button) => { button.disabled = false; });
  }
}

async function sendMessage() {
  const input = $("[data-composer-input]");
  const text = (input?.value || state.draftText).trim();
  if (!text || state.sending) return;
  const generation = state.workspaceGeneration;
  const workspace = state.activeWorkspace;
  const controller = new AbortController();
  state.chatController?.abort();
  state.chatController = controller;
  state.draftText = ""; state.sending = true; state.streamingText = ""; state.streamingRendered = ""; render();
  try {
    const res = await fetch(workspaceURL("/api/chat/stream"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, mode: state.goalMode ? "goal" : "interactive" }),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) throw new Error(`请求失败(${res.status})`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    // 流式渲染（codex 式）：token 直接 append 到单一 buffer `streamingText`；
    // 一个 rAF 渲染循环每帧把 buffer 局部同步到 busy 节点 textContent，绝不 render() 全量重建。
    let rafId = null;
    const streamLoop = () => {
      if (state.streamingText !== state.streamingRendered) {
        state.streamingRendered = state.streamingText;
        const node = $("[data-streaming-text]");
        if (node) {
          node.textContent = state.streamingText + "▌";
          node.style.color = "var(--text)";  // 流式文本用正式颜色，非灰色占位
          const scroll = $(".chat-scroll");
          if (scroll) scroll.scrollTop = scroll.scrollHeight;  // 滚动跟随
        } else render();  // 首帧：节点还没建，全量建一次
      }
      rafId = state.sending ? requestAnimationFrame(streamLoop) : null;
    };
    rafId = requestAnimationFrame(streamLoop);
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = raw.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let data; try { data = JSON.parse(line.slice(6)); } catch { continue; }
        if (generation !== state.workspaceGeneration || workspace !== state.activeWorkspace) return;
        if (data.type === "token") {
          state.streamingText += data.text;  // 直接 append，rAF 循环负责渲染
        } else if (data.type === "tool_started") {
          state.streamingText += `\n▸ ${data.name}…\n`;
        } else if (data.type === "tool_completed") {
          state.streamingText += data.ok ? "  ✓ 完成\n" : "  ✕ 失败\n";
        } else if (data.type === "done") {
          if (data.state) state.snapshot = data.state;
        } else if (data.type === "error") {
          throw new Error(data.detail);
        }
      }
    }
    if (rafId) cancelAnimationFrame(rafId);
    // 最后一次同步（去掉光标），再拉正式 state
    const node = $("[data-streaming-text]");
    if (node) node.textContent = state.streamingText;
    await refresh({ silent: true });
  } catch (error) {
    if (error?.name !== "AbortError" && generation === state.workspaceGeneration) {
      state.draftText = text;
      showToast(`本轮未完成：${error.message}`);
    }
  }
  finally {
    if (state.chatController === controller) state.chatController = null;
    if (generation === state.workspaceGeneration) {
      // 就地转正：把流式 busy 消息原地转成正式消息（去光标、去灰色 class），
      // 不 clear+重建整个视图，避免 done 瞬间的"页面跳一下"。
      const node = $("[data-streaming-text]");
      if (node) {
        node.textContent = state.streamingText;  // 去掉光标 ▌
        const article = node.closest(".message");
        if (article) article.classList.remove("message-busy");
      }
      state.sending = false;
      state.streamingText = "";
      // 只恢复 composer（发送按钮可点、提示复原），不整页重绘
      const send = $(".composer .primary-button[type='submit']");
      if (send) send.disabled = false;
      const hint = $(".composer-hint");
      if (hint) { hint.textContent = "Enter 发送 · Shift+Enter 换行"; hint.classList.remove("is-busy"); }
    }
  }
}

async function resumeResearch() {
  $$('[data-action="resume"]').forEach((button) => { button.disabled = true; });
  try { await postJSON(workspaceURL("/api/control/resume")); await refresh({ silent: true }); showToast("已从当前停点请求续跑。"); }
  catch (error) { showToast(`续跑未完成：${error.message}`); }
  finally { $$('[data-action="resume"]').forEach((button) => { button.disabled = false; }); }
}

function setPage(page) {
  state.page = page || "chat";
  setSidebarOpen(false);
  render();
  if (state.page === "trace") loadTrace({ reset: true, renderAfter: true });
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
  } catch (error) { showToast(`新建工作区失败：${error.message}`); }
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
  $("#user-button")?.addEventListener("click", () => showToast("当前为本机研究工作区，用户设置将在后续版本开放。"));
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
    if (event.target?.dataset.traceSearch !== undefined) {
      state.traceQuery = event.target.value.trim();
      window.clearTimeout(state.traceSearchTimer);
      state.traceSearchTimer = window.setTimeout(() => loadTrace({ reset: true, renderAfter: true }), 260);
    }
  });
  $("#view-root")?.addEventListener("keydown", (event) => {
    const target = event.target instanceof HTMLElement ? event.target : null;
    if (target?.dataset.composerInput !== undefined && event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); return; }
    if (target?.dataset.traceSearch !== undefined && event.key === "Enter") { event.preventDefault(); loadTrace({ reset: true, renderAfter: true }); }
    const row = target?.closest("[data-trace-seq]");
    if (row && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); row.click(); }
  });
  $("#view-root")?.addEventListener("submit", (event) => { if (event.target?.dataset.composer) { event.preventDefault(); sendMessage(); } });
  $("#view-root")?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const prompt = target.closest("[data-prompt]");
    if (prompt) { state.draftText = prompt.dataset.prompt || ""; render(); $("[data-composer-input]")?.focus(); return; }
    const nav = target.closest("[data-nav-page]");
    if (nav) { setPage(nav.dataset.navPage); return; }
    const filter = target.closest("[data-trace-filter]");
    if (filter) {
      state.traceFilter = filter.dataset.traceFilter || "all";
      state.traceExpandedSeq = null;
      loadTrace({ reset: true, renderAfter: true });
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
      else if (decision.dataset.decision === "reject") openDecision(requestId);
      else if (decision.dataset.decision === "modify") { state.draftText = `请修改“${approvalSubject(approvalFor(requestId) || { act: "研究动作" })}”的要求。`; setPage("chat"); render(); $("[data-composer-input]")?.focus(); }
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
      else if (name === "resume") resumeResearch();
      else if (name === "generate-draft") { if (state.snapshot?.draft_ready) window.location.assign(workspaceURL("/api/draft.docx")); else showToast("尚无已确认结论。"); }
      else if (name === "load-trace") { loadTrace({ reset: false, renderAfter: true }); }
      return;
    }
    const traceRow = target.closest("[data-trace-seq]");
    if (traceRow) { state.traceExpandedSeq = String(state.traceExpandedSeq) === String(traceRow.dataset.traceSeq) ? null : traceRow.dataset.traceSeq; render(); }
  });
  $("#close-evidence-button")?.addEventListener("click", () => closeOverlay("evidence-overlay"));
  $("#close-decision-button")?.addEventListener("click", () => closeOverlay("decision-overlay"));
  $("#cancel-decision-button")?.addEventListener("click", () => closeOverlay("decision-overlay"));
  $("#submit-decision-button")?.addEventListener("click", () => { const context = state.decisionContext; if (context) submitDecision(context.requestId, "reject", $("#decision-note")?.value.trim() || ""); });
  $("#view-run-button")?.addEventListener("click", () => { closeOverlay("evidence-overlay"); setPage("models"); });
  $("#copy-citation-button")?.addEventListener("click", async () => {
    const detail = state.selectedEvidence?.detail; const claim = state.selectedEvidence?.claim;
    const citation = [claim?.claim_id, detail?.card_id, detail?.run_id || detail?.locator?.run_id].filter(Boolean).join(" → ");
    if (!citation) return;
    try { await navigator.clipboard.writeText(citation); showToast("已复制引用标识。"); } catch { showToast("当前浏览器不允许访问剪贴板，请手动记录引用标识。"); }
  });
  $$('[data-close-overlay]')?.forEach((button) => button.addEventListener("click", () => closeOverlay(button.dataset.closeOverlay)));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeOverlay("evidence-overlay"); closeOverlay("decision-overlay"); setSidebarOpen(false);
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh({ silent: true }); });
}

bindEvents();
setSidebarCollapsed(state.sidebarCollapsed);
refresh();
