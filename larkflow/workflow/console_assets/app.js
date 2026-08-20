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

function linkedTaskFromLocation() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("action") !== "task") {
    try {
      return JSON.parse(sessionStorage.getItem("larkflow.console.linkedTask") || "null");
    } catch (_error) {
      return null;
    }
  }
  const value = { instanceId: params.get("instance"), nodeKey: params.get("node") };
  if (!value.instanceId || !value.nodeKey) return null;
  try {
    sessionStorage.setItem("larkflow.console.linkedTask", JSON.stringify(value));
  } catch (_error) {
    // The current page can still open the task after authentication.
  }
  return value;
}

const state = {
  token: sessionStorage.getItem("larkflow.console.token") || "",
  authMode: "unknown",
  loginUrl: "/console/auth/login",
  instances: [],
  attention: [],
  humanTasks: [],
  draftRequests: [],
  currentDraft: null,
  draftAttachments: [],
  autoOpenDraftId: null,
  activeTask: null,
  detail: null,
  isAdmin: false,
  attachmentPlanningEnabled: false,
  enterpriseKnowledgeSelectionEnabled: false,
  knowledgeCandidates: [],
  unavailableKnowledgeSelections: [],
  selectedKnowledgeSourceIds: new Set(),
  knowledgeSelectionVersion: 0,
  view: "owner",
  adminOverview: null,
  adminSessions: null,
  adminSessionPreview: null,
  selectedNode: null,
  ownerSection: "attention",
  returnSection: "attention",
  detailTab: "overview",
  workflowFilter: "open",
  workflowQuery: "",
  expandedAttentionKinds: new Set(),
  graphEditor: null,
  autoOpenTask: linkedTaskFromLocation(),
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
const draftStudio = el("draft-studio");
const draftNav = el("draft-nav");
const attentionNav = el("attention-nav");
const workflowNav = el("workflow-nav");
const workspace = document.querySelector(".workspace");
const adminConsole = el("admin-console");
const ownerViewButton = el("owner-view");
const adminViewButton = el("admin-view");
const humanTaskDialog = el("human-task-dialog");
const graphEditDialog = el("graph-edit-dialog");

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

const EXECUTOR = { human: "人工", agent: "AI", tool: "工具" };
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
  draft_requests: "网页草稿生成",
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

let canvasLoadPromise = null;

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

const FIELD_LABELS = {
  content: "内容",
  summary: "摘要",
  decision: "判断",
  feedback: "意见",
  facts: "来源事实",
  inferences: "建议与推断",
  open_questions: "待确认问题",
  acceptance: "验收条件",
  objective: "目标",
  url: "来源链接",
  title: "标题",
};

function readableFieldLabel(key) {
  return FIELD_LABELS[key] || String(key).replaceAll("_", " ");
}

function readableValue(value) {
  if (value === null || value === undefined || value === "") return "无";
  if (["string", "number", "boolean"].includes(typeof value)) {
    if (value === true) return "是";
    if (value === false) return "否";
    return String(value);
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return "无";
    return value.map((item) => {
      const body = readableValue(item).replaceAll("\n", "\n  ");
      return `• ${body}`;
    }).join("\n");
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0) return "无";
    return entries.map(([key, item]) => {
      const body = readableValue(item);
      if (item !== null && typeof item === "object") {
        return `${readableFieldLabel(key)}：\n${body.split("\n").map((line) => `  ${line}`).join("\n")}`;
      }
      return `${readableFieldLabel(key)}：${body}`;
    }).join("\n");
  }
  return String(value);
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
    headers["X-Larkflow-Console-Action"] = path.endsWith("/knowledge-selection")
      ? "knowledge-selection-v1"
      : path.startsWith("/console/api/v1/admin/")
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
    throw new Error("无法连接工作台服务");
  }
  const payload = await response.json();
  if (response.status === 401) {
    const error = new Error(
      state.authMode === "feishu"
        ? "飞书登录已过期，请重新进入"
        : "访问令牌无效或已经更新",
    );
    error.authentication = true;
    if (state.authMode === "feishu") {
      showFeishuLogin(error.message, { reauthorize: true });
    } else {
      lockConsole();
    }
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
  const [payload, taskPayload, draftPayload] = await Promise.all([
    request("/console/api/v1/instances?limit=30"),
    request("/console/api/v1/tasks?limit=30"),
    request("/console/api/v1/drafts?limit=10"),
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
  syncDraftRequests(draftPayload.requests || []);
  if (selectId) {
    await loadDetail(selectId);
  } else if (state.autoOpenTask) {
    const linked = state.autoOpenTask;
    const item = state.attention.find((candidate) => (
      candidate.action?.kind === "human_task"
      && candidate.instance_id === linked.instanceId
      && candidate.action.node_key === linked.nodeKey
    ));
    state.autoOpenTask = null;
    try {
      sessionStorage.removeItem("larkflow.console.linkedTask");
    } catch (_error) {
      // Nothing else depends on persistent cleanup.
    }
    window.history.replaceState({}, "", "/console/");
    if (item) {
      await openHumanTask(item, document.createElement("button"));
    } else {
      showToast("该待办已处理、已转交或你无权访问", "error");
    }
  } else if (
    state.ownerSection === "attention"
    && state.attention.length === 0
  ) {
    showOwnerSection("drafts");
    loadDraftCollaborators().catch(showWorkspaceError);
  }
}

function humanTaskAttentionItem(task) {
  const isDecision = task.kind === "decision";
  return {
    id: `complete_human:${task.instance_id}:${task.node.key}`,
    kind: "complete_human",
    priority: 1,
    instance_id: task.instance_id,
    goal: task.goal,
    instance_status: task.instance_status,
    title: `${isDecision ? "作出判断" : "完成任务"}：${task.node.title}`,
    detail: isDecision
      ? "查看上游结果后，在本页接受或退回。"
      : task.instance_owner_relation === "you"
        ? "这一步正在等待你的输入，可在本页提交或转交。"
        : "你是当前负责人，可在本页提交或转交。",
    occurred_at: task.started_at,
    node: { key: task.node.key, title: task.node.title },
    action: { kind: isDecision ? "human_decision" : "human_task", node_key: task.node.key },
  };
}

const DRAFT_ACTIVE_STATUSES = new Set([
  "collecting",
  "queued",
  "generating",
  "repairing",
  "preparing",
  "retrying",
]);
const DRAFT_POLLING_STATUSES = new Set([
  "queued",
  "generating",
  "repairing",
  "preparing",
  "retrying",
]);

const DRAFT_STATUS = {
  collecting: "整理资料",
  queued: "排队中",
  generating: "生成中",
  repairing: "重新校验",
  preparing: "保存草稿",
  retrying: "自动重试",
  ready: "待确认",
  rejected: "需要调整",
  failed: "生成失败",
};

const DRAFT_STARTERS = {
  material: {
    brief: "把一份现有材料整理成清晰摘要，由我确认输入，AI 生成草稿，最后由人工检查是否准确。",
    context: "只依据已有材料，不补写来源之外的事实；无法确认的内容单独列出。",
  },
  release: {
    brief: "根据已有发布说明整理上线检查清单，由我确认范围，AI 生成初稿，最后由人工判断是否可以发布。",
    context: "检查清单需要包含风险、回滚条件和发布后的观察项。",
  },
  meeting: {
    brief: "把会议结论整理成行动清单，明确每项工作的交付物和完成条件，最后由人工确认没有遗漏。",
    context: "只提取会议中明确达成的结论，不推测尚未形成共识的事项。",
  },
};

function newDraftRequestId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function syncDraftRequests(requests) {
  state.draftRequests = requests;
  const current = state.currentDraft
    ? requests.find((item) => item.id === state.currentDraft.id)
    : null;
  const active = requests.find((item) => DRAFT_ACTIVE_STATUSES.has(item.status));
  if (current) {
    state.currentDraft = current;
    renderCurrentDraft(current);
  } else if (!state.currentDraft && active) {
    state.currentDraft = active;
    renderCurrentDraft(active);
    if (active.status === "collecting") loadDraftAttachments(active.id).catch(showWorkspaceError);
    else pollDraftRequest(active.id);
  }
  renderDraftRequests();
}

function renderDraftRequests() {
  const list = el("draft-recent");
  const panel = el("draft-recent-panel");
  list.replaceChildren();
  const resumable = state.draftRequests.find((item) => (
    DRAFT_ACTIVE_STATUSES.has(item.status) || item.status === "ready"
  ));
  if (resumable && state.currentDraft?.id === resumable.id) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  if (!resumable) {
    list.append(node("p", "draft-recent-empty", "没有尚待继续的草稿。以前的流程仍可在“流程记录”中查看。"));
    return;
  }
  const item = node("article", "draft-recent-item");
  const open = node("button", "", resumable.brief || "未命名草稿请求");
  open.type = "button";
  open.addEventListener("click", () => {
    state.currentDraft = resumable;
    renderCurrentDraft(resumable);
    renderDraftRequests();
    if (resumable.status === "ready") openDraftInstance(resumable).catch(showWorkspaceError);
    else if (resumable.status === "collecting") loadDraftAttachments(resumable.id).catch(showWorkspaceError);
    else pollDraftRequest(resumable.id);
  });
  const meta = node("div", "draft-recent-meta");
  meta.append(
    node("span", "", DRAFT_STATUS[resumable.status] || resumable.status),
    node("time", "", formatDate(resumable.created_at)),
  );
  item.append(open, meta);
  list.append(item);
}

function renderCurrentDraft(requestItem) {
  const panel = el("draft-current");
  const status = el("draft-current-status");
  const terminal = !DRAFT_ACTIVE_STATUSES.has(requestItem.status);
  panel.hidden = false;
  panel.dataset.terminal = String(terminal);
  panel.dataset.collecting = String(requestItem.status === "collecting");
  status.className = `status-pill ${requestItem.status === "ready" ? "status-done" : ["rejected", "failed"].includes(requestItem.status) ? "status-failed" : "status-running"}`;
  status.textContent = DRAFT_STATUS[requestItem.status] || requestItem.status;
  el("draft-current-id").textContent = requestItem.id.slice(-8);
  el("draft-current-title").textContent = requestItem.status === "ready"
    ? "流程草稿已经生成"
    : requestItem.status === "collecting"
      ? "资料已暂存，等待你确认"
    : requestItem.status === "rejected"
      ? "当前描述需要调整"
      : requestItem.status === "failed"
        ? "本次生成没有完成"
        : "AI 正在准备候选流程";
  el("draft-current-message").textContent = requestItem.message;
  const open = el("draft-open-instance");
  open.hidden = requestItem.status !== "ready" || !requestItem.instance_id;
  open.disabled = false;
  const submit = el("draft-submit");
  submit.disabled = !terminal;
  submit.dataset.state = requestItem.status === "ready" ? "done" : terminal ? "idle" : "working";
  submit.textContent = !terminal
    ? DRAFT_STATUS[requestItem.status] || "生成中"
    : requestItem.status === "ready"
      ? "调整描述后再生成"
      : "调整后重新生成";
  const attachmentPanel = el("draft-attachment-panel");
  attachmentPanel.hidden = requestItem.status !== "collecting";
  if (requestItem.status === "collecting") renderDraftAttachments();
}

function readableBytes(value) {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}

function renderDraftAttachments() {
  const list = el("draft-attachment-list");
  list.replaceChildren();
  const ready = state.draftAttachments.filter((item) => item.status === "ready");
  if (!ready.length) {
    list.append(node("p", "draft-recent-empty", "尚未上传资料。可以直接开始生成。"));
  }
  ready.forEach((attachment) => {
    const item = node("div", "draft-attachment-item");
    const copy = document.createElement("div");
    copy.append(
      node("strong", "", attachment.display_filename),
      node("small", "", `${readableBytes(attachment.size_bytes)} · 内部资料`),
    );
    const revoke = node("button", "", "撤销");
    revoke.type = "button";
    revoke.addEventListener("click", () => revokeDraftAttachment(attachment.id));
    item.append(copy, revoke);
    list.append(item);
  });
  const knowledge = el("draft-frozen-knowledge");
  knowledge.replaceChildren();
  const selected = state.knowledgeCandidates.filter((item) => (
    state.selectedKnowledgeSourceIds.has(item.source_id)
  ));
  if (selected.length) {
    knowledge.append(node("p", "", "本次生成还将使用以下企业共享资料："));
    selected.forEach((item) => {
      knowledge.append(node("p", "", `• ${item.display_label} · ${item.version_id}`));
    });
  }
  if (state.unavailableKnowledgeSelections.length) {
    knowledge.append(node("p", "", "以下已选资料当前不可用，请取消选择并保存："));
    state.unavailableKnowledgeSelections.forEach((item) => {
      knowledge.append(node("p", "", `• ${item.source_id} · ${item.unavailable_reason}`));
    });
  }
  const saveKnowledge = el("draft-knowledge-save");
  saveKnowledge.hidden = !state.enterpriseKnowledgeSelectionEnabled;
  saveKnowledge.disabled = false;
  saveKnowledge.textContent = "保存企业资料选择";
  el("draft-generate").disabled = state.unavailableKnowledgeSelections.length > 0;
}

function renderKnowledgeCandidates() {
  const list = el("draft-knowledge-list");
  list.replaceChildren();
  const unavailable = state.unavailableKnowledgeSelections.filter((item) => (
    !state.knowledgeCandidates.some((candidate) => candidate.source_id === item.source_id)
  ));
  if (!state.knowledgeCandidates.length && !unavailable.length) {
    list.append(node("p", "draft-recent-empty", "当前没有可选择的企业共享资料。"));
  }
  [...state.knowledgeCandidates, ...unavailable].forEach((item) => {
    const label = node("label", "draft-knowledge-option");
    label.dataset.unavailable = item.selectable ? "false" : "true";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = item.source_id;
    const selected = state.selectedKnowledgeSourceIds.has(item.source_id);
    input.disabled = !item.selectable && !selected;
    input.checked = selected;
    input.addEventListener("change", () => {
      if (input.checked && item.selectable) {
        state.selectedKnowledgeSourceIds.add(item.source_id);
      } else {
        input.checked = false;
        state.selectedKnowledgeSourceIds.delete(item.source_id);
        if (!item.selectable) input.disabled = true;
      }
      el("draft-knowledge-selection").textContent = state.selectedKnowledgeSourceIds.size
        ? `已选择 ${state.selectedKnowledgeSourceIds.size} 份资料，创建草稿后会再次确认。`
        : "默认不使用企业资料。";
    });
    const copy = document.createElement("div");
    copy.append(
      node("strong", "", item.display_label || item.source_id),
      node(
        "small",
        "",
        item.selectable
          ? `${item.media_type} · ${readableBytes(item.size_bytes)} · 发布于 ${formatDate(item.published_at)}`
          : item.unavailable_reason || "当前不可用于规划",
      ),
    );
    label.append(input, copy);
    list.append(label);
  });
}

async function loadKnowledgeCatalog() {
  const payload = await request("/console/api/v1/knowledge");
  state.knowledgeCandidates = payload.sources || [];
  renderKnowledgeCandidates();
}

async function loadDraftKnowledgeSelection(requestId) {
  if (!state.enterpriseKnowledgeSelectionEnabled) return;
  const payload = await request(
    `/console/api/v1/drafts/${encodeURIComponent(requestId)}/knowledge-selection`,
  );
  state.selectedKnowledgeSourceIds = new Set(payload.source_ids || []);
  state.unavailableKnowledgeSelections = payload.unavailable_selected || [];
  state.knowledgeSelectionVersion = payload.selection_version || 0;
  renderKnowledgeCandidates();
  renderDraftAttachments();
}

async function saveCurrentKnowledgeSelection() {
  if (!state.currentDraft?.id || state.currentDraft.status !== "collecting") return;
  const button = el("draft-knowledge-save");
  const errorNode = el("draft-attachment-error");
  button.disabled = true;
  button.textContent = "正在保存选择";
  errorNode.textContent = "";
  try {
    const payload = await request(
      `/console/api/v1/drafts/${encodeURIComponent(state.currentDraft.id)}/knowledge-selection`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_ids: Array.from(state.selectedKnowledgeSourceIds),
          expected_version: state.knowledgeSelectionVersion,
        }),
      },
    );
    state.knowledgeSelectionVersion = payload.selection_version;
    state.selectedKnowledgeSourceIds = new Set(payload.source_ids || []);
    state.unavailableKnowledgeSelections = payload.unavailable_selected || [];
    renderKnowledgeCandidates();
    renderDraftAttachments();
    showToast("企业资料选择已保存");
  } catch (error) {
    button.disabled = false;
    button.textContent = "保存企业资料选择";
    errorNode.textContent = error.message || "企业资料选择没有保存，请重试。";
  }
}

