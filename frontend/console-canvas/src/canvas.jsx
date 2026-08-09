import React, {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import ELK from "elkjs/lib/elk.bundled.js";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./canvas.css";

const elk = new ELK();
const roots = new WeakMap();
const controllers = new WeakMap();
const NODE_WIDTH = 292;
const NODE_HEIGHT = 156;
const FIT_OPTIONS = { padding: 0.2, minZoom: 0.38, maxZoom: 1.15, duration: 320 };
const LAYOUT_STORAGE_PREFIX = "larkflow.canvas.layout.v1";

const STATUS_LABELS = {
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
const EXECUTOR_LABELS = { human: "Human", agent: "Agent", tool: "Tool" };
const OWNER_LABELS = { you: "你", collaborator: "协作者", system: "系统" };
const STATUS_COLORS = {
  done: "#58d6b0",
  failed: "#fa7e88",
  running: "#5e8cff",
  ready: "#5e8cff",
  waiting_human: "#f1b45b",
  paused: "#b895ff",
  canceled: "#7e8795",
  discarded: "#7e8795",
  pending: "#8f9aab",
  draft: "#8f9aab",
};

function statusLabel(status) {
  return STATUS_LABELS[status] || status || "未知";
}

function fallbackLayout(items) {
  const byKey = new Map(items.map((item) => [item.key, item]));
  const depths = new Map();
  const visiting = new Set();
  const depth = (item) => {
    if (depths.has(item.key)) return depths.get(item.key);
    if (visiting.has(item.key)) return 0;
    visiting.add(item.key);
    const parents = (item.deps || []).map((key) => byKey.get(key)).filter(Boolean);
    const value = parents.length ? Math.max(...parents.map(depth)) + 1 : 0;
    visiting.delete(item.key);
    depths.set(item.key, value);
    return value;
  };
  const rows = new Map();
  return items.map((item) => {
    const column = depth(item);
    const row = rows.get(column) || 0;
    rows.set(column, row + 1);
    return {
      id: item.key,
      type: "workflowNode",
      position: { x: column * (NODE_WIDTH + 86), y: row * (NODE_HEIGHT + 42) },
      data: item,
    };
  });
}

async function layoutWorkflow(items) {
  const children = items.map((item) => ({
    id: item.key,
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
  }));
  const edges = items.flatMap((item) => (item.deps || []).map((source) => ({
    id: `${source}:${item.key}`,
    sources: [source],
    targets: [item.key],
  })));
  const result = await elk.layout({
    id: "workflow",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.spacing.nodeNode": "42",
      "elk.layered.spacing.nodeNodeBetweenLayers": "92",
      "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
    },
    children,
    edges,
  });
  const positions = new Map((result.children || []).map((item) => [item.id, item]));
  return items.map((item) => {
    const position = positions.get(item.key) || { x: 0, y: 0 };
    return {
      id: item.key,
      type: "workflowNode",
      position: { x: position.x || 0, y: position.y || 0 },
      data: item,
    };
  });
}

function layoutStorageKey(instanceId) {
  return `${LAYOUT_STORAGE_PREFIX}:${instanceId || "unknown"}`;
}

function readSavedPositions(instanceId) {
  if (!instanceId) return {};
  try {
    const stored = JSON.parse(localStorage.getItem(layoutStorageKey(instanceId)) || "{}");
    if (!stored || typeof stored !== "object" || Array.isArray(stored)) return {};
    return Object.fromEntries(Object.entries(stored).filter(([, position]) => (
      position
      && Number.isFinite(position.x)
      && Number.isFinite(position.y)
    )));
  } catch (_error) {
    return {};
  }
}

function savePositions(instanceId, nodes) {
  if (!instanceId) return false;
  const positions = Object.fromEntries(nodes.map((item) => [item.id, {
    x: Math.round(item.position.x * 10) / 10,
    y: Math.round(item.position.y * 10) / 10,
  }]));
  try {
    localStorage.setItem(layoutStorageKey(instanceId), JSON.stringify(positions));
    return true;
  } catch (_error) {
    return false;
  }
}

function clearSavedPositions(instanceId) {
  if (!instanceId) return;
  try {
    localStorage.removeItem(layoutStorageKey(instanceId));
  } catch (_error) {
    // Auto layout still works when browser storage is unavailable.
  }
}

const WorkflowNode = memo(function WorkflowNode({ data, selected }) {
  const dependencies = data.deps || [];
  return (
    <div
      className="lfc-node"
      data-status={data.status}
      data-selected={String(Boolean(selected))}
      aria-label={`${data.title}，${statusLabel(data.status)}，Attempt ${data.current_attempt_no}`}
    >
      <Handle className="lfc-handle" type="target" position={Position.Left} isConnectable={false} />
      <div className="lfc-node-heading">
        <span className="lfc-node-ordinal">{String(data.ordinal).padStart(2, "0")}</span>
        <span className="lfc-node-status" data-status={data.status}>
          <i aria-hidden="true" />
          {statusLabel(data.status)}
        </span>
      </div>
      <strong className="lfc-node-title">{data.title}</strong>
      <code className="lfc-node-key">{data.key}</code>
      <div className="lfc-node-meta">
        <span data-executor={data.executor}>{EXECUTOR_LABELS[data.executor] || data.executor}</span>
        <span>负责人：{OWNER_LABELS[data.owner_relation] || data.owner_relation}</span>
        <span>Attempt {data.current_attempt_no}</span>
      </div>
      <p className="lfc-node-deps">
        {dependencies.length ? `依赖 ${dependencies.length} 个上游节点` : "入口节点，无依赖"}
      </p>
      <Handle className="lfc-handle" type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
});

const nodeTypes = { workflowNode: WorkflowNode };

function useConsoleTheme() {
  const readTheme = () => document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const [theme, setTheme] = useState(readTheme);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(readTheme()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}

function CanvasBody({
  hostElement,
  items,
  selectedNode,
  instanceId,
  editable,
  restartable,
  onNodeSelect,
  onRequestAdd,
  onRequestEdit,
  onRequestRestart,
}) {
  const reactFlow = useReactFlow();
  const searchRef = useRef(null);
  const [query, setQuery] = useState("");
  const [layoutNodes, setLayoutNodes] = useState([]);
  const [layoutState, setLayoutState] = useState("loading");
  const [busyAction, setBusyAction] = useState(null);
  const theme = useConsoleTheme();
  const graphSignature = useMemo(
    () => JSON.stringify([instanceId, items.map((item) => [item.key, item.deps || []])]),
    [instanceId, items],
  );

  useEffect(() => {
    let canceled = false;
    setLayoutState("loading");
    layoutWorkflow(items)
      .then((nodes) => {
        if (canceled) return;
        const saved = readSavedPositions(instanceId);
        const restored = nodes.map((item) => saved[item.id]
          ? { ...item, position: saved[item.id] }
          : item);
        setLayoutNodes(restored);
        setLayoutState(Object.keys(saved).length ? "saved" : "ready");
        requestAnimationFrame(() => fitHost(hostElement));
      })
      .catch(() => {
        if (canceled) return;
        const saved = readSavedPositions(instanceId);
        setLayoutNodes(fallbackLayout(items).map((item) => saved[item.id]
          ? { ...item, position: saved[item.id] }
          : item));
        setLayoutState("fallback");
        requestAnimationFrame(() => fitHost(hostElement));
      });
    return () => { canceled = true; };
  }, [graphSignature, hostElement, instanceId, items]);

  const nodes = useMemo(() => layoutNodes.map((item) => {
    const current = items.find((candidate) => candidate.key === item.id) || item.data;
    return {
      ...item,
      selected: item.id === selectedNode,
      data: {
        ...current,
        ordinal: items.findIndex((candidate) => candidate.key === item.id) + 1,
      },
    };
  }), [items, layoutNodes, selectedNode]);

  const edges = useMemo(() => items.flatMap((item) => (item.deps || []).map((source) => {
    const related = selectedNode === source || selectedNode === item.key;
    return {
      id: `${source}:${item.key}`,
      source,
      target: item.key,
      type: "smoothstep",
      animated: related && ["running", "ready"].includes(item.status),
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      className: related ? "lfc-edge-related" : "lfc-edge",
      style: related
        ? { stroke: "var(--blue)", strokeWidth: 2.2 }
        : { stroke: "var(--lfc-edge)", strokeWidth: 1.35 },
    };
  })), [items, selectedNode]);

  const matches = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("zh-CN");
    if (!normalized) return [];
    return items.filter((item) => (
      item.key.toLocaleLowerCase("zh-CN").includes(normalized)
      || item.title.toLocaleLowerCase("zh-CN").includes(normalized)
    )).slice(0, 6);
  }, [items, query]);

  const focusNode = useCallback((key) => {
    onNodeSelect?.(key);
    const target = reactFlow.getNode(key);
    if (target) {
      reactFlow.setCenter(
        target.position.x + NODE_WIDTH / 2,
        target.position.y + NODE_HEIGHT / 2,
        { zoom: Math.max(reactFlow.getZoom(), 0.86), duration: 300 },
      );
    }
    setQuery("");
  }, [onNodeSelect, reactFlow]);

  const onNodesChange = useCallback((changes) => {
    setLayoutNodes((current) => applyNodeChanges(
      changes.filter((change) => change.type !== "remove"),
      current,
    ));
  }, []);

  const onNodeDragStop = useCallback(() => {
    const saved = savePositions(instanceId, reactFlow.getNodes());
    setLayoutState(saved ? "saved" : "unsaved");
  }, [instanceId, reactFlow]);

  const restoreAutoLayout = useCallback(async () => {
    setBusyAction("layout");
    clearSavedPositions(instanceId);
    try {
      setLayoutNodes(await layoutWorkflow(items));
      setLayoutState("ready");
      requestAnimationFrame(() => fitHost(hostElement, true));
    } catch (_error) {
      setLayoutNodes(fallbackLayout(items));
      setLayoutState("fallback");
      requestAnimationFrame(() => fitHost(hostElement, true));
    } finally {
      setBusyAction(null);
    }
  }, [hostElement, instanceId, items]);

  const invokeAction = useCallback(async (name, callback) => {
    if (!callback || busyAction) return;
    setBusyAction(name);
    try {
      await callback();
    } finally {
      setBusyAction(null);
    }
  }, [busyAction]);

  const onKeyDown = useCallback((event) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName);
    if (typing) {
      if (event.key === "Escape") {
        setQuery("");
        event.target.blur();
      }
      return;
    }
    if (event.key === "/") {
      event.preventDefault();
      searchRef.current?.focus();
    } else if (event.key.toLowerCase() === "f" || event.key === "0") {
      event.preventDefault();
      fitHost(hostElement, true);
    } else if (["+", "="].includes(event.key)) {
      event.preventDefault();
      reactFlow.zoomIn({ duration: 160 });
    } else if (event.key === "-") {
      event.preventDefault();
      reactFlow.zoomOut({ duration: 160 });
    }
  }, [hostElement, reactFlow]);

  const completed = items.filter((item) => item.status === "done").length;
  return (
    <div className="lfc-shell" data-theme={theme} tabIndex={0} onKeyDown={onKeyDown}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        colorMode={theme}
        minZoom={0.32}
        maxZoom={1.8}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
        edgesFocusable={false}
        deleteKeyCode={null}
        panOnDrag
        zoomOnScroll
        zoomOnPinch
        preventScrolling
        onlyRenderVisibleElements
        onNodesChange={onNodesChange}
        onNodeDragStop={onNodeDragStop}
        onInit={(instance) => registerController(hostElement, instance, graphSignature)}
        onNodeClick={(_event, item) => focusNode(item.id)}
        fitView
        fitViewOptions={FIT_OPTIONS}
        aria-label="受控流程运行画板"
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.25} color="var(--lfc-grid)" />
        <MiniMap
          className="lfc-minimap"
          pannable
          zoomable
          nodeColor={(item) => STATUS_COLORS[item.data?.status] || STATUS_COLORS.pending}
          nodeStrokeWidth={2}
          maskColor="var(--lfc-minimap-mask)"
          ariaLabel="流程缩略图"
        />
        <Controls
          className="lfc-controls"
          position="bottom-right"
          showInteractive={false}
          fitViewOptions={FIT_OPTIONS}
        />
        <Panel className="lfc-search-panel" position="top-left">
          <label className="lfc-search">
            <span aria-hidden="true">⌕</span>
            <input
              ref={searchRef}
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索节点名称或 ID"
              aria-label="搜索流程节点"
              autoComplete="off"
            />
            <kbd>/</kbd>
          </label>
          {query.trim() && (
            <div className="lfc-search-results" role="listbox" aria-label="节点搜索结果">
              {matches.length ? matches.map((item) => (
                <button key={item.key} type="button" role="option" onClick={() => focusNode(item.key)}>
                  <span>{item.title}</span>
                  <code>{item.key}</code>
                </button>
              )) : <p>没有匹配节点</p>}
            </div>
          )}
        </Panel>
        <Panel className="lfc-edit-panel" position="top-right">
          <button
            type="button"
            disabled={!editable || Boolean(busyAction)}
            onClick={() => invokeAction("add", onRequestAdd)}
          >
            <span aria-hidden="true">＋</span>增加节点
          </button>
          <button
            type="button"
            disabled={!editable || !selectedNode || Boolean(busyAction)}
            onClick={() => invokeAction("edit", () => onRequestEdit?.(selectedNode))}
          >
            编辑节点
          </button>
          <button
            type="button"
            disabled={!restartable || !selectedNode || Boolean(busyAction)}
            data-tone="danger"
            onClick={() => invokeAction("restart", () => onRequestRestart?.(selectedNode))}
          >
            {busyAction === "restart" ? "生成预览中" : "打回到此节点"}
          </button>
          <button
            type="button"
            disabled={Boolean(busyAction)}
            onClick={restoreAutoLayout}
          >
            {busyAction === "layout" ? "正在布局" : "恢复自动布局"}
          </button>
        </Panel>
        <Panel className="lfc-summary-panel" position="bottom-left">
          <span className="lfc-readonly"><i aria-hidden="true">⌁</i>受控运行画板</span>
          <span>{completed}/{items.length} 已完成</span>
          {layoutState === "loading" && <span>正在布局</span>}
          {layoutState === "saved" && <span>布局已保存</span>}
          {layoutState === "unsaved" && <span>布局仅本次有效</span>}
          {layoutState === "fallback" && <span>已使用备用布局</span>}
        </Panel>
      </ReactFlow>
    </div>
  );
}

