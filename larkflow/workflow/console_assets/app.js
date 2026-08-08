"use strict";

const THEME_STORAGE_KEY = "larkflow.console.theme";
const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

function storedTheme() {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch (_error) {
    return null;
  }
}

function applyTheme(theme, persist = false) {
  const resolved = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved;
  document.querySelectorAll(".theme-toggle").forEach((button) => {
    const isDark = resolved === "dark";
    button.setAttribute("aria-pressed", String(isDark));
    button.setAttribute("aria-label", isDark ? "切换到浅色模式" : "切换到深色模式");
    button.title = isDark ? "切换到浅色模式" : "切换到深色模式";
    button.querySelector(".theme-toggle-icon").textContent = isDark ? "☾" : "☀";
    button.querySelector(".theme-toggle-label").textContent = isDark ? "深色" : "浅色";
  });
  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, resolved);
    } catch (_error) {
      // The theme still applies when browser storage is unavailable.
    }
  }
}

applyTheme(storedTheme() || (systemTheme.matches ? "dark" : "light"));
systemTheme.addEventListener("change", (event) => {
  if (!storedTheme()) applyTheme(event.matches ? "dark" : "light");
});

const state = {
  token: sessionStorage.getItem("larkflow.console.token") || "",
  authMode: "unknown",
  loginUrl: "/console/auth/login",
  instances: [],
  attention: [],
  humanTasks: [],
  activeTask: null,
  detail: null,
  isAdmin: false,
  view: "owner",
  adminOverview: null,
  adminSessions: null,
  adminSessionPreview: null,
  selectedNode: null,
  graphScale: 1,
  ownerSection: "attention",
  returnSection: "attention",
  detailTab: "overview",
  workflowFilter: "all",
  workflowQuery: "",
  expandedAttentionKinds: new Set(),
};

const el = (id) => document.getElementById(id);
const unlock = el("unlock");
const app = el("app");
const unlockForm = el("unlock-form");
const tokenInput = el("access-token");
const unlockError = el("unlock-error");
const unlockCopy = el("unlock-copy");
const feishuLogin = el("feishu-login");
const authNote = el("auth-note");
const instanceList = el("instance-list");
const emptyState = el("empty-state");
const detailView = el("detail");
const attentionCenter = el("attention-center");
const workflowLibrary = el("workflow-library");
const attentionNav = el("attention-nav");
const workflowNav = el("workflow-nav");
const workspace = document.querySelector(".workspace");
const adminConsole = el("admin-console");
const ownerViewButton = el("owner-view");
const adminViewButton = el("admin-view");
const humanTaskDialog = el("human-task-dialog");

document.querySelectorAll(".theme-toggle").forEach((button) => {
  button.addEventListener("click", () => {
    const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(nextTheme, true);
  });
});

const STATUS = {
  draft: "草稿",
  running: "进行中",
  paused: "已暂停",
  done: "已完成",
  failed: "失败",
  canceled: "已取消",
  discarded: "已废弃",
  pending: "等待依赖",
  ready: "待调度",
  waiting_human: "等待人工",
};

const EXECUTOR = { human: "Human", agent: "Agent", tool: "Tool" };
const RELATION = { you: "你", collaborator: "协作者", system: "系统" };
const ATTENTION = {
  recover_failed: "失败恢复",
  complete_human: "等待你处理",
  resume_flow: "已暂停",
  confirm_draft: "待确认",
};
const ATTENTION_DESCRIPTION = {
  recover_failed: "先确认影响范围，再决定是否重新执行",
  complete_human: "这些节点正在等待你的输入或判断",
  resume_flow: "流程已暂停，需要你决定是否继续",
  confirm_draft: "核对目标和节点后，直接在本页确认启动",
};
const QUEUE_LANE = {
  outbox: "外部投影",
  inbox: "任务事件入站",
  im_commands: "飞书命令",
  im_replies: "命令回复",
  role_actions: "人员分工动作",
  role_replies: "人员分工回复",
  role_progress: "草稿生成进度",
};
const SESSION_RELATION = {
  you: "你的会话",
  member: "其他成员会话",
};
const EVENT = {
  "instance.draft_created": "流程草稿已创建",
  "instance.confirmed": "流程已确认启动",
  "instance.completed": "流程已完成",
  "instance.discarded": "流程草稿已废弃",
  "instance.graph_edited": "流程图已更新",
  "instance.node_restarted": "节点已重新执行",
  "instance.restarted": "完整流程已重新执行",
  "node.activated": "节点已激活",
  "node.automated_completed": "自动执行已完成",
  "node.automated_failed": "自动执行失败",
  "node.automated_retry_started": "自动执行已重试",
  "node.claim_recovered": "执行租约已恢复",
  "node.claim_renewed": "执行租约已续期",
  "node.human_submitted": "人工结果已提交",
  "node.human_task_transferred": "人工待办已转交",
  "node.human_decision_accepted": "人工决定已接受",
  "node.human_decision_rejected": "人工决定已退回",
  "node.human_takeover_started": "人工接管已开始",
};

const GRAPH_MIN_SCALE = 0.5;
const GRAPH_MAX_SCALE = 1.6;
const GRAPH_SCALE_STEP = 0.1;
let graphDrag = null;
let suppressGraphClick = false;

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}

function formatDate(value, includeTime = true) {
  if (!value) return "尚未发生";
  const options = includeTime
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Shanghai" }
    : { year: "numeric", month: "2-digit", day: "2-digit", timeZone: "Asia/Shanghai" };
  return new Intl.DateTimeFormat("zh-CN", options).format(new Date(value));
}

function statusLabel(value) {
  return STATUS[value] || value || "未知";
}

function setConnection(mode, text) {
  const connection = el("connection");
  connection.dataset.mode = mode;
  connection.querySelector("span").textContent = text;
}

