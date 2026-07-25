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

from .engine.gates import illegal_reopen
from .engine.livegraph import GraphEditError, apply_ops
from .engine.support import assert_v1_supported
from .io.correlations import Correlation, Correlations
from .io.events import CARD_ACTION, TASK_UPDATE
from .io.lark_io import Button, LarkIO
from .model.template import load_template, validate_template

PASS_LABEL = "通过"
REOPEN_LABEL = "打回"
DONE_LABEL = "完成"


class LarkFlowService:
    def __init__(self, *, graph, io: LarkIO, correlations: Correlations, resolver,
                 dag: list[dict], executors=None):
        self.graph = graph
        self.io = io
        self.corr = correlations
        self.resolver = resolver
        self.dag = dag
        self.executors = executors   # 改图后复查 tool handler 覆盖（可选）
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
    def start(self, *, instance_id: str, inputs: dict | None = None, reporter: str | None = None,
              template: str | list[dict] | None = None) -> str:
        """起一个实例（ADR-021：两个入口都收敛到 start(template, inputs)）。

        template 省略则用装配时的默认模板；传名字则按名加载，传 dag 则直接用（都过校验）。
        inputs = 项目要素（llm 节点 prompt 里可见），随 meta 持久在 checkpointer。
        """
        dag = self._resolve_template(template)
        with self._thread_lock(instance_id):
            state0 = {
                "dag": dag,
                "status": {},
                "outputs": {},
                "meta": {"instance_id": instance_id, "reporter": reporter,
                         "inputs": inputs or {}},
            }
            self.graph.invoke(state0, self._run_cfg(instance_id), durability="sync")
            self._handle(instance_id)
            return instance_id

    def _resolve_template(self, template) -> list[dict]:
        if template is None:
            return self.dag
        dag = load_template(template) if isinstance(template, str) else template
        validate_template(dag)
        assert_v1_supported(dag)
        if self.executors is not None:
            self.executors.validate_coverage(dag)
        return dag

    def resume_from_event(self, event: dict) -> dict:
        route = self._route(event)
        if not route:
            return {"skipped": "unrouted"}
        return self.resume(**route)

    def resume(self, *, instance_id: str, interrupt_id: str, value: dict, node_id: str | None = None) -> dict:
        with self._thread_lock(instance_id):  # 同 thread_id 串行（修 E）
            values = self._values(instance_id)
            # 打回合法域在**引擎权威侧**算，不信前端 / 卡片回传（ADR-014 / ADR-023）。
            # 用运行时 dag（活图会改它），不用装配期模板。
            reopen = (value or {}).get("reopen")
            if reopen and node_id:
                bad = illegal_reopen(values.get("dag") or self.dag, node_id, reopen)
                if bad:
                    return {"rejected": "illegal_reopen", "illegal": bad, "node_id": node_id}
            status = values.get("status", {})
            pending = {i.id for i in self._pending_interrupts(instance_id)}
            # 改图会让挂起中断换 id；顺迁移链把旧卡 / 旧任务上的 id 重绑到当前中断
            interrupt_id = self.corr.resolve_interrupt(
                instance_id, interrupt_id, is_live=lambda i: i in pending)
            # 修 F：并行下已 resume 的中断会滞留在 get_state().interrupts（直到同批兄弟也 resolve）。
            # 交叉核对节点自身状态：done 即已答复，配合 interrupt_id 仍 pending 才 resume，陈旧 no-op。
            resolved = node_id is not None and status.get(node_id) == "done"
            if resolved or interrupt_id not in pending:
                return {"skipped": "stale", "interrupt_id": interrupt_id}
            self.graph.invoke(Command(resume={interrupt_id: value}), self._run_cfg(instance_id), durability="sync")
            self._handle(instance_id)
            return {"resumed": interrupt_id}

    def edit_graph(self, instance_id: str, ops: list[dict]) -> dict:
        """受控活图：运行中改未来（ADR-013）。校验 → 写回 dag channel → 触发下一次 dispatch。

        校验一律在**引擎权威侧**做，不信前端（ADR-019 / ADR-023）：
          ① ops 只触 pending 子图（apply_ops 的冻结线）
          ② 新图仍过 validate_template（仍是 DAG / 不悬挂 / 全部护栏）
          ③ 新图不用引擎 v1 未实现的语义；新增 tool 节点得有 handler
        """
        with self._thread_lock(instance_id):
            values = self._values(instance_id)
            if not values:
                raise GraphEditError(f"实例不存在: {instance_id}")
            # 「在跑」= 已派出去还没回来的节点。tool/llm 在一个 super-step 内跑完（本方法
            # 又与 invoke 同锁），故唯一会长时间在飞的是挂起的 human 节点：把它们当 running
            # 并入冻结线，落地 ADR-013「不删在跑节点」。
            in_flight = {(i.value or {}).get("node_id"): "running"
                         for i in self._pending_interrupts(instance_id)}
            frontier = {**values.get("status", {}), **{k: v for k, v in in_flight.items() if k}}
            new_dag = apply_ops(values["dag"], frontier, ops)
            validate_template(new_dag)
            assert_v1_supported(new_dag)
            if self.executors is not None:
                self.executors.validate_coverage(new_dag)

            before = {i.id: (i.value or {}).get("node_id")
                      for i in self._pending_interrupts(instance_id)}
            self.graph.update_state(self._cfg(instance_id), {"dag": new_dag})
            # 改完立刻推一步：新就绪的未来节点当场跑起来（唯一真环边照旧）
            self.graph.invoke(None, self._run_cfg(instance_id), durability="sync")

            remapped = self._remap_interrupts(instance_id, before)
            self._handle(instance_id, skip=remapped)
            return {"edited": len(ops), "nodes": [n["id"] for n in new_dag],
                    "remapped": len(remapped)}

    def status(self, instance_id: str) -> dict:
        return self._values(instance_id).get("status", {})

    def outputs(self, instance_id: str) -> dict:
        """节点产出 + 交付物 handle 权威登记表（ADR-020）。"""
        return self._values(instance_id).get("outputs", {})

    def pending(self, instance_id: str) -> list[dict]:
        """当前卡在谁手上：每个挂起的人工节点一条（含交付物链接、打回候选）。

        供驱动 / 前端读（前端读接口形态待定，见 SPEC 待填）；不含真相源以外的东西。
        """
        return [{"interrupt_id": it.id, **(it.value or {})}
                for it in self._pending_interrupts(instance_id)]

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

    def _handle(self, instance_id: str, skip: set[str] | None = None) -> None:
        for it in self._pending_interrupts(instance_id):
            if skip and it.id in skip:
                continue   # 只是改图导致的换 id：卡 / 任务还在人手里，别重复派
            self._provision(instance_id, it)
        self._project(instance_id)

    def _remap_interrupts(self, instance_id: str, before: dict[str, str]) -> set[str]:
        """改图后中断换了 id：按 node_id 对上号，记下迁移链，让旧卡 / 旧任务继续有效。

        只记「同一节点、未重跑」的迁移；打回产生的新中断不在此列（那本就该出新单）。
        """
        after = {(i.value or {}).get("node_id"): i.id
                 for i in self._pending_interrupts(instance_id)}
        remapped: set[str] = set()
        for old_id, node_id in before.items():
            new_id = after.get(node_id)
            if new_id and new_id != old_id:
                self.corr.put_remap(instance_id, old_id, new_id)
                remapped.add(new_id)
        return remapped

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
                # 卡片正文也要带交付物链接：门禁走卡不走任务，只给个标题等于让人空手审
                summary=self._criteria(v),
                buttons=self._buttons(instance_id, iid, nid, v),
                idem_key=idem,
            )
            self.corr.put(Correlation(msg, instance_id, iid, nid, "card"))

    def _assignee(self, instance_id: str, nid: str, assignee_role: str) -> str:
        """派单对象只认模板里的 assignee_role（驱动层不认识任何具体模板）。

        「按上游产出动态定人」（seg-1 的 fix 曾特判 assign 节点算出的 owner）留到 v1.1
        与意图路由 / 生成一起做，届时是节点字段、不是驱动层硬编码。
        """
        return self.resolver.resolve(assignee_role)

    def _criteria(self, v: dict) -> str:
        label, url = v.get("label", ""), v.get("deliverable_url")
        if v.get("role") == "gate":
            lines = [f"审核「{label}」：通过或打回上游重做。"]
        else:
            lines = [f"{label}：完成后在飞书任务上点「完成」。"]
        if url:
            lines.append(f"你的交付物：{url}")
        # 审核人得先能打开要审的那份东西（gate 自己不产出交付物）
        lines += [f"待审：{u['label']} {u['url']}" for u in v.get("upstream") or []]
        return "\n".join(lines)

    def _buttons(self, instance_id: str, iid: str, nid: str, v: dict) -> list[Button]:
        base = {"thread_id": instance_id, "interrupt_id": iid, "node_id": nid}
        if v.get("role") != "gate":  # human-produce（定稿确认）：单按钮
            return [Button(DONE_LABEL, {**base, "verdict": "pass"}, "primary_filled")]
        # 打回按钮带默认目标组；多选 reopen 的卡片视觉 schema 待 dev app（见 SPEC 待填），
        # 前端 / app 可用同一自描述封套回传任意合法目标组，引擎侧再校验一次。
        reopen = {"reopen": v.get("reopen_default")} if v.get("reopen_default") else {}
        return [
            Button(PASS_LABEL, {**base, "verdict": "pass"}, "primary_filled"),
            Button(REOPEN_LABEL, {**base, "verdict": "fail", **reopen}, "danger_filled"),
        ]

    def _route(self, event: dict) -> dict | None:
        key = event.get("key")
        if key == CARD_ACTION:
            av = event.get("action_value") or {}
            if "thread_id" not in av or "interrupt_id" not in av:
                return None
            passed = av.get("verdict") == "pass"
            value = {"passed": passed, "verdict": av.get("verdict"),
                     "by": event.get("operator_id")}
            if not passed:   # 打回才带目标组 + 意见（放行时的 reopen 是噪音，丢弃）
                if av.get("reopen"):
                    value["reopen"] = list(av["reopen"])
                if av.get("comment"):
                    value["comment"] = av["comment"]
            return {
                "instance_id": av["thread_id"],
                "interrupt_id": av["interrupt_id"],
                "node_id": av.get("node_id"),
                "value": value,
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