function CanvasRoot(props) {
  return (
    <ReactFlowProvider>
      <CanvasBody {...props} />
    </ReactFlowProvider>
  );
}

function registerController(element, instance, signature) {
  const current = controllers.get(element) || {};
  controllers.set(element, {
    ...current,
    instance,
    signature,
  });
  requestAnimationFrame(() => fitHost(element));
}

function fitHost(element, force = false) {
  const controller = controllers.get(element);
  if (!controller?.instance || element.clientWidth < 40 || element.clientHeight < 40) return;
  if (!force && controller.fittedSignature === controller.signature) return;
  const selected = !force && controller.selectedNode
    ? controller.instance.getNode(controller.selectedNode)
    : null;
  if (selected) {
    controller.instance.setCenter(
      selected.position.x + NODE_WIDTH / 2,
      selected.position.y + NODE_HEIGHT / 2,
      { zoom: 0.82, duration: 320 },
    );
  } else {
    controller.instance.fitView(FIT_OPTIONS);
  }
  controller.fittedSignature = controller.signature;
  controllers.set(element, controller);
}

window.LarkflowCanvas = {
  render(element, props) {
    let root = roots.get(element);
    if (!root) {
      root = createRoot(element);
      roots.set(element, root);
    }
    const signature = JSON.stringify((props.items || []).map((item) => [item.key, item.deps || []]));
    const controller = controllers.get(element) || {};
    if (controller.signature !== signature) {
      controller.signature = signature;
      controller.fittedSignature = null;
    }
    controller.selectedNode = props.selectedNode || null;
    controllers.set(element, controller);
    root.render(<CanvasRoot hostElement={element} {...props} />);
  },
  refresh(element) {
    requestAnimationFrame(() => fitHost(element));
  },
  fit(element) {
    requestAnimationFrame(() => fitHost(element, true));
  },
};
