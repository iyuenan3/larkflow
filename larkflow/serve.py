"""常驻服务层：把「装配好的 service」变成一个真的跑得起来的进程。

在这之前，引擎全绿但跑不起来：`EventPump` 写好了却没有任何一行生产代码把它接到
`service.resume_from_event`；`reconcile()` 实现了却从不在启动时跑（崩溃自愈只做了一半）；
`build_real_service()` 造出来的对象没人 start、没人 serve、没有信号处理。

一个 larkflow 进程的一生：

    startup_reconcile()   按 checkpointer 里的实例逐个对账（重建丢掉的投影 + 把被
                          super-step 屏障挡住的分支推到位）。**真相源是 checkpointer**，
                          绝不新建一张实例表当真相源（那就有两个真相源了）。
    start()               每个 EventKey 起一条 `lark-cli event consume` 子进程 + 泵线程。
    serve_forever()       装 SIGINT / SIGTERM，block 到收到信号。
    stop()                停泵 → 等在飞的那条事件处理完 → 关 SQLite 连接。

三条硬要求（每条都对应一个「进程还活着但产品已经哑了」的失效模式）：
  ① 启动对账**逐实例容错**：一个坏实例不许让整个服务起不来。
  ② 事件处理**逐条隔离**：一条事件炸了不许弄死泵线程（`EventPump` 已有一层，这里
     把 on_error 接上，让故障有人喊）。
  ③ 退出**不硬 kill 在飞的事件**：先停订阅、再等线程、最后才关连接。
"""
from __future__ import annotations

import json
import signal as _signal
import sys
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from .config import env
from .io.events import CARD_ACTION, TASK_UPDATE, EventPump

DEFAULT_EVENT_KEYS = (CARD_ACTION, TASK_UPDATE)
_CST = timezone(timedelta(hours=8))       # 日志时间一律 UTC+8，读的人不用猜时区


def _stamp() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def default_log(msg: str) -> None:
    print(f"[{_stamp()}] {msg}", file=sys.stderr, flush=True)


def normalize_event(key: str, payload: dict) -> dict:
    """`lark-cli event consume` 的一行 NDJSON → `service.resume_from_event` 认识的形状。

    两处**核对过 lark-cli 内嵌 skill 字段表**、不靠猜的差异：
      · `card.action.trigger` 被 lark-cli 拍平（字段在顶层，`operator_id` 就在那儿），
        但 `action_value` 是**开发者自定义值序列化成的 JSON 字符串**，不是对象。不解开
        的话 `_route` 里的 `av.get(...)` 每次都 AttributeError，整条入站通道对卡片按钮
        永久失聪（而进程还活着、systemd 看不出问题）。
      · `task.task.update_user_access_v2` 是 V2 信封、根在 `.event`，lark-cli 原样透传，
        与 `_route` 读 `event["event"]` 正好对上，不动它。

    路由键一律用**我们订阅的那个 EventKey**，绝不让 payload 里的同名字段改写它（payload
    是外部输入）。同理，整行不是 JSON 对象时当空事件（`_route` 会判成 unrouted 跳过），
    而不是抛给泵去记一笔故障：那是脏输入，不是我们的故障。
    """
    event = {**(payload if isinstance(payload, dict) else {}), "key": key}
    av = event.get("action_value")
    if isinstance(av, str):
        try:
            parsed = json.loads(av)
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        event["action_value"] = parsed if isinstance(parsed, dict) else {}
    return event