async function request(path, options = {}) {
  setConnection("loading", "同步中");
  let response;
  const method = options.method || "GET";
  const headers = { ...(options.headers || {}) };
  if (state.authMode === "static" && state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }
  if (method !== "GET" && method !== "HEAD") {
    headers["X-Larkflow-Console-Action"] = path.startsWith("/console/api/v1/admin/")
      ? "session-governance-v1"
      : "workflow-action-v1";
  }
  try {
    response = await fetch(path, {
      method,
      headers,
      body: options.body,
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (error) {
    setConnection("error", "连接失败");
    throw new Error("无法连接本机控制台服务");
  }
  const payload = await response.json();
  if (response.status === 401) {
    const error = new Error(
      state.authMode === "feishu"
        ? "飞书登录已过期，请重新进入"
        : "访问令牌无效或已经更新",
    );
    error.authentication = true;
    if (state.authMode === "feishu") showFeishuLogin(error.message);
    else lockConsole();
    throw error;
  }
  if (!response.ok) {
    setConnection("error", "读取失败");
    const error = new Error(payload.error?.message || "读取失败");
    error.code = payload.error?.code;
    throw error;
  }
  setConnection("ready", "已连接");
  return payload;
}

async function loadInstances(selectId = null) {
  const [payload, taskPayload] = await Promise.all([
    request("/console/api/v1/instances?limit=30"),
    request("/console/api/v1/tasks?limit=30"),
  ]);
  state.instances = payload.instances;
  state.humanTasks = taskPayload.tasks || [];
  const taskItems = state.humanTasks.map(humanTaskAttentionItem);
  const taskKeys = new Set(taskItems.map((item) => item.id));
  state.attention = [
    ...(payload.attention?.items || []).filter((item) => !taskKeys.has(item.id)),
    ...taskItems,
  ].sort((left, right) => {
    if (left.priority !== right.priority) return left.priority - right.priority;
    return String(right.occurred_at).localeCompare(String(left.occurred_at));
  });
  const counts = Object.fromEntries(Object.keys(ATTENTION).map((kind) => [
    kind,
    state.attention.filter((item) => item.kind === kind).length,
  ]));
  const attention = {
    items: state.attention,
    counts,
    total: state.attention.length,
    instance_limit: payload.attention?.instance_limit || 30,
  };
  renderInstances();
  renderAttention(attention);
  if (selectId) {
    await loadDetail(selectId);
  }
}

function humanTaskAttentionItem(task) {
  return {
    id: `complete_human:${task.instance_id}:${task.node.key}`,
    kind: "complete_human",
    priority: 1,
    instance_id: task.instance_id,
    goal: task.goal,
    instance_status: task.instance_status,
    title: `完成待办：${task.node.title}`,
    detail: task.instance_owner_relation === "you"
      ? "该普通 Human 节点正在等待你的输入，可在本页提交或转交。"
      : "你是该协作流程当前节点的负责人，可在本页提交或转交。",
    occurred_at: task.started_at,
    node: { key: task.node.key, title: task.node.title },
    action: { kind: "human_task", node_key: task.node.key },
  };
}

async function loadAdminOverview() {
  if (!state.isAdmin) return;
  const [overview, sessions] = await Promise.all([
    request("/console/api/v1/admin/overview"),
    request("/console/api/v1/admin/sessions?limit=100"),
  ]);
  state.adminOverview = overview;
  state.adminSessions = sessions;
  renderAdminOverview(overview);
  renderAdminSessions(sessions);
}

function renderAdminOverview(payload) {
  const metrics = el("admin-metrics");
  metrics.replaceChildren();
  const workflow = payload.workflows || {};
  const sessions = payload.sessions || {};
  const queues = payload.queues || {};
  [
    ["流程总数", workflow.total || 0, `${workflow.distinct_owners || 0} 位发起人`],
    ["有效会话", sessions.active || 0, `${sessions.active_people || 0} 位登录成员`],
    ["一小时内到期", sessions.expiring_within_hour || 0, "完成登录的浏览器会话"],
    ["需关注信号", queues.attention_total || 0, "失败、耗尽或过期租约"],
  ].forEach(([label, value, detail]) => {
    const card = node("article", "admin-metric-card");
    card.append(
      node("span", "admin-metric-label", label),
      node("strong", "admin-metric-value", value),
      node("small", "admin-metric-detail", detail),
    );
    metrics.append(card);
  });

  const statuses = el("admin-workflow-statuses");
  statuses.replaceChildren();
  Object.entries(workflow.by_status || {}).forEach(([status, count]) => {
    const item = node("div", "admin-status-row");
    item.append(statusBadge(status), node("strong", "", count));
    statuses.append(item);
  });

  const migrations = payload.migrations || {};
  const migrationState = el("admin-migration-state");
  migrationState.className = `status-pill ${migrations.up_to_date ? "status-done" : "status-failed"}`;
  migrationState.textContent = migrations.up_to_date ? "版本一致" : "需要检查";
  const migrationDetail = el("admin-migrations");
  migrationDetail.replaceChildren(
    fact("已应用", `${migrations.applied_count || 0}/${migrations.expected_count || 0}`),
    fact("最新版本", migrations.latest_applied || "无"),
    fact("缺失", migrations.missing_count || 0),
    fact("非预期", migrations.unexpected_count || 0),
  );

  const queueBody = el("admin-queue-body");
  queueBody.replaceChildren();
  (queues.lanes || []).forEach((lane) => {
    const row = node("tr", "");
    row.dataset.attention = String(
      lane.failed > 0 || lane.exhausted > 0 || lane.expired_claims > 0,
    );
    [
      QUEUE_LANE[lane.key] || lane.key,
      lane.total,
      lane.ready,
      lane.in_flight,
      lane.failed,
      lane.exhausted,
      lane.expired_claims,
      lane.oldest_ready_at ? formatDate(lane.oldest_ready_at) : "无",
    ].forEach((value, index) => {
      const cell = node(index === 0 ? "th" : "td", "", value);
      if (index === 0) cell.scope = "row";
      row.append(cell);
    });
    queueBody.append(row);
  });
  el("admin-queue-attention").textContent = queues.attention_total || 0;
  el("admin-generated-at").textContent = `数据生成于 ${formatDate(payload.generated_at)}`;
}

function renderAdminSessions(payload) {
  const list = el("admin-session-list");
  const auditList = el("admin-session-audit-list");
  list.replaceChildren();
  auditList.replaceChildren();
  el("admin-session-count").textContent = payload.total || 0;

  (payload.sessions || []).forEach((session) => {
    const item = node("article", "admin-session-item");
    item.dataset.current = String(session.current === true);
    const copy = node("div", "admin-session-copy");
    const heading = node("div", "admin-session-heading");
    heading.append(
      node("strong", "", SESSION_RELATION[session.relation] || "企业成员会话"),
      node("span", "mono", `尾号 ${session.id.slice(-8)}`),
    );
    const details = node("div", "admin-session-details");
    details.append(
      fact("创建", formatDate(session.created_at)),
      fact("到期", formatDate(session.expires_at)),
    );
    copy.append(heading, details);
    const action = node(
      "button",
      session.current ? "admin-session-current" : "admin-session-revoke",
      session.current ? "当前会话" : "撤销会话",
    );
    action.type = "button";
    action.disabled = session.current || session.revocable !== true;
    if (!action.disabled) {
      action.addEventListener("click", () => createSessionRevocationPreview(session, action));
    }
    item.append(copy, action);
    list.append(item);
  });
  if ((payload.sessions || []).length === 0) {
    list.append(node("p", "admin-session-empty", "当前企业没有有效登录会话。"));
  }

  (payload.recent_revocations || []).forEach((event) => {
    const item = node("div", "admin-session-audit-item");
    item.append(
      node(
        "span",
        "",
        `${event.actor_relation === "you" ? "你" : "另一管理员"}撤销了${event.target_relation === "you" ? "你的" : "其他成员的"}会话`,
      ),
      node("code", "", event.target_session_id.slice(-8)),
      node("time", "", formatDate(event.occurred_at)),
    );
    auditList.append(item);
  });
  if ((payload.recent_revocations || []).length === 0) {
    auditList.append(node("p", "admin-session-empty", "还没有管理员会话撤销记录。"));
  }
}

async function createSessionRevocationPreview(session, button) {
  button.disabled = true;
  button.dataset.state = "working";
  button.textContent = "创建预览中";
  try {
    const preview = await request(
      `/console/api/v1/admin/sessions/${encodeURIComponent(session.id)}/revoke-preview`,
      { method: "POST" },
    );
    state.adminSessionPreview = preview;
    renderSessionRevocationPreview(preview);
    button.dataset.state = "done";
    button.textContent = "等待确认";
  } catch (error) {
    button.dataset.state = "error";
    button.textContent = sessionActionError(error);
    setTimeout(() => {
      button.dataset.state = "idle";
      button.textContent = "撤销会话";
      button.disabled = false;
    }, 1800);
  }
}

function renderSessionRevocationPreview(preview) {
  const panel = el("admin-session-preview");
  panel.replaceChildren();
  const copy = node("div", "admin-session-preview-copy");
  copy.append(
    node("strong", "", "确认撤销这条登录会话？"),
    node(
      "p",
      "",
      `${SESSION_RELATION[preview.target.relation] || "企业成员会话"}，尾号 ${preview.target.id.slice(-8)}，到期时间 ${formatDate(preview.target.expires_at)}。确认后该浏览器需要重新登录。`,
    ),
    node("small", "", `预览有效至 ${formatDate(preview.expires_at)}`),
  );
  const actions = node("div", "admin-session-preview-actions");
  const cancel = node("button", "admin-session-preview-cancel", "取消");
  cancel.type = "button";
  cancel.addEventListener("click", clearSessionRevocationPreview);
  const confirm = node("button", "admin-session-preview-confirm", "确认撤销");
  confirm.type = "button";
  confirm.addEventListener("click", () => confirmSessionRevocation(preview, confirm, cancel));
  actions.append(cancel, confirm);
  panel.append(copy, actions);
  panel.hidden = false;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearSessionRevocationPreview() {
  state.adminSessionPreview = null;
  const panel = el("admin-session-preview");
  panel.hidden = true;
  panel.replaceChildren();
  if (state.adminSessions) renderAdminSessions(state.adminSessions);
}

async function confirmSessionRevocation(preview, confirm, cancel) {
  confirm.disabled = true;
  cancel.disabled = true;
  confirm.dataset.state = "working";
  confirm.textContent = "正在撤销";
  try {
    await request(
      `/console/api/v1/admin/session-revocations/${encodeURIComponent(preview.preview_id)}/confirm`,
      { method: "POST" },
    );
    confirm.dataset.state = "done";
    confirm.textContent = "已撤销";
    state.adminSessionPreview = null;
    await loadAdminOverview();
    setTimeout(() => {
      const panel = el("admin-session-preview");
      panel.hidden = true;
      panel.replaceChildren();
    }, 900);
  } catch (error) {
    confirm.dataset.state = "error";
    confirm.textContent = sessionActionError(error);
    cancel.disabled = false;
    setTimeout(() => {
      confirm.dataset.state = "idle";
      confirm.textContent = "重新确认";
      confirm.disabled = false;
    }, 1800);
  }
}

function sessionActionError(error) {
  if (["preview_expired", "preview_stale"].includes(error.code)) return "状态已变化";
  if (error.code === "session_conflict") return "当前会话不可撤销";
  if (error.code === "request_rejected") return "请求已拒绝";
  return "操作失败";
}

function showOwnerView() {
  state.view = "owner";
  workspace.hidden = false;
  adminConsole.hidden = true;
  ownerViewButton.dataset.active = "true";
  adminViewButton.dataset.active = "false";
  showOwnerSection(state.ownerSection);
}

async function showAdminView() {
  if (!state.isAdmin) return;
  adminViewButton.disabled = true;
  adminViewButton.textContent = "读取中";
  try {
    await loadAdminOverview();
    state.view = "admin";
    workspace.hidden = true;
    adminConsole.hidden = false;
    ownerViewButton.dataset.active = "false";
    adminViewButton.dataset.active = "true";
    adminViewButton.textContent = "管理概览";
  } catch (error) {
    adminViewButton.textContent = "读取失败";
    showWorkspaceError(error);
    setTimeout(() => { adminViewButton.textContent = "管理概览"; }, 1600);
  } finally {
    adminViewButton.disabled = false;
  }
}

function showOwnerSection(section) {
  state.ownerSection = section;
  attentionCenter.hidden = section !== "attention";
  workflowLibrary.hidden = section !== "workflows";
  detailView.hidden = section !== "detail";
  emptyState.hidden = section !== "loading";
  attentionNav.dataset.active = String(section === "attention");
  workflowNav.dataset.active = String(section === "workflows");
  if (section === "detail" && state.detailTab === "execution") {
    requestAnimationFrame(layoutGraphCanvas);
  }
  window.scrollTo({ top: 0, behavior: "auto" });
}

function showDetailLoading(goal) {
  emptyState.querySelector("h2").textContent = "正在读取流程";
  emptyState.querySelector("p:last-child").textContent = goal || "请稍候，中央节点正在返回最新状态。";
  showOwnerSection("loading");
}

function showToast(message, tone = "success") {
  const toast = el("toast");
  toast.textContent = message;
  toast.dataset.tone = tone;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 2400);
}

function setDetailTab(tab) {
  state.detailTab = tab;
  el("detail-tabs").querySelectorAll("button").forEach((button) => {
    const active = button.dataset.tab === tab;
    button.dataset.active = String(active);
    button.setAttribute("aria-selected", String(active));
  });
  el("detail-overview-panel").hidden = tab !== "overview";
  el("detail-execution-panel").hidden = tab !== "execution";
  el("detail-audit-panel").hidden = tab !== "audit";
  if (tab === "execution") requestAnimationFrame(layoutGraphCanvas);
}

function renderAttention(attention) {
  const list = el("attention-list");
  const summary = el("attention-summary");
  list.replaceChildren();
  summary.replaceChildren();
  el("attention-count").textContent = attention.total ?? state.attention.length;
  el("attention-nav-count").textContent = attention.total ?? state.attention.length;
  el("attention-scope").textContent = `汇总最近 ${attention.instance_limit || 30} 个本人流程，以及当前分配给你的普通 Human 待办`;

  Object.entries(ATTENTION).forEach(([kind, label]) => {
    const count = attention.counts?.[kind] || 0;
    if (count < 1) return;
    const chip = node("span", `attention-summary-chip attention-kind-${kind}`);
    chip.append(
      node("strong", "", count),
      node("span", "", label),
      node("small", "", ATTENTION_DESCRIPTION[kind]),
    );
    summary.append(chip);
  });

  if (state.attention.length === 0) {
    const empty = node("div", "attention-empty");
    empty.append(
      node("strong", "", "当前没有需要你处理的流程"),
      node("p", "", "草稿确认、本人 Human 待办、暂停流程和失败恢复会出现在这里。"),
    );
    list.append(empty);
    return;
  }

  Object.keys(ATTENTION).forEach((kind) => {
    const items = state.attention.filter((item) => item.kind === kind);
    if (items.length === 0) return;
    const expanded = state.expandedAttentionKinds.has(kind);
    const visibleItems = expanded ? items : items.slice(0, 5);
    const group = node("section", `attention-group attention-kind-${kind}`);
    group.dataset.kind = kind;
    const heading = node("div", "attention-group-heading");
    const headingCopy = node("div", "attention-group-copy");
    headingCopy.append(
      node("strong", "", ATTENTION[kind]),
      node("span", "", ATTENTION_DESCRIPTION[kind]),
    );
    heading.append(headingCopy, node("span", "attention-group-count", items.length));
    const rows = node("div", "attention-group-rows");
    visibleItems.forEach((item) => rows.append(attentionCard(item)));
    group.append(heading, rows);
    if (items.length > 5) {
      const toggle = node(
        "button",
        "attention-group-toggle",
        expanded ? "收起" : `显示其余 ${items.length - 5} 项`,
      );
      toggle.type = "button";
      toggle.addEventListener("click", () => {
        if (expanded) state.expandedAttentionKinds.delete(kind);
        else state.expandedAttentionKinds.add(kind);
        renderAttention(attention);
      });
      group.append(toggle);
    }
    list.append(group);
  });
}

function attentionCard(item) {
  const card = node("article", `attention-card attention-kind-${item.kind}`);
  card.dataset.instanceId = item.instance_id;
  const marker = node("span", "attention-marker");
  marker.setAttribute("aria-hidden", "true");
  const copy = node("div", "attention-copy");
  const meta = node("div", "attention-meta");
  meta.append(
    node("span", "attention-kind", item.instance_status ? statusLabel(item.instance_status) : ATTENTION[item.kind] || item.kind),
    node("span", "attention-time", formatDate(item.occurred_at)),
  );
  copy.append(
    meta,
    node("strong", "attention-title", item.title),
    node("span", "attention-goal", item.goal || "未命名流程"),
    node("p", "attention-detail", item.detail),
  );

  const actions = node("div", "attention-actions");
  if (item.action) {
    const actionButton = node(
      "button",
      "attention-action-button",
      attentionActionLabel(item.action),
    );
    actionButton.type = "button";
    actionButton.dataset.defaultLabel = attentionActionLabel(item.action);
    actionButton.addEventListener("click", () => runAttentionAction(item, card, actionButton));
    actions.append(actionButton);
  }
  if (item.action?.kind !== "human_task") {
    const openButton = node("button", "attention-open-button", "查看详情");
    openButton.type = "button";
    openButton.addEventListener("click", async () => {
      openButton.disabled = true;
      openButton.dataset.state = "working";
      openButton.textContent = "正在打开";
      state.returnSection = "attention";
      showDetailLoading(item.goal || item.title);
      try {
        await loadDetail(item.instance_id);
        showToast("流程详情已打开");
      } catch (error) {
        showOwnerSection("attention");
        showToast("流程读取失败，请稍后重试", "error");
        showWorkspaceError(error);
      }
    });
    actions.append(openButton);
  }
  card.append(marker, copy, actions);
  return card;
}

function attentionActionLabel(action) {
  if (action.kind === "human_task") return "处理待办";
  if (action.kind === "confirm_draft") return "确认并启动";
  if (action.kind === "resume") return "继续流程";
  if (action.kind === "restart") return "查看重启影响";
  return "执行操作";
}

function attentionActionPath(item) {
  const instanceId = encodeURIComponent(item.instance_id);
  if (item.action.kind === "confirm_draft") {
    return `/console/api/v1/instances/${instanceId}/confirm`;
  }
  if (item.action.kind === "resume") {
    return `/console/api/v1/instances/${instanceId}/resume`;
  }
  if (item.action.kind === "restart" && item.action.scope === "node") {
    return `/console/api/v1/instances/${instanceId}/nodes/${encodeURIComponent(item.action.node_key)}/restart-preview`;
  }
  if (item.action.kind === "restart") {
    return `/console/api/v1/instances/${instanceId}/restart-preview`;
  }
  throw new Error("当前操作尚未接入中央节点");
}

async function runAttentionAction(item, card, button) {
  button.disabled = true;
  button.dataset.state = "working";
  if (item.action.kind === "human_task") {
    button.textContent = "正在打开";
    await openHumanTask(item, button);
    return;
  }
  button.textContent = item.action.kind === "restart" ? "正在生成预览" : "正在执行";
  try {
    const payload = await request(attentionActionPath(item), { method: "POST" });
    if (payload.stage === "preview") {
      button.dataset.state = "done";
      button.textContent = "等待你确认";
      renderWorkflowActionPreview(card, payload, button);
      return;
    }
    button.dataset.state = "done";
    button.textContent = payload.already_applied ? "状态已经生效" : "操作已完成";
    showToast(workflowActionSuccess(payload));
    await loadInstances();
  } catch (error) {
    button.dataset.state = "error";
    button.textContent = workflowActionError(error);
    showToast(error.message || "操作失败，请重试", "error");
    button.disabled = false;
    setTimeout(() => {
      button.dataset.state = "idle";
      button.textContent = attentionActionLabel(item.action);
    }, 2600);
  }
}

async function openHumanTask(item, sourceButton) {
  const instanceId = encodeURIComponent(item.instance_id);
  const nodeKey = encodeURIComponent(item.action.node_key);
  state.activeTask = null;
  el("human-task-title").textContent = item.node?.title || "处理待办";
  el("human-task-goal").textContent = item.goal || "";
  el("human-task-loading").textContent = "正在读取最新待办状态";
  el("human-task-loading").hidden = false;
  el("human-task-content").hidden = true;
  resetHumanTaskControls();
  if (!humanTaskDialog.open) humanTaskDialog.showModal();
  try {
    const payload = await request(
      `/console/api/v1/tasks/${instanceId}/nodes/${nodeKey}`,
    );
    state.activeTask = payload.task;
    renderHumanTask(payload.task);
    sourceButton.disabled = false;
    sourceButton.dataset.state = "idle";
    sourceButton.textContent = attentionActionLabel(item.action);
  } catch (error) {
    el("human-task-loading").textContent = error.message || "待办读取失败，请稍后重试";
    sourceButton.disabled = false;
    sourceButton.dataset.state = "error";
    sourceButton.textContent = "读取失败";
    showToast(error.message || "待办读取失败，请稍后重试", "error");
    setTimeout(() => {
      sourceButton.dataset.state = "idle";
      sourceButton.textContent = attentionActionLabel(item.action);
    }, 2200);
  }
}

function resetHumanTaskControls() {
  el("human-task-result").value = "";
  el("human-task-result").disabled = false;
  el("human-task-result-count").textContent = "0";
  el("human-task-error").textContent = "";
  el("human-task-transfer-panel").hidden = true;
  el("human-task-person").replaceChildren(node("option", "", "正在读取可选成员"));
  el("human-task-person").value = "";
  const submit = el("human-task-submit");
  submit.disabled = false;
  submit.dataset.state = "idle";
  submit.textContent = "提交并推进流程";
  const transferOpen = el("human-task-transfer-open");
  transferOpen.disabled = false;
  transferOpen.textContent = "转交给其他人";
  const transfer = el("human-task-transfer-confirm");
  transfer.disabled = false;
  transfer.dataset.state = "idle";
  transfer.textContent = "确认转交";
}

function renderHumanTask(task) {
  el("human-task-title").textContent = task.node.title;
  el("human-task-goal").textContent = task.goal;
  el("human-task-objective").textContent = task.work.objective || "完成当前节点";
  const acceptance = el("human-task-acceptance");
  acceptance.replaceChildren();
  const acceptanceItems = task.work.acceptance || [];
  if (acceptanceItems.length === 0) {
    acceptance.append(node("li", "", "提交明确、可复核的处理结果"));
  } else {
    acceptanceItems.forEach((item) => acceptance.append(node("li", "", item)));
  }
  el("human-task-context").textContent = JSON.stringify(task.work.context || {}, null, 2);
  el("human-task-transfer-open").hidden = !task.actions.transfer;
  el("human-task-loading").hidden = true;
  el("human-task-content").hidden = false;
  requestAnimationFrame(() => el("human-task-result").focus());
}

function currentTaskBinding() {
  const task = state.activeTask;
  if (!task) throw new Error("待办状态尚未载入");
  return {
    task,
    instanceId: encodeURIComponent(task.instance_id),
    nodeKey: encodeURIComponent(task.node.key),
  };
}

function taskWriteOptions(document) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(document),
  };
}

async function submitHumanTaskFromPage() {
  const button = el("human-task-submit");
  const transfer = el("human-task-transfer-open");
  const content = el("human-task-result").value.trim();
  el("human-task-error").textContent = "";
  if (!content) {
    el("human-task-error").textContent = "请先输入处理结果。";
    el("human-task-result").focus();
    return;
  }
  button.disabled = true;
  transfer.disabled = true;
  button.dataset.state = "working";
  button.textContent = "正在提交";
  try {
    const { task, instanceId, nodeKey } = currentTaskBinding();
    await request(
      `/console/api/v1/tasks/${instanceId}/nodes/${nodeKey}/submit`,
      taskWriteOptions({
        attempt_no: task.node.attempt_no,
        expected_node_version: task.node.version,
        content,
      }),
    );
    button.dataset.state = "done";
    button.textContent = "已提交，流程已推进";
    el("human-task-result").disabled = true;
    showToast("待办已提交，中央工作流正在继续");
    await loadInstances();
    setTimeout(() => humanTaskDialog.open && humanTaskDialog.close(), 700);
  } catch (error) {
    button.disabled = false;
    transfer.disabled = false;
    button.dataset.state = "error";
    button.textContent = "提交失败，请重试";
    el("human-task-error").textContent = error.message || "提交失败，请刷新后重试。";
    showToast(error.message || "提交失败，请重试", "error");
  }
}

async function openHumanTaskTransfer() {
  const button = el("human-task-transfer-open");
  const panel = el("human-task-transfer-panel");
  button.disabled = true;
  button.textContent = "正在读取成员";
  panel.hidden = false;
  try {
    const payload = await request("/console/api/v1/people?limit=100");
    const select = el("human-task-person");
    select.replaceChildren();
    select.append(node("option", "", payload.people.length ? "请选择成员" : "没有可转交的成员"));
    payload.people.forEach((person) => {
      const option = node("option", "", person.name);
      option.value = person.person_id;
      select.append(option);
    });
    el("human-task-transfer-confirm").disabled = payload.people.length === 0;
    button.textContent = "成员列表已打开";
  } catch (error) {
    panel.hidden = true;
    button.disabled = false;
    button.textContent = "转交给其他人";
    el("human-task-error").textContent = error.message || "成员列表读取失败。";
    showToast(error.message || "成员列表读取失败", "error");
  }
}

async function transferHumanTaskFromPage() {
  const button = el("human-task-transfer-confirm");
  const submit = el("human-task-submit");
  const newOwner = el("human-task-person").value;
  el("human-task-error").textContent = "";
  if (!newOwner) {
    el("human-task-error").textContent = "请选择新的负责人。";
    el("human-task-person").focus();
    return;
  }
  button.disabled = true;
  submit.disabled = true;
  button.dataset.state = "working";
  button.textContent = "正在转交";
  try {
    const { task, instanceId, nodeKey } = currentTaskBinding();
    await request(
      `/console/api/v1/tasks/${instanceId}/nodes/${nodeKey}/transfer`,
      taskWriteOptions({
        attempt_no: task.node.attempt_no,
        expected_node_version: task.node.version,
        new_owner_person_id: newOwner,
      }),
    );
    button.dataset.state = "done";
    button.textContent = "已转交给新负责人";
    el("human-task-result").disabled = true;
    showToast("待办已转交，新负责人将获得飞书待办和页面处理权限");
    await loadInstances();
    setTimeout(() => humanTaskDialog.open && humanTaskDialog.close(), 900);
  } catch (error) {
    button.disabled = false;
    submit.disabled = false;
    button.dataset.state = "error";
    button.textContent = "转交失败，请重试";
    el("human-task-error").textContent = error.message || "转交失败，请刷新后重试。";
    showToast(error.message || "转交失败，请重试", "error");
  }
}

function workflowActionSuccess(payload) {
  if (payload.action === "confirm_draft") return "流程已确认启动";
  if (payload.action === "pause") return "流程已暂停";
  if (payload.action === "resume") return "流程已继续";
  if (payload.action === "cancel") return "流程已取消";
  if (payload.action === "restart") return "已创建新的 Attempt";
  return "操作已完成";
}

function workflowActionError(error) {
  if (["preview_stale", "preview_expired", "state_conflict"].includes(error.code)) {
    return "状态已变化";
  }
  return "操作失败";
}

function renderWorkflowActionPreview(container, payload, sourceButton = null) {
  container.querySelector(".workflow-action-preview")?.remove();
  const preview = payload.preview;
  const panel = node("section", "workflow-action-preview");
  panel.dataset.action = payload.action;
  const copy = node("div", "workflow-action-preview-copy");
  const title = payload.action === "cancel"
    ? "确认取消整个流程？"
    : preview.scope === "instance"
      ? "确认重新执行全部节点？"
      : `确认从“${preview.target_node?.title || "目标节点"}”重新执行？`;
  const affected = preview.affected_nodes || [];
  copy.append(
    node("strong", "", title),
    node("p", "", `将影响 ${affected.length} 个节点。旧 Attempt、结果和审计都会保留。`),
  );
  const list = node("div", "workflow-action-preview-list");
  affected.slice(0, 6).forEach((item) => {
    list.append(node("span", "", item.title || item.key));
  });
  if (affected.length > 6) list.append(node("span", "", `另有 ${affected.length - 6} 个节点`));
  copy.append(list);
  const controls = node("div", "workflow-action-preview-controls");
  const dismiss = node("button", "workflow-action-preview-dismiss", "暂不执行");
  dismiss.type = "button";
  dismiss.addEventListener("click", () => {
    panel.remove();
    if (sourceButton) {
      sourceButton.disabled = false;
      sourceButton.dataset.state = "idle";
      sourceButton.textContent = sourceButton.dataset.defaultLabel || "查看重启影响";
    }
  });
  const confirm = node(
    "button",
    "workflow-action-preview-confirm",
    payload.action === "cancel" ? "确认取消" : "确认重新执行",
  );
  confirm.type = "button";
  confirm.addEventListener("click", () => confirmWorkflowActionPreview(payload, panel, confirm));
  controls.append(dismiss, confirm);
  panel.append(copy, controls);
  container.append(panel);
}

async function confirmWorkflowActionPreview(payload, panel, button) {
  button.disabled = true;
  panel.dataset.state = "working";
  button.textContent = "正在执行";
  const preview = payload.preview;
  const path = payload.action === "cancel"
    ? `/console/api/v1/instances/${encodeURIComponent(preview.instance_id)}/cancel-confirm/${preview.expected_instance_version}`
    : `/console/api/v1/restart-previews/${encodeURIComponent(preview.id)}/confirm`;
  try {
    const result = await request(path, { method: "POST" });
    panel.dataset.state = "done";
    button.textContent = result.already_applied ? "操作已经生效" : "操作已完成";
    showToast(workflowActionSuccess(result));
    await loadInstances(result.instance?.id || null);
  } catch (error) {
    panel.dataset.state = "error";
    button.textContent = workflowActionError(error);
    button.disabled = false;
    showToast(error.message || "操作失败，请重试", "error");
  }
}

async function loadDetail(instanceId) {
  const payload = await request(`/console/api/v1/instances/${encodeURIComponent(instanceId)}`);
  const instanceChanged = state.detail?.instance.id !== payload.instance.id;
  state.detail = payload;
  state.selectedNode = chooseNode(payload.nodes);
  if (instanceChanged) {
    state.graphScale = 1;
    state.detailTab = "overview";
  }
  renderInstances();
  renderDetail();
  if (instanceChanged) {
    el("graph").scrollLeft = 0;
    el("graph").scrollTop = 0;
  }
}

function chooseNode(nodes) {
  return nodes.find((item) => ["running", "waiting_human", "failed"].includes(item.status))?.key
    || nodes[nodes.length - 1]?.key
    || null;
}

function renderInstances() {
  instanceList.replaceChildren();
  el("instance-count").textContent = state.instances.length;
  const query = state.workflowQuery.trim().toLocaleLowerCase("zh-CN");
  const visible = state.instances.filter((item) => {
    const matchesQuery = !query
      || item.id.toLocaleLowerCase("zh-CN").includes(query)
      || (item.goal || "").toLocaleLowerCase("zh-CN").includes(query);
    if (!matchesQuery) return false;
    if (state.workflowFilter === "all") return true;
    if (state.workflowFilter === "active") return ["running", "paused"].includes(item.status);
    if (state.workflowFilter === "draft") return item.status === "draft";
    if (state.workflowFilter === "done") return item.status === "done";
    return !["running", "paused", "draft", "done"].includes(item.status);
  });
  if (state.instances.length === 0) {
    const empty = node("div", "list-empty");
    empty.append(node("strong", "", "还没有流程"), node("p", "", "通过飞书发起流程后会出现在这里。"));
    instanceList.append(empty);
    return;
  }
  if (visible.length === 0) {
    const empty = node("div", "list-empty");
    empty.append(node("strong", "", "没有匹配的流程"), node("p", "", "可以清除搜索词或切换状态筛选。"));
    instanceList.append(empty);
    return;
  }
  visible.forEach((item) => {
    const button = node("button", "instance-item");
    button.type = "button";
    button.dataset.active = String(state.detail?.instance.id === item.id);
    button.addEventListener("click", async () => {
      state.returnSection = "workflows";
      button.dataset.state = "working";
      showDetailLoading(item.goal || item.id);
      try {
        await loadDetail(item.id);
      } catch (error) {
        showOwnerSection("workflows");
        showToast("流程读取失败，请稍后重试", "error");
        showWorkspaceError(error);
      }
    });

    const top = node("div", "instance-top");
    top.append(statusBadge(item.status), node("span", "instance-time", formatDate(item.created_at)));
    const goal = node("strong", "instance-goal", item.goal || "未命名流程");
    const footer = node("div", "instance-footer");
    const progress = item.total_nodes ? Math.round(item.completed_nodes / item.total_nodes * 100) : 0;
    const progressTrack = node("span", "instance-progress-track");
    const progressValue = node("span", "instance-progress-value");
    progressValue.style.width = `${progress}%`;
    progressTrack.append(progressValue);
    footer.append(
      node("span", "mono", item.id),
      node("span", "instance-progress", `${item.completed_nodes}/${item.total_nodes}`),
    );
    button.append(top, goal, footer, progressTrack);
    instanceList.append(button);
  });
}

function renderDetail() {
  const payload = state.detail;
  if (!payload) return;
  showOwnerSection("detail");
  const instance = payload.instance;
  const progress = instance.progress;
  el("detail-status").replaceWith(statusBadge(instance.status, "detail-status"));
  el("detail-id").textContent = instance.id;
  el("detail-goal").textContent = instance.goal || "未命名流程";
  el("detail-created").textContent = `创建于 ${formatDate(instance.created_at)} · 实例版本 ${instance.version}`;
  el("progress-value").textContent = `${progress.completed_nodes}/${progress.total_nodes}`;
  const percent = progress.total_nodes ? Math.round(progress.completed_nodes / progress.total_nodes * 100) : 0;
  el("progress-ring").style.setProperty("--progress", `${percent * 3.6}deg`);
  el("graph-revision").textContent = `Graph r${instance.graph_revision}`;
  renderDetailActions(instance);
  renderInsights(payload);
  renderOverviewNodes(payload.nodes);
  renderGraph(payload.nodes);
  renderAttempts(payload.nodes.find((item) => item.key === state.selectedNode));
  renderAudit(payload.audit);
  setDetailTab(state.detailTab);
}

function renderDetailActions(instance) {
  const container = el("detail-actions");
  container.replaceChildren();
  const copy = node("div", "detail-actions-copy");
  copy.append(
    node("strong", "", "流程操作"),
    node("span", "", "所有操作都使用当前飞书身份重新校验权限和状态"),
  );
  const controls = node("div", "detail-action-controls");

  if (instance.status === "draft") {
    controls.append(detailActionButton("确认并启动", "primary", (button) => runDetailDirectAction(instance, "confirm", button)));
  } else if (instance.status === "running") {
    controls.append(detailActionButton("暂停流程", "secondary", (button) => runDetailDirectAction(instance, "pause", button)));
    controls.append(detailActionButton("重新执行", "secondary", (button) => previewDetailAction(instance, "restart", button)));
    controls.append(detailActionButton("取消流程", "danger", (button) => previewDetailAction(instance, "cancel", button)));
  } else if (instance.status === "paused") {
    controls.append(detailActionButton("继续流程", "primary", (button) => runDetailDirectAction(instance, "resume", button)));
    controls.append(detailActionButton("取消流程", "danger", (button) => previewDetailAction(instance, "cancel", button)));
  } else if (["done", "failed"].includes(instance.status)) {
    controls.append(detailActionButton("重新执行", "primary", (button) => previewDetailAction(instance, "restart", button)));
  }

  if (controls.childElementCount === 0) {
    controls.append(node("span", "detail-action-empty", "当前状态没有可执行操作"));
  }
  container.append(copy, controls);
}

function detailActionButton(label, tone, action) {
  const button = node("button", `detail-action-button detail-action-${tone}`, label);
  button.type = "button";
  button.dataset.defaultLabel = label;
  button.addEventListener("click", () => action(button));
  return button;
}

async function runDetailDirectAction(instance, action, button) {
  button.disabled = true;
  button.dataset.state = "working";
  button.textContent = "正在执行";
  try {
    const payload = await request(
      `/console/api/v1/instances/${encodeURIComponent(instance.id)}/${action}`,
      { method: "POST" },
    );
    button.dataset.state = "done";
    button.textContent = payload.already_applied ? "状态已经生效" : "操作已完成";
    showToast(workflowActionSuccess(payload));
    await loadInstances(instance.id);
  } catch (error) {
    button.dataset.state = "error";
    button.textContent = workflowActionError(error);
    button.disabled = false;
    showToast(error.message || "操作失败，请重试", "error");
  }
}

async function previewDetailAction(instance, action, button) {
  button.disabled = true;
  button.dataset.state = "working";
  button.textContent = "正在生成预览";
  const path = action === "cancel"
    ? `/console/api/v1/instances/${encodeURIComponent(instance.id)}/cancel-preview`
    : `/console/api/v1/instances/${encodeURIComponent(instance.id)}/restart-preview`;
  try {
    const payload = await request(path, { method: "POST" });
    button.dataset.state = "done";
    button.textContent = "等待你确认";
    renderWorkflowActionPreview(el("detail-actions"), payload, button);
  } catch (error) {
    button.dataset.state = "error";
    button.textContent = workflowActionError(error);
    button.disabled = false;
    showToast(error.message || "预览失败，请刷新后重试", "error");
  }
}

function renderOverviewNodes(nodes) {
  const container = el("overview-nodes");
  container.replaceChildren();
  el("overview-node-count").textContent = nodes.length;
  nodes.forEach((item, index) => {
    const button = node("button", "overview-node");
    button.type = "button";
    button.dataset.status = item.status;
    button.addEventListener("click", () => {
      state.selectedNode = item.key;
      updateGraphSelection(nodes);
      renderAttempts(item);
      setDetailTab("execution");
      showToast(`已打开节点：${item.title}`);
    });
    const ordinal = node("span", "overview-node-ordinal", String(index + 1).padStart(2, "0"));
    const copy = node("span", "overview-node-copy");
    const title = node("span", "overview-node-title");
    title.append(node("strong", "", item.title), statusBadge(item.status));
    copy.append(
      title,
      node("small", "", `${EXECUTOR[item.executor] || item.executor} · ${RELATION[item.owner_relation] || item.owner_relation} · Attempt ${item.current_attempt_no}`),
    );
    const action = node("span", "overview-node-action", "查看结果");
    button.append(ordinal, copy, action);
    container.append(button);
  });
}

function renderInsights(payload) {
  const instance = payload.instance;
  const progress = instance.progress;
  const insights = payload.insights || { reworked_nodes: [], latest_restart: null };

  const status = el("insight-status");
  status.replaceChildren(statusBadge(instance.status));
  el("insight-status-detail").textContent = `${progress.completed_nodes}/${progress.total_nodes} 个节点完成 · 实例版本 ${instance.version}`;

  const rework = el("insight-rework");
  rework.replaceChildren();
  if (insights.reworked_nodes.length === 0) {
    rework.append(node("p", "insight-empty", "未发现多轮执行节点"));
  } else {
    insights.reworked_nodes.forEach((item) => {
      const row = node("div", "insight-node");
      const copy = node("div", "insight-node-copy");
      copy.append(node("strong", "", item.title), node("span", "mono", item.key));
      row.append(copy, node("span", "attempt-chip", `Attempt ${item.current_attempt_no}`));
      rework.append(row);
    });
  }

  const restart = el("insight-restart");
  restart.replaceChildren();
  const event = insights.latest_restart;
  if (!event) {
    restart.append(node("p", "insight-empty", "最近审计中未发现受控重启"));
    return;
  }
  const title = event.scope === "instance" ? "完整实例重启" : "节点重启";
  const target = event.target_node ? ` · 起点：${event.target_node.title}` : "";
  restart.append(
    node("strong", "insight-restart-title", title),
    node("p", "", `${formatDate(event.occurred_at)} · 操作者：${RELATION[event.actor_relation] || event.actor_relation}${target}`),
    node("p", "", `影响 ${event.affected_nodes.length} 个节点${event.attempt_no ? ` · 新 Attempt ${event.attempt_no}` : ""}`),
  );
  if (event.affected_nodes.length > 0) {
    restart.append(node("p", "insight-affected", event.affected_nodes.map((item) => item.title).join("、")));
  }
}

function statusBadge(status, id = "") {
  const badge = node("span", `status-pill status-${status}`, statusLabel(status));
  if (id) badge.id = id;
  return badge;
}

function renderGraph(nodes) {
  const graph = el("graph");
  graph.replaceChildren();
  if (nodes.length === 0) {
    graph.append(node("p", "muted", "草稿中没有节点。"));
    return;
  }

  const layers = topologicalLayers(nodes);
  const ordinalByKey = new Map(nodes.map((item, index) => [item.key, index + 1]));
  const minimumWidth = Math.max(1, layers.length) * 250 + Math.max(0, layers.length - 1) * 52;
  const stage = node("div", "dag-stage");
  const canvas = node("div", "dag-canvas");
  canvas.dataset.minimumWidth = String(minimumWidth);
  const edges = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  edges.classList.add("dag-edges");
  edges.setAttribute("aria-hidden", "true");
  const grid = node("div", "dag-grid");
  grid.style.gridTemplateColumns = `repeat(${layers.length}, minmax(250px, 1fr))`;

  layers.forEach((items, layerIndex) => {
    const layer = node("section", "dag-layer");
    layer.append(node("p", "dag-layer-label", `阶段 ${String(layerIndex + 1).padStart(2, "0")}`));
    const stack = node("div", "dag-layer-nodes");
    items.forEach((item) => {
      stack.append(graphNodeCard(item, ordinalByKey.get(item.key), nodes));
    });
    layer.append(stack);
    grid.append(layer);
  });

  canvas.append(edges, grid);
  stage.append(canvas);
  graph.append(stage);
  requestAnimationFrame(layoutGraphCanvas);
}

function graphNodeCard(item, ordinal, nodes) {
  const card = node("button", "graph-node");
  card.type = "button";
  card.dataset.nodeKey = item.key;
  card.dataset.status = item.status;
  card.dataset.selected = String(state.selectedNode === item.key);
  card.addEventListener("click", () => {
    state.selectedNode = item.key;
    updateGraphSelection(nodes);
    renderAttempts(item);
  });

  const badge = node("span", "node-ordinal", String(ordinal).padStart(2, "0"));
  const content = node("div", "node-content");
  const top = node("div", "node-title-row");
  top.append(node("strong", "", item.title), statusBadge(item.status));
  const metadata = node("div", "node-metadata");
  metadata.append(
    node("span", `executor executor-${item.executor}`, EXECUTOR[item.executor] || item.executor),
    node("span", "", `Owner: ${RELATION[item.owner_relation] || item.owner_relation}`),
    node("span", "", `Attempt ${item.current_attempt_no}`),
  );
  const dependency = item.deps.length > 0
    ? `依赖 ${item.deps.join("、")}`
    : "入口节点，无依赖";
  content.append(
    top,
    node("span", "node-key mono", item.key),
    metadata,
    node("span", "node-dependencies", dependency),
  );
  card.append(badge, content);
  return card;
}

function topologicalLayers(nodes) {
  const byKey = new Map(nodes.map((item) => [item.key, item]));
  const depthByKey = new Map();
  const visiting = new Set();

  function depth(item) {
    if (depthByKey.has(item.key)) return depthByKey.get(item.key);
    if (visiting.has(item.key)) return 0;
    visiting.add(item.key);
    const dependencies = item.deps
      .map((key) => byKey.get(key))
      .filter(Boolean);
    const value = dependencies.length > 0
      ? Math.max(...dependencies.map((dependency) => depth(dependency))) + 1
      : 0;
    visiting.delete(item.key);
    depthByKey.set(item.key, value);
    return value;
  }

  const layers = [];
  nodes.forEach((item) => {
    const layerIndex = depth(item);
    if (!layers[layerIndex]) layers[layerIndex] = [];
    layers[layerIndex].push(item);
  });
  return layers.filter(Boolean);
}

function clampGraphScale(value) {
  return Math.min(GRAPH_MAX_SCALE, Math.max(GRAPH_MIN_SCALE, value));
}

function updateGraphControls() {
  const percent = Math.round(state.graphScale * 100);
  el("graph-zoom-reset").textContent = `${percent}%`;
  el("graph-zoom-out").disabled = state.graphScale <= GRAPH_MIN_SCALE;
  el("graph-zoom-in").disabled = state.graphScale >= GRAPH_MAX_SCALE;
}

function layoutGraphCanvas() {
  const graph = el("graph");
  const stage = graph.querySelector(".dag-stage");
  const canvas = graph.querySelector(".dag-canvas");
  if (!stage || !canvas || !state.detail) {
    updateGraphControls();
    return;
  }
  const minimumWidth = Number(canvas.dataset.minimumWidth) || 250;
  const naturalWidth = Math.max(minimumWidth, graph.clientWidth - 36);
  canvas.style.width = `${naturalWidth}px`;
  canvas.style.transform = `scale(${state.graphScale})`;
  const naturalHeight = Math.max(344, canvas.scrollHeight);
  stage.style.width = `${naturalWidth * state.graphScale}px`;
  stage.style.height = `${naturalHeight * state.graphScale}px`;
  drawDagEdges(canvas, state.detail.nodes);
  updateGraphControls();
}

function setGraphScale(value, anchorX = null, anchorY = null) {
  const graph = el("graph");
  const previousScale = state.graphScale;
  const nextScale = clampGraphScale(Math.round(value * 100) / 100);
  if (nextScale === previousScale) return;
  const viewportX = anchorX ?? graph.clientWidth / 2;
  const viewportY = anchorY ?? graph.clientHeight / 2;
  const contentX = (graph.scrollLeft + viewportX) / previousScale;
  const contentY = (graph.scrollTop + viewportY) / previousScale;
  state.graphScale = nextScale;
  layoutGraphCanvas();
  graph.scrollLeft = contentX * nextScale - viewportX;
  graph.scrollTop = contentY * nextScale - viewportY;
}

function fitGraph() {
  const graph = el("graph");
  const canvas = graph.querySelector(".dag-canvas");
  if (!canvas) return;
  const minimumWidth = Number(canvas.dataset.minimumWidth) || 250;
  const availableWidth = Math.max(1, graph.clientWidth - 36);
  setGraphScale(Math.min(1, availableWidth / minimumWidth), 0, 0);
  graph.scrollLeft = 0;
  graph.scrollTop = 0;
}

function updateGraphSelection(nodes) {
  const graph = el("graph");
  graph.querySelectorAll(".graph-node").forEach((card) => {
    card.dataset.selected = String(card.dataset.nodeKey === state.selectedNode);
  });
  const canvas = graph.querySelector(".dag-canvas");
  if (canvas) drawDagEdges(canvas, nodes);
}

function drawDagEdges(canvas, nodes) {
  const svg = canvas.querySelector(".dag-edges");
  if (!svg) return;
  svg.replaceChildren();
  const width = canvas.offsetWidth;
  const height = canvas.offsetHeight;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));

  const namespace = "http://www.w3.org/2000/svg";
  const definitions = document.createElementNS(namespace, "defs");
  const marker = document.createElementNS(namespace, "marker");
  marker.setAttribute("id", "dag-arrow");
  marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "8");
  marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "6");
  marker.setAttribute("markerHeight", "6");
  marker.setAttribute("orient", "auto-start-reverse");
  const arrow = document.createElementNS(namespace, "path");
  arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
  marker.append(arrow);
  definitions.append(marker);
  svg.append(definitions);

  const cardByKey = new Map(
    [...canvas.querySelectorAll(".graph-node")].map((card) => [card.dataset.nodeKey, card]),
  );
  const canvasRect = canvas.getBoundingClientRect();
  nodes.forEach((targetNode) => {
    const target = cardByKey.get(targetNode.key);
    if (!target) return;
    targetNode.deps.forEach((dependencyKey) => {
      const source = cardByKey.get(dependencyKey);
      if (!source) return;
      const sourceRect = source.getBoundingClientRect();
      const targetRect = target.getBoundingClientRect();
      const startX = (sourceRect.right - canvasRect.left) / state.graphScale;
      const startY = (sourceRect.top + sourceRect.height / 2 - canvasRect.top) / state.graphScale;
      const endX = (targetRect.left - canvasRect.left) / state.graphScale;
      const endY = (targetRect.top + targetRect.height / 2 - canvasRect.top) / state.graphScale;
      const bend = Math.max(30, (endX - startX) * 0.45);
      const path = document.createElementNS(namespace, "path");
      path.classList.add("dag-edge");
      path.dataset.selected = String(
        state.selectedNode === dependencyKey || state.selectedNode === targetNode.key,
      );
      path.setAttribute(
        "d",
        `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`,
      );
      path.setAttribute("marker-end", "url(#dag-arrow)");
      svg.append(path);
    });
  });
}