async function loadDraftAttachments(requestId) {
  const payload = await request(`/console/api/v1/drafts/${encodeURIComponent(requestId)}/attachments`);
  state.draftAttachments = payload.attachments || [];
  if (state.currentDraft?.id === requestId) renderDraftAttachments();
}

async function revokeDraftAttachment(attachmentId) {
  if (!state.currentDraft?.id) return;
  const errorNode = el("draft-attachment-error");
  errorNode.textContent = "";
  try {
    await request(
      `/console/api/v1/drafts/${encodeURIComponent(state.currentDraft.id)}/attachments/${encodeURIComponent(attachmentId)}/revoke`,
      { method: "POST" },
    );
    await loadDraftAttachments(state.currentDraft.id);
    showToast("附件已撤销");
  } catch (error) {
    errorNode.textContent = error.message || "附件没有撤销，请重试。";
  }
}

async function generateCurrentDraft() {
  if (!state.currentDraft?.id || state.currentDraft.status !== "collecting") return;
  const button = el("draft-generate");
  const errorNode = el("draft-attachment-error");
  button.disabled = true;
  button.textContent = "正在冻结资料清单";
  errorNode.textContent = "";
  if (state.unavailableKnowledgeSelections.length) {
    button.disabled = true;
    button.textContent = "请先移除不可用资料";
    errorNode.textContent = "已选企业资料中有内容已撤销或不再可用，请取消选择并保存。";
    return;
  }
  try {
    const payload = await request(
      `/console/api/v1/drafts/${encodeURIComponent(state.currentDraft.id)}/generate`,
      { method: "POST" },
    );
    state.currentDraft = { ...state.currentDraft, ...payload.request, message: "已进入生成队列" };
    renderCurrentDraft(state.currentDraft);
    pollDraftRequest(state.currentDraft.id);
  } catch (error) {
    button.disabled = false;
    button.textContent = "确认资料并开始生成";
    errorNode.textContent = error.message || "资料清单没有冻结，请重试。";
  }
}