def list_instances(graph) -> tuple[list[str], str | None]:
    """枚举 checkpointer 里的实例 → (ids, 降级原因)。

    真相源就是 checkpointer：`list(None)` 拿全部 checkpoint，按 `thread_id` 去重
    （一个实例有很多 checkpoint）。**先物化成 list 再返回**：调用方会在循环里 reconcile，
    而 reconcile 要用同一个连接 / 同一把 saver 锁，边迭代边写是自找死锁。

    换 checkpointer（内存 / Postgres / 自研）时可能没有 `list`：优雅降级返回空 + 原因，
    绝不让服务起不来。
    """
    saver = getattr(graph, "checkpointer", None)
    lister = getattr(saver, "list", None)
    if not callable(lister):
        return [], f"checkpointer {type(saver).__name__} 没有 list()，无法枚举实例"
    try:
        tuples = list(lister(None))
    except Exception as exc:
        return [], f"枚举实例失败: {type(exc).__name__}: {exc}"
    out, seen = [], set()
    for t in tuples:
        cfg = getattr(t, "config", None) or {}
        tid = (cfg.get("configurable") or {}).get("thread_id")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return sorted(out), None


def instance_ids(graph) -> list[str]:
    return list_instances(graph)[0]


class LarkFlowServer:
    """常驻进程。所有外部依赖（service / 泵 / 信号 / 日志）都可注入，好在不碰真飞书的
    前提下把这一层测穿。"""

    def __init__(self, service, *, event_keys=DEFAULT_EVENT_KEYS, pump_factory=EventPump,
                 on_error=None, identity: str = "bot", profile: str | None = None,
                 signals=_signal, log=None, max_errors: int = 200,
                 sweep_seconds: float | None = None, event_observers=()):
        self.service = service
        self.event_keys = list(event_keys)
        self.pump_factory = pump_factory
        self.on_error = on_error
        self.identity = identity
        self.profile = profile
        self.event_observers = tuple(event_observers)
        self.signals = signals
        self.log = log or default_log
        self.pump = None
        self.report: dict = {}
        self.errors: deque = deque(maxlen=max_errors)   # 有界：故障风暴不许把内存吃掉
        self.stats = {
            "events": 0,
            "handled": 0,
            "skipped": 0,
            "forwarded": 0,
            "errors": 0,
        }
        self._stopped = threading.Event()
        self._stopping = False
        self._clean_exit = True
        self._lock = threading.Lock()
        # 定期对账。**任务事件那条推送在真栈上根本不到**（实测：bot 身份 + pre-consume 正常
        # + websocket 已连 + app 建的任务 + app 完成 + app 加成 follower，一条都收不到），
        # 于是 ADR-038 的轮询是 `task_complete` 节点唯一可靠的通道，只在启动时跑一次
        # 远远不够：人交了卷，引擎要等到下次重启才知道。配 0 = 关掉。
        self.sweep_seconds = (float(env("LARKFLOW_SWEEP_SECONDS", "120"))
                              if sweep_seconds is None else float(sweep_seconds))
        self._sweeper: threading.Thread | None = None

    # ---------- 启动对账 ----------
    def instances(self) -> list[str]:
        return instance_ids(self.service.graph)

    def startup_reconcile(self) -> dict:
        """启动时全实例对账：崩溃自愈的另一半（ADR-028 的推进拍 + 投影重建都在 reconcile 里）。

        **逐实例容错**：一个实例的 state 坏了 / 派单一直失败，不许让整个服务起不来
        （那会变成「一个坏实例锁死全公司」）。失败记下来、继续下一个、最后汇总返回。
        """
        ids, degraded = list_instances(self.service.graph)
        report = {"instances": len(ids), "reconciled": [], "finished": [],
                  "failed": [], "errors": {}, "degraded": degraded is not None,
                  "aborted": False, "pending": []}
        if degraded:
            report["degraded_reason"] = degraded
            self.log(f"实例枚举降级：{degraded}")
        for n, iid in enumerate(ids):
            if self._stopped.is_set():
                # 每个实例都要发卡 / 建待办 / 推进拍，几百个实例要跑很久。收到 SIGTERM 之后
                # 还把剩下的跑完 = 「停不下来」，而且停之前还会白起一次泵。没轮到的报出来，
                # 别让人从 reconciled 的条数以为全对过账了。
                report["aborted"] = True
                report["pending"] = list(ids[n:])
                self.log(f"收到停机信号，启动对账中止：还剩 {len(report['pending'])} 个实例没对")
                break
            try:
                if self._finished(iid):
                    report["finished"].append(iid)
                    continue
                result = self.service.reconcile(iid)
                report["reconciled"].append(iid)
                if result.get("errors"):
                    report["errors"][iid] = result["errors"]
            except Exception as exc:
                report["failed"].append(
                    {"instance_id": iid, "error": f"{type(exc).__name__}: {exc}"})
                self._error(f"reconcile:{iid}", exc)
        return report

    def _finished(self, instance_id: str) -> bool:
        """跑完的实例没有投影要重建、没有活要推。重推它只会重发通知、白烧一轮推进。"""
        try:
            return self.service.finished(instance_id)
        except Exception:
            return False        # 读不出来就当没跑完，交给 reconcile 去炸（有人接住）

    def start_sweeper(self) -> None:
        """起定期对账线程。与启动对账同款纪律：**一轮出错不许让后续所有轮停摆**。"""
        if self.sweep_seconds <= 0 or self._sweeper is not None:
            return
        self._sweeper = threading.Thread(target=self._sweep_loop, daemon=True,
                                         name="larkflow-sweeper")
        self._sweeper.start()
        self.log(f"定期对账已起：每 {self.sweep_seconds:g}s 一轮")

    def _sweep_loop(self) -> None:
        while not self._stopped.wait(self.sweep_seconds):
            try:
                ids, _ = list_instances(self.service.graph)
            except Exception as exc:
                self._error("sweep:list", exc)
                continue
            for iid in ids:
                if self._stopped.is_set():
                    return
                try:
                    if self._finished(iid):
                        continue
                    result = self.service.reconcile(iid)
                    for e in result.get("errors") or ():
                        # 「捞回来了」是故障信号不是好消息：它意味着推送漏了
                        self.log(f"对账 {iid}：{e}")
                except Exception as exc:
                    self._error(f"sweep:{iid}", exc)

    # ---------- 事件 ----------
    def start(self) -> None:
        """起泵：每个 EventKey 一条 `lark-cli event consume` 子进程 + 一条泵线程。"""
        if self.pump is not None:
            raise RuntimeError("pump 已经起过了")
        self.pump = self.pump_factory(self._on_event, identity=self.identity,
                                      profile=self.profile, on_error=self._error)
        self.pump.start(self.event_keys)
        self.log(f"入站通道已就绪：{self.event_keys}")

    def _bump(self, key: str) -> None:
        """计数器是被**多条泵线程**（每 EventKey 一条）同时加的，`d[k] += 1` 不是原子操作。"""
        with self._lock:
            self.stats[key] += 1

    def _on_event(self, key: str, payload: dict) -> None:
        """一条飞书事件 → 引擎。

        **不在这里 try/except**：兜底只留一处（`EventPump._pump` 那一层），异常抛给它，
        它会 on_error 出来并继续下一条。两处都兜会让故障计数与日志各说各话。
        """
        self._bump("events")
        event = normalize_event(key, payload)
        for observer in self.event_observers:
            try:
                forwarded = observer(key, payload)
            except Exception as exc:
                self._error(f"event_observer:{type(observer).__name__}", exc)
                continue
            if forwarded:
                self._bump("forwarded")
        result = self.service.resume_from_event(event) or {}
        if result.get("resumed"):
            self._bump("handled")
        else:
            self._bump("skipped")
            self.log(f"事件未推进实例：{ {k: v for k, v in result.items() if k != 'value'} }")

    def _error(self, where: str, exc: Exception) -> None:
        self._bump("errors")
        record = {"at": _stamp(), "where": where, "error": f"{type(exc).__name__}: {exc}"}
        self.errors.append(record)
        self.log(f"故障 {where}: {record['error']}")
        if self.on_error is not None:
            try:
                self.on_error(where, exc)
            except Exception:      # 报错的路上再报错，不许把泵线程带走
                pass

    # ---------- 退出 ----------
    def stop(self, timeout: float = 10.0, *, close_db: bool = True) -> bool:
        """优雅停：先停订阅，再**等在飞的那条事件处理完**，最后才关连接。

        绝不硬 kill 正在处理的事件：那一刻它可能正握着实例锁写 checkpointer。

        **返回是否干净收工。** 没排空就**不关连接**：在飞的那条事件正拿着这个连接写
        checkpointer，关掉等于把桌子从它手底下抽走（写一半的实例只能等下次启动对账去救）。
        宁可让进程退出时由 OS 收连接，也不主动制造一次半截写。同时记一笔故障 + 让退出码
        非 0，否则运维看到的是「errors=0、exit 0」，查不出所以然。
        """
        with self._lock:
            if self._stopping:
                return self._clean_exit
            self._stopping = True
        self._stopped.set()
        drained = True
        pump = self.pump
        if pump is not None:
            try:
                pump.stop()
            except Exception as exc:
                self._error("pump.stop", exc)
                drained = False
            join = getattr(pump, "join", None)
            if callable(join):
                # join 返回 None 的实现（老泵 / 替身）按「排空了」算，别凭空判脏
                drained = join(timeout) is not False and drained
        if not drained:
            self._error("drain", TimeoutError(
                f"{timeout}s 内没排空在飞的事件，连接不关（半截写留给下次启动对账兜）"))
        elif close_db:
            self._close_db()
        self._clean_exit = drained
        self.log(f"已停止（{'干净' if drained else '未排空'}）。事件 {self.stats['events']} 条"
                 f"（推进 {self.stats['handled']} / 跳过 {self.stats['skipped']}"
                 f" / 转存 {self.stats['forwarded']} / 故障 {self.stats['errors']}）")
        return drained

    def _close_db(self) -> None:
        seen = set()
        for owner in (getattr(self.service.graph, "checkpointer", None),
                      getattr(self.service, "corr", None)):
            conn = getattr(owner, "conn", None)
            if conn is None or id(conn) in seen:
                continue
            seen.add(id(conn))
            try:
                conn.close()
            except Exception as exc:
                self._error("close_db", exc)

    def serve_forever(self, *, drain_timeout: float = 10.0) -> int:
        """装 SIGINT / SIGTERM，对账，起泵，block 到收到信号。返回进程退出码。"""
        self._install_signals()
        try:
            self.report = self.startup_reconcile()
            self._log_report(self.report)
            # 对账期间就被喊停了（几百个实例要对很久）：别再起泵，那只会起一下又立刻停。
            if not self._stopped.is_set():
                self.start()
                self.start_sweeper()
        except Exception as exc:
            self._error("startup", exc)
            self.stop(drain_timeout)
            return 1
        try:
            self._stopped.wait()
        except KeyboardInterrupt:       # 信号没装上时的兜底路径
            pass
        finally:
            clean = self.stop(drain_timeout)
        return 0 if clean else 1

    def _install_signals(self) -> None:
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(self.signals, name, None)
            if sig is None:
                continue
            try:
                self.signals.signal(sig, self._on_signal)
            except (ValueError, OSError, RuntimeError) as exc:
                # 非主线程装不了 handler（supervisor 起子线程时会遇到）。不是致命错：
                # 外部照样可以调 stop() 收工，只是没法靠信号停。
                self.log(f"装不上 {name} handler（{type(exc).__name__}: {exc}），"
                         "改由调用方显式 stop()")

    def _on_signal(self, signum, frame) -> None:
        # 信号上下文里只做一件事：置位。真正的收尾在主线程的 finally 里做。
        self._stopped.set()

    def _log_report(self, report: dict) -> None:
        self.log(f"启动对账：实例 {report['instances']}｜已对账 {len(report['reconciled'])}"
                 f"｜已完成 {len(report['finished'])}｜失败 {len(report['failed'])}"
                 + (f"｜**中止，未对账 {len(report.get('pending') or [])}**"
                    if report.get("aborted") else ""))
        for f in report["failed"]:
            self.log(f"  对账失败 {f['instance_id']}: {f['error']}")
        for iid, errs in report.get("errors", {}).items():
            self.log(f"  {iid} 派单仍有失败：{errs}")