function renderAttempts(selected) {
  const attempts = el("attempts");
  attempts.replaceChildren();
  if (!selected) {
    el("attempt-heading").textContent = "执行记录";
    el("attempt-count").textContent = "0";
    attempts.append(node("p", "muted", "没有节点执行记录。"));
    return;
  }
  el("attempt-heading").textContent = selected.title;
  el("attempt-count").textContent = selected.attempts.length;
  [...selected.attempts].reverse().forEach((item) => {
    const card = node("article", "attempt-card");
    card.dataset.current = String(item.attempt_no === selected.current_attempt_no);
    const head = node("div", "attempt-head");
    const title = node("div", "attempt-title");
    title.append(node("strong", "", `Attempt ${item.attempt_no}`), statusBadge(item.status));
    head.append(title, node("span", "muted", formatDate(item.completed_at || item.started_at)));
    const facts = node("div", "attempt-facts");
    facts.append(
      fact("提交者", RELATION[item.submitted_by] || item.submitted_by),
      fact("执行锁", item.claimed ? "已领取" : "未领取"),
      fact("质量判断", item.quality?.verdict ? item.quality.verdict.toUpperCase() : "无"),
    );
    card.append(head, facts);
    if (item.result !== null) {
      const result = node("div", "result-block");
      result.append(node("span", "result-label", "结果快照"));
      const pre = node("pre", "");
      pre.textContent = JSON.stringify(item.result, null, 2);
      result.append(pre);
      card.append(result);
    }
    if (item.error_code) {
      card.append(node("p", "attempt-error", `错误代码：${item.error_code}`));
    }
    attempts.append(card);
  });
}