async function refreshDraftRequests() {
  const payload = await request("/console/api/v1/drafts?limit=10");
  syncDraftRequests(payload.requests || []);
}

async function pollDraftRequest(requestId) {
  clearTimeout(pollDraftRequest.timer);
  if (!requestId) return;
  try {
    const payload = await request(`/console/api/v1/drafts/${encodeURIComponent(requestId)}`);
    const requestItem = payload.request;
    state.currentDraft = requestItem;
    const existing = state.draftRequests.findIndex((item) => item.id === requestItem.id);
    if (existing >= 0) state.draftRequests.splice(existing, 1, requestItem);
    else state.draftRequests.unshift(requestItem);
    renderCurrentDraft(requestItem);
    renderDraftRequests();
    if (DRAFT_POLLING_STATUSES.has(requestItem.status)) {
      pollDraftRequest.timer = setTimeout(() => pollDraftRequest(requestId), 1000);
      return;
    }
    if (requestItem.status === "collecting") {
      await Promise.all([
        state.attachmentPlanningEnabled
          ? loadDraftAttachments(requestItem.id)
          : Promise.resolve(),
        state.enterpriseKnowledgeSelectionEnabled
          ? loadDraftKnowledgeSelection(requestItem.id)
          : Promise.resolve(),
      ]);
      return;
    }
    if (requestItem.status === "ready" && state.autoOpenDraftId === requestItem.id) {
      state.autoOpenDraftId = null;
      await openDraftInstance(requestItem);
    }
  } catch (error) {
    if (error.authentication) return;
    pollDraftRequest.timer = setTimeout(() => pollDraftRequest(requestId), 1800);
  }
}

async function submitDraftRequest(event) {
  event.preventDefault();
  const brief = el("draft-brief").value.trim();
  const context = el("draft-context").value.trim();
  const collaborator = el("draft-collaborator").value || null;
  const selectedFiles = Array.from(el("draft-attachments").files || []);
  const selectedKnowledge = Array.from(state.selectedKnowledgeSourceIds);
  const button = el("draft-submit");
  const errorNode = el("draft-form-error");
  errorNode.textContent = "";
  if (!brief) {
    errorNode.textContent = "请先描述要完成的工作。";
    el("draft-brief").focus();
    return;
  }
  if (selectedFiles.length && !state.attachmentPlanningEnabled) {
    errorNode.textContent = "当前部署未启用项目资料规划，请先移除附件后继续。";
    return;
  }
  if (selectedFiles.length > 8) {
    errorNode.textContent = "一次最多选择 8 个附件。";
    return;
  }
  let totalBytes = 0;
  for (const file of selectedFiles) {
    const lowerName = file.name.toLowerCase();
    if (!lowerName.endsWith(".txt") && !lowerName.endsWith(".md")) {
      errorNode.textContent = `不支持 ${file.name}，请选择 txt 或 md。`;
      return;
    }
    if (file.size < 1 || file.size > 32 * 1024) {
      errorNode.textContent = `${file.name} 必须大于 0 字节且不超过 32 KB。`;
      return;
    }
    totalBytes += file.size;
  }
  if (totalBytes > 128 * 1024) {
    errorNode.textContent = "附件总量不能超过 128 KB。";
    return;
  }
  const preparedFiles = [];
  try {
    for (const file of selectedFiles) {
      preparedFiles.push({
        file,
        content: new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()),
      });
    }
  } catch (_error) {
    errorNode.textContent = "附件必须是有效的 UTF-8 文本。";
    return;
  }
  const requestId = newDraftRequestId();
  button.disabled = true;
  button.dataset.state = "working";
  const deferred = selectedFiles.length > 0 || selectedKnowledge.length > 0;
  button.textContent = deferred ? "正在创建资料工作区" : "正在进入生成队列";
  state.autoOpenDraftId = requestId;
  const optimistic = {
    id: requestId,
    status: deferred ? "collecting" : "queued",
    message: deferred ? "正在保存本次授权资料选择" : "正在把请求写入中央耐久队列",
    brief,
    instance_id: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
  state.currentDraft = optimistic;
  renderCurrentDraft(optimistic);
  try {
    const payload = await request("/console/api/v1/drafts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: requestId,
        brief,
        context,
        collaborator_person_id: collaborator,
        defer_generation: deferred,
      }),
    });
    state.currentDraft = payload.request;
    state.draftRequests.unshift(payload.request);
    state.draftAttachments = [];
    renderCurrentDraft(payload.request);
    renderDraftRequests();
    if (selectedKnowledge.length) {
      const selection = await request(
        `/console/api/v1/drafts/${encodeURIComponent(requestId)}/knowledge-selection`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source_ids: selectedKnowledge,
            expected_version: 0,
          }),
        },
      );
      state.knowledgeSelectionVersion = selection.selection_version;
    }
    if (selectedFiles.length) {
      for (const prepared of preparedFiles) {
        const file = prepared.file;
        const lowerName = file.name.toLowerCase();
        const mediaType = lowerName.endsWith(".md") ? "text/markdown" : "text/plain";
        const uploaded = await request(
          `/console/api/v1/drafts/${encodeURIComponent(requestId)}/attachments`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              display_filename: file.name,
              media_type: mediaType,
              content: prepared.content,
            }),
          },
        );
        state.draftAttachments.push(uploaded.attachment);
        renderDraftAttachments();
      }
      button.textContent = "资料已上传，请在右侧确认";
      el("draft-attachments").value = "";
      el("draft-attachment-selection").textContent = "资料已上传并绑定到当前草稿请求。";
      showToast("资料已上传，确认清单后开始生成");
    } else if (selectedKnowledge.length) {
      renderDraftAttachments();
      button.textContent = "资料已选择，请在右侧确认";
      showToast("企业资料已选择，确认清单后开始生成");
    } else {
      pollDraftRequest(requestId);
    }
  } catch (error) {
    state.autoOpenDraftId = null;
    const collecting = state.currentDraft?.status === "collecting";
    button.disabled = collecting;
    button.dataset.state = collecting ? "working" : "idle";
    button.textContent = collecting ? "资料未全部上传，请检查右侧状态" : "重新提交";
    errorNode.textContent = error.message || "草稿请求没有创建，请重试。";
    if (!collecting) el("draft-current").dataset.terminal = "true";
  }
}

async function openDraftInstance(requestItem = state.currentDraft) {
  if (!requestItem?.instance_id) return;
  const button = el("draft-open-instance");
  button.disabled = true;
  button.textContent = "正在打开流程";
  state.returnSection = "drafts";
  showDetailLoading(requestItem.brief || "正在读取流程草稿");
  try {
    await loadDetail(requestItem.instance_id);
    setDetailTab("overview");
    showToast("草稿已生成，请核对流程后确认启动");
  } catch (error) {
    showOwnerSection("drafts");
    button.disabled = false;
    button.textContent = "重新打开流程";
    showToast("流程草稿读取失败，请稍后重试", "error");
    throw error;
  }
}

async function loadDraftCollaborators() {
  const select = el("draft-collaborator");
  try {
    const payload = await request("/console/api/v1/people?limit=100");
    select.replaceChildren(node("option", "", "由我负责全部人工步骤"));
    select.firstChild.value = "";
    (payload.people || []).forEach((person) => {
      const option = node("option", "", person.name || "企业成员");
      option.value = person.person_id;
      select.append(option);
    });
    select.disabled = false;
  } catch (_error) {
    select.replaceChildren(node("option", "", "成员目录暂时不可用，可先由我负责"));
    select.firstChild.value = "";
    select.disabled = true;
  }
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
  draftStudio.hidden = section !== "drafts";
  attentionCenter.hidden = section !== "attention";
  workflowLibrary.hidden = section !== "workflows";
  detailView.hidden = section !== "detail";
  emptyState.hidden = section !== "loading";
  draftNav.dataset.active = String(section === "drafts");
  attentionNav.dataset.active = String(section === "attention");
  workflowNav.dataset.active = String(section === "workflows");
  if (
    section === "detail"
    && el("detail-advanced").open
    && state.detailTab === "execution"
  ) {
    requestAnimationFrame(layoutGraphCanvas);
  }
  window.scrollTo({ top: 0, behavior: "auto" });
}

