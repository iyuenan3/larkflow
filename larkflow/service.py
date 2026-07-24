"""驱动层 LarkFlowService：把 LangGraph（权威）与飞书（投影）缝在一起。

单一事实源：checkpointer(thread_id) 是权威运行态；飞书任务/卡 = 投影；关联表 = 路由索引。
节点保持纯（human 只 interrupt）；provision / project / 事件路由都在这里，天然规避
「resume 重跑整个节点」的副作用重放。

时序（按对抗复核意见兜住 race）：
  · durability="sync" → interrupt 先落盘再发卡。
  · provision 在 invoke 返回后由驱动做，拿得到 interrupt.id 做关联；idem_key 含 interrupt.id
    → 同一中断重放去重、reopen（新中断 id）拿到全新任务/卡。
  · 事件 at-least-once → resume 前重读 state，只有该 interrupt.id 仍 pending 才 resume，陈旧 no-op。
  · 卡片 action_value 自描述 {thread_id, interrupt_id, node_id, verdict}；任务走关联表回映射。
"""
from __future__ import annotations

import threading

from langgraph.types import Command

from .io.correlations import Correlation, Correlations
from .io.events import CARD_ACTION, TASK_UPDATE
from .io.lark_io import Button, LarkIO

PASS_LABEL = "通过"
REOPEN_LABEL = "打回"
DONE_LABEL = "完成"