function fact(label, value) {
  const item = node("span", "fact");
  item.append(node("small", "", label), node("strong", "", value));
  return item;
}

function renderAudit(events) {
  const audit = el("audit");
  audit.replaceChildren();
  el("audit-count").textContent = events.length;
  if (events.length === 0) {
    audit.append(node("p", "muted", "尚无审计事件。"));
    return;
  }
  [...events].reverse().forEach((item) => {
    const row = node("article", "audit-row");
    const marker = node("span", "audit-marker");
    const body = node("div", "audit-body");
    const title = node("div", "audit-title");
    title.append(
      node("strong", "", EVENT[item.event_type] || item.event_type),
      node("span", "muted", formatDate(item.occurred_at)),
    );
    const description = [
      item.node_key ? `节点 ${item.node_key}` : "流程级事件",
      item.attempt_no ? `Attempt ${item.attempt_no}` : null,
      RELATION[item.actor_relation] || item.actor_relation,
      item.source,
    ].filter(Boolean).join(" · ");
    body.append(title, node("p", "", description));
    row.append(marker, body);
    audit.append(row);
  });
}

function lockConsole() {
  state.token = "";
  state.attention = [];
  state.detail = null;
  state.isAdmin = false;
  state.adminOverview = null;
  state.adminSessions = null;
  state.adminSessionPreview = null;
  state.view = "owner";
  state.ownerSection = "attention";
  state.returnSection = "attention";
  state.detailTab = "overview";
  state.workflowFilter = "all";
  state.workflowQuery = "";
  state.expandedAttentionKinds.clear();
  sessionStorage.removeItem("larkflow.console.token");
  adminViewButton.hidden = true;
  showOwnerView();
  app.hidden = true;
  unlock.hidden = false;
  tokenInput.value = "";
  el("admin-session-preview").hidden = true;
  el("admin-session-preview").replaceChildren();
  tokenInput.focus();
}