function showDetailLoading(goal) {
  emptyState.querySelector("h2").textContent = "正在读取流程";
  emptyState.querySelector("p:last-child").textContent = goal || "请稍候，正在读取最新状态。";
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
  el("attention-scope").textContent = `汇总最近 ${attention.instance_limit || 30} 个本人流程，以及当前分配给你的人工任务和判断`;

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
      node("p", "", "草稿确认、人工任务、结果判断、暂停流程和失败恢复会出现在这里。"),
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
  if (!["human_task", "human_decision"].includes(item.action?.kind)) {
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
  if (action.kind === "human_decision") return "查看并判断";
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
  throw new Error("当前操作暂时不可用");
}

async function runAttentionAction(item, card, button) {
  button.disabled = true;
  button.dataset.state = "working";
  if (["human_task", "human_decision"].includes(item.action.kind)) {
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
  el("human-task-deliverable-fields").replaceChildren();
  el("human-task-deliverable-fields").hidden = true;
  el("human-task-legacy-field").hidden = false;
  el("human-task-error").textContent = "";
  el("human-task-submit-panel").hidden = false;
  el("human-task-actions").hidden = false;
  el("human-decision-panel").hidden = true;
  el("human-decision-feedback").value = "";
  el("human-decision-feedback").disabled = false;
  el("human-decision-feedback-count").textContent = "0";
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
  ["human-decision-accept", "human-decision-reject"].forEach((id) => {
    const decisionButton = el(id);
    decisionButton.disabled = false;
    decisionButton.dataset.state = "idle";
    decisionButton.textContent = id.endsWith("accept") ? "接受并继续" : "退回修改";
  });
}

function renderHumanTask(task) {
  const isDecision = task.kind === "decision";
  el("human-task-title").textContent = task.node.title;
  el("human-task-kind-label").textContent = isDecision ? "需要你判断" : "需要你完成";
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
  renderHumanTaskContext(task.work.context || {});
  renderHumanDeliverableFields(task.work.outputs || []);
  el("human-task-submit-panel").hidden = isDecision;
  el("human-task-actions").hidden = isDecision;
  el("human-decision-panel").hidden = !isDecision;
  el("human-task-transfer-open").hidden = isDecision || !task.actions.transfer;
  el("human-task-loading").hidden = true;
  el("human-task-content").hidden = false;
  requestAnimationFrame(() => {
    if (isDecision) el("human-decision-accept").focus();
    else (el("human-task-deliverable-fields").querySelector("input, textarea, select")
      || el("human-task-result")).focus();
  });
}

function renderHumanDeliverableFields(outputs) {
  const container = el("human-task-deliverable-fields");
  container.replaceChildren();
  const structured = Array.isArray(outputs)
    && outputs.some((output) => output && output.required === true);
  container.hidden = !structured;
  el("human-task-legacy-field").hidden = structured;
  if (!structured) return;

  outputs.forEach((output) => {
    if (!output || !output.id) return;
    const field = node("label", "human-deliverable-field");
    const title = node("span", "", output.label || output.id);
    if (output.required) title.append(node("em", "", "必填"));
    field.append(title);
    let input;
    if (output.type === "choice") {
      input = document.createElement("select");
      input.append(node("option", "", "请选择"));
      input.firstElementChild.value = "";
      (output.options || []).forEach((choice) => {
        const option = node("option", "", choice);
        option.value = choice;
        input.append(option);
      });
    } else if (["long_text", "data", "object", "document", "file", "string_list"].includes(output.type)) {
      input = document.createElement("textarea");
      input.rows = output.type === "long_text" ? 5 : 3;
      input.maxLength = 12000;
    } else {
      input = document.createElement("input");
      input.type = ({
        boolean: "checkbox",
        date: "date",
        integer: "number",
        money: "number",
        number: "number",
        url: "url",
      })[output.type] || "text";
      if (output.type === "integer") input.step = "1";
      if (output.type === "number" || output.type === "money") input.step = "any";
      if (input.type !== "checkbox") input.maxLength = 12000;
    }
    input.dataset.outputId = output.id;
    input.dataset.outputType = output.type || "text";
    input.required = Boolean(output.required) && input.type !== "checkbox";
    if (output.placeholder) input.placeholder = output.placeholder;
    field.append(input);
    if (output.help) field.append(node("small", "", output.help));
    container.append(field);
  });
}

function humanDeliverableResult() {
  const container = el("human-task-deliverable-fields");
  if (container.hidden) {
    const content = el("human-task-result").value.trim();
    if (!content) throw new Error("请先输入处理结果。");
    return { content };
  }
  const result = {};
  container.querySelectorAll("[data-output-id]").forEach((input) => {
    const outputId = input.dataset.outputId;
    const type = input.dataset.outputType;
    let value;
    if (type === "boolean") value = input.checked;
    else if (type === "integer") value = input.value === "" ? null : Number.parseInt(input.value, 10);
    else if (["number", "money"].includes(type)) value = input.value === "" ? null : Number(input.value);
    else if (type === "string_list") {
      value = input.value.split("\n").map((item) => item.trim()).filter(Boolean);
    } else if (["data", "object"].includes(type)) {
      const raw = input.value.trim();
      if (!raw) value = null;
      else {
        try { value = JSON.parse(raw); }
        catch (_error) { throw new Error(`${input.closest("label")?.querySelector("span")?.textContent || outputId}需要填写有效 JSON`); }
      }
    } else value = input.value.trim();
    if (value !== null && value !== "" && !(Array.isArray(value) && value.length === 0)) {
      result[outputId] = value;
    }
  });
  return result;
}

function renderHumanTaskContext(context) {
  const container = el("human-task-context");
  container.replaceChildren();
  const sections = [
    ["原始输入", context.instance_inputs],
    ["上游结果", context.dependencies],
    ["返工意见", context.rework_feedback],
  ].filter(([, value]) => value !== undefined && value !== null && (
    typeof value !== "object" || Object.keys(value).length > 0
  ));
  if (sections.length === 0) {
    container.append(node("p", "human-task-context-empty", "当前任务没有额外输入。"));
    return;
  }
  sections.forEach(([label, value]) => {
    const article = node("article", "human-task-context-section");
    article.append(node("strong", "", label));
    const body = node("pre", "", readableValue(value));
    article.append(body);
    container.append(article);
  });
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

async function reloadAfterTaskMutation(instanceId) {
  const keepDetail = state.ownerSection === "detail" && state.detail?.instance.id === instanceId;
  await loadInstances(keepDetail ? instanceId : null);
}

async function submitHumanTaskFromPage() {
  const button = el("human-task-submit");
  const transfer = el("human-task-transfer-open");
  el("human-task-error").textContent = "";
  let result;
  try {
    result = humanDeliverableResult();
  } catch (error) {
    el("human-task-error").textContent = error.message || "请填写节点交付物。";
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
        result,
      }),
    );
    button.dataset.state = "done";
    button.textContent = "已提交，流程已推进";
    el("human-task-result").disabled = true;
    showToast("待办已提交，中央工作流正在继续");
    await reloadAfterTaskMutation(task.instance_id);
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

async function submitHumanDecisionFromPage(decision) {
  const accept = el("human-decision-accept");
  const reject = el("human-decision-reject");
  const feedbackInput = el("human-decision-feedback");
  const feedback = feedbackInput.value.trim();
  el("human-task-error").textContent = "";
  if (decision === "reject" && !feedback) {
    el("human-task-error").textContent = "退回时请说明需要修改什么。";
    feedbackInput.focus();
    return;
  }
  accept.disabled = true;
  reject.disabled = true;
  feedbackInput.disabled = true;
  const activeButton = decision === "accept" ? accept : reject;
  activeButton.dataset.state = "working";
  activeButton.textContent = decision === "accept" ? "正在接受" : "正在退回";
  try {
    const { task, instanceId, nodeKey } = currentTaskBinding();
    await request(
      `/console/api/v1/tasks/${instanceId}/nodes/${nodeKey}/decision`,
      taskWriteOptions({
        attempt_no: task.node.attempt_no,
        expected_instance_version: task.instance_version,
        expected_node_version: task.node.version,
        decision,
        feedback: decision === "reject" ? feedback : null,
      }),
    );
    activeButton.dataset.state = "done";
    activeButton.textContent = decision === "accept" ? "已接受" : "已退回";
    showToast(decision === "accept" ? "结果已接受，流程正在继续" : "结果已退回，意见已经保存");
    await reloadAfterTaskMutation(task.instance_id);
    setTimeout(() => humanTaskDialog.open && humanTaskDialog.close(), 800);
  } catch (error) {
    accept.disabled = false;
    reject.disabled = false;
    feedbackInput.disabled = false;
    activeButton.dataset.state = "error";
    activeButton.textContent = decision === "accept" ? "接受失败，请重试" : "退回失败，请重试";
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
    const result = await request(
      `/console/api/v1/tasks/${instanceId}/nodes/${nodeKey}/transfer`,
      taskWriteOptions({
        attempt_no: task.node.attempt_no,
        expected_node_version: task.node.version,
        new_owner_person_id: newOwner,
      }),
    );
    button.dataset.state = "done";
    button.textContent = "负责人已更换，飞书同步中";
    el("human-task-result").disabled = true;
    showToast(
      result.projection?.message || "负责人已更换，飞书待办正在同步。",
    );
    await reloadAfterTaskMutation(task.instance_id);
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
  if (payload.action === "restart") return "已创建新一轮执行";
  if (payload.action === "graph_edit") return "流程图修改已应用";
  return "操作已完成";
}

function workflowActionError(error) {
  if (["preview_stale", "preview_expired", "state_conflict"].includes(error.code)) {
    return "状态已变化";
  }
  if (error.code === "edit_not_allowed") return "当前节点不可修改";
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
    node("p", "", `将影响 ${affected.length} 个节点。旧执行记录、结果和审计都会保留。`),
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
    state.detailTab = "overview";
    el("detail-advanced").open = false;
  }
  renderInstances();
  renderDetail();
}

function chooseNode(nodes) {
  return nodes.find((item) => ["running", "waiting_human", "failed"].includes(item.status))?.key
    || nodes[nodes.length - 1]?.key
    || null;
}

function setWorkflowFilter(filter, rerender = true) {
  state.workflowFilter = filter;
  el("workflow-filters").querySelectorAll("button").forEach((button) => {
    button.dataset.active = String(button.dataset.filter === filter);
  });
  if (rerender) renderInstances();
}

function renderInstances() {
  instanceList.replaceChildren();
  const openInstances = state.instances.filter((item) => ["draft", "running", "paused", "failed"].includes(item.status));
  el("instance-count").textContent = openInstances.length;
  const query = state.workflowQuery.trim().toLocaleLowerCase("zh-CN");
  const visible = state.instances.filter((item) => {
    const matchesQuery = !query
      || item.id.toLocaleLowerCase("zh-CN").includes(query)
      || (item.goal || "").toLocaleLowerCase("zh-CN").includes(query);
    if (!matchesQuery) return false;
    if (state.workflowFilter === "all") return true;
    if (state.workflowFilter === "open") return ["draft", "running", "paused", "failed"].includes(item.status);
    if (state.workflowFilter === "active") return ["running", "paused"].includes(item.status);
    if (state.workflowFilter === "draft") return item.status === "draft";
    if (state.workflowFilter === "done") return item.status === "done";
    return !["running", "paused", "draft", "done"].includes(item.status);
  });
  if (state.instances.length === 0) {
    const empty = node("div", "list-empty");
    empty.append(node("strong", "", "还没有流程"), node("p", "", "点击左侧“发起流程”，描述目标后先生成一份草稿。"));
    instanceList.append(empty);
    return;
  }
  if (visible.length === 0) {
    const empty = node("div", "list-empty");
    if (state.workflowFilter === "open" && !query) {
      const start = node("button", "list-empty-action", "发起一项工作");
      start.type = "button";
      start.addEventListener("click", () => {
        showOwnerSection("drafts");
        loadDraftCollaborators().catch(showWorkspaceError);
      });
      empty.append(
        node("strong", "", "没有需要继续跟进的流程"),
        node("p", "", "已经结束的记录默认隐藏，可以切换到“已完成”或“全部”查看。"),
        start,
      );
    } else {
      empty.append(node("strong", "", "没有匹配的流程"), node("p", "", "可以清除搜索词或切换状态筛选。"));
    }
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
  el("detail-created").textContent = `创建于 ${formatDate(instance.created_at)} · 流程版本 ${instance.version}`;
  el("progress-value").textContent = `${progress.completed_nodes}/${progress.total_nodes}`;
  const percent = progress.total_nodes ? Math.round(progress.completed_nodes / progress.total_nodes * 100) : 0;
  el("progress-ring").style.setProperty("--progress", `${percent * 3.6}deg`);
  el("graph-revision").textContent = `流程图版本 ${instance.graph_revision}`;
  el("graph-action-preview").replaceChildren();
  renderWorkflowJourney(instance);
  renderWorkflowNextStep(instance);
  renderDetailActions(instance);
  renderInsights(payload);
  renderWorkflowResults(payload.nodes, instance);
  renderOverviewNodes(payload.nodes);
  renderGraph(payload.nodes);
  renderAttempts(payload.nodes.find((item) => item.key === state.selectedNode));
  renderAudit(payload.audit);
  setDetailTab(state.detailTab);
}

const WORKFLOW_JOURNEY = [
  "描述目标",
  "核对流程",
  "确认启动",
  "完成或判断",
  "查看结果",
];

function workflowJourneyPosition(instance) {
  if (instance.status === "draft") return 1;
  if (["running", "paused", "failed"].includes(instance.status)) return 3;
  if (instance.status === "discarded") return 1;
  if (instance.status === "canceled") return 3;
  return 4;
}

function renderWorkflowJourney(instance) {
  const container = el("workflow-journey");
  const position = workflowJourneyPosition(instance);
  container.replaceChildren();
  WORKFLOW_JOURNEY.forEach((label, index) => {
    const item = node("li", "workflow-journey-step");
    item.dataset.state = index < position ? "done" : index === position ? "current" : "upcoming";
    item.append(
      node("span", "workflow-journey-number", index < position ? "✓" : index + 1),
      node("span", "workflow-journey-label", label),
    );
    container.append(item);
  });
  container.dataset.status = instance.status;
}

function taskForInstance(instanceId) {
  return state.humanTasks.find((task) => task.instance_id === instanceId) || null;
}

function renderWorkflowNextStep(instance) {
  const container = el("workflow-next-step");
  const task = taskForInstance(instance.id);
  const copy = node("div", "workflow-next-step-copy");
  const controls = node("div", "workflow-next-step-controls");
  let title = "查看流程状态";
  let description = "根据当前状态决定下一步。";

  if (instance.status === "draft") {
    title = "核对步骤，然后启动";
    description = "确认下方每一步和负责人符合预期。启动后，系统才会通知参与人并开始执行。";
    controls.append(detailActionButton(
      "确认并启动流程",
      "primary",
      (button) => runDetailDirectAction(instance, "confirm", button),
    ));
  } else if (instance.status === "running" && task) {
    const isDecision = task.kind === "decision";
    title = isDecision ? `现在由你判断：${task.node.title}` : `现在由你处理：${task.node.title}`;
    description = isDecision
      ? "查看已有结果后，直接在本页接受或填写意见退回。"
      : "直接在本页提交结果，或者把这项待办转交给其他成员。";
    const item = humanTaskAttentionItem(task);
    const label = attentionActionLabel(item.action);
    const button = detailActionButton(label, "primary", (sourceButton) => {
      sourceButton.disabled = true;
      sourceButton.dataset.state = "working";
      sourceButton.textContent = "正在打开";
      openHumanTask(item, sourceButton);
    });
    button.dataset.defaultLabel = label;
    controls.append(button);
  } else if (instance.status === "running") {
    const activeNode = state.detail?.nodes.find((item) => ["running", "waiting_human", "ready"].includes(item.status));
    title = "流程正在推进，目前无需你操作";
    description = activeNode
      ? `当前步骤是“${activeNode.title}”。轮到你处理时，入口会直接出现在这里，飞书也会同步提醒。`
      : "系统正在调度后续步骤。轮到你处理时，入口会直接出现在这里。";
  } else if (instance.status === "paused") {
    title = "流程已暂停";
    description = "确认当前情况后，可以继续推进。取消流程属于其他操作。";
    controls.append(detailActionButton(
      "继续流程",
      "primary",
      (button) => runDetailDirectAction(instance, "resume", button),
    ));
  } else if (instance.status === "failed") {
    title = "流程需要恢复";
    description = "先查看重新执行会影响哪些步骤，确认后再创建新一轮，旧结果和审计都会保留。";
    controls.append(detailActionButton(
      "查看恢复影响",
      "primary",
      (button) => previewDetailAction(instance, "restart", button),
    ));
  } else if (instance.status === "done") {
    title = "流程已经完成";
    description = "结果已整理在下方。只有确实需要调整时，才从其他操作重新执行。";
    const button = detailActionButton("查看流程结果", "primary", () => {
      const resultPanel = el("workflow-results-panel");
      const target = resultPanel.hidden ? el("instance-insights") : resultPanel;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    controls.append(button);
  } else if (instance.status === "canceled") {
    title = "流程已经取消";
    description = "执行记录仍然保留，当前不需要继续处理。";
  } else if (instance.status === "discarded") {
    title = "草稿已经废弃";
    description = "如需继续这项工作，请重新发起一份流程草稿。";
  }

  copy.append(node("span", "workflow-next-step-kicker", "当前要做"), node("strong", "", title), node("p", "", description));
  container.replaceChildren(copy, controls);
  container.dataset.status = instance.status;
}

function renderDetailActions(instance) {
  const container = el("detail-actions");
  container.replaceChildren();
  const controls = node("div", "detail-action-controls");

  if (instance.status === "running") {
    controls.append(detailActionButton("暂停流程", "secondary", (button) => runDetailDirectAction(instance, "pause", button)));
    controls.append(detailActionButton("重新执行", "secondary", (button) => previewDetailAction(instance, "restart", button)));
    controls.append(detailActionButton("取消流程", "danger", (button) => previewDetailAction(instance, "cancel", button)));
  } else if (instance.status === "paused") {
    controls.append(detailActionButton("取消流程", "danger", (button) => previewDetailAction(instance, "cancel", button)));
  } else if (instance.status === "done") {
    controls.append(detailActionButton("重新执行", "secondary", (button) => previewDetailAction(instance, "restart", button)));
  }

  if (controls.childElementCount === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  const disclosure = document.createElement("details");
  disclosure.className = "detail-actions-disclosure";
  const summary = document.createElement("summary");
  const copy = node("span", "detail-actions-copy");
  copy.append(
    node("strong", "", "其他操作"),
    node("span", "", "暂停、取消或重新执行时使用"),
  );
  summary.append(copy, node("span", "detail-actions-boundary", "所有操作都会重新校验权限和状态"));
  disclosure.append(summary, controls);
  container.append(disclosure);
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
      el("detail-advanced").open = true;
      setDetailTab("execution");
      el("detail-advanced").scrollIntoView({ behavior: "smooth", block: "start" });
      showToast(`已在高级视图打开：${item.title}`);
    });
    const ordinal = node("span", "overview-node-ordinal", String(index + 1).padStart(2, "0"));
    const copy = node("span", "overview-node-copy");
    const title = node("span", "overview-node-title");
    title.append(node("strong", "", item.title), statusBadge(item.status));
    copy.append(
      title,
      node("small", "", `${EXECUTOR[item.executor] || item.executor} · ${RELATION[item.owner_relation] || item.owner_relation} · 第 ${item.current_attempt_no} 次执行`),
    );
    const action = node("span", "overview-node-action", "查看结果");
    button.append(ordinal, copy, action);
    container.append(button);
  });
}

function renderWorkflowResults(nodes, instance) {
  const panel = el("workflow-results-panel");
  const container = el("workflow-results");
  const results = [];
  [...nodes].reverse().forEach((item) => {
    const attempt = [...item.attempts].reverse().find((candidate) => candidate.result !== null);
    if (attempt) results.push({ node: item, attempt });
  });
  results.sort((left, right) => {
    const priority = (entry) => {
      const keys = entry.attempt.result && typeof entry.attempt.result === "object"
        ? Object.keys(entry.attempt.result)
        : [];
      if (keys.length > 0 && keys.every((key) => ["decision", "feedback"].includes(key))) return 2;
      if (["agent", "tool"].includes(entry.node.executor)) return 0;
      return 1;
    };
    return priority(left) - priority(right);
  });

  container.replaceChildren();
  el("workflow-results-count").textContent = results.length;
  panel.hidden = results.length === 0;
  if (results.length === 0) return;
  el("workflow-results-title").textContent = instance.status === "done" ? "流程结果" : "已产生的结果";

  results.forEach((entry, index) => {
    const item = document.createElement("details");
    item.className = "workflow-result-item";
    item.open = index === 0;
    const summary = document.createElement("summary");
    const copy = node("span", "workflow-result-heading");
    copy.append(
      node("strong", "", entry.node.title),
      node("small", "", `第 ${entry.attempt.attempt_no} 次执行 · ${statusLabel(entry.attempt.status)}`),
    );
    summary.append(copy, node("span", "workflow-result-toggle", index === 0 ? "已展开" : "查看"));
    item.addEventListener("toggle", () => {
      item.querySelector(".workflow-result-toggle").textContent = item.open ? "已展开" : "查看";
    });
    const body = node("div", "workflow-result-body");
    const pre = node("pre", "");
    pre.textContent = readableValue(entry.attempt.result);
    body.append(pre);
    item.append(summary, body);
    container.append(item);
  });
}

function renderInsights(payload) {
  const instance = payload.instance;
  const progress = instance.progress;
  const insights = payload.insights || { reworked_nodes: [], latest_restart: null };

  const status = el("insight-status");
  status.replaceChildren(statusBadge(instance.status));
  el("insight-status-detail").textContent = `${progress.completed_nodes}/${progress.total_nodes} 个节点完成 · 流程版本 ${instance.version}`;

  const rework = el("insight-rework");
  rework.replaceChildren();
  if (insights.reworked_nodes.length === 0) {
    rework.append(node("p", "insight-empty", "未发现多轮执行节点"));
  } else {
    insights.reworked_nodes.forEach((item) => {
      const row = node("div", "insight-node");
      const copy = node("div", "insight-node-copy");
      copy.append(node("strong", "", item.title), node("span", "mono", item.key));
      row.append(copy, node("span", "attempt-chip", `第 ${item.current_attempt_no} 次`));
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
    node("p", "", `影响 ${event.affected_nodes.length} 个节点${event.attempt_no ? ` · 新一轮为第 ${event.attempt_no} 次` : ""}`),
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

function ensureCanvasBundle() {
  if (window.LarkflowCanvas) return Promise.resolve(window.LarkflowCanvas);
  if (canvasLoadPromise) return canvasLoadPromise;
  canvasLoadPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/console/canvas.js";
    script.async = true;
    script.addEventListener("load", () => {
      if (window.LarkflowCanvas) resolve(window.LarkflowCanvas);
      else reject(new Error("流程画板初始化失败"));
    }, { once: true });
    script.addEventListener("error", () => reject(new Error("流程画板资源加载失败")), { once: true });
    document.head.append(script);
  });
  return canvasLoadPromise;
}

function renderGraphFallback(nodes, message) {
  const graph = el("graph");
  graph.replaceChildren();
  const fallback = node("div", "graph-fallback");
  fallback.append(
    node("strong", "", message),
    node("p", "", "仍可从下方节点列表选择并查看执行记录。"),
  );
  nodes.forEach((item) => {
    const button = node("button", "graph-fallback-node");
    button.type = "button";
    button.dataset.selected = String(state.selectedNode === item.key);
    button.append(
      node("span", "", item.title),
      node("code", "", item.key),
      statusBadge(item.status),
    );
    button.addEventListener("click", () => {
      state.selectedNode = item.key;
      renderGraphFallback(nodes, message);
      renderAttempts(item);
    });
    fallback.append(button);
  });
  graph.append(fallback);
}

function mountGraphCanvas(nodes) {
  const graph = el("graph");
  const instance = state.detail?.instance;
  window.LarkflowCanvas.render(graph, {
    items: nodes,
    selectedNode: state.selectedNode,
    instanceId: instance?.id || "",
    editable: ["draft", "running"].includes(instance?.status),
    restartable: ["running", "done", "failed"].includes(instance?.status),
    onNodeSelect: (nodeKey) => {
      state.selectedNode = nodeKey;
      updateGraphSelection(nodes);
      renderAttempts(nodes.find((item) => item.key === nodeKey));
    },
    onRequestAdd: () => openGraphEditor("add"),
    onRequestEdit: (nodeKey) => openGraphEditor("edit", nodeKey),
    onRequestConnect: (sourceKey, targetKey) => previewGraphDependency(sourceKey, targetKey, true),
    onRequestDisconnect: (sourceKey, targetKey) => previewGraphDependency(sourceKey, targetKey, false),
    onRequestRestart: (nodeKey) => previewNodeRestart(nodeKey),
  });
}

function renderGraph(nodes) {
  if (nodes.length === 0) {
    renderGraphFallback(nodes, "草稿中没有节点。");
    return;
  }
  if (window.LarkflowCanvas) {
    mountGraphCanvas(nodes);
    return;
  }
  renderGraphFallback(nodes, "正在启动流程画板");
  ensureCanvasBundle()
    .then(() => {
      const currentNodes = state.detail?.nodes || nodes;
      mountGraphCanvas(currentNodes);
      if (state.detailTab === "execution") layoutGraphCanvas();
    })
    .catch((error) => {
      renderGraphFallback(nodes, error.message);
      showToast(error.message, "error");
    });
}

function updateGraphSelection(nodes) {
  if (window.LarkflowCanvas) mountGraphCanvas(nodes);
  else renderGraph(nodes);
}

function layoutGraphCanvas() {
  if (window.LarkflowCanvas) window.LarkflowCanvas.refresh(el("graph"));
}

async function openGraphEditor(mode, nodeKey = null) {
  const instance = state.detail?.instance;
  const nodes = state.detail?.nodes || [];
  if (!instance || !["draft", "running"].includes(instance.status)) {
    showToast("只有草稿或运行中的流程可以修改", "error");
    return;
  }
  const selected = mode === "edit"
    ? nodes.find((item) => item.key === nodeKey)
    : null;
  if (mode === "edit" && !selected) {
    showToast("请先选择要修改的节点", "error");
    return;
  }
  state.graphEditor = { mode, nodeKey: selected?.key || null };
  el("graph-edit-form").dataset.mode = mode;
  el("graph-edit-title").textContent = mode === "add" ? "增加流程节点" : "编辑流程节点";
  const isDraft = instance.status === "draft";
  el("graph-edit-boundary").textContent = mode === "add"
    ? (isDraft
      ? "新节点会先加入草稿，确认修改后仍不会自动启动流程。"
      : "新节点先进入未来区域，提交后生成可核对的修改预览。")
    : (isDraft
      ? "草稿中的节点可以修改，确认修改后仍需单独确认启动。"
      : "只能修改尚未开始执行且没有执行历史的未来节点。");
  el("graph-edit-key").value = selected?.key || "";
  el("graph-edit-title-input").value = selected?.title || "";
  el("graph-edit-executor").value = selected?.executor || "human";
  const toolOption = el("graph-edit-executor").querySelector('option[value="tool"]');
  toolOption.disabled = mode === "edit" && selected?.executor !== "tool";
  el("graph-edit-work-fields").hidden = mode === "edit";
  el("graph-edit-objective").value = "";
  el("graph-edit-acceptance").value = "";
  el("graph-edit-output-label").value = "";
  el("graph-edit-remove").hidden = mode !== "edit";
  el("graph-edit-error").textContent = "";
  renderGraphDependencyOptions(nodes, selected);
  renderGraphBeforeOptions(nodes, mode);
  resetGraphOwnerOptions(mode);
  graphEditDialog.showModal();
  el("graph-edit-title-input").focus();
  loadGraphEditorPeople(state.graphEditor).catch(() => {});
}

function renderGraphBeforeOptions(nodes, mode) {
  const fieldset = el("graph-edit-before");
  const container = el("graph-edit-before-list");
  fieldset.hidden = mode !== "add";
  container.replaceChildren();
  if (mode !== "add") return;
  nodes.forEach((item) => {
    const option = node("label", "graph-edit-dependency-option");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = item.key;
    option.append(checkbox, node("span", "", item.title), node("code", "", item.key));
    container.append(option);
  });
  if (container.childElementCount === 0) {
    container.append(node("p", "muted", "当前没有需要增加前置条件的节点。"));
  }
}

function renderGraphDependencyOptions(nodes, selected) {
  const container = el("graph-edit-dependency-list");
  container.replaceChildren();
  const dependencies = new Set(selected?.deps || []);
  nodes.filter((item) => item.key !== selected?.key).forEach((item) => {
    const option = node("label", "graph-edit-dependency-option");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = item.key;
    checkbox.checked = dependencies.has(item.key);
    option.append(checkbox, node("span", "", item.title), node("code", "", item.key));
    container.append(option);
  });
  if (container.childElementCount === 0) {
    container.append(node("p", "muted", "当前没有可选上游节点。"));
  }
}

function resetGraphOwnerOptions(mode) {
  const select = el("graph-edit-owner");
  select.replaceChildren();
  if (mode === "edit") {
    const keep = node("option", "", "保持当前负责人");
    keep.value = "";
    select.append(keep);
  }
  const current = node("option", "", "我（当前登录成员）");
  current.value = "__current_user__";
  select.append(current);
  select.value = mode === "add" ? "__current_user__" : "";
}

async function loadGraphEditorPeople(editor) {
  const payload = await request("/console/api/v1/people?limit=100");
  if (state.graphEditor !== editor || !graphEditDialog.open) return;
  const select = el("graph-edit-owner");
  (payload.people || []).forEach((person) => {
    const option = node("option", "", person.name || "企业成员");
    option.value = person.person_id;
    select.append(option);
  });
}

function graphEditDependencies() {
  return Array.from(el("graph-edit-dependency-list").querySelectorAll("input:checked"))
    .map((item) => item.value);
}

function graphEditBeforeNodes() {
  return Array.from(el("graph-edit-before-list").querySelectorAll("input:checked"))
    .map((item) => item.value);
}

function graphEditOperation() {
  const mode = state.graphEditor?.mode;
  const title = el("graph-edit-title-input").value.trim();
  const executor = el("graph-edit-executor").value;
  const owner = el("graph-edit-owner").value;
  const deps = graphEditDependencies();
  if (!title) throw new Error("请填写节点名称");
  if (mode === "add") {
    const acceptance = el("graph-edit-acceptance").value
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);
    const objective = el("graph-edit-objective").value.trim() || title;
    const outputLabel = el("graph-edit-output-label").value.trim()
      || (executor === "human" ? "处理结果" : "生成结果");
    const work = {
      objective,
      inputs: deps.map((item) => `dependencies.${item}`),
      outputs: [{
        id: "content",
        label: outputLabel,
        type: executor === "human" ? "long_text" : "text",
        required: true,
      }],
      acceptance: acceptance.length ? acceptance : [`${title} 已完成`],
    };
    if (executor === "agent") {
      work.agent = {
        kind: "llm.generate",
        model_role: "default",
        instructions: objective,
      };
    }
    return {
      op: "add_node",
      node: {
        title,
        owner_person_id: owner || "__current_user__",
        executor,
        deps,
        insert_before: graphEditBeforeNodes(),
        work,
      },
    };
  }
  const current = state.detail.nodes.find((item) => item.key === state.graphEditor?.nodeKey);
  if (!current) throw new Error("节点已经不存在，请刷新后重试");
  const changes = {};
  if (title !== current.title) changes.title = title;
  if (executor !== current.executor) changes.executor = executor;
  if (JSON.stringify(deps) !== JSON.stringify(current.deps || [])) changes.deps = deps;
  if (owner) changes.owner_person_id = owner;
  if (Object.keys(changes).length === 0) throw new Error("请先修改至少一项内容");
  return { op: "update_node", node_key: current.key, set: changes };
}

async function submitGraphEditForm(event) {
  event.preventDefault();
  const button = el("graph-edit-submit");
  const errorNode = el("graph-edit-error");
  errorNode.textContent = "";
  button.disabled = true;
  button.textContent = "正在生成预览";
  try {
    const payload = await createGraphEditPreview([graphEditOperation()]);
    graphEditDialog.close();
    renderGraphEditPreview(payload);
  } catch (error) {
    errorNode.textContent = error.message || "修改预览生成失败，请重试。";
  } finally {
    button.disabled = false;
    button.textContent = "生成修改预览";
  }
}

async function removeGraphNode() {
  const current = state.detail?.nodes.find((item) => item.key === state.graphEditor?.nodeKey);
  if (!current) return;
  const button = el("graph-edit-remove");
  button.disabled = true;
  button.textContent = "正在生成删除预览";
  el("graph-edit-error").textContent = "";
  try {
    const payload = await createGraphEditPreview([
      { op: "remove_node", node_key: current.key },
    ]);
    graphEditDialog.close();
    renderGraphEditPreview(payload);
  } catch (error) {
    el("graph-edit-error").textContent = error.message || "删除预览生成失败，请重试。";
  } finally {
    button.disabled = false;
    button.textContent = "删除此节点";
  }
}

function createGraphEditPreview(operations) {
  const instanceId = state.detail?.instance.id;
  showToast("正在校验流程修改并生成预览");
  return request(
    `/console/api/v1/instances/${encodeURIComponent(instanceId)}/graph-edit-preview`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operations }),
    },
  );
}

