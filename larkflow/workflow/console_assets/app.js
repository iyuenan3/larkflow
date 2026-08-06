"use strict";

const state = {
  token: sessionStorage.getItem("larkflow.console.token") || "",
  instances: [],
  detail: null,
  selectedNode: null,
};

const el = (id) => document.getElementById(id);
const unlock = el("unlock");
const app = el("app");
const unlockForm = el("unlock-form");
const tokenInput = el("access-token");
const unlockError = el("unlock-error");
const instanceList = el("instance-list");
const emptyState = el("empty-state");
const detailView = el("detail");

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
  "node.human_decision_accepted": "人工决定已接受",
  "node.human_decision_rejected": "人工决定已退回",
  "node.human_takeover_started": "人工接管已开始",
};

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

async function request(path) {
  setConnection("loading", "同步中");
  let response;
  try {
    response = await fetch(path, {
      headers: { Authorization: `Bearer ${state.token}` },
      cache: "no-store",
    });
  } catch (error) {
    setConnection("error", "连接失败");
    throw new Error("无法连接本机控制台服务");
  }
  const payload = await response.json();
  if (response.status === 401) {
    lockConsole();
    throw new Error("访问令牌无效或已经更新");
  }
  if (!response.ok) {
    setConnection("error", "读取失败");
    throw new Error(payload.error?.message || "读取失败");
  }
  setConnection("ready", "已连接");
  return payload;
}

async function loadInstances(selectId = null) {
  const payload = await request("/console/api/v1/instances?limit=30");
  state.instances = payload.instances;
  renderInstances();
  if (selectId) {
    await loadDetail(selectId);
  } else if (!state.detail && state.instances.length > 0) {
    const active = state.instances.find((item) => ["running", "paused", "failed"].includes(item.status));
    await loadDetail((active || state.instances[0]).id);
  }
}

async function loadDetail(instanceId) {
  const payload = await request(`/console/api/v1/instances/${encodeURIComponent(instanceId)}`);
  state.detail = payload;
  state.selectedNode = chooseNode(payload.nodes);
  renderInstances();
  renderDetail();
}

function chooseNode(nodes) {
  return nodes.find((item) => ["running", "waiting_human", "failed"].includes(item.status))?.key
    || nodes[nodes.length - 1]?.key
    || null;
}

function renderInstances() {
  instanceList.replaceChildren();
  el("instance-count").textContent = state.instances.length;
  if (state.instances.length === 0) {
    const empty = node("div", "list-empty");
    empty.append(node("strong", "", "还没有流程"), node("p", "", "通过飞书发起流程后会出现在这里。"));
    instanceList.append(empty);
    return;
  }
  state.instances.forEach((item) => {
    const button = node("button", "instance-item");
    button.type = "button";
    button.dataset.active = String(state.detail?.instance.id === item.id);
    button.addEventListener("click", () => loadDetail(item.id).catch(showWorkspaceError));

    const top = node("div", "instance-top");
    top.append(statusBadge(item.status), node("span", "instance-time", formatDate(item.created_at)));
    const goal = node("strong", "instance-goal", item.goal || "未命名流程");
    const footer = node("div", "instance-footer");
    footer.append(
      node("span", "mono", item.id),
      node("span", "instance-progress", `${item.completed_nodes}/${item.total_nodes}`),
    );
    button.append(top, goal, footer);
    instanceList.append(button);
  });
}

function renderDetail() {
  const payload = state.detail;
  if (!payload) return;
  emptyState.hidden = true;
  detailView.hidden = false;
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
  renderGraph(payload.nodes);
  renderAttempts(payload.nodes.find((item) => item.key === state.selectedNode));
  renderAudit(payload.audit);
}

function statusBadge(status, id = "") {
  const badge = node("span", `status-pill status-${status}`, statusLabel(status));
  if (id) badge.id = id;
  return badge;
}

function renderGraph(nodes) {
  const graph = el("graph");
  graph.replaceChildren();
  nodes.forEach((item, index) => {
    if (index > 0) {
      const connector = node("div", "graph-connector");
      connector.append(node("span", ""), node("i", ""));
      graph.append(connector);
    }
    const card = node("button", "graph-node");
    card.type = "button";
    card.dataset.status = item.status;
    card.dataset.selected = String(state.selectedNode === item.key);
    card.addEventListener("click", () => {
      state.selectedNode = item.key;
      renderGraph(nodes);
      renderAttempts(item);
    });

    const ordinal = node("span", "node-ordinal", String(index + 1).padStart(2, "0"));
    const content = node("div", "node-content");
    const top = node("div", "node-title-row");
    top.append(node("strong", "", item.title), statusBadge(item.status));
    const metadata = node("div", "node-metadata");
    metadata.append(
      node("span", `executor executor-${item.executor}`, EXECUTOR[item.executor] || item.executor),
      node("span", "", `Owner: ${RELATION[item.owner_relation] || item.owner_relation}`),
      node("span", "", `Attempt ${item.current_attempt_no}`),
    );
    content.append(top, node("span", "node-key mono", item.key), metadata);
    card.append(ordinal, content, node("span", "node-arrow", "›"));
    graph.append(card);
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
  state.detail = null;
  sessionStorage.removeItem("larkflow.console.token");
  app.hidden = true;
  unlock.hidden = false;
  tokenInput.value = "";
  tokenInput.focus();
}

function showWorkspaceError(error) {
  setConnection("error", error.message || "读取失败");
}

unlockForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  unlockError.textContent = "";
  state.token = tokenInput.value.trim();
  try {
    await loadInstances();
    sessionStorage.setItem("larkflow.console.token", state.token);
    unlock.hidden = true;
    app.hidden = false;
  } catch (error) {
    unlockError.textContent = error.message;
    unlock.hidden = false;
    app.hidden = true;
  }
});

el("refresh").addEventListener("click", () => {
  loadInstances(state.detail?.instance.id || null).catch(showWorkspaceError);
});
el("lock").addEventListener("click", lockConsole);

if (state.token) {
  unlock.hidden = true;
  app.hidden = false;
  loadInstances().catch((error) => {
    unlockError.textContent = error.message;
    lockConsole();
  });
} else {
  tokenInput.focus();
}