function showStaticLogin(message = "") {
  state.authMode = "static";
  unlockCopy.textContent = "输入开发控制台访问令牌，只读取由你发起的流程、节点执行和审计记录。";
  authNote.lastChild.textContent = "开发令牌只保存在当前标签页。";
  unlockForm.hidden = false;
  feishuLogin.hidden = true;
  unlockError.textContent = message;
  app.hidden = true;
  unlock.hidden = false;
  tokenInput.focus();
}

function showFeishuLogin(message = "") {
  state.authMode = "feishu";
  state.token = "";
  state.isAdmin = false;
  state.adminOverview = null;
  state.adminSessions = null;
  state.adminSessionPreview = null;
  state.view = "owner";
  state.ownerSection = "attention";
  sessionStorage.removeItem("larkflow.console.token");
  adminViewButton.hidden = true;
  unlockCopy.textContent = "使用当前飞书身份进入，只展示你有权查看的流程和待处理事项。";
  authNote.lastChild.textContent = "身份和流程权限均由中央节点校验。";
  unlockForm.hidden = true;
  feishuLogin.hidden = false;
  feishuLogin.disabled = false;
  feishuLogin.textContent = "使用飞书身份进入";
  unlockError.textContent = message;
  el("admin-session-preview").hidden = true;
  el("admin-session-preview").replaceChildren();
  app.hidden = true;
  unlock.hidden = false;
  feishuLogin.focus();
}