async function previewGraphDependency(sourceKey, targetKey, connect) {
  const target = state.detail?.nodes.find((item) => item.key === targetKey);
  if (!target || sourceKey === targetKey) {
    showToast("请选择两个不同的有效节点", "error");
    return;
  }
  const dependencies = new Set(target.deps || []);
  if (connect) dependencies.add(sourceKey);
  else dependencies.delete(sourceKey);
  if (JSON.stringify(Array.from(dependencies)) === JSON.stringify(target.deps || [])) {
    showToast(connect ? "该依赖已经存在" : "该依赖已经移除");
    return;
  }
  showToast(connect ? "正在校验新增依赖" : "正在校验断开依赖");
  try {
    const payload = await createGraphEditPreview([{
      op: "update_node",
      node_key: targetKey,
      set: { deps: Array.from(dependencies) },
    }]);
    renderGraphEditPreview(payload);
  } catch (error) {
    showToast(error.message || "依赖修改预览生成失败，请重试", "error");
    throw error;
  }
}

function renderGraphEditPreview(payload) {
  const container = el("graph-action-preview");
  container.replaceChildren();
  const preview = payload.preview;
  const panel = node("section", "workflow-action-preview");
  panel.dataset.action = "graph_edit";
  const copy = node("div", "workflow-action-preview-copy");
  copy.append(
    node("strong", "", `确认把流程版本 ${preview.graph_revision} 更新为 ${preview.proposed_graph_revision}？`),
    node("p", "", state.detail?.instance.status === "draft"
      ? "系统已检查权限和流程结构。确认后只修改草稿，不会启动流程。"
      : "系统已检查权限、已执行步骤和流程结构。确认后才会修改流程。"),
  );
  const list = node("div", "workflow-action-preview-list");
  [
    ["新增", preview.added_nodes || []],
    ["修改", preview.updated_nodes || []],
    ["删除", preview.removed_nodes || []],
  ].forEach(([label, items]) => items.forEach((item) => {
    list.append(node("span", "", `${label}：${item.title || item.key}`));
  }));
  copy.append(list);
  const controls = node("div", "workflow-action-preview-controls");
  const dismiss = node("button", "workflow-action-preview-dismiss", "暂不应用");
  dismiss.type = "button";
  dismiss.addEventListener("click", () => panel.remove());
  const confirm = node("button", "workflow-action-preview-confirm", "确认应用修改");
  confirm.type = "button";
  confirm.addEventListener("click", () => confirmGraphEditPreview(payload, panel, confirm));
  controls.append(dismiss, confirm);
  panel.append(copy, controls);
  container.append(panel);
  showToast("修改预览已生成，请核对后确认");
}

