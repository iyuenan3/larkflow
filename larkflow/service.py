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

import copy
import threading
from datetime import datetime, timedelta, timezone

from langgraph.types import Command

from .engine.gates import (
    BLOCKED,
    MAX_UNBLOCK_GRANTS,
    all_done,
    blocked_nodes,
    clamp_grant,
    grants_used,
    illegal_reopen,
    ready_nodes,
    reopen_resets,
    total_reopen_budget,
    unblock_resets,
)
from .engine.livegraph import GraphEditError, apply_ops
from .engine.permissions import MAX_PENDING_ESCALATIONS, can_answer, reopen_verdict
from .engine.support import assert_v1_supported
from .io.correlations import Correlation, Correlations
from .io.events import CARD_ACTION, TASK_UPDATE
from .io.lark_io import Button, LarkIO, settled_card
from .model.node import is_gate, node_by_id
from .model.template import load_template, validate_template

def _target_key(targets) -> tuple:
    """一组打回目标的**语义**标识：去重 + 排序。

    前端给的原始列表不能当键（红线⑤ 的精神：不信回传的形状）。`["a","a"]` /
    `["b","a"]` / `["a","b"]` 是同一笔申请，引擎裁定时本来就这么算（见
    `permissions.reopen_verdict` 的 `seen` 去重），去重键必须与它同一口径。
    """
    return tuple(sorted(set(targets or ())))


def _requests(log) -> list[dict]:
    """log 里的**申请**（对照 `kind == "verdict"` 的裁决）。

    早于一键同意通道的记录没有 `kind` 字段，缺省即申请：追加型 channel 里躺着的历史
    不能改（红线：只改未来不改历史），所以新字段只能靠缺省值向后兼容。
    """
    return [r for r in log or () if r.get("kind", "request") == "request"]


def _verdicts_by_ref(log) -> dict:
    """已被拍板的申请：`seq` → 那条裁决记录。"""
    return {r["ref"]: r for r in log or ()
            if r.get("kind") == "verdict" and r.get("ref") is not None}


def _effective_status(record: dict, verdicts: dict, attempt, answered: bool) -> str:
    """一笔申请**当下**的真实状态（派生，不是存出来的）。

    `escalations` 是追加型 channel，物理上没有 UPDATE，所以记录里那个 `status` 字面量
    冻的是「落库那一刻」，永远是 pending。把当下状态算出来，顺带修掉「旧记录 status
    恒为 pending」那条挂了很久的 finding：它不是漏写，是存储模型决定的，只能改读法。
    """
    v = verdicts.get(record.get("seq"))
    if v is not None:
        return v.get("verdict") or "settled"
    return "pending" if (record.get("attempt") == attempt and not answered) else "expired"


def _live_escalations(log, attempt, *, answered: bool = False) -> list[dict]:
    """这道门**现在**还等着人拍板的跨界打回申请。

    三条出局判据：
      · **已被拍板**（log 里有一条 `ref` 指向它的裁决记录）。
      · **轮次已过**：申请是对「这道门这一轮的那一版」提的，门一进新一轮（被打回 /
        被解除重置），它要打回的那一版已经被重做过了，旧申请随之作废。
      · **门已答复**：申请不是裁决，提了申请的人手里那张卡**仍然有效**（`_ack_escalation`
        就是这么告诉他的），所以他完全可能没等批下来就自己点了通过。轮次那把尺在这里不
        管用（点通过不会让 `attempts` 变），必须另看门的状态：否则驾驶舱一直显示「等人
        拍板」而门早就过去了，真有人去点同意还会试着掀开一道已经放行的门。

    配额与对外读接口必须用这同一把尺：两套口径正是「审批通道被永久锁死」那条缺陷的
    根因（当时没有 approve / reject 通道，`status` 永远停在 pending，按整条历史算就
    再也降不下来）。
    """
    if answered:
        return []
    settled = _verdicts_by_ref(log)
    return [r for r in _requests(log)
            if r.get("status") == "pending" and r.get("attempt") == attempt
            and r.get("seq") not in settled]


def _unanswered(status: dict, node_id) -> bool:
    """这个人还在等吗。done / failed 都表示他本轮已经答过了（failed = 他点了打回）。"""
    return (status or {}).get(node_id) not in ("done", "failed")


PASS_LABEL = "通过"
REOPEN_LABEL = "打回"
DONE_LABEL = "完成"
# 审批卡（ADR-023 ③ 的「一键同意」）。刻意与门禁卡的「通过 / 打回」用不同的字：
# 审批人批的是**一笔申请**，不是那道门本身，两者在同一个 node_id 上并存。
ESC_APPROVE_LABEL = "同意"
ESC_REJECT_LABEL = "驳回"
ESC_KIND = "escalation"          # 封套的自描述标记，`_route` 据它分流

# 借任一 worker 的位置写回 state：它的唯一出边就是 dispatch，于是 dispatch 会**真的执行**
# （打回重置逻辑仍留在引擎里，驱动层不重算）。我们并不执行这个 worker，只是占它的位。
_PUMP_AS_NODE = "tool_worker"

# 审计时间戳一律 UTC+8（带偏移量，读的人不用猜时区）
_CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


class InstanceExists(ValueError):
    """`start` 打到一个已经存在的实例上。

    不是「重复起实例」这种手滑级问题，是 ADR-042 那条鉴权的**旁路**：`meta` / `dag` 是
    无 reducer 的通道，`invoke(state0)` 对它们是整个替换，于是一条 `start --id <既有>`
    就能换掉 owner、换掉项目要素、把整张图（含一道还在等的门）替成自己给的那张，而且
    `edits` 审计里一个字都不留。`status` 走 merge reducer，`{}` 合并等于不动，所以实例
    看起来**没被重置**，它带着原来的进度按新图跑到底，比整个清空更难被发现。

    照 store.py 顶部那句口径：宁可失败得响，不要静默覆盖。
    """