function showConsole() {
  unlock.hidden = true;
  app.hidden = false;
  adminViewButton.hidden = !state.isAdmin;
  if (!state.isAdmin && state.view === "admin") showOwnerView();
  if (state.view === "owner") showOwnerSection(state.ownerSection);
}

function beginFeishuLogin() {
  feishuLogin.disabled = true;
  feishuLogin.textContent = "正在连接飞书";
  window.location.assign(state.loginUrl);
}

async function logoutConsole() {
  const button = el("lock");
  button.disabled = true;
  button.textContent = "退出中";
  try {
    await fetch("/console/auth/logout", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
    });
  } finally {
    window.location.replace(state.loginUrl);
  }
}

async function loadAuthConfiguration() {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch("/console/api/v1/auth", {
    headers,
    cache: "no-store",
    credentials: "same-origin",
  });
  if (!response.ok) throw new Error("无法读取登录配置");
  const payload = await response.json();
  if (!["static", "feishu"].includes(payload.mode)) {
    throw new Error("登录配置无效");
  }
  state.authMode = payload.mode;
  if (payload.mode === "feishu") {
    if (typeof payload.login_url !== "string" || !payload.login_url.startsWith("/console/")) {
      throw new Error("飞书登录入口无效");
    }
    state.loginUrl = payload.login_url;
  }
  state.isAdmin = payload.admin === true;
  adminViewButton.hidden = !state.isAdmin;
  el("view-switch").hidden = !state.isAdmin;
  return payload;
}

