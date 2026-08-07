"use strict";

const state = {
  token: sessionStorage.getItem("larkflow.console.token") || "",
  authMode: "unknown",
  loginUrl: "/console/auth/login",
  instances: [],
  attention: [],
  detail: null,
  selectedNode: null,
  graphScale: 1,
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

async function request(path) {
  setConnection("loading", "同步中");
  let response;
  const headers = {};
  if (state.authMode === "static" && state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }
  try {
    response = await fetch(path, {
      headers,
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
    throw new Error(payload.error?.message || "读取失败");
  }
  setConnection("ready", "已连接");
  return payload;
}

async function loadInstances(selectId = null) {
  const payload = await request("/console/api/v1/instances?limit=30");
  state.instances = payload.instances;
  state.attention = payload.attention?.items || [];
  renderInstances();
  renderAttention(payload.attention || { items: [], counts: {}, instance_limit: 30 });
  if (selectId) {
    await loadDetail(selectId);
  } else if (!state.detail && state.instances.length > 0) {
    const active = state.instances.find((item) => ["running", "paused", "failed"].includes(item.status));
    await loadDetail((active || state.instances[0]).id);
  }
}

function renderAttention(attention) {
  const list = el("attention-list");
  const summary = el("attention-summary");
  list.replaceChildren();
  summary.replaceChildren();
  el("attention-count").textContent = attention.total ?? state.attention.length;
  el("attention-scope").textContent = `基于最近 ${attention.instance_limit || 30} 个本人流程，只提供安全的下一步提示`;

  Object.entries(ATTENTION).forEach(([kind, label]) => {
    const count = attention.counts?.[kind] || 0;
    if (count < 1) return;
    const chip = node("span", `attention-summary-chip attention-kind-${kind}`);
    chip.append(node("strong", "", count), document.createTextNode(label));
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

  state.attention.forEach((item) => list.append(attentionCard(item)));
}

function attentionCard(item) {
  const card = node("article", `attention-card attention-kind-${item.kind}`);
  const copy = node("div", "attention-copy");
  const meta = node("div", "attention-meta");
  meta.append(
    node("span", "attention-kind", ATTENTION[item.kind] || item.kind),
    node("span", "attention-time", formatDate(item.occurred_at)),
  );
  copy.append(
    meta,
    node("strong", "attention-title", item.title),
    node("span", "attention-goal", item.goal || "未命名流程"),
    node("p", "attention-detail", item.detail),
    node("p", "attention-hint", item.action_hint),
  );

  const actions = node("div", "attention-actions");
  if (item.command) {
    const command = node("div", "attention-command");
    command.append(node("code", "", item.command));
    const copyButton = node("button", "attention-copy-button", "复制命令");
    copyButton.type = "button";
    copyButton.addEventListener("click", () => copyCommand(item.command, copyButton));
    command.append(copyButton);
    actions.append(command);
  }
  const openButton = node("button", "attention-open-button", "查看流程");
  openButton.type = "button";
  openButton.addEventListener("click", async () => {
    openButton.disabled = true;
    openButton.dataset.state = "working";
    openButton.textContent = "正在打开";
    try {
      await loadDetail(item.instance_id);
      openButton.dataset.state = "done";
      openButton.textContent = "已打开";
      detailView.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      openButton.dataset.state = "error";
      openButton.textContent = "打开失败";
      showWorkspaceError(error);
    } finally {
      openButton.disabled = false;
      setTimeout(() => {
        openButton.dataset.state = "idle";
        openButton.textContent = "查看流程";
      }, 1600);
    }
  });
  actions.append(openButton);
  card.append(copy, actions);
  return card;
}

async function copyCommand(command, button) {
  button.disabled = true;
  button.dataset.state = "working";
  button.textContent = "复制中";
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(command);
    } else {
      copyCommandFallback(command);
    }
    button.dataset.state = "done";
    button.textContent = "已复制";
  } catch (error) {
    button.dataset.state = "error";
    button.textContent = "复制失败";
  } finally {
    button.disabled = false;
    setTimeout(() => {
      button.dataset.state = "idle";
      button.textContent = "复制命令";
    }, 1600);
  }
}

function copyCommandFallback(command) {
  const textarea = node("textarea", "clipboard-fallback");
  textarea.value = command;
  textarea.readOnly = true;
  document.body.append(textarea);
  let copied = false;
  try {
    textarea.select();
    copied = document.execCommand("copy");
  } finally {
    textarea.remove();
  }
  if (!copied) throw new Error("copy failed");
}

async function loadDetail(instanceId) {
  const payload = await request(`/console/api/v1/instances/${encodeURIComponent(instanceId)}`);
  const instanceChanged = state.detail?.instance.id !== payload.instance.id;
  state.detail = payload;
  state.selectedNode = chooseNode(payload.nodes);
  if (instanceChanged) state.graphScale = 1;
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
  renderInsights(payload);
  renderGraph(payload.nodes);
  renderAttempts(payload.nodes.find((item) => item.key === state.selectedNode));
  renderAudit(payload.audit);
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
  sessionStorage.removeItem("larkflow.console.token");
  app.hidden = true;
  unlock.hidden = false;
  tokenInput.value = "";
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
  sessionStorage.removeItem("larkflow.console.token");
  unlockCopy.textContent = "使用当前飞书身份进入，只展示你有权查看的流程和待处理事项。";
  authNote.lastChild.textContent = "身份和流程权限均由中央节点校验。";
  unlockForm.hidden = true;
  feishuLogin.hidden = false;
  feishuLogin.disabled = false;
  feishuLogin.textContent = "使用飞书身份进入";
  unlockError.textContent = message;
  app.hidden = true;
  unlock.hidden = false;
  feishuLogin.focus();
}

function showConsole() {
  unlock.hidden = true;
  app.hidden = false;
  requestAnimationFrame(layoutGraphCanvas);
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
  const response = await fetch("/console/api/v1/auth", {
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

el("refresh").addEventListener("click", () => {
  loadInstances(state.detail?.instance.id || null).catch(showWorkspaceError);
});
el("lock").addEventListener("click", () => {
  if (state.authMode === "feishu") {
    logoutConsole().catch(showWorkspaceError);
  } else {
    lockConsole();
  }
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