class LarkFlowService:
    def __init__(self, *, graph, io: LarkIO, correlations: Correlations, resolver,
                 dag: list[dict], executors=None, lock_factory=None):
        self.graph = graph
        self.io = io
        self.corr = correlations
        # 外部写动作的**本地**幂等表（与 checkpointer 同一个 SQLite）。绝不把幂等性外包给
        # 飞书的 --idempotency-key：那个窗口只有 1 小时（见 io/lark_io.py 的命令注释），
        # 而人工节点等的是人，超过 1 小时是常态。押在它上面的话，每次 serve 重启 / 每次
        # `larkflow reconcile` 都会给所有还在等的人再发一遍卡、再建一条待办（实测）。
        self._idem = correlations.idem_store()
        self.resolver = resolver
        self.dag = dag
        self.executors = executors   # 改图后复查 tool handler 覆盖（可选）
        # per-thread_id 串行化：EventPump 多线程（每 EventKey 一线程），防同实例并发 resume 丢更新（修 E）
        self._locks: dict = {}
        self._locks_guard = threading.Lock()
        # `lock_factory(instance_id) -> 上下文管理器`。默认只是进程内锁；真栈注
        # `store.InstanceLocks`，把这条串行不变量扩展到**跨进程**（serve 常驻 + 一次性
        # CLI 命令写的是同一个 SQLite 文件，而下面每一处状态变更都是读改写）。
        self._lock_factory = lock_factory or (lambda instance_id: threading.Lock())
        # 派单失败不再吞掉：每实例记最近的失败，供 reconcile / 运维查
        self.provision_errors: dict[str, list[dict]] = {}

    def _validate_roles(self, dag: list[dict]) -> None:
        """派单对象必须解析得出（真栈 strict 下才生效）。别等人工节点挂起那一刻才炸。"""
        check = getattr(self.resolver, "validate_coverage", None)
        if check is not None:
            check(dag)

    @staticmethod
    def _dispatch_key(instance_id: str, node_id: str, attempt: int) -> str:
        """派单幂等键 = 「这个实例的这个节点在这一轮」。

        **只此一处拼**：派单用它记「派过了」，对账轮询用它反查「本轮那条待办是哪条」。
        两处各拼一次的话，改一个忘一个，后果是轮询永远查不到 → 丢事件永远捞不回来，
        而且没有任何症状（我第一版就漏了 kind 段）。
        """
        return f"{instance_id}:{node_id}:{attempt}"

    def _once(self, key: str, make):
        """外部写动作的本地幂等闸：同一个 idem_key **一辈子**只真的做一次。

        返回外部对象 id（没有就是 ""）。约定与全仓一致：idem_key 标识「一件事」，要让同类
        事情再发一次，就把区分它的东西放进 key（`:{attempt}` / `:{seq}` / `:{used}` 全是
        这么来的）。

        先调外部、成功了才记键：失败就当没做过，下次对账自然重试（这正是「派单失败的人
        被补上」那条测试要的）。残留窗口只剩一个：调用成功但进程死在记键之前：那一段
        仍然只能靠飞书的 1 小时窗口兜（超时会留一条孤儿待办，见 known gaps）。写前置
        「意图」并不能消掉它：拿不回外部 id，重启后照样得再调一次。
        """
        cached = self._idem.get(key)
        if cached is not None:
            return cached
        value = make() or ""
        self._idem.put(key, value)
        return value

    def _tell(self, instance_id: str, target: str | None, text: str, idem_key: str,
              *, node_id: str | None = None, what: str = "notify") -> bool:
        """给某个人发一条通知（投影侧动作，失败只记不抛）。**返回是否真送到了。**

        走 `_once`：通知的 idem_key 一样是「一件事」的标识（`:{seq}` / `:{used}` /
        `:{attempt}` 已经把该区分的都区分了），只押飞书那 1 小时窗口的话，每次重启都会
        把「卡死了」「有人申请打回」原样再播一遍。

        返回值是给**要把「通知过谁」写进权威 state** 的调用方用的（见 `_escalate`）：
        把没送到的人记成「已通知」，等于在审计里造一条假事实。
        """
        if not target:
            return False
        try:
            self._once(f"notify:{idem_key}:{target}",
                       lambda: self.io.notify(target=target, text=text, idem_key=idem_key))
            return True
        except Exception as exc:
            self.provision_errors.setdefault(instance_id, []).append(
                {"node_id": node_id, "error": f"{what} {type(exc).__name__}: {exc}"})
            return False

    def _notify_owner(self, instance_id: str, text: str, idem_key: str) -> None:
        self._tell(instance_id, (self._values(instance_id).get("meta") or {}).get("reporter"),
                   text, idem_key)

    def _thread_lock(self, instance_id: str):
        with self._locks_guard:
            lk = self._locks.get(instance_id)
            if lk is None:
                lk = self._locks[instance_id] = self._lock_factory(instance_id)
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
            # 判在锁**内**：`larkflow start` 是一次性命令，两个人同时敲同一个 --id 时
            # 锁外判会让两边都看到「不存在」。判据借 `dag_of` 那把尺，不另写一个：CLI 的
            # status / pending / edit 判 no_such_instance 用的就是它，两把尺迟早分叉成
            # 「查无此实例」与「已存在，不许起」并存（v0.7.0 活性判据那条教训）。
            if self.dag_of(instance_id):
                raise InstanceExists(
                    f"实例已存在：{instance_id}。起新实例请换一个 id；要改这个实例的图走 "
                    f"`larkflow edit`（它有 owner-only 鉴权与审计，ADR-042）。")
            state0 = {
                "dag": dag,
                "status": {},
                "outputs": {},
                "reopen_counts": {},
                "attempts": {},
                "unblocks": {},
                "escalations": {},
                "edits": {},
                "meta": {"instance_id": instance_id, "reporter": reporter,
                         "inputs": inputs or {}},
            }
            self.graph.invoke(state0, self._run_cfg(instance_id, dag), durability="sync")
            self._advance(instance_id)
            return instance_id

    def _resolve_template(self, template) -> list[dict]:
        if template is None:
            return self.dag
        dag = load_template(template) if isinstance(template, str) else template
        validate_template(dag)
        assert_v1_supported(dag)
        if self.executors is not None:
            self.executors.validate_coverage(dag)
        self._validate_roles(dag)
        return dag

    def resume_from_event(self, event: dict) -> dict:
        route = self._route(event)
        if not route:
            return {"skipped": "unrouted"}
        if "escalation" in route:
            esc = route["escalation"]
            if not esc.get("actor"):
                return {"skipped": "unidentified_actor"}   # 同下：fail closed
            return self._settle_from_card(**esc)
        if event.get("key") == CARD_ACTION and not route.get("actor"):
            # 身份缺失 = fail closed。飞书的卡片回调一定带 operator_id；没有它的封套是畸形
            # 输入，绝不能当「匿名点击」放进来：那会让 `resume` 的 actor 变 None，而 None
            # 是留给**引擎自己路由**的那条通道的（见 `resume` 的说明）。
            # 单独一个 skip 值：daemon 日志得分得清「这条事件与我无关」和「认不出人」，
            # 后者若成规模就是入站通道认错了字段名，那是要人管的故障。
            return {"skipped": "unidentified_actor"}
        return self.resume(**route)

    def resume(self, *, instance_id: str, interrupt_id: str, value: dict,
               node_id: str | None = None, actor: str | None = None,
               token: str | None = None) -> dict:
        """把一个人的答复喂回引擎。

        `actor` = **事件里的** `operator_id`（谁真的点了这一下）。绝不从 `value` /
        `action_value` 里取身份：卡片封套是前端可自由构造的攻击面（红线⑤）。省略 actor 的
        直调路径（运维脚本）拿不到任何打回权，要打回就得显式传身份，传发起人即拿 owner 全域权。

        身份判定对**两个按钮都做**：放行 / 定稿走 `can_answer`（谁的活谁签），打回另走更严
        的 ADR-023 三条规则。只判打回那半边的话，任何拿得到 interrupt_id 的人（卡被转发、
        assignee 解析成群）都能替把关人放行，让交付物直接生效。

        身份判定按「这一下是打回还是应答」分支（`_is_reopen`），**绝不按 `passed` 分支**：
        非 gate 节点的 fail 不是打回，可 `gates.finish` 对非 gate 根本不看 passed、照样标
        done：按 passed 分的话那条路一道校验都不过（实测：改一个字段就替别人签了定稿）。

        `actor is None` 只可能来自两条**引擎自己路由**的通道：飞书任务完成事件（事件里不带
        operator，身份由「这条 task_guid 是引擎发给谁的、只有他点得动完成」+ 关联表保证，
        且 `_route` 已禁止 gate 走这条路、并核对关联行 kind 必须是 task），以及进程内直调。
        卡片通道缺 operator 一律在 `resume_from_event` 拒掉，不会落到这里。
        """
        with self._thread_lock(instance_id):  # 同 thread_id 串行（修 E）
            values = self._values(instance_id)
            status = values.get("status", {})
            live = {i.id: (i.value or {}).get("node_id")
                    for i in self._pending_interrupts(instance_id)}
            # 改图 / 推进会让挂起中断换 id；顺迁移链把旧卡 / 旧任务上的 id 重绑到当前中断
            interrupt_id = self.corr.resolve_interrupt(
                instance_id, interrupt_id, is_live=lambda i: i in live)
            pending = set(live)

            # 打回合法域在**引擎权威侧**算，不信前端 / 卡片回传（ADR-014 / ADR-023）。
            # 关键：gate 身份取自**中断本身**，绝不用回传的 node_id：卡片 action_value 是前端
            # 可自由构造的封套，少一个 node_id 就能绕开这道校验，而非法值一旦落进权威 state，
            # 此后每一次推进都在同一处炸，实例永久砖化（实测）。
            reopen = (value or {}).get("reopen")
            gate_id = live.get(interrupt_id) or node_id
            if value is not None and not value.get("passed", False):
                if not gate_id:
                    return {"rejected": "unidentified_gate", "interrupt_id": interrupt_id}
                if reopen:
                    bad = illegal_reopen(values.get("dag") or self.dag, gate_id, reopen)
                    if bad:
                        return {"rejected": "illegal_reopen", "illegal": bad, "node_id": gate_id}
            # 修 F：并行下已 resume 的中断会滞留在 get_state().interrupts（直到同批兄弟也 resolve）。
            # 交叉核对节点自身状态：done 即已答复，配合 interrupt_id 仍 pending 才 resume，陈旧 no-op。
            # **必须排在权限层之前**：escalation 会写权威 state，一条重放的旧「打回」不该
            # 在一道早就答完的门上凭空生出审批申请。
            resolved = node_id is not None and status.get(node_id) == "done"
            if resolved or interrupt_id not in pending:
                # 旧卡与新卡长得一模一样，人翻聊天记录往上点只会得到静默 no-op。
                # 当场把它标失效，「不能点」本身就是反馈。
                self._settle_card(instance_id, values, gate_id or node_id, token,
                                  "⌛ **这张卡已失效**（这一环已经进入新一轮，请找最新那张）")
                return {"skipped": "stale", "interrupt_id": interrupt_id}
            if value is not None:
                if self._is_reopen(values, gate_id, value):
                    # 机制层过了，再过权限层（ADR-023）。**不带 reopen 的「打回」也要过**：
                    # 那时用的是引擎默认目标组（gate 的直接上游），漏了它就留下一条绕行路，
                    # 前端只要什么都不带就能把任何人的活踢回去。
                    verdict = self._check_reopen(instance_id, values, gate_id, value, actor)
                    if verdict is not None:
                        return verdict
                else:
                    # 「不是打回」的每一下都是**应答**，一律过同一把尺（只判打回那半边 =
                    # 让人返工要过三条规则、让交付物生效零校验）。判据见 `_check_answer`。
                    denied = self._check_answer(instance_id, values, gate_id, actor)
                    if denied is not None:
                        return denied
            # 先回一张「已收到」（真部署第一条 e2e 撞出来的）。**下游是在下面这个 invoke
            # 里面就跑掉的**，不是在 `_advance` 里：一道门放行后紧跟着的 llm 节点实测要两分
            # 多钟（配额耗尽切备用线路时更久）。只在 invoke 之后改卡，人点完就得盯着一张毫无
            # 变化的卡等几分钟，而这正是 ADR-037 要消灭的那句原话「点了通过或者打回，卡片
            # 没有任何变化，会让用户不知道点过了没」。ADR-037 补的是「有没有」，这里补「多久」。
            # 为什么不干脆把**结论**提前写：那等于在裁决落库之前替引擎许一个还没兑现的诺，
            # invoke 万一抛了，卡上就留着一条假事实。所以这一段只说「收到了」，这在此刻是
            # 无条件为真的（权限已经验完、马上就提交）。飞书的 token 恰好可以用 2 次，正好够。
            self._settle_card(instance_id, values, gate_id or node_id, token,
                              "⏳ **已收到，正在处理**…（处理完这张卡会更新成结论）")
            self.graph.invoke(Command(resume={interrupt_id: value}),
                              self._run_cfg(instance_id), durability="sync")
            # 结论这一次仍排在 `_advance` **之前**：`_advance` 抛异常时改卡就轮不到执行，
            # 而裁决其实早就以 durability="sync" 落库了，「引擎已经收下」这个事实不该因为
            # 下游推不动就完全不可见。`_verdict_line` 只读点击前的 `values` 与点击报文，
            # 不依赖任何下游结果；`_settle_card` 自己吞异常，绝不会反过来挡住推进。
            self._settle_card(instance_id, values, gate_id or node_id, token,
                              self._verdict_line(values, gate_id or node_id, value, actor))
            self._advance(instance_id)
            return {"resumed": interrupt_id}

    def edit_graph(self, instance_id: str, ops: list[dict], *,
                   by: str | None = None, reason: str | None = None) -> dict:
        """受控活图：运行中改未来（ADR-013）。鉴权 → 校验 → 写回 dag channel → 触发下一次 dispatch。

        校验一律在**引擎权威侧**做，不信前端（ADR-019 / ADR-023）：
          ① ops 只触 pending 子图（apply_ops 的冻结线）
          ② 新图仍过 validate_template（仍是 DAG / 不悬挂 / 全部护栏）
          ③ 新图不用引擎 v1 未实现的语义；新增 tool 节点得有 handler

        **鉴权 = owner-only + 必署名**（照 ADR-024：改 / 删一道门是 owner 跳过审核的正路，
        故不套 ADR-023 那三条，那是「让别人返工」的尺）。此前这个方法连 actor 都不收，
        而它比无鉴权的 `unblock` 更狠：`unblock` 最多让人返工，`edit_graph` 能**直接删掉
        一道还在等的门**，那道审核从此不存在、流程静默放行、没有任何人收到信号。接 CLI
        （`larkflow edit`）就是把这个天窗开到真实攻击面上，故必须先补。

        审计落 `edits`（追加型 channel，与 `unblocks` / `escalations` 同类），只记**真发生
        过的**改动（ADR-034）：被拒的、被引擎校验拦下的都不留痕，否则就是假审计。
        """
        if not (by or "").strip() or not (reason or "").strip():
            return {"rejected": "missing_audit", "instance_id": instance_id,
                    "detail": "改图必须署名并说明原因（谁改的 / 为什么改是审计不变量）"}
        with self._thread_lock(instance_id):
            values = self._values(instance_id)
            if not values:
                raise GraphEditError(f"实例不存在: {instance_id}")
            if by not in self._owner_roles(values):
                return {"rejected": "unauthorized_edit", "instance_id": instance_id,
                        "actor": by,
                        "detail": "只有项目发起人能改图（ADR-024：改 / 删一道门是 owner 的正路）"}
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
            self._validate_roles(new_dag)

            # 校验全过了才记账：上面任何一条抛出去，这次改图就是**没发生过**（ADR-034）。
            audit = {"by": by, "at": _now(), "reason": reason, "ops": copy.deepcopy(ops),
                     "nodes_after": [n["id"] for n in new_dag]}
            try:
                remapped = self._write_state(instance_id, {"dag": new_dag,
                                                           "edits": {"log": [audit]}})
                self._advance(instance_id, skip=remapped)
            except Exception as exc:
                # **抛异常 ≠ 图没变**。`_write_state` 是先 `update_state` 落 checkpoint、
                # 再 `invoke` 跑一拍，而新节点就在这一拍上执行，执行体那条路上没有任何
                # try/except。真栈里这条最普通的改图就能走到：加一个知会某角色的 notify
                # 节点，四道前置校验全过（`validate_coverage` 只扫 assignee_role / voters，
                # 不看 tool.args），到 `resolver.resolve` 才抛 RoleError；LLMUnavailable 同理。
                # 让它裸抛出去的话，调用方与 CLI 会报「改图被拒」并退 1，而人照提示重试就
                # 撞「id 已存在」。所以要分清「没落库」（照抛）与「已落库但推进失败」（如实报）。
                if (self._values(instance_id).get("dag") or []) != new_dag:
                    raise                       # 还没落库，这次改图真的没发生
                self.provision_errors.setdefault(instance_id, []).append(
                    {"node_id": None,
                     "error": f"改图已生效，但随后的推进失败: {type(exc).__name__}: {exc}"})
                return {"edited": len(ops), "nodes": [n["id"] for n in new_dag], "remapped": 0,
                        "advance_error": f"{type(exc).__name__}: {exc}",
                        "detail": "改图已经落库生效，不要重试（重试会撞 id 已存在）；"
                                  "推进失败的原因见 error，修好后 `larkflow reconcile` 继续"}
            return {"edited": len(ops), "nodes": [n["id"] for n in new_dag],
                    "remapped": len(remapped)}

    def edit_log(self, instance_id: str) -> list[dict]:
        """这个实例被改过几次图、谁改的、为什么、改完长什么样（追加型，只增不改）。

        没有它的话，一张跑到一半的图为什么长成现在这样，事后只能靠猜。
        """
        return list((self._values(instance_id).get("edits") or {}).get("log") or [])

    def unblock(self, instance_id: str, node_id: str, *, by: str, reason: str,
                grant: int = 1, reopen: list[str] | None = None) -> dict:
        """人显式介入：把一道 `blocked` 的门放回执行前沿再试一次（ADR-029 的恢复路径）。

        没有这条通道，ADR-029 的 `blocked` 就是死局：门自己过不了冻结线（受控活图只动
        pending）、它的上游是 done 也过不了、`reopen_resets` 每次都把它重新算成 blocked。
        发起人收到「可改要素 / 改图后重试」的通知，却无路可走。

        五条不变量（每条都对应一个真实的失效模式）：
          ① **只能由人显式触发**。自动解除 = 把 ADR-029 消灭的无限重算原样放回来。
          ② **必审计**：谁 / 何时 / 为什么 / 追加了多少，落进权威 state（`unblocks`，追加型
             channel）；历史尝试（outputs / reopen_counts / attempts）原样保留，绝不覆盖。
          ③ **额度有限且自身有上界**：grant 是**追加**预算不是重置计数（reopen_counts 只增
             不减），单次收进 [1, MAX_GRANT_PER_UNBLOCK]、同一节点累计不超过
             MAX_UNBLOCK_GRANTS 次，耗尽即拒并通知发起人。否则人可以无限点，等于没有预算。
          ④ **回到 pending 后一切照常规走**：受控活图这才改得动它（不许为此放宽冻结线），
             打回规则照旧，于是「改图后重试」这条路自然通。
          ⑤ **reopen 目标过引擎侧合法域校验**（⊆ 传递祖先），绝不信调用方给的目标。

        返回结构化结果（拒绝也返回，不抛裸异常）：实例 / 节点不存在、节点不是 blocked、
        额度耗尽、目标非法各有各的 `rejected` 值。

        **这条路没有权限层**：`by` 只进审计，不做鉴权。打回的权限层（ADR-023）已经落在
        `resume` 上了，但**没有接到这里**，于是 `unblock(reopen=[...])` 是一条绕过它的路：
        谁调得到 service，谁就能借解除一道 blocked 门把任意合法祖先踢回去返工。今天的调用方
        只有运维 / demo，所以可接受；**接前端或卡片按钮之前必须先补**，做法是拿 `by` 当
        actor 过一遍 `engine.permissions.reopen_verdict`（owner 直接放行，参与人按同一条防
        踢皮球判据判，跨界走 escalation），与 `resume` 用同一把尺。
        """
        with self._thread_lock(instance_id):
            values = self._values(instance_id)
            if not values:
                return {"rejected": "no_such_instance", "instance_id": instance_id}
            dag = values.get("dag") or []
            try:
                node = node_by_id(dag, node_id)
            except KeyError:
                return {"rejected": "no_such_node", "instance_id": instance_id, "node_id": node_id}
            status = values.get("status", {})
            if status.get(node_id) != BLOCKED:
                return {"rejected": "not_blocked", "instance_id": instance_id, "node_id": node_id,
                        "status": status.get(node_id, "pending")}
            if not str(by or "").strip() or not str(reason or "").strip():
                return {"rejected": "missing_audit", "instance_id": instance_id,
                        "node_id": node_id, "detail": "解除必须记名记因：by / reason 不得为空"}

            grants = values.get("unblocks") or {}
            used = grants_used(grants, node_id)
            if used >= MAX_UNBLOCK_GRANTS:
                self._notify_owner(
                    instance_id,
                    f"「{node.get('label', node_id)}」的解除额度已用尽"
                    f"（{used}/{MAX_UNBLOCK_GRANTS} 次，实例 {instance_id}），"
                    "本次解除被拒。请改图 / 换要素重开一个实例。",
                    f"{instance_id}:{node_id}:unblock-denied:{used}")
                return {"rejected": "unblock_exhausted", "instance_id": instance_id,
                        "node_id": node_id, "grants_used": used, "max_grants": MAX_UNBLOCK_GRANTS}

            targets = list(reopen or [])
            bad = illegal_reopen(dag, node_id, targets)
            if bad:   # 合法域在引擎权威侧算：调用方（前端 / 卡片 / 运维脚本）说了不算
                return {"rejected": "illegal_reopen", "instance_id": instance_id,
                        "node_id": node_id, "illegal": bad,
                        "detail": "解冻目标须是这道门的传递祖先"}

            granted = clamp_grant(grant)
            resets = unblock_resets(dag, node_id, targets)
            record = {"by": by, "reason": reason, "grant": granted, "at": _now(),
                      "reopen": targets, "seq": used + 1}
            remapped = self._write_state(instance_id, {
                "status": resets,
                # 被解冻的节点进入**新一轮**：轮次是派单幂等键的一部分，不 +1 的话人手里
                # 还是上一轮那张卡 / 那条待办，新一轮无人被叫（实测过同款坑，见 _provision）
                "attempts": {k: 1 for k in resets},
                "unblocks": {node_id: [record]},
            }, reset=set(resets))
            try:
                self._advance(instance_id, skip=remapped)
            except Exception as exc:
                # 额度只有 MAX_UNBLOCK_GRANTS 次、不可退，而重试这一拍要跑 LLM / 发飞书：
                # 基础设施抖一下就吃掉人的一次机会，几次之后这道门就再也解不开了。
                # 审计只追加不改写，所以退法是**补一条 refund 记录**（`grants_used` /
                # `granted_budget` 做减法），历史那笔 grant 原样留着，看得出「试过、失败了」。
                try:
                    self._write_state(instance_id, {"unblocks": {node_id: [
                        {**record, "refund": True, "at": _now(),
                         "error": f"{type(exc).__name__}: {exc}"}]}})
                    self._advance(instance_id)     # 让它回到稳定态（多半是重新 blocked）
                except Exception:
                    # 已经在错误路径上了，尽力而为。残留窗口：退款那一笔也写不进去时，
                    # 额度算花掉了（有界、可审：unblocks 里看得到那笔 grant 没有配对的 refund）。
                    pass
                self.provision_errors.setdefault(instance_id, []).append(
                    {"node_id": node_id, "error": f"unblock advance {type(exc).__name__}: {exc}"})
                return {"rejected": "unblock_failed", "instance_id": instance_id,
                        "node_id": node_id, "refunded": True,
                        "error": f"{type(exc).__name__}: {exc}",
                        "detail": "解除后的重试当场失败，额度已退回，可重试"}
            return {"unblocked": node_id, "instance_id": instance_id,
                    "granted": granted, "requested": grant,
                    "grants_used": used + 1, "grants_left": MAX_UNBLOCK_GRANTS - used - 1,
                    "reopen": targets, "reset": sorted(resets),
                    "status": self.status(instance_id).get(node_id)}

    def _verdict_line(self, values: dict, node_id, value: dict, actor: str | None) -> str:
        """卡上那一行结论。**说人话**：退回到哪一环用标签不用 node id，意见原样带上。"""
        who = actor or "（引擎）"
        if value.get("passed", False):
            return f"✅ **已通过** · {who} · {_now()}"
        targets = list(value.get("reopen") or [])
        if not targets:
            node = self._node(values.get("dag") or self.dag, node_id)
            targets = list((node or {}).get("deps") or [])
        back = "、".join(self._label(values, t) for t in targets) or "上游"
        line = f"↩ **已打回** → {back} · {who} · {_now()}"
        if value.get("comment"):
            line += f"\n> 意见：{value['comment']}"
        return line

    def _settle_card(self, instance_id: str, values: dict, node_id, token, verdict: str) -> None:
        """把卡片换成「已处理」的样子。**失败绝不影响已经落地的裁决**：卡片是投影，
        权威结论在 checkpointer 里（红线）。没有 token 就跳过（任务通道 / 进程内直调）。
        """
        if not token:
            return
        update = getattr(self.io, "update_card", None)
        if update is None:
            return
        try:
            update(token=token, card=settled_card(
                f"审核「{self._label(values, node_id)}」", verdict))
        except Exception as exc:
            self.provision_errors.setdefault(instance_id, []).append(
                {"node_id": node_id, "error": f"update_card {type(exc).__name__}: {exc}"})

    # ---------- 打回权限层（ADR-023）----------
    def _actor_roles(self, actor: str | None) -> set[str]:
        """把一个 open_id 展开成他的**身份令牌集合**（他担的全部角色 + 他自己的 id）。

        一个人可能担多个角色，故是集合不是单值。带上 open_id 本身，是为了让 owner 判定
        （`meta.reporter` 存的是 open_id，不是角色）与角色判定用同一套集合运算。
        """
        if not actor:
            return set()
        fn = getattr(self.resolver, "roles_of", None)
        roles = set(fn(actor) or ()) if fn is not None else set()
        return roles | {actor}

    def _owner_roles(self, values: dict) -> set[str]:
        """持有即拥有 owner 全域打回权的令牌。v1 = 发起人的 open_id（`meta.reporter`）。"""
        reporter = (values.get("meta") or {}).get("reporter")
        return {reporter} if reporter else set()

    def _label(self, values: dict, nid) -> str:
        return (self._node(values.get("dag") or [], nid) or {}).get("label", nid)

    def _is_reopen(self, values: dict, node_id, value: dict) -> bool:
        """这一下是**打回**（走 ADR-023 三条规则）还是**应答**（谁的活谁签）。

        判据只有一个：gate + 判不通过 + 真有目标组。绝不拿「passed 不为真」当判据：
        非 gate 节点的 fail 不触发任何打回，可 `gates.finish` 对非 gate 根本不看 passed，
        照样把它标成 done。按 passed 分支的话，那条路上一道校验都不过：陌生人把卡片封套里的
        `verdict` 从 pass 改一个字，就替别人把定稿签了（实测复现，见 test_permissions）。
        目标组为空的 gate 同理落到应答那一支（护栏② 下不可达，但不留这个缺口）。
        """
        if value.get("passed", False):
            return False
        node = self._node(values.get("dag") or self.dag, node_id)
        if node is None or not is_gate(node):
            return False
        return bool(value.get("reopen") or node.get("deps"))

    def _check_answer(self, instance_id: str, values: dict, node_id, actor: str | None):
        """放行 / 定稿的身份判定。放行返回 None；否则返回结构化拒绝。

        与打回同一把尺都在引擎权威侧算（红线⑤）。`actor is None` = 引擎自己路由的通道
        （见 `resume` 的说明），不在这里判。
        """
        if actor is None:
            return None
        dag = values.get("dag") or self.dag
        if can_answer(dag, actor_roles=self._actor_roles(actor), node_id=node_id):
            return None
        attempt = (values.get("attempts") or {}).get(node_id, 0)
        self._tell(instance_id, actor,
                   f"你不是「{self._label(values, node_id)}」的应答人，这一下没有生效"
                   f"（实例 {instance_id}）。这份活归它的负责人签，卡转给你也点不动。",
                   f"{instance_id}:{node_id}:{attempt}:denied-pass:{actor}")
        return {"rejected": "unauthorized_pass", "node_id": node_id, "actor": actor,
                "detail": "放行 / 定稿只有这个节点的应答人点得动（卡片可被转发，故在引擎侧判）"}

    def _check_reopen(self, instance_id: str, values: dict, gate_id: str,
                      value: dict, actor: str | None):
        """打回的权限层判定。放行返回 None；否则返回结构化拒绝 / escalation 结果。

        目标组取「显式回传的一组」或「引擎默认（gate 的直接上游）」，与 `gates.reopen_targets`
        同一口径：默认目标同样是「让别人返工」，同样要过这一层。
        """
        dag = values.get("dag") or self.dag
        node = self._node(dag, gate_id)
        if node is None or not is_gate(node):
            return None                      # 非 gate 的 fail 不触发打回，无需权限判定
        targets = list(value.get("reopen") or node.get("deps", []))
        if not targets:
            return None
        verdict = reopen_verdict(dag, actor_roles=self._actor_roles(actor),
                                 owner_roles=self._owner_roles(values),
                                 from_node=gate_id, targets=targets)
        if verdict["denied"]:
            attempt = (values.get("attempts") or {}).get(gate_id, 0)
            self._tell(instance_id, actor,
                       f"你没有把 {[self._label(values, t) for t in verdict['denied']]} 打回重做的"
                       f"权限，这一下没有生效（实例 {instance_id}）。"
                       "打回要么是本项目发起人，要么是这道门的负责人。",
                       f"{instance_id}:{gate_id}:{attempt}:denied-reopen:{actor}")
            return {"rejected": "unauthorized_reopen", "node_id": gate_id, "actor": actor,
                    "denied": verdict["denied"],
                    "detail": "打回 = 机制层 ∩ 权限层：你不是本项目发起人，也不是这道门的负责人"}
        if verdict["needs_escalation"]:
            # **全或无**：一次请求里只要有一个目标跨界，整笔都不执行。部分执行会让人以为
            # 打回成功了，实际只回退了一半，比什么都不做更难排查。
            return self._escalate(instance_id, gate_id, verdict, actor=actor, targets=targets,
                                  comment=value.get("comment"))
        return None

    def _escalate(self, instance_id: str, gate_id: str, verdict: dict, *,
                  actor: str | None, targets: list[str], comment: str | None) -> dict:
        """跨界打回 → 落申请 + 通知审批人，**不执行打回**（ADR-023 ③）。

        v1 只做「申请 + 通知」：一键同意要真 dev app 的卡片回调，等接真栈（见 known gaps）。
        但申请必须落进**权威 state**（`escalations`，追加型 channel）而不是发条消息就没了，
        否则审批人隔天想起来这事，系统里查无此事。
        """
        values = self._values(instance_id)
        log = list((values.get("escalations") or {}).get(gate_id) or [])
        escalated = [e["target"] for e in verdict["needs_escalation"]]
        approvers = sorted({a for e in verdict["needs_escalation"] for a in e["approvers"]})
        collateral = sorted({c for e in verdict["needs_escalation"] for c in e["collateral"]})
        attempt = (values.get("attempts") or {}).get(gate_id, 0)
        whom = self._resolve_approvers(instance_id, approvers, self._owner_roles(values))

        # 去重与配额都只看**还等着拍板的那些**（`_live_escalations` 那把尺，已排掉裁决过的
        # 与轮次过期的）。这里曾经自己写了一遍判据、拿 `r["status"] == "pending"` 当活性，
        # 而那个字面量是**落库那一刻冻住的、永远是 pending**（追加型 channel 没有 UPDATE）。
        # 后果：一笔申请被驳回之后，申请人再点同一个打回会命中 duplicate 分支，卡不变、
        # `_ack_escalation` 的幂等键与上一次逐字相同又被 `_once` 吞掉，于是**他一个字都收不到，
        # 也永远提不了这笔申请**，而审批人那边查无此事（对抗 review 实测复现）。
        waiting = _live_escalations(log, attempt, answered=self._answered(values, gate_id))

        # 双击 / 事件重放不该把申请堆成一摞：同一轮、同一人、同一组目标只留一笔。
        # 目标组按**集合**比，绝不拿前端给的原始列表逐字比：`reopen_verdict` 内部本来就把
        # targets 去过重了，两处口径不一致的话，`["a"]` / `["a","a"]` / 多选框顺序不同的
        # `["b","a"]` 会被当成三笔不同申请，各占一格配额（实测：5 次语义相同的点击把这道门
        # 的审批通道点死，发起人还收到 5 条逐字相同的通知）。
        key = _target_key(targets)
        same = next((r for r in waiting if r.get("by") == actor
                     and _target_key(r.get("targets")) == key), None)
        if same is not None:
            self._ack_escalation(instance_id, gate_id, same, whom, actor)
            return {"escalated": list(same.get("escalated") or escalated), "node_id": gate_id,
                    "instance_id": instance_id, "approvers": whom, "seq": same.get("seq"),
                    "duplicate": True}
        # 换一组目标就能再发一笔，目标组有 2^|祖先| 种：不设上界的话申请队列、发起人的通知、
        # 以及 checkpointer 里的追加型 channel 都能被一个手里有卡的人刷爆。
        # 上限只管**本轮**（口径见 `_live_escalations`）：按整条历史算的话，5 格一满这道门
        # 此后**永久**提不了申请，连新一轮的合法申请也提不了（实测）。轮次本身有上界，
        # 由打回预算兜（ADR-029），所以总量仍然是有界的。
        if len(waiting) >= MAX_PENDING_ESCALATIONS:
            self._tell(instance_id, actor,
                       f"「{self._label(values, gate_id)}」本轮待批的打回申请已达上限"
                       f"（{MAX_PENDING_ESCALATIONS} 笔，实例 {instance_id}），这一笔没有提交。"
                       f"请直接找 {whom} 定夺。",
                       f"{instance_id}:{gate_id}:{attempt}:esc-full:{actor}")
            return {"rejected": "too_many_escalations", "node_id": gate_id,
                    "instance_id": instance_id, "pending": len(waiting),
                    "attempt": attempt, "max_pending": MAX_PENDING_ESCALATIONS,
                    "detail": "这道门本轮待批的打回申请已达上限；v1 还没有一键同意，"
                              "请直接联系审批人（上游改过一版之后可以再提）"}

        # 「这道门第几笔**申请**」：log 里现在还混着裁决记录，拿 len(log) 会跳号。
        seq = len(_requests(log)) + 1
        labels = {n["id"]: n.get("label", n["id"]) for n in values.get("dag") or []}
        text = (f"「{labels.get(gate_id, gate_id)}」的负责人申请把 "
                f"{[labels.get(t, t) for t in escalated]} 打回重做"
                f"（实例 {instance_id}）。这会连累 {[labels.get(c, c) for c in collateral]} 一起返工，"
                f"故需要你或项目发起人同意。申请人：{actor or '未知'}。"
                + (f"理由：{comment}" if comment else ""))

        # **先发、后记**：`notified` 写的是「真发出去了给谁」，那是投影侧的事实，不是意图。
        # 反过来（先记后发）时，飞书那一下失败就会在权威 state 里留一条「已通知」的假审计：
        # 审批人隔天来查「谁该拍板」，系统说通知过了、人却从没收到，于是没人再去追。
        # 代价是另一头的窗口：发完但写 state 之前进程死了 = 人收到了、系统里查无此事。
        # 那一头**可恢复**（他一问就发现，申请人重点一次即可），假审计不可恢复，故取这一侧。
        delivered, failed = [], []
        for who in whom:
            ok = self._ask_approval(instance_id, gate_id, who, text, seq)
            (delivered if ok else failed).append(who)

        # `approvers` 存**令牌**（角色名 / owner 的 open_id）：角色到 open_id 的映射会变，
        # 权威 state 里冻结一个当时的 id 会让日后查审计对不上人。`notified` 另存「当时真发给了谁」，
        # 两者都要，缺一不可。
        record = {"by": actor, "at": _now(), "from_node": gate_id, "targets": list(targets),
                  "escalated": escalated, "approvers": approvers, "notified": delivered,
                  "collateral": collateral, "comment": comment, "attempt": attempt,
                  "seq": seq, "status": "pending"}
        if failed:
            # 一个审批人都没通知到 = 这笔申请事实上没人知道。记下来，好让运维 / 对账看得见
            # 「有申请但没送达」，而不是以为在等人拍板。
            record["notify_failed"] = failed
        remapped = self._write_state(instance_id, {"escalations": {gate_id: [record]}})
        self._advance(instance_id, skip=remapped)

        self._ack_escalation(instance_id, gate_id, record, delivered, actor)
        return {"escalated": escalated, "node_id": gate_id, "instance_id": instance_id,
                "approvers": delivered, "notify_failed": failed,
                "collateral": collateral, "seq": seq}

    def _ask_approval(self, instance_id: str, gate_id: str, who: str, summary: str,
                      seq: int) -> bool:
        """给审批人发一张**可点的**审批卡。ADR-023 ③ 说的「一键」就在这里。

        在此之前这里发的是纯文本，拍板要有人去敲 `larkflow approve`：对一个飞书原生的
        产品来说，等于把出口修在大多数审批人根本走不到的地方。

        封套**没有 `interrupt_id`**：拍板不是在答复某个中断，而是对一笔申请表态。它靠
        `{kind, thread_id, node_id, seq, decision}` 自描述，`_route` 据 `kind` 分流。
        封套只用来路由，**身份一律取事件顶层的 `operator_id`**（红线⑤）。

        返回是否真发出去了：`notified` 记的是投影侧事实，把没送到的人记成「已通知」就是
        假审计（ADR-034）。幂等键与旧的纯文本通知同款（`{实例}:{门}:escalation:{seq}:{人}`），
        同一笔申请对同一个人一辈子只发一次。
        """
        base = {"kind": ESC_KIND, "thread_id": instance_id, "node_id": gate_id, "seq": seq}
        buttons = [
            Button(ESC_APPROVE_LABEL, {**base, "decision": "approve"}, "primary_filled"),
            Button(ESC_REJECT_LABEL, {**base, "decision": "reject"}, "danger_filled"),
        ]
        key = f"{instance_id}:{gate_id}:escalation:{seq}:{who}"
        try:
            self._once(f"esc-card:{key}",
                       lambda: self.io.send_card(target=who, summary=summary,
                                                 buttons=buttons, idem_key=key))
            return True
        except Exception as exc:
            self.provision_errors.setdefault(instance_id, []).append(
                {"node_id": gate_id,
                 "error": f"escalation card {type(exc).__name__}: {exc}"})
            return False

    def _settle_from_card(self, *, instance_id: str, gate_id: str, seq, decision,
                          actor: str | None, token: str | None) -> dict:
        """审批卡被点了：判 decision → 走引擎那条通道 → 把卡改成「已处理」。

        `decision` 只认 approve / reject。封套可伪造，认不出来的一律当没发生，**绝不猜**：
        猜错的方向是「把一个说不清的点击当成同意」，那会真的让别人返工。
        """
        if decision not in ("approve", "reject"):
            return {"skipped": "unknown_decision", "instance_id": instance_id,
                    "node_id": gate_id}
        settle = self.approve_escalation if decision == "approve" else self.reject_escalation
        out = settle(instance_id, gate_id, by=actor,
                     seq=seq if isinstance(seq, int) else None)
        self._settle_approval_card(instance_id, gate_id, out, actor=actor, token=token,
                                   decision=decision)
        return out

    def _settle_approval_card(self, instance_id: str, gate_id: str, out: dict, *,
                              actor: str | None, token: str | None, decision: str) -> None:
        """审批卡点完之后要变样（ADR-037 的纪律搬到这条通道上）。

        **越权不改卡**：卡可能已被转发，越权的是看到卡的某个人，不是这张卡本身；把「你没有
        权限」写上去会改掉所有人看到的内容，包括真正的审批人。
        """
        if not token or out.get("rejected") in ("unauthorized_approve", "self_approve"):
            return
        if out.get("approved"):
            line = (f"✅ **已同意**（{actor}）"
                    + ("；打回已执行" if out.get("landed", True) else "；但预算已耗尽，什么都没能退回"))
        elif out.get("rejected_request"):
            line = f"🚫 **已驳回**（{actor}）；没有任何东西被退回"
        elif out.get("rejected") == "already_settled":
            line = f"⌛ **这张卡已失效**：这笔申请已由 {out.get('settled_by') or '他人'} 处理过了"
        else:
            line = f"⌛ **这张卡已失效**（{out.get('rejected') or out.get('skipped')}）"
        self._settle_card(instance_id, self._values(instance_id), gate_id, token, line)

    def _ack_escalation(self, instance_id: str, gate_id: str, record: dict,
                        whom: list[str], actor: str | None) -> None:
        """给申请人一条回执。

        没有回执的话，跨界打回对点卡的人**完全没有可见反馈**（卡不变、不发消息，返回值
        只进 daemon 的 stderr），于是他只会以为没点上、接着点：而每一次重复点击都在烧
        本轮的申请配额、刷屏审批人。静默失败是那条缺陷真正的燃料。
        """
        self._tell(instance_id, actor,
                   f"你的打回申请已提交（实例 {instance_id}，第 {record.get('seq')} 笔），"
                   f"已通知 {whom}。审批同意之前不会执行，你手里这张卡仍然有效。",
                   f"{instance_id}:{gate_id}:esc-ack:{record.get('seq')}:{actor}")

    def _resolve_approvers(self, instance_id: str, approvers, owner_roles: set[str]) -> list[str]:
        """审批人令牌 → 飞书 open_id。owner 令牌本来就是 id；角色令牌**必须**过 resolver
        （不过的话真栈会把「法务」这种中文名当 open_id 直接发出去）。"""
        out: set[str] = set()
        for a in approvers or ():
            if a in owner_roles:
                out.add(a)
                continue
            try:
                out.add(self.resolver.resolve(a))
            except Exception as exc:
                self.provision_errors.setdefault(instance_id, []).append(
                    {"node_id": None, "error": f"approver {a} 解析失败: {type(exc).__name__}: {exc}"})
        return sorted(out)

    def escalations(self, instance_id: str, node_id: str | None = None):
        """跨界打回的审批申请**全量历史**（谁 / 何时 / 想打回谁 / 会连累谁 / 该谁批 / 后来谁拍的板）。

        审计口径：只追加、一条不删（`escalations` 是追加型 channel）。里面混着两类记录，
        靠 `kind` 分：`request`（缺省，向后兼容旧记录）与 `verdict`。申请这一类额外带一个
        **派生**字段 `effective_status`（pending / approved / rejected / expired），因为
        记录里那个 `status` 冻的是落库那一刻、永远是 pending。

        「现在还等着谁拍板」问 `pending_escalations`，别拿这个当待办列表。
        """
        values = self._values(instance_id)
        log = values.get("escalations") or {}
        attempts = values.get("attempts") or {}

        def annotate(gid, rows):
            verdicts, attempt = _verdicts_by_ref(rows), attempts.get(gid, 0)
            answered = self._answered(values, gid)
            return [dict(r) if r.get("kind") == "verdict"
                    else dict(r, effective_status=_effective_status(r, verdicts, attempt,
                                                                    answered))
                    for r in rows]

        if node_id:
            return annotate(node_id, list(log.get(node_id) or []))
        return {k: annotate(k, list(v)) for k, v in log.items()}

    # ---------- 一键同意 / 拒绝（ADR-023 ③ 那半边） ----------

    def approve_escalation(self, instance_id: str, gate_id: str, *, by: str | None,
                           seq: int | None = None, comment: str | None = None) -> dict:
        """同意一笔跨界打回申请，**并当场把打回执行掉**。

        没有这条通道，`_escalate` 就是个死局：申请落进权威 state，而全仓没有任何代码能
        让它前进一步。默认那颗「打回」按钮又天然带着跨界目标（`_permitted_default` 只剔
        denied），于是这是**默认路径**上的死局，不是边角。
        """
        return self._settle_escalation(instance_id, gate_id, by=by, seq=seq,
                                       comment=comment, approve=True)

    def reject_escalation(self, instance_id: str, gate_id: str, *, by: str | None,
                          seq: int | None = None, comment: str | None = None) -> dict:
        """驳回一笔申请：什么都不执行，但要**明确关掉它**并告诉申请人。

        没有这一半的话，不同意就只能干晾着：申请永远挂在待批里占配额，申请人永远在等回音。
        """
        return self._settle_escalation(instance_id, gate_id, by=by, seq=seq,
                                       comment=comment, approve=False)

    def _settle_escalation(self, instance_id: str, gate_id: str, *, by: str | None,
                           seq: int | None, comment: str | None, approve: bool) -> dict:
        """同意 / 拒绝共用的那条路：定位 → 五道闸 → 执行 → 记账 → 通知。

        闸的**顺序**是正确性的一部分，与 `resume` 同一条纪律：
          ① 审计（`by` 空即拒，照 ADR-030 的 `missing_audit`）
          ② 已拍过板（幂等：双击 / 重放 / CLI 手抖不许烧掉两轮返工）
          ③ 陈旧（门已进新一轮 = 这笔申请针对的那一版早被重做过，再批就是拿过期判定改真相源）
          ④ 自批（申请人可能正好在审批人集合里，见下）
          ⑤ 权限
        ②③ 必须排在 ④⑤ 之前：一条重放的旧同意不该在早就结束的申请上跑出新的判定。
        """
        if not (by or "").strip():
            return {"rejected": "missing_audit", "instance_id": instance_id, "node_id": gate_id,
                    "detail": "同意 / 拒绝必须署名（谁拍的板是审计不变量，照 ADR-030）"}
        with self._thread_lock(instance_id):
            values = self._values(instance_id)
            log = list((values.get("escalations") or {}).get(gate_id) or [])
            attempt = (values.get("attempts") or {}).get(gate_id, 0)
            settled = _verdicts_by_ref(log)
            base = {"instance_id": instance_id, "node_id": gate_id}

            record = self._pick_escalation(log, attempt, seq,
                                           answered=self._answered(values, gate_id))
            if isinstance(record, dict) and record.get("rejected"):
                return {**base, **record}

            if record["seq"] in settled:
                done = settled[record["seq"]]
                return {**base, "rejected": "already_settled", "seq": record["seq"],
                        "verdict": done.get("verdict"), "settled_by": done.get("by"),
                        "detail": "这笔申请已经有人拍过板了"}
            if record.get("attempt") != attempt or self._answered(values, gate_id):
                # 两种作废：门进了新一轮（那一版早被重做过），或门已经被答复掉了
                # （申请不是裁决，提申请的人手里那张卡仍然有效，他完全可能自己点了通过）。
                return {**base, "skipped": "stale", "seq": record["seq"],
                        "detail": "这笔申请已随轮次推进 / 这道门被答复而作废"}
            if by == record.get("by"):
                # `approvers_for` = owner 令牌 ∪ 目标节点主负责人，而申请人完全可能正好是
                # 后者（他打回自己的活，但重算集牵连了第三个人）。不禁的话他自己提、自己批，
                # ADR-023 那三条规则被整个绕开。owner 恒在审批人里、且 owner 走不到 escalation
                # 这条路（他有全域权、直接执行），所以禁自批不会造成一笔申请无人可批。
                return {**base, "rejected": "self_approve", "seq": record["seq"],
                        "detail": "不能同意自己提的申请，请找 owner 或目标节点负责人"}
            if not self._can_approve(by, record):
                return {**base, "rejected": "unauthorized_approve", "seq": record["seq"],
                        "approvers": list(record.get("approvers") or []),
                        "detail": "只有项目发起人或被打回节点的负责人能拍这个板"}

            reopened: list[str] = []
            if approve:
                blocked = self._execute_approved_reopen(instance_id, values, gate_id, record)
                if isinstance(blocked, dict):
                    return {**base, **blocked, "seq": record["seq"]}
                reopened = blocked

            # **先执行、后记账**（ADR-034）。中间崩掉的话：打回已落地 → 门进了新一轮 →
            # 这笔申请按轮次自然作废，不会被二次同意。反过来（先记后执行）崩掉，就是
            # 「显示已批准、其实什么都没发生」，没有任何机制能发现，不可恢复。
            verdict = {"kind": "verdict", "ref": record["seq"], "node_id": gate_id,
                       "verdict": "approved" if approve else "rejected",
                       "by": by, "at": _now(), "attempt": attempt, "comment": comment}
            if approve:
                verdict["reopened"] = reopened
            remapped = self._write_state(instance_id, {"escalations": {gate_id: [verdict]}})
            self._advance(instance_id, skip=remapped)

            self._announce_verdict(instance_id, values, gate_id, record,
                                   by=by, comment=comment, approve=approve, reopened=reopened)
            out = {**base, "seq": record["seq"], "by": by}
            if not approve:
                return {**out, "rejected_request": True}
            if not reopened:
                # 同意了，但引擎**什么都没退回**：打回预算已耗尽，`reopen_resets` 直接把这道门
                # 标成 blocked、一个节点都不重置（gates.py）。此前这里照样回 approved 并且
                # 宣告「X 已退回重做 / 你被卷进返工」，而两人的节点其实一动没动，谁也不会收到
                # 新单；整条流程停在 blocked 等 unblock，只有发起人从 ADR-029 那条独立通知里
                # 知道。**批准是真的，落地不是**，这两件事必须分开报（对抗 review 实测）。
                return {**out, "approved": True, "reopened": [], "landed": False,
                        "gate_status": (self._values(instance_id).get("status") or {}).get(gate_id),
                        "detail": "已同意，但这道门的打回预算已耗尽，什么都没能退回；"
                                  "实例已停下等人 unblock（ADR-029 / ADR-030）"}
            return {**out, "approved": True, "reopened": reopened, "landed": True}

    def _pick_escalation(self, log: list[dict], attempt: int, seq: int | None, *,
                         answered: bool):
        """定位要拍板的那一笔。省略 seq 时**绝不瞎猜**：一道门本轮可以挂多笔申请。"""
        requests = _requests(log)
        if seq is not None:
            found = next((r for r in requests if r.get("seq") == seq), None)
            return found if found is not None else {
                "rejected": "no_such_escalation", "seq": seq,
                "detail": "这道门没有第 %s 笔申请" % seq}
        live = _live_escalations(log, attempt, answered=answered)
        if not live:
            return {"rejected": "no_such_escalation",
                    "detail": "这道门本轮没有待拍板的申请"}
        if len(live) > 1:
            return {"rejected": "ambiguous_escalation",
                    "candidates": [r.get("seq") for r in live],
                    "detail": "本轮有多笔待批，请用 seq 指明是哪一笔"}
        return live[0]

    def _can_approve(self, actor: str, record: dict) -> bool:
        """这个人拍得动这个板吗。**两把尺，缺一不可。**

        ① 令牌求交：`approvers` 存的是令牌（角色名 / owner 的 open_id），拿 `roles_of`
           把 operator 反解成他的令牌集合再求交，与打回权限层同一口径。
        ② **当时真通知到的那些 open_id**：①会静默失效（自定义 resolver 没有 `roles_of`、
           角色映射后来改了、assignee 配成飞书群），一旦失效这笔申请就**没人同意得了**,
           死局原样复发。我们当初亲口告诉了他「该你拍板」，他就该点得动。这不是放宽，
           是把当时那次通知本身当成授权凭据（它已经在权威 state 里，不可伪造）。
        """
        tokens = self._actor_roles(actor)
        if tokens & set(record.get("approvers") or ()):
            return True
        return actor in set(record.get("notified") or ())

    def _execute_approved_reopen(self, instance_id: str, values: dict, gate_id: str,
                                 record: dict):
        """把这笔申请当初想做的打回真做掉。成功返回被退回的节点，失败返回结构化拒绝。

        批准替代的是**权限层**，不是机制层：申请挂着的这段时间图可能被改（受控活图），
        当时的合法祖先今天可能已经不是祖先了。拿一份过期的目标组直接写真相源，正是
        「一切合法性在引擎权威侧现算」要防的事。
        """
        targets = list(record.get("targets") or [])
        dag = values.get("dag") or self.dag
        bad = illegal_reopen(dag, gate_id, targets)
        if bad:
            return {"rejected": "illegal_reopen", "illegal": bad,
                    "detail": "图改过之后这些目标已经不是这道门的祖先了，不能照当时的申请执行"}
        live = {i.id: (i.value or {}).get("node_id")
                for i in self._pending_interrupts(instance_id)}
        interrupt_id = next((k for k, v in live.items() if v == gate_id), None)
        if interrupt_id is None:
            # 门已经不在等了（被别的路答掉 / 图改没了）。批准一个没有落点的打回会静默丢失。
            return {"skipped": "stale", "detail": "这道门已经不在等人了"}

        before = dict(values.get("attempts") or {})
        self.graph.invoke(
            Command(resume={interrupt_id: {"verdict": "fail", "reopen": targets,
                                           "comment": record.get("comment")}}),
            self._run_cfg(instance_id), durability="sync")
        self._advance(instance_id)
        # 「退回了什么」按**轮次增量**算，不按 status 前后差：llm / tool 节点会在同一次
        # `_advance` 里重跑完又回到 done，看 status 的话它们会凭空消失。
        after = self._values(instance_id).get("attempts") or {}
        return sorted(k for k, v in after.items() if v > before.get(k, 0))

    def _announce_verdict(self, instance_id: str, values: dict, gate_id: str, record: dict,
                          *, by: str, comment: str | None, approve: bool,
                          reopened: list[str]) -> None:
        """申请人在等回音，被连累的人即将平白返工。两边都不该靠猜。

        投影侧动作，失败只记不抛（`_tell` 自带这条纪律）：权威结论已经在 checkpointer 里。
        """
        seq, gate = record.get("seq"), self._label(values, gate_id)
        tail = f"，附言：{comment}" if comment else ""
        if approve:
            # **只说真发生了的事**：报 `reopened`（引擎实际退回的）与 targets 的交集，
            # 不报申请里写的那一组。打回预算耗尽时 `reopened` 是空的，一个节点都没动，
            # 而此前这里照读 `record["targets"]`，于是申请人被告知「已退回重做」、旁支
            # 负责人被告知「你也被卷进返工」，两句都是假的（形参 `reopened` 收了却没用）。
            landed = [t for t in record.get("targets") or () if t in set(reopened or ())]
            what = [self._label(values, t) for t in landed]
            if not landed:
                self._tell(instance_id, record.get("by"),
                           f"你对「{gate}」提的打回申请（第 {seq} 笔）已由 {by} 同意，"
                           f"但这道门的打回预算已耗尽，**什么都没能退回**，实例已停下等人"
                           f"解除（实例 {instance_id}）{tail}。",
                           f"{instance_id}:{gate_id}:esc-approved-void:{seq}")
                return          # 没人被卷进返工，就别去惊动旁支负责人
            self._tell(instance_id, record.get("by"),
                       f"你对「{gate}」提的打回申请（第 {seq} 笔）已由 {by} 同意，"
                       f"{what} 已退回重做（实例 {instance_id}）{tail}。",
                       f"{instance_id}:{gate_id}:esc-approved:{seq}")
            for cid in record.get("collateral") or ():
                if cid not in set(reopened or ()):
                    continue    # 没被真卷进来就不发，假通知比不发更糟
                self._tell(instance_id, self._owner_of(values, cid),
                           f"「{gate}」申请把 {what} 打回重做，已获同意，"
                           f"你负责的「{self._label(values, cid)}」也被卷进这一轮返工"
                           f"（实例 {instance_id}）。",
                           f"{instance_id}:{gate_id}:esc-collateral:{seq}:{cid}")
        else:
            what = [self._label(values, t) for t in record.get("targets") or ()]
            self._tell(instance_id, record.get("by"),
                       f"你对「{gate}」提的打回申请（第 {seq} 笔）被 {by} 驳回，"
                       f"{what} 没有退回，这道门还在等你的裁决（实例 {instance_id}）{tail}。",
                       f"{instance_id}:{gate_id}:esc-rejected:{seq}")

    def _owner_of(self, values: dict, node_id: str) -> str | None:
        """节点主负责人令牌 → open_id（解析不出来就不发，别把中文角色名当 open_id 发出去）。"""
        from .engine.permissions import primary_owner

        token = primary_owner(self._node(values.get("dag") or [], node_id) or {})
        if not token:
            return None
        try:
            return self.resolver.resolve(token)
        except Exception:
            return None

    def pending_escalations(self, instance_id: str, node_id: str | None = None):
        """**现在还等着人拍板**的申请（每道门只留它当前那一轮的，口径见 `_live_escalations`）。

        与配额用同一把尺：驾驶舱 / 审批人看到的「待批」条数，就是引擎算配额时看到的那些。
        """
        values = self._values(instance_id)
        log = values.get("escalations") or {}
        attempts = values.get("attempts") or {}
        live = {k: _live_escalations(v, attempts.get(k, 0),
                                     answered=self._answered(values, k))
                for k, v in log.items()}
        if node_id:
            return live.get(node_id) or []
        return {k: v for k, v in live.items() if v}

    def _answered(self, values: dict, gate_id: str) -> bool:
        """这道门**已经不在等人拍板**了吗。

        判据是 `done / failed / blocked` 三种，不是 `_unanswered` 的两种：**`blocked` 必须
        算进来**。一道门被打回预算掐成 `blocked` 时 `attempt_increments` 为空、`attempts`
        一动不动，于是「轮次已过」那把尺不触发；而它也没被答复，「门已答复」那把尺按
        `_unanswered` 的口径同样不触发。三条出局判据一条都不命中的后果：同轮那些没拍板的
        申请**永远显示待批**，而 `_execute_approved_reopen` 那边按「还有没有挂起中断」判，
        blocked 之后没有中断 → 每次同意都回 `stale`，只有 reject 出得去（对抗 review 实测）。
        """
        return (values.get("status") or {}).get(gate_id) in ("done", "failed", BLOCKED)

    def _node(self, dag: list[dict], nid) -> dict | None:
        try:
            return node_by_id(dag or [], nid)
        except (KeyError, TypeError):
            return None

    def unblock_log(self, instance_id: str, node_id: str | None = None):
        """解除审计（谁 / 何时 / 为什么 / 追加多少）。node_id 省略则返回全实例的。"""
        log = self._values(instance_id).get("unblocks") or {}
        return list(log.get(node_id) or []) if node_id else {k: list(v) for k, v in log.items()}

    def reconcile(self, instance_id: str) -> dict:
        """对账：按当前权威 state 重建飞书投影（幂等）+ 把被屏障挡住的分支推到位。

        用于「进程崩在建任务与写关联表之间」「某个人派单失败」这类投影缺失，
        以及运维手动催单。**不动 state 的业务值**，故可随时重跑。
        """
        # 扫描在**锁外**做：它内部走 `resume`，而 resume 自己要取同一把锁，
        # 那把锁不可重入（跨进程 flock 更是），在锁内调会直接死锁。
        self.provision_errors.pop(instance_id, None)
        self._sweep_tasks(instance_id)
        with self._thread_lock(instance_id):
            self._advance(instance_id)
        return {"reconciled": instance_id, "errors": self.provision_errors.get(instance_id, [])}

    def _sweep_tasks(self, instance_id: str) -> None:
        """把**丢掉的任务完成事件**捞回来。

        长连接会**静默死亡**：进程活着、TCP 显示 ESTABLISHED、日志无异常，而一条事件都
        收不到（实测一次睡眠后连着 10 小时 48 分 RECEIVED=0）。这时两条入站通道的表现
        完全不对称：
          · **卡片**失败得响：用户当场看到「目标回调服务当前未在线」，会再点一次；
            而且卡片没有「状态」可查，本来也轮询不了。
          · **任务**失败得无声无息：用户看到任务已完成、引擎还在等，双方都觉得自己对，
            谁也不会去查。这条不轮询就永远发现不了，实例就此停死。
        故只扫 `task_complete` 的在等节点，逐个反查飞书。查不到 / 报错都只记一笔，
        绝不因此推进（红线：完成必须来自显式信号，轮询读的仍是人的真实动作）。
        """
        get = getattr(self.io, "get_task", None)
        if get is None:
            return
        values = self._values(instance_id)
        status, attempts = values.get("status", {}), values.get("attempts") or {}
        for it in self._live_interrupts(instance_id, status):
            v = it.value or {}
            nid = v.get("node_id")
            if v.get("signal") != "task_complete" or not nid:
                continue        # 卡片没有「状态」可查，而且它失败得响，不需要补
            # **只看本轮那条待办**。一个节点被打回 N 次就有 N+1 条飞书待办，旧的那几条
            # 永远停在「已完成」；按 node_id 去翻关联表会拿第 1 轮的完成去推第 3 轮，
            # 于是每对账一次就白烧一轮打回预算（真栈实测：两次重启把预算烧到上限、
            # 实例直奔 blocked）。派单幂等键 `{实例}:{节点}:{轮次}` 天然只指向本轮。
            guid = self._idem.get(
                self._dispatch_key(instance_id, nid, attempts.get(nid, 0)) + ":task")
            if not guid:
                continue
            try:
                if not (get(guid) or {}).get("completed"):
                    continue
            except Exception as exc:
                self.provision_errors.setdefault(instance_id, []).append(
                    {"node_id": nid, "error": f"get_task {type(exc).__name__}: {exc}"})
                continue
            # 捞回来这件事必须**看得见**：它意味着入站通道漏过事件，是要人管的故障信号，
            # 不是「系统很聪明地自愈了」。静默自愈会让一条死掉的通道永远不被发现。
            self.provision_errors.setdefault(instance_id, []).append(
                {"node_id": nid,
                 "error": "recovered: 任务已完成但没收到事件（入站通道漏了，请查 event status）"})
            self.resume(instance_id=instance_id, interrupt_id=it.id,
                        value={"passed": True, "completed": True}, node_id=nid)

    def status(self, instance_id: str) -> dict:
        return self._values(instance_id).get("status", {})

    def dag_of(self, instance_id: str) -> list[dict]:
        """这个实例**自己**的活图（受控活图会让它与装配期模板不同）。

        驾驶舱 / CLI / 对账一律按它算，绝不拿装配期的 `self.dag` 当所有实例的图。
        实例不存在时返回空表（调用方据此判「查无此实例」，不必抓异常）。
        """
        return self._values(instance_id).get("dag") or []

    def finished(self, instance_id: str) -> bool:
        """全部节点 done。启动对账据它跳过跑完的实例（没有投影要重建、没有活要推）。"""
        values = self._values(instance_id)
        dag = values.get("dag") or []
        return bool(dag) and all_done(dag, values.get("status", {}))

    def outputs(self, instance_id: str) -> dict:
        """节点产出 + 交付物 handle 权威登记表（ADR-020）。"""
        return self._values(instance_id).get("outputs", {})

    def pending(self, instance_id: str, *, actor: str | None = None) -> list[dict]:
        """当前卡在谁手上：每个挂起的人工节点一条（含交付物链接、打回候选）。

        供驱动 / 前端读（前端读接口形态待定，见 SPEC 待填）；不含真相源以外的东西。

        `actor`（open_id）= **以谁的视角看**。传了就把 `reopen_candidates` / `reopen_default`
        按 ADR-023 的权限层过滤成「这个人真点得动的」，并多给一个 `reopen_escalation`
        （要走审批才点得动的）。**不传 = 机制层全集**，这是给运维 / 驾驶舱看的口径，
        向后兼容；它不表示「谁都能点」，真正的判定在 `resume` 的引擎权威侧再做一次。
        """
        values = self._values(instance_id)
        status = values.get("status", {})
        items = [{"interrupt_id": it.id, **(it.value or {})}
                 for it in self._live_interrupts(instance_id, status)]
        if actor is None:
            return items
        dag = values.get("dag") or self.dag
        roles, owner = self._actor_roles(actor), self._owner_roles(values)
        for p in items:
            if p.get("reopen_candidates") is None:
                continue
            v = reopen_verdict(dag, actor_roles=roles, owner_roles=owner,
                               from_node=p["node_id"], targets=p["reopen_candidates"])
            p["reopen_candidates"] = v["allowed"]
            p["reopen_escalation"] = [e["target"] for e in v["needs_escalation"]]
            # 默认目标与卡上那颗按钮**同一把尺**（见 `_permitted_default`）：只剔 denied，
            # 保留要走审批的。只留 allowed 的话，照这个读接口预勾选再回传，就又是一次
            # 静默的部分打回：同一个「打回」有两种语义比没有过滤更糟。
            denied = set(v["denied"])
            p["reopen_default"] = [t for t in (p.get("reopen_default") or []) if t not in denied]
        return items

    # ---------- 内部 ----------
    def _cfg(self, instance_id: str) -> dict:
        return {"configurable": {"thread_id": instance_id}}

    def _run_cfg(self, instance_id: str, dag: list[dict] | None = None) -> dict:
        """运行（invoke）用；recursion_limit 按**运行时** dag + 已追加的解除额度现算。

        活图会让图长大、start(template=…) 也可能比装配期模板大得多，
        按装配期算死会在中途炸 GraphRecursionError（实例停半截、投影孤悬）。
        """
        grants: dict = {}
        if not dag:
            values = self._values(instance_id)
            dag = values.get("dag") or self.dag
            grants = values.get("unblocks") or {}
        # 每 DAG 层约 2 super-step，每轮打回约 4 个；预算配大了也不能退回 GraphRecursionError
        # （那正是 ADR-029 要消灭的现象），故 unblock 追加的额度也必须算进来
        return {"configurable": {"thread_id": instance_id},
                "recursion_limit": 2 * len(dag) + 4 * total_reopen_budget(dag, grants) + 25}

    # ---------- 推进：绕开 super-step 屏障 ----------
    def _write_state(self, instance_id: str, updates: dict,
                     *, reset: set[str] | None = None) -> set[str]:
        """**保值**写回 state 并推一步；返回被重绑的新中断 id。

        两条实测出来的硬约束（见 AIREADME/MEMORY）：
        ① `update_state` 会落一个新 checkpoint，而在飞的 super-step 里**已完成任务的写入还没提交**，
           不把当前观测到的 status/outputs 原样带上，就会被静默丢掉（有人刚点的裁决会凭空消失）。
        ② `as_node` 指到某个 worker，其唯一出边是 dispatch，于是 dispatch **真的执行一次**：
           打回重置逻辑仍留在引擎里，驱动层不重算。

        `reset` = 调用方这一拍主动重置为 pending 的节点（目前只有 unblock）。它们与被打回
        重置的节点同性质：进新一轮、不许重绑旧中断。
        """
        values = self._values(instance_id)
        status = values.get("status", {})
        # 只有「这一拍之后仍是同一轮、同一个人在等」的中断才允许重绑：
        #  · 已答复的（done / failed）不算，他的旧卡该失效；
        #  · 这一拍要被打回 / 解除重置的不算，那是**新一轮**，人必须收到新卡。
        # 漏掉第二条会出人命：被卷进新一轮的旁观者不补发新单，而他上一轮的旧卡被重绑到新一轮，
        # 一点就把「对 v1 的裁决」记成「放行 v2」（实测复现过）。
        reopened = {k for k, v in reopen_resets(values.get("dag") or [], status,
                                                values.get("outputs", {}),
                                                values.get("reopen_counts", {}),
                                                values.get("unblocks", {})).items()
                    if v == "pending"} | (reset or set())
        before = {i.id: (i.value or {}).get("node_id")
                  for i in self._pending_interrupts(instance_id)
                  if _unanswered(status, (i.value or {}).get("node_id"))
                  and (i.value or {}).get("node_id") not in reopened}
        payload = {
            "dag": updates.get("dag") or values.get("dag"),
            "status": {**values.get("status", {}), **updates.get("status", {})},
            "outputs": {**values.get("outputs", {}), **updates.get("outputs", {})},
            # 累加 / 追加型 channel（attempts / unblocks）原样透传：它们**不能**进保值集，
            # 保值写回每推进一拍就会再累加一次（预算 3 秒变 1、审计凭空多出几条假记录）。
            # reopen_counts 同理，且只由 dispatch 写，驱动层根本不碰。
            **{k: v for k, v in updates.items() if k not in ("dag", "status", "outputs")},
        }
        self.graph.update_state(self._cfg(instance_id), payload, as_node=_PUMP_AS_NODE)
        self.graph.invoke(None, self._run_cfg(instance_id), durability="sync")
        return self._remap_interrupts(instance_id, before)

    @staticmethod
    def _fingerprint(values: dict) -> tuple:
        """「这一拍到底动了没有」的判据。

        只比 `status` 会漏判：一道门重试**再次失败**时，status 从 `failed` 出发、经
        pending、重跑、又回到 `failed`，前后快照**逐字相同**，于是被当成「推不动了」提前
        返回，实例停在 `failed` 而不是 `blocked`。后果是双重的：`blocked` 通知不发（没人
        知道它又死了），`unblock` 还会以 `not_blocked` 拒绝它，ADR-029 的出口当场失效。
        累加型通道（`reopen_counts` / `attempts`）单调递增，正好补上 status 看不见的那一拍。
        """
        return (tuple(sorted((values.get("status") or {}).items())),
                tuple(sorted((values.get("reopen_counts") or {}).items())),
                tuple(sorted((values.get("attempts") or {}).items())))

    def _stalled(self, values: dict, live: set[str]) -> bool:
        """还有活该干却没被派出去吗？

        LangGraph 的 super-step 是**屏障**：只要有人工节点挂着，dispatch 就不会再跑。于是
        ① gate 判了打回却落不了地（上游不重算、没人被通知）；
        ② 完全不相干的并行分支一起停（实测：B 跑完，C/D/E 全卡在另一条支的签字上）。
        两者都表现为「按 dag 该就绪的节点没有在飞」。
        """
        dag, status = values.get("dag") or [], values.get("status", {})
        if reopen_resets(dag, status, values.get("outputs", {}),
                         values.get("reopen_counts", {}), values.get("unblocks", {})):
            return True
        return any(n["id"] not in live for n in ready_nodes(dag, status))

    def _advance(self, instance_id: str, *, skip: set[str] | None = None) -> None:
        """处理挂起（派单 / 投影），把被屏障挡住的分支推到位，最后收拾旧轮次的待办。"""
        try:
            self._pump(instance_id, skip=skip)
        finally:
            # 关旧单挂在**一次推进一次**，而不是挂在 `_handle`（泵循环里每拍都调）。
            # 成功时两者没差别（`_once` 之后全是本地幂等表查询，很便宜），但**失败时**
            # 挂在 `_handle` 上会每一拍都真去 spawn 一次 lark-cli，一次推进能放大到几十次。
            # 放在 finally 里：推进本身炸了，旧单照样该收拾（它与推进成不成功无关）。
            try:
                self._close_stale_tasks(instance_id, self._values(instance_id))
            except Exception as exc:
                self.provision_errors.setdefault(instance_id, []).append(
                    {"node_id": None, "error": f"关旧单扫描失败: {type(exc).__name__}: {exc}"})

    def _pump(self, instance_id: str, *, skip: set[str] | None = None) -> None:
        self._handle(instance_id, skip=skip)
        snapshot = self._values(instance_id)
        dag = snapshot.get("dag") or self.dag
        rounds = 2 * len(dag) + 2 * total_reopen_budget(dag, snapshot.get("unblocks") or {}) + 5
        for _ in range(rounds):
            values = self._values(instance_id)
            live = {(i.value or {}).get("node_id") for i in self._pending_interrupts(instance_id)}
            if not self._stalled(values, live):
                return
            before = self._fingerprint(values)
            remapped = self._write_state(instance_id, {})
            self._handle(instance_id, skip=remapped)
            if self._fingerprint(self._values(instance_id)) == before:
                return   # 推不动了就停，别空转
        # 上界耗尽仍没推完：实例停在半截态，绝不能静默返回（那正是 ADR-029 要消灭的现象）
        self.provision_errors.setdefault(instance_id, []).append(
            {"node_id": None, "error": f"advance 推进 {rounds} 拍仍未收敛，实例停在半截态"})
        # 幂等键含**当前总轮次**：同一个键一辈子只发一次（见 `_once`），不带轮次的话
        # 「这个实例又卡住了」第二次起就再也没人知道。
        rounds_so_far = sum((self._values(instance_id).get("attempts") or {}).values())
        self._notify_owner(instance_id, f"实例 {instance_id} 推进未收敛，已停下等人介入。",
                           f"{instance_id}:advance:stalled:{rounds_so_far}")

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
        values = self._values(instance_id)
        status = values.get("status", {})
        for it in self._live_interrupts(instance_id, status):
            if skip and it.id in skip:
                continue   # 只是改图 / 推进导致的换 id：卡 / 任务还在人手里，别重复派
            try:
                self._provision(instance_id, it)
            except Exception as exc:   # 一个人派失败，不能连累同批其他人
                self.provision_errors.setdefault(instance_id, []).append(
                    {"node_id": (it.value or {}).get("node_id"), "interrupt_id": it.id,
                     "error": f"{type(exc).__name__}: {exc}"}
                )
        self._project(instance_id)

    def _close_stale_tasks(self, instance_id: str, values: dict) -> None:
        """把**旧轮次**那些还开着的飞书待办关掉（ADR-040）。

        `_provision` 每一轮建一条新待办，而在此之前**没有任何代码去关旧的**：一个节点被
        打回 N 次，人的待办列表里就留 N 条永远不会有人点的僵尸（真栈第一条 e2e 之后
        实测留下 2 条，手工清的）。

        最难受的不是「旧轮次里人已经点完的那些」，是**被卷进新一轮、但新一轮还没轮到派单**
        的旁支节点：它得等上游返工完成，这中间人手里那条旧单一直开着，长得和能干的活一模
        一样，点下去只有静默 no-op（任务通道没有卡片那套「陈旧当场作废」，`_settle_card`
        要回调 token，任务事件没有）。所以关的时机挂在这里（每次推进都跑）而不是挂在
        `_provision`（那要等到给他派新单的时候，旁支节点可能要等一整轮）。

        为什么按 `range(当前轮次)` 现算而不是前后 diff：轮次是在调用方的 `graph.invoke`
        里就 +1 的，`_advance` 拿不到那一刻的前值。现算是幂等的，重启 / 对账重跑都对，
        而且天然把「上次关单失败的」补上。

        **绝不关本轮那条**（`range` 天然排除）：`_sweep_tasks` 会把 `completed == True`
        当成人的真实完成信号并 resume，关错一条就是引擎替人交了卷，破「完成必须来自
        显式信号」这条红线。
        """
        attempts = values.get("attempts") or {}
        for n in values.get("dag") or ():
            nid = n.get("id")
            if not nid or n.get("signal") != "task_complete":
                continue          # 卡片另有 ADR-037 的「当场标失效」，且它没有「完成」这个动作
            for old in range(attempts.get(nid, 0)):
                guid = self._idem.get(self._dispatch_key(instance_id, nid, old) + ":task")
                if not guid:
                    continue
                try:
                    # 键里必须带**旧轮次号**：不带的话同一个节点第二次被打回会被幂等表
                    # 整个吞掉，第二条僵尸永远关不掉。失败不记键（`_once` 先做后记），
                    # 于是下一次推进 / 对账会自己补上。
                    self._once(f"{instance_id}:{nid}:{old}:task-closed",
                               lambda g=guid, o=old: self._do_close(instance_id, nid, g, o))
                except Exception as exc:
                    self.provision_errors.setdefault(instance_id, []).append(
                        {"node_id": nid,
                         "error": f"complete_task 关第 {old} 轮旧单失败: "
                                  f"{type(exc).__name__}: {exc}"})

    def _do_close(self, instance_id: str, node_id: str, guid: str, attempt: int) -> str:
        self.io.complete_task(guid, idem_key=f"{instance_id}:{node_id}:{attempt}:close")
        return ""

    def _live_interrupts(self, instance_id: str, status: dict) -> list:
        """真正还在等人的中断。

        并行下已 resume 的中断会滞留在 `get_state().interrupts`（直到同批兄弟也 resolve），
        直接用会把已答复的节点当成「还卡在他手上」，既误导驾驶舱也会重复派单（修 F 同款判据）。
        """
        return [it for it in self._pending_interrupts(instance_id)
                if _unanswered(status, (it.value or {}).get("node_id"))]

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
        """把一个挂起的人工节点投影成飞书待办 / 卡片。**同一件事一辈子只派一次。**

        两层幂等，缺一不可：
          · 幂等键用**轮次**不用中断 id：中断 id 每推进一拍就换，拿它当键会让同一个人、
            同一件事反复收到新卡 / 新待办且无上限（实测）。轮次只在真被打回时 +1。
          · 键记在**本地**幂等表里（`_once`）。只靠飞书 --idempotency-key 的话，那个窗口
            只有 1 小时：隔夜重启 / `larkflow reconcile` 会真的再建一条待办、再发一张卡，
            而重复的待办没有任何代码去关掉它，永远躺在人的待办列表里（实测）。
        本地命中时仍然补写一次关联表：崩在「建任务」与「写关联表」之间的那种投影缺失，
        正是靠这一笔补回来的（不补的话那条待办点了引擎收不到）。
        """
        v = it.value or {}
        nid, signal = v["node_id"], v.get("signal")
        iid = it.id
        kind = {"task_complete": "task", "card_action": "card"}.get(signal)
        if kind is None:
            return
        attempt = (self._values(instance_id).get("attempts") or {}).get(nid, 0)
        idem = self._dispatch_key(instance_id, nid, attempt)

        def make():
            # 派单对象在这里才解析：重放时连 resolver 都不必碰（真栈 strict 下它会抛）
            assignee = self._assignee(instance_id, nid, v.get("assignee_role"))
            if kind == "task":
                out = self.io.create_task(assignee=assignee, summary=v.get("label", nid),
                                          description=self._criteria(v), idem_key=idem)
            else:
                # 卡片正文也要带交付物链接：门禁走卡不走任务，只给个标题等于让人空手审
                out = self.io.send_card(target=assignee, summary=self._criteria(v),
                                        buttons=self._buttons(instance_id, iid, nid, v),
                                        idem_key=idem)
            if not out:
                # 拿不到外部 id = 这条待办 / 这张卡回不来（关联表路由不了）。**别把它记成
                # 「已派」**，否则本地幂等表会永久挡住重试，这个人就此没人叫。抛出去由
                # `_handle` 记一笔派单失败，下次对账重试。
                raise RuntimeError(f"{kind} 派单没拿到外部 id（{idem}）")
            return out

        external = self._once(f"{idem}:{kind}", make)
        self.corr.put(Correlation(external, instance_id, iid, nid, kind))

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
        for f in v.get("feedback") or []:
            lines.append(f"上一轮被「{f.get('label') or f.get('from')}」打回：{f.get('comment') or '（未留言）'}")
        # 审核人得先能打开要审的那份东西（gate 自己不产出交付物）
        lines += [f"待审：{u['label']} {u['url']}" for u in v.get("upstream") or []]
        return "\n".join(lines)

    def _buttons(self, instance_id: str, iid: str, nid: str, v: dict) -> list[Button]:
        base = {"thread_id": instance_id, "interrupt_id": iid, "node_id": nid}
        if v.get("role") != "gate":  # human-produce（定稿确认）：单按钮
            return [Button(DONE_LABEL, {**base, "verdict": "pass"}, "primary_filled")]
        # 打回按钮带默认目标组；多选 reopen 的卡片视觉 schema 待 dev app（见 SPEC 待填），
        # 前端 / app 可用同一自描述封套回传任意合法目标组，引擎侧再校验一次。
        default = self._permitted_default(instance_id, nid, v)
        reopen = {"reopen": default} if default else {}
        return [
            Button(PASS_LABEL, {**base, "verdict": "pass"}, "primary_filled"),
            Button(REOPEN_LABEL, {**base, "verdict": "fail", **reopen}, "danger_filled"),
        ]

    def _permitted_default(self, instance_id: str, nid: str, v: dict) -> list[str]:
        """卡上的默认打回目标也要过权限层：别给收卡人一个**点了必被拒**的目标（ADR-023）。

        视角 = **这张卡将要发给谁**（该节点的负责人），不是随便某个人。

        只剔 `denied`（机制层就回不去 / 他根本没资格站在这道门上），**绝不剔
        `needs_escalation`**：那些是「点得动，但要先请人同意」，剔掉就成了一次静默的
        部分打回：审核人判的是「这一版不行」，引擎却只把其中一半退回去重做，另一半
        原样留着、申请没落、谁都没被告知。`_check_reopen` 的「全或无」正是为了防这件事，
        在发卡那一刻把目标削一半等于把它架空（实测复现：g 的默认目标 [c, b] 被削成 [c]，
        点一下只重算 c，b 与它的审批申请一起消失）。
        全被剔光时返回空：`_buttons` 会退回引擎默认目标组，引擎侧照样再判一次。
        """
        default = list(v.get("reopen_default") or [])
        if not default:
            return []
        values = self._values(instance_id)
        try:
            who = self._assignee(instance_id, nid, v.get("assignee_role"))
        except Exception:
            return default          # 解析不出人就别自作主张改默认值，引擎侧仍会再判一次
        verdict = reopen_verdict(values.get("dag") or self.dag,
                                 actor_roles=self._actor_roles(who),
                                 owner_roles=self._owner_roles(values),
                                 from_node=nid, targets=default)
        denied = set(verdict["denied"])
        return [t for t in default if t not in denied]

    def _route(self, event: dict) -> dict | None:
        key = event.get("key")
        if key == CARD_ACTION:
            av = event.get("action_value") or {}
            if av.get("kind") == ESC_KIND:
                # 审批卡（ADR-023 ③）：拍板不是答复中断，所以**没有 interrupt_id**，
                # 不能沿用下面那把「thread_id + interrupt_id」的钥匙。
                if "thread_id" not in av or "node_id" not in av:
                    return None
                return {"escalation": {
                    "instance_id": av["thread_id"], "gate_id": av["node_id"],
                    "seq": av.get("seq"), "decision": av.get("decision"),
                    # 身份**只**取事件顶层：封套里的 by / actor / operator_id 一律无效
                    "actor": event.get("operator_id"), "token": event.get("token")}}
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
                # 身份走**事件顶层**的 operator_id，与 action_value 分开传：卡片封套是前端可
                # 自由构造的，往里塞 by / actor / operator_id 一律无效（ADR-023 / 红线⑤）。
                "actor": event.get("operator_id"),
                "value": value,
                # 延迟更新 token：点完之后要把这张卡改成「已处理」的样子（投影侧）
                "token": event.get("token"),
            }
        if key == TASK_UPDATE:
            ev = event.get("event", {})
            if "task_completed_update" not in (ev.get("event_types") or []):
                return None
            guid = ev.get("task_guid") or ""
            corr = self.corr.get(guid) if guid else None
            # 关联表按 external_id 索引、**不分种类**，所以这里必须自己核对 kind：任务通道
            # 是唯一不判身份的入口（事件不带 operator，靠「这条 task_guid 是引擎发给指定人的
            # 待办」兜底）。不核对的话，拿一张卡的 message_id 冒充 task_guid 递进来，就把整条
            # 卡片通道的身份判定绕过去了（实测：陌生人据此把别人的定稿签了）。
            if not corr or corr.kind != "task":
                return None
            # 「完成任务」是产出定稿信号，**不是审批裁决**：绝不把它翻译成 gate 的放行
            # （模板护栏已禁止 gate 配 task_complete，这里对既有实例 / 手改 dag 再兜一道）
            if self._is_gate_node(corr.thread_id, corr.node_id):
                return None
            return {
                "instance_id": corr.thread_id,
                "interrupt_id": corr.interrupt_id,
                "node_id": corr.node_id,
                "value": {"passed": True, "completed": True},
            }
        return None

    def _is_gate_node(self, instance_id: str, node_id: str) -> bool:
        dag = self._values(instance_id).get("dag") or self.dag
        try:
            return is_gate(node_by_id(dag, node_id))
        except KeyError:
            return False

    def blocked(self, instance_id: str) -> list[str]:
        """反复打回仍不通过、已停下等人介入的门（终态，见 gates.BLOCKED）。"""
        values = self._values(instance_id)
        return blocked_nodes(values.get("dag") or [], values.get("status", {}))

    def _project(self, instance_id: str) -> None:
        """投影钩子（进度卡 / 多维表格看板见 ROADMAP）。

        目前只做一件不能省的事：**卡死了得有人知道**。超预算的门是终态，不通知的话
        项目就静静躺在那儿，谁都以为还在流转。
        """
        values = self._values(instance_id)
        reporter = (values.get("meta") or {}).get("reporter")
        stuck = blocked_nodes(values.get("dag") or [], values.get("status", {}))
        if not reporter or not stuck:
            return
        labels = {n["id"]: n.get("label", n["id"]) for n in values.get("dag") or []}
        grants = values.get("unblocks") or {}
        attempts = values.get("attempts") or {}
        for nid in stuck:
            # 幂等键含**已解除次数 + 轮次**：再次卡死是一件新事，同一个键会被幂等吞掉，
            # 于是第二次停摆没有任何人知道（项目静静躺死）。
            # 只含解除次数不够：`blocked` 并不是真终态，别的门打回共同祖先就能把它重置回
            # pending 再跑一次，那条路一次解除都没花，于是重新卡死时键没变、彻底静默（实测）。
            # 轮次（attempts）才是「这是新的一次尝试」的判别式。
            used = grants_used(grants, nid)
            left = MAX_UNBLOCK_GRANTS - used
            how = (f"可改要素后由发起人解除（unblock）再试，剩余解除额度 {left} 次。"
                   if left > 0 else "解除额度已用尽，请改图 / 换要素重开一个实例。")
            self._notify_owner(
                instance_id,
                f"「{labels.get(nid, nid)}」反复打回仍未通过，已停下等人介入"
                f"（实例 {instance_id}）。{how}",
                f"{instance_id}:{nid}:blocked:{used}:{attempts.get(nid, 0)}")