async function bootstrap() {
  const auth = await loadAuthConfiguration();
  if (auth.mode === "feishu") {
    el("lock").textContent = "退出";
    const authError = new URLSearchParams(window.location.search).get("auth_error");
    if (authError) {
      const message = authError === "access_denied"
        ? "你已取消飞书授权，可以重新进入"
        : "飞书登录没有完成，请重试";
      showFeishuLogin(message);
      window.history.replaceState({}, "", "/console/");
      return;
    }
    if (!auth.authenticated) {
      showFeishuLogin();
      beginFeishuLogin();
      return;
    }
    showConsole();
    await loadInstances();
    return;
  }

  el("lock").textContent = "锁定";
  if (!state.token) {
    showStaticLogin();
    return;
  }
  showConsole();
  try {
    await loadInstances();
  } catch (error) {
    unlockError.textContent = error.message;
    lockConsole();
  }
}

function showWorkspaceError(error) {
  setConnection("error", error.message || "读取失败");
}

unlockForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.authMode !== "static") return;
  unlockError.textContent = "";
  state.token = tokenInput.value.trim();
  try {
    await loadAuthConfiguration();
    await loadInstances();
    sessionStorage.setItem("larkflow.console.token", state.token);
    showConsole();
  } catch (error) {
    unlockError.textContent = error.message;
    unlock.hidden = false;
    app.hidden = true;
  }
});