async function confirmGraphEditPreview(payload, panel, button) {
  button.disabled = true;
  panel.dataset.state = "working";
  button.textContent = "正在应用";
  try {
    const result = await request(
      `/console/api/v1/graph-edit-previews/${encodeURIComponent(payload.preview.id)}/confirm`,
      { method: "POST" },
    );
    panel.dataset.state = "done";
    button.textContent = result.already_applied ? "修改已经生效" : "修改已应用";
    showToast("流程图修改已应用");
    await loadInstances(result.instance?.id || null);
  } catch (error) {
    panel.dataset.state = "error";
    button.textContent = workflowActionError(error);
    button.disabled = false;
    showToast(error.message || "修改应用失败，请重试", "error");
  }
}

async function previewNodeRestart(nodeKey) {
  const instanceId = state.detail?.instance.id;
  showToast("正在生成节点打回影响预览");
  try {
    const payload = await request(
      `/console/api/v1/instances/${encodeURIComponent(instanceId)}/nodes/${encodeURIComponent(nodeKey)}/restart-preview`,
      { method: "POST" },
    );
    const container = el("graph-action-preview");
    container.replaceChildren();
    renderWorkflowActionPreview(container, payload);
    showToast("打回预览已生成，请核对影响节点");
  } catch (error) {
    showToast(error.message || "打回预览生成失败，请重试", "error");
    throw error;
  }
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
    title.append(node("strong", "", `第 ${item.attempt_no} 次执行`), statusBadge(item.status));
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
      pre.textContent = readableValue(item.result);
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
      item.attempt_no ? `第 ${item.attempt_no} 次执行` : null,
      RELATION[item.actor_relation] || item.actor_relation,
      item.source,
    ].filter(Boolean).join(" · ");
    body.append(title, node("p", "", description));
    row.append(marker, body);
    audit.append(row);
  });
}