class LarkFlowService:
    def __init__(self, *, graph, io: LarkIO, correlations: Correlations, resolver, dag: list[dict]):
        self.graph = graph
        self.io = io
        self.corr = correlations
        self.resolver = resolver
        self.dag = dag
        # 每 DAG 层约 2 super-step；给回边重跑留余量，防默认 recursion_limit=25 中途崩（修 C 之belt）
        self._recursion_limit = 2 * len(dag) + 25
        # per-thread_id 串行化：EventPump 多线程（每 EventKey 一线程），防同实例并发 resume 丢更新（修 E）
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _thread_lock(self, instance_id: str) -> threading.Lock:
        with self._locks_guard:
            lk = self._locks.get(instance_id)
            if lk is None:
                lk = self._locks[instance_id] = threading.Lock()
            return lk

    # ---------- 对外 ----------
    def start(self, *, instance_id: str, reporter: str, bug: dict) -> str:
        with self._thread_lock(instance_id):
            state0 = {
                "dag": self.dag,
                "status": {},
                "outputs": {},
                "meta": {"instance_id": instance_id, "reporter": reporter, "bug": bug},
            }
            self.graph.invoke(state0, self._run_cfg(instance_id), durability="sync")
            self._handle(instance_id)
            return instance_id

    def resume_from_event(self, event: dict) -> dict:
        route = self._route(event)
        if not route:
            return {"skipped": "unrouted"}
        return self.resume(**route)

    def resume(self, *, instance_id: str, interrupt_id: str, value: dict, node_id: str | None = None) -> dict:
        with self._thread_lock(instance_id):  # 同 thread_id 串行（修 E）
            status = self._values(instance_id).get("status", {})
            pending = {i.id for i in self._pending_interrupts(instance_id)}
            # 修 F：并行下已 resume 的中断会滞留在 get_state().interrupts（直到同批兄弟也 resolve）。
            # 交叉核对节点自身状态：done 即已答复，配合 interrupt_id 仍 pending 才 resume，陈旧 no-op。
            resolved = node_id is not None and status.get(node_id) == "done"
            if resolved or interrupt_id not in pending:
                return {"skipped": "stale", "interrupt_id": interrupt_id}
            self.graph.invoke(Command(resume={interrupt_id: value}), self._run_cfg(instance_id), durability="sync")
            self._handle(instance_id)
            return {"resumed": interrupt_id}

    def status(self, instance_id: str) -> dict:
        return (self.graph.get_state(self._cfg(instance_id)).values or {}).get("status", {})

    # ---------- 内部 ----------
    def _cfg(self, instance_id: str) -> dict:
        return {"configurable": {"thread_id": instance_id}}

    def _run_cfg(self, instance_id: str) -> dict:
        # 运行（invoke）用；带 recursion_limit。读态（get_state）用 _cfg，不需要。
        return {"configurable": {"thread_id": instance_id}, "recursion_limit": self._recursion_limit}

    def _values(self, instance_id: str) -> dict:
        return self.graph.get_state(self._cfg(instance_id)).values or {}

    def _pending_interrupts(self, instance_id: str) -> list:
        snap = self.graph.get_state(self._cfg(instance_id))
        ints = getattr(snap, "interrupts", None)
        if ints:
            return list(ints)
        out = []
        for t in getattr(snap, "tasks", ()) or ():
            out.extend(getattr(t, "interrupts", ()) or ())
        return out

    def _handle(self, instance_id: str) -> None:
        for it in self._pending_interrupts(instance_id):
            self._provision(instance_id, it)
        self._project(instance_id)

    def _provision(self, instance_id: str, it) -> None:
        v = it.value or {}
        nid, signal = v["node_id"], v.get("signal")
        iid = it.id
        idem = f"{instance_id}:{nid}:{iid}"  # 含中断 id：重放去重、reopen 出新单
        assignee = self._assignee(instance_id, nid, v.get("assignee_role"))
        if signal == "task_complete":
            guid = self.io.create_task(
                assignee=assignee,
                summary=v.get("label", nid),
                description=self._criteria(v),
                idem_key=idem,
            )
            self.corr.put(Correlation(guid, instance_id, iid, nid, "task"))
        elif signal == "card_action":
            msg = self.io.send_card(
                target=assignee,
                summary=v.get("label", nid),
                buttons=self._buttons(instance_id, iid, nid, v),
                idem_key=idem,
            )
            self.corr.put(Correlation(msg, instance_id, iid, nid, "card"))

    def _assignee(self, instance_id: str, nid: str, assignee_role: str) -> str:
        # fix 的执行人优先用 assign 节点定的 owner（seg-1 遗留硬编码，step 8 泛化）
        if nid == "fix":
            owner = (self._values(instance_id).get("outputs", {}).get("assign") or {}).get("owner")
            if owner:
                return owner
        return self.resolver.resolve(assignee_role)

    def _criteria(self, v: dict) -> str:
        label = v.get("label", "")
        if v.get("role") == "gate":
            return f"审核「{label}」：通过或打回上游重做。"
        return f"{label}：完成后在飞书任务上点「完成」。"

    def _buttons(self, instance_id: str, iid: str, nid: str, v: dict) -> list[Button]:
        base = {"thread_id": instance_id, "interrupt_id": iid, "node_id": nid}
        if v.get("role") != "gate":  # human-produce（定稿确认）：单按钮
            return [Button(DONE_LABEL, {**base, "verdict": "pass"}, "primary_filled")]
        return [
            Button(PASS_LABEL, {**base, "verdict": "pass"}, "primary_filled"),
            Button(REOPEN_LABEL, {**base, "verdict": "fail"}, "danger_filled"),
        ]

    def _route(self, event: dict) -> dict | None:
        key = event.get("key")
        if key == CARD_ACTION:
            av = event.get("action_value") or {}
            if "thread_id" not in av or "interrupt_id" not in av:
                return None
            return {
                "instance_id": av["thread_id"],
                "interrupt_id": av["interrupt_id"],
                "node_id": av.get("node_id"),
                "value": {"passed": av.get("verdict") == "pass", "verdict": av.get("verdict"),
                          "by": event.get("operator_id")},
            }
        if key == TASK_UPDATE:
            ev = event.get("event", {})
            if "task_completed_update" not in (ev.get("event_types") or []):
                return None
            corr = self.corr.get(ev.get("task_guid", ""))
            if not corr:
                return None
            return {
                "instance_id": corr.thread_id,
                "interrupt_id": corr.interrupt_id,
                "node_id": corr.node_id,
                "value": {"passed": True, "completed": True},
            }
        return None

    def _project(self, instance_id: str) -> None:
        """投影钩子（seg-1 留空；进度卡 / 多维表格看板见 ROADMAP 第二段）。"""
        return