feishuLogin.addEventListener("click", beginFeishuLogin);

attentionNav.addEventListener("click", () => showOwnerSection("attention"));
workflowNav.addEventListener("click", () => {
  renderInstances();
  showOwnerSection("workflows");
});
el("detail-back").addEventListener("click", () => showOwnerSection(state.returnSection));
el("detail-tabs").querySelectorAll("button").forEach((button) => {
  button.addEventListener("click", () => setDetailTab(button.dataset.tab));
});
el("workflow-query").addEventListener("input", (event) => {
  state.workflowQuery = event.target.value;
  renderInstances();
});
el("workflow-filters").querySelectorAll("button").forEach((button) => {
  button.addEventListener("click", () => {
    state.workflowFilter = button.dataset.filter;
    el("workflow-filters").querySelectorAll("button").forEach((candidate) => {
      candidate.dataset.active = String(candidate === button);
    });
    renderInstances();
  });
});

el("refresh").addEventListener("click", () => {
  const operation = state.view === "admin"
    ? loadAdminOverview()
    : loadInstances(state.detail?.instance.id || null);
  operation.catch(showWorkspaceError);
});
ownerViewButton.addEventListener("click", showOwnerView);
adminViewButton.addEventListener("click", () => {
  showAdminView().catch(showWorkspaceError);
});
el("lock").addEventListener("click", () => {
  if (state.authMode === "feishu") {
    logoutConsole().catch(showWorkspaceError);
  } else {
    lockConsole();
  }
});
el("human-task-close").addEventListener("click", () => humanTaskDialog.close());
el("human-task-result").addEventListener("input", (event) => {
  el("human-task-result-count").textContent = event.target.value.length;
});
el("human-task-submit").addEventListener("click", submitHumanTaskFromPage);
el("human-task-transfer-open").addEventListener("click", openHumanTaskTransfer);
el("human-task-transfer-confirm").addEventListener("click", transferHumanTaskFromPage);
humanTaskDialog.addEventListener("close", () => {
  state.activeTask = null;
  resetHumanTaskControls();
});
el("graph-zoom-out").addEventListener("click", () => setGraphScale(state.graphScale - GRAPH_SCALE_STEP));
el("graph-zoom-reset").addEventListener("click", () => setGraphScale(1));
el("graph-zoom-in").addEventListener("click", () => setGraphScale(state.graphScale + GRAPH_SCALE_STEP));
el("graph-fit").addEventListener("click", fitGraph);

el("graph").addEventListener("wheel", (event) => {
  if (!event.ctrlKey && !event.metaKey) return;
  event.preventDefault();
  const rect = el("graph").getBoundingClientRect();
  const direction = event.deltaY < 0 ? GRAPH_SCALE_STEP : -GRAPH_SCALE_STEP;
  setGraphScale(
    state.graphScale + direction,
    event.clientX - rect.left,
    event.clientY - rect.top,
  );
}, { passive: false });

el("graph").addEventListener("pointerdown", (event) => {
  if (event.pointerType === "mouse" && event.button !== 0) return;
  if (event.target.closest(".graph-node")) return;
  const graph = el("graph");
  graphDrag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    scrollLeft: graph.scrollLeft,
    scrollTop: graph.scrollTop,
    moved: false,
  };
  graph.setPointerCapture(event.pointerId);
});

el("graph").addEventListener("pointermove", (event) => {
  if (!graphDrag || graphDrag.pointerId !== event.pointerId) return;
  const deltaX = event.clientX - graphDrag.startX;
  const deltaY = event.clientY - graphDrag.startY;
  if (!graphDrag.moved && Math.hypot(deltaX, deltaY) < 4) return;
  graphDrag.moved = true;
  const graph = el("graph");
  graph.dataset.panning = "true";
  graph.scrollLeft = graphDrag.scrollLeft - deltaX;
  graph.scrollTop = graphDrag.scrollTop - deltaY;
  event.preventDefault();
});

function finishGraphPan(event) {
  if (!graphDrag || graphDrag.pointerId !== event.pointerId) return;
  const graph = el("graph");
  if (graph.hasPointerCapture(event.pointerId)) graph.releasePointerCapture(event.pointerId);
  suppressGraphClick = graphDrag.moved;
  if (suppressGraphClick) {
    setTimeout(() => { suppressGraphClick = false; }, 0);
  }
  graphDrag = null;
  delete graph.dataset.panning;
}

el("graph").addEventListener("pointerup", finishGraphPan);
el("graph").addEventListener("pointercancel", finishGraphPan);
el("graph").addEventListener("click", (event) => {
  if (!suppressGraphClick) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  suppressGraphClick = false;
}, true);

el("graph").addEventListener("keydown", (event) => {
  const graph = el("graph");
  if (["Enter", " "].includes(event.key) && event.target.closest(".graph-node")) return;
  if (event.key === "ArrowLeft") graph.scrollLeft -= 80;
  else if (event.key === "ArrowRight") graph.scrollLeft += 80;
  else if (event.key === "ArrowUp") graph.scrollTop -= 80;
  else if (event.key === "ArrowDown") graph.scrollTop += 80;
  else if (["+", "="].includes(event.key)) setGraphScale(state.graphScale + GRAPH_SCALE_STEP);
  else if (event.key === "-") setGraphScale(state.graphScale - GRAPH_SCALE_STEP);
  else if (event.key === "0") setGraphScale(1);
  else if (event.key.toLowerCase() === "f") fitGraph();
  else return;
  event.preventDefault();
});

window.addEventListener("resize", () => {
  if (state.detail) layoutGraphCanvas();
});

bootstrap().catch((error) => {
  unlockCopy.textContent = "工作台暂时无法初始化。";
  unlockForm.hidden = true;
  feishuLogin.hidden = true;
  unlockError.textContent = error.message;
  app.hidden = true;
  unlock.hidden = false;
});