function lockConsole() {
  clearTimeout(pollDraftRequest.timer);
  state.token = "";
  state.attention = [];
  state.detail = null;
  state.draftRequests = [];
  state.currentDraft = null;
  state.autoOpenDraftId = null;
  state.isAdmin = false;
  state.adminOverview = null;
  state.adminSessions = null;
  state.adminSessionPreview = null;
  state.view = "owner";
  state.ownerSection = "attention";
  state.returnSection = "attention";
  state.detailTab = "overview";
  setWorkflowFilter("open", false);
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

function showFeishuLogin(message = "", options = {}) {
  const reauthorize = options.reauthorize === true;
  clearTimeout(pollDraftRequest.timer);
  state.authMode = "feishu";
  state.token = "";
  state.isAdmin = false;
  state.adminOverview = null;
  state.adminSessions = null;
  state.adminSessionPreview = null;
  state.draftRequests = [];
  state.currentDraft = null;
  state.autoOpenDraftId = null;
  state.view = "owner";
  state.ownerSection = "attention";
  setWorkflowFilter("open", false);
  state.workflowQuery = "";
  sessionStorage.removeItem("larkflow.console.token");
  adminViewButton.hidden = true;
  unlockCopy.textContent = "使用当前飞书身份进入，只展示你有权查看的流程和待处理事项。";
  authNote.lastChild.textContent = "系统会校验你的身份和每次操作权限。";
  unlockForm.hidden = true;
  feishuLogin.hidden = false;
  feishuLogin.disabled = false;
  feishuLogin.textContent = reauthorize ? "重新授权飞书" : "使用飞书身份进入";
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

async function beginFeishuLogin() {
  feishuLogin.disabled = true;
  feishuLogin.textContent = "正在连接飞书";
  try {
    await fetch("/console/auth/logout", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (_error) {
    // OAuth recovery must remain available when session cleanup is unavailable.
  } finally {
    window.location.assign(state.loginUrl);
  }
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
  if (
    !payload.capabilities
    || typeof payload.capabilities.attachment_planning !== "boolean"
  ) {
    throw new Error("服务能力配置无效");
  }
  state.attachmentPlanningEnabled = payload.capabilities.attachment_planning;
  state.enterpriseKnowledgeSelectionEnabled = (
    payload.capabilities.enterprise_knowledge_selection === true
  );
  el("draft-attachment-input").hidden = !state.attachmentPlanningEnabled;
  el("draft-attachments").disabled = !state.attachmentPlanningEnabled;
  el("draft-knowledge-input").hidden = !state.enterpriseKnowledgeSelectionEnabled;
  if (state.enterpriseKnowledgeSelectionEnabled) await loadKnowledgeCatalog();
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
      showFeishuLogin(message, { reauthorize: true });
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

draftNav.addEventListener("click", () => {
  showOwnerSection("drafts");
  loadDraftCollaborators().catch(showWorkspaceError);
});
attentionNav.addEventListener("click", () => showOwnerSection("attention"));
workflowNav.addEventListener("click", () => {
  renderInstances();
  showOwnerSection("workflows");
});
el("detail-back").addEventListener("click", () => showOwnerSection(state.returnSection));
el("draft-form").addEventListener("submit", submitDraftRequest);
el("draft-generate").addEventListener("click", generateCurrentDraft);
el("draft-knowledge-save").addEventListener("click", saveCurrentKnowledgeSelection);
el("draft-attachments").addEventListener("change", (event) => {
  const files = Array.from(event.target.files || []);
  el("draft-attachment-selection").textContent = files.length
    ? `已选择 ${files.length} 个文件，共 ${readableBytes(files.reduce((sum, file) => sum + file.size, 0))}。提交后先上传，确认清单后才开始生成。`
    : "未选择附件。无附件时仍按原方式直接生成。";
});
document.querySelectorAll("[data-draft-starter]").forEach((button) => {
  button.addEventListener("click", () => {
    const starter = DRAFT_STARTERS[button.dataset.draftStarter];
    if (!starter) return;
    el("draft-brief").value = starter.brief;
    el("draft-context").value = starter.context;
    el("draft-brief-count").textContent = starter.brief.length;
    el("draft-context-count").textContent = starter.context.length;
    el("draft-advanced").open = true;
    document.querySelectorAll("[data-draft-starter]").forEach((candidate) => {
      candidate.dataset.active = String(candidate === button);
    });
    el("draft-form-error").textContent = "";
    el("draft-brief").focus();
    showToast("示例已填入，可以继续修改");
  });
});
el("draft-brief").addEventListener("input", (event) => {
  el("draft-brief-count").textContent = event.target.value.length;
  document.querySelectorAll("[data-draft-starter]").forEach((button) => {
    button.dataset.active = "false";
  });
});
el("draft-context").addEventListener("input", (event) => {
  el("draft-context-count").textContent = event.target.value.length;
});
el("draft-refresh").addEventListener("click", () => {
  refreshDraftRequests().catch(showWorkspaceError);
});
el("draft-open-instance").addEventListener("click", () => {
  openDraftInstance().catch(showWorkspaceError);
});
el("detail-tabs").querySelectorAll("button").forEach((button) => {
  button.addEventListener("click", () => setDetailTab(button.dataset.tab));
});
el("detail-advanced").addEventListener("toggle", (event) => {
  if (!event.currentTarget.open) return;
  if (state.detailTab === "overview") setDetailTab("execution");
  if (state.detailTab === "execution") requestAnimationFrame(layoutGraphCanvas);
});
el("workflow-query").addEventListener("input", (event) => {
  state.workflowQuery = event.target.value;
  renderInstances();
});
el("workflow-filters").querySelectorAll("button").forEach((button) => {
  button.addEventListener("click", () => {
    setWorkflowFilter(button.dataset.filter);
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
el("human-decision-feedback").addEventListener("input", (event) => {
  el("human-decision-feedback-count").textContent = event.target.value.length;
});
el("human-task-submit").addEventListener("click", submitHumanTaskFromPage);
el("human-task-transfer-open").addEventListener("click", openHumanTaskTransfer);
el("human-task-transfer-confirm").addEventListener("click", transferHumanTaskFromPage);
el("human-decision-accept").addEventListener("click", () => submitHumanDecisionFromPage("accept"));
el("human-decision-reject").addEventListener("click", () => submitHumanDecisionFromPage("reject"));
humanTaskDialog.addEventListener("close", () => {
  state.activeTask = null;
  resetHumanTaskControls();
});
el("graph-edit-form").addEventListener("submit", submitGraphEditForm);
el("graph-edit-close").addEventListener("click", () => graphEditDialog.close());
el("graph-edit-cancel").addEventListener("click", () => graphEditDialog.close());
el("graph-edit-remove").addEventListener("click", removeGraphNode);
graphEditDialog.addEventListener("close", () => {
  state.graphEditor = null;
  el("graph-edit-error").textContent = "";
});
el("graph-expand").addEventListener("click", (event) => {
  const grid = el("detail-grid");
  const expanded = grid.dataset.canvasExpanded !== "true";
  grid.dataset.canvasExpanded = String(expanded);
  event.currentTarget.setAttribute("aria-pressed", String(expanded));
  event.currentTarget.textContent = expanded ? "收起画板" : "展开画板";
  requestAnimationFrame(() => {
    if (window.LarkflowCanvas) window.LarkflowCanvas.fit(el("graph"));
  });
});
window.addEventListener("resize", () => {
  if (state.detail) layoutGraphCanvas();
});

bootstrap().catch((error) => {
  if (state.authMode === "feishu" && error.authentication === true) {
    showFeishuLogin(error.message, { reauthorize: true });
    return;
  }
  unlockCopy.textContent = "工作台暂时无法初始化。";
  unlockForm.hidden = true;
  feishuLogin.hidden = true;
  unlockError.textContent = error.message;
  app.hidden = true;
  unlock.hidden = false;
});
