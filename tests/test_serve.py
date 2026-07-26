"""可运行的服务层：常驻进程（对账 + 事件泵 + 优雅退出）、CLI、跨进程 SQLite。

这一层之前是空的：引擎全绿但没有任何一行生产代码把 EventPump 接到 service，
`build_real_service()` 造出来的对象没人 start、没人 serve、崩溃自愈只做了一半。

**全程 Mock / Stub / 临时目录，绝不构造 build_real_service**（它会真发飞书消息、真建文档）。
"""
from __future__ import annotations

import io as _io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from larkflow.app import build_service
from larkflow.config import RoleError
from larkflow.engine.executors import ExecutorError
from larkflow.engine.livegraph import GraphEditError
from larkflow.io import FakeDeliverableStore
from larkflow.model.template import TemplateError
from larkflow.io.events import CARD_ACTION, TASK_UPDATE, EventPump
from larkflow import store as store_module
from larkflow.serve import LarkFlowServer, instance_ids, normalize_event
from larkflow.store import (
    DEFAULT_LOCK_TIMEOUT,
    FileLock,
    InstanceLocks,
    LockBusy,
    daemon_lock_for,
    lock_dir_for,
    open_db,
)
from support import CountingLLM, card_target

INPUTS = {"甲方": "A", "乙方": "B", "价款": "30万", "期限": "12个月"}


def contract():
    llm = CountingLLM({"writer": "商务条款", "legal": "法律条款", "editor": "合并稿"})
    svc, io = build_service("contract", llm=llm, deliverables=FakeDeliverableStore())
    return svc, io, llm


class FakePump:
    """假事件泵：测试直接 feed 脚本化事件，不 spawn 任何子进程、不碰飞书。"""

    def __init__(self, on_event, *, identity="bot", profile=None, on_error=None, **kw):
        self.on_event, self.on_error = on_event, on_error
        self.identity, self.profile = identity, profile
        self.keys: list[str] | None = None
        self.stopped = False
        self.joined: float | None = None

    def start(self, event_keys):
        self.keys = list(event_keys)

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = timeout

    def feed(self, key, payload):
        self.on_event(key, payload)


def served(svc, **kw):
    """返回 (server, pumps)：pumps[0] 在 server.start() 之后才有。"""
    made: list[FakePump] = []

    def factory(on_event, **kwargs):
        p = FakePump(on_event, **kwargs)
        made.append(p)
        return p

    return LarkFlowServer(svc, pump_factory=factory, **kw), made


def cli_card(io, node_id, label, *, operator=None):
    """真 lark-cli 吐出来的那种 card.action.trigger 行：扁平 + action_value 是 **JSON 字符串**。

    （lark-event / lark-im 内嵌 skill 的字段表：`action_value` = "serialized to JSON string"）
    """
    av = io.button_value(node_id, label)
    return {"type": CARD_ACTION, "event_id": "ev-1", "timestamp": "1700000000000",
            "operator_id": operator or card_target(io, node_id), "message_id": "om_x",
            "action_tag": "button", "action_value": json.dumps(av, ensure_ascii=False)}


# ---------- 启动对账：崩溃自愈的另一半 ----------

def test_startup_reconcile_walks_every_instance_in_the_checkpointer():
    """真相源是 checkpointer，不新建一张实例表当真相源（那会有两个真相源）。"""
    svc, io, llm = contract()
    for iid in ("s-1", "s-2", "s-3"):
        svc.start(instance_id=iid, reporter="ou_owner", inputs=INPUTS)
    srv, _ = served(svc)

    report = srv.startup_reconcile()

    assert report["instances"] == 3
    assert sorted(report["reconciled"]) == ["s-1", "s-2", "s-3"]
    assert report["failed"] == []


def test_startup_reconcile_actually_reprovisions_a_projection_that_was_lost():
    """崩在「建卡」与「写关联表」之间 = 有人该收到卡却没收到。启动就该把它补上。"""
    svc, io, llm = contract()
    sent: list[str] = []
    real_send = io.send_card

    def flaky(*, target, summary, buttons, idem_key):
        if target == "ou_财务" and not sent:
            sent.append(target)
            raise RuntimeError("invalid open_id")
        return real_send(target=target, summary=summary, buttons=buttons, idem_key=idem_key)

    io.send_card = flaky
    svc.start(instance_id="s-heal", reporter="ou_owner", inputs=INPUTS)
    assert "ou_财务" not in {c["target"] for c in io.cards.values()}

    srv, _ = served(svc)
    srv.startup_reconcile()

    assert "ou_财务" in {c["target"] for c in io.cards.values()}


def test_one_broken_instance_does_not_abort_the_whole_startup():
    """启动阶段炸掉 = 一个坏实例让整个服务起不来。记下来，继续下一个。"""
    svc, io, llm = contract()
    for iid in ("b-1", "b-2", "b-3"):
        svc.start(instance_id=iid, reporter="ou_owner", inputs=INPUTS)
    real = svc.reconcile

    def boom(instance_id):
        if instance_id == "b-2":
            raise RuntimeError("这个实例的 state 坏了")
        return real(instance_id)

    svc.reconcile = boom
    srv, _ = served(svc)

    report = srv.startup_reconcile()

    assert sorted(report["reconciled"]) == ["b-1", "b-3"]
    assert [f["instance_id"] for f in report["failed"]] == ["b-2"]
    assert "这个实例的 state 坏了" in report["failed"][0]["error"]


def test_a_finished_instance_is_not_reconciled_again():
    """跑完的实例没有投影要重建、没有活要推；重推只会重发通知、白烧一轮。"""
    dag = [{"id": "d", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
            "prompt": "p", "model_role": "w", "deliverable": {"region": "whole"}}]
    svc, io = build_service(dag, llm=CountingLLM({"w": "正文"}))
    svc.start(instance_id="fin-1", reporter="ou_o", inputs={})
    assert svc.status("fin-1") == {"d": "done"}
    srv, _ = served(svc)

    report = srv.startup_reconcile()

    assert report["finished"] == ["fin-1"] and report["reconciled"] == []


def test_provision_errors_left_over_from_reconcile_are_reported_not_swallowed():
    svc, io, llm = contract()

    def always_bad(*, target, **kw):
        raise RuntimeError("飞书挂了")

    # 从一开始就发不出去：**已经送到人手里**的卡不该被对账再发一次（那是另一条测试），
    # 所以这里要的是一件「至今没成功过」的派单，它才是对账该重试、失败该上报的那一件。
    io.send_card = always_bad
    svc.start(instance_id="e-1", reporter="ou_owner", inputs=INPUTS)
    srv, _ = served(svc)
    report = srv.startup_reconcile()

    assert report["errors"]["e-1"], "派单失败必须冒到启动报告里，不能只留在 service 内部"


def restart(db_path, template="contract", **kw):
    """开一台「不记得上一次调用」的飞书 + 一个新进程，接着同一个 SQLite 文件跑。

    第二个 MockLarkIO 就是真栈过了那 1 小时幂等窗口之后的样子（`lark_io` 里写着：
    同 key 1 小时内只发一条）。崩溃恢复的正确性不能押在这个窗口上。
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return build_service(template, conn=conn,
                         llm=CountingLLM({"writer": "商务条款", "legal": "法律条款",
                                          "editor": "合并稿", "w": "正文"}),
                         deliverables=FakeDeliverableStore(), **kw)


def test_a_restart_does_not_dispatch_the_same_work_to_the_same_people_again(tmp_path):
    """隔夜重启：还在等的人手里已经有卡 / 有待办，启动对账绝不许再发一份。

    飞书那个 --idempotency-key 只有 1 小时窗口，过期后同一个 key 会真的再建一个对象，
    而人工节点等的是人、超过 1 小时是常态。重复的待办还没有任何代码去关掉它。
    """
    db = tmp_path / "restart.sqlite"
    svc, io = restart(db)
    svc.start(instance_id="rs-1", reporter="ou_owner", inputs=INPUTS)
    assert len(io.cards) == 2
    old_card = io.button_value("finance_gate", "通过")
    who = card_target(io, "finance_gate")

    svc2, io2 = restart(db)
    srv, _ = served(svc2)
    report = srv.startup_reconcile()

    assert report["reconciled"] == ["rs-1"] and report["failed"] == []
    assert io2.cards == {} and io2.tasks == {}, "重启给还在等的人又发了一遍"
    # 而人手里那张旧卡照样点得动（不重派 ≠ 把路由搞丢）
    assert "resumed" in svc2.resume_from_event(
        {"key": CARD_ACTION, "operator_id": who, "action_value": old_card})


def test_a_restart_does_not_re_notify_the_owner_about_a_gate_that_is_still_blocked(tmp_path):
    """`blocked` 通知同理：幂等键说的是「这件事只做一次」，不是「一小时内只做一次」。"""
    dag = [
        {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "chk", "label": "机检", "executor": "tool", "role": "gate", "deps": ["draft"],
         "approval_policy": "auto", "reopen_budget": 1,
         "tool": {"kind": "format_check", "args": {"required": ["永不出现"]}}},
    ]
    db = tmp_path / "blocked.sqlite"
    svc, io = restart(db, dag)
    svc.start(instance_id="bl-1", reporter="ou_owner", inputs={})
    assert len([n for n in io.notifications if "等人介入" in n["text"]]) == 1

    svc2, io2 = restart(db, dag)
    srv, _ = served(svc2)
    srv.startup_reconcile()

    assert io2.notifications == [], "重启又把同一条「卡死了」的通知发了一遍"


def test_a_checkpointer_without_list_degrades_instead_of_crashing():
    """换 checkpointer（内存 / Postgres / 自研）不该让服务起不来。"""
    svc, io, llm = contract()
    svc.start(instance_id="n-1", reporter="ou_owner", inputs=INPUTS)

    class Blind:
        pass

    svc.graph.checkpointer = Blind()
    srv, _ = served(svc)

    assert instance_ids(svc.graph) == []
    report = srv.startup_reconcile()
    assert report["instances"] == 0 and report["degraded"] is True


def test_instance_ids_dedupes_the_many_checkpoints_of_one_thread():
    svc, io, llm = contract()
    svc.start(instance_id="d-1", reporter="ou_owner", inputs=INPUTS)
    assert len(list(svc.graph.checkpointer.list(None))) > 1     # 一个实例有很多 checkpoint
    assert instance_ids(svc.graph) == ["d-1"]


# ---------- 事件：泵 → service → 实例真的动了 ----------

def test_an_event_from_the_pump_reaches_the_engine_and_moves_the_instance():
    svc, io, llm = contract()
    svc.start(instance_id="p-1", reporter="ou_owner", inputs=INPUTS)
    srv, pumps = served(svc)
    srv.start()

    pumps[0].feed(CARD_ACTION, cli_card(io, "legal_gate", "通过"))

    assert svc.status("p-1")["legal_gate"] == "done"
    assert srv.stats["handled"] == 1


def test_the_pump_subscribes_exactly_the_two_event_keys_the_engine_understands():
    svc, io, llm = contract()
    srv, pumps = served(svc)
    srv.start()
    assert pumps[0].keys == [CARD_ACTION, TASK_UPDATE]


def test_a_real_lark_cli_card_payload_is_normalized_before_it_reaches_the_router():
    """lark-cli 把开发者自定义的 value **序列化成 JSON 字符串**再吐出来。

    不解开的话 `_route` 拿到的是 str，`av.get` 直接 AttributeError，
    每一次点击都在同一处炸：整条入站通道对卡片按钮永久失聪。
    """
    ev = normalize_event(CARD_ACTION, {"operator_id": "ou_x",
                                       "action_value": '{"thread_id":"t","interrupt_id":"i"}'})
    assert ev["key"] == CARD_ACTION
    assert ev["action_value"] == {"thread_id": "t", "interrupt_id": "i"}
    assert ev["operator_id"] == "ou_x"


def test_a_card_payload_that_is_not_json_does_not_take_the_channel_down():
    ev = normalize_event(CARD_ACTION, {"action_value": "not json at all"})
    assert ev["action_value"] == {}


def test_a_task_event_keeps_its_v2_envelope():
    """task.task.update_user_access_v2 是 V2 信封（根在 .event），lark-cli 不拍平它。"""
    raw = {"header": {"event_id": "x"},
           "event": {"task_guid": "g1", "event_types": ["task_completed_update"]}}
    assert normalize_event(TASK_UPDATE, raw)["event"] == raw["event"]


def test_the_event_key_we_subscribed_wins_over_anything_in_the_payload():
    """payload 是外部输入，绝不让它改写路由键。"""
    ev = normalize_event(CARD_ACTION, {"key": "im.message.receive_v1"})
    assert ev["key"] == CARD_ACTION


def test_a_task_completion_event_from_the_pump_finishes_a_human_produce_node():
    svc, io, llm = contract()
    svc.start(instance_id="t-1", reporter="ou_owner", inputs=INPUTS)
    srv, pumps = served(svc)
    srv.start()
    pumps[0].feed(CARD_ACTION, cli_card(io, "legal_gate", "通过"))
    pumps[0].feed(CARD_ACTION, cli_card(io, "finance_gate", "通过"))
    guid = next(t["guid"] for t in io.tasks.values() if t["summary"] == "负责人定稿")

    pumps[0].feed(TASK_UPDATE, {"header": {"event_id": "e"}, "event": {
        "task_guid": guid, "event_types": ["task_completed_update"]}})

    # 「完成任务」是产出定稿信号：它必须落进权威 state。（这份稿是空的，随后的机检门当场
    # 把定稿打回，于是 status 又回 pending 并发出第二轮任务，那正是引擎该有的反应。）
    assert svc.outputs("t-1")["finalize"]["completed"] is True
    assert srv.stats["handled"] == 3
    assert len([t for t in io.tasks.values() if t["summary"] == "负责人定稿"]) == 2


def test_one_failing_event_does_not_kill_the_only_inbound_channel():
    """EventPump 已有一层兜底；这条测的是**我这条路径**真的被它兜住了。"""
    svc, io, llm = contract()
    svc.start(instance_id="k-1", reporter="ou_owner", inputs=INPUTS)
    real = svc.resume_from_event
    seen: list[dict] = []
    # 等条件必须是「第二条**处理完了**」而不是「第二条**到了**」：泵在另一条线程上，
    # 拿「到达」当等条件会在处理途中就去断言 state（我第一版就踩了，50% 概率红）。
    handled = threading.Event()

    def flaky(event):
        seen.append(event)
        if len(seen) == 1:
            raise RuntimeError("resume 里炸了")
        try:
            return real(event)
        finally:
            handled.set()

    svc.resume_from_event = flaky
    srv = LarkFlowServer(svc, pump_factory=EventPump, log=lambda *a: None)
    lines = [json.dumps(cli_card(io, "legal_gate", "打回")) + "\n",
             json.dumps(cli_card(io, "legal_gate", "通过")) + "\n"]
    pump = EventPump(srv._on_event, on_error=srv._error, max_restarts=0)
    pump._spawn = lambda key: FakeProc(lines)
    srv.pump = pump
    pump.start([CARD_ACTION])
    assert handled.wait(10), "第一条炸了之后，泵必须继续处理第二条"
    pump.stop()

    assert len(seen) == 2
    assert svc.status("k-1")["legal_gate"] == "done"
    assert any("on_event" in e["where"] for e in srv.errors)   # 但故障被喊出来了
    assert srv.stats["errors"] >= 1


class FakeProc:
    def __init__(self, lines, stderr_lines=("[event] ready event_key=k\n",)):
        self.stdout = _io.StringIO("".join(lines))
        self.stderr = _io.StringIO("".join(stderr_lines))
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_an_unroutable_event_is_counted_not_raised():
    svc, io, llm = contract()
    srv, pumps = served(svc)
    srv.start()
    pumps[0].feed(CARD_ACTION, {"action_value": "{}"})
    assert srv.stats["skipped"] == 1 and srv.stats["errors"] == 0


# ---------- 优雅退出 ----------

def test_stop_stops_the_pump_waits_for_it_and_closes_the_database():
    svc, io, llm = contract()
    svc.start(instance_id="q-1", reporter="ou_owner", inputs=INPUTS)
    srv, pumps = served(svc)
    srv.start()

    srv.stop(timeout=3.0)

    assert pumps[0].stopped and pumps[0].joined == 3.0
    with pytest.raises(sqlite3.ProgrammingError):
        svc.graph.checkpointer.conn.execute("select 1")


def test_stop_is_idempotent_and_survives_a_pump_that_never_started():
    svc, io, llm = contract()
    srv, _ = served(svc)
    srv.stop(close_db=False)
    srv.stop(close_db=False)


class FakeSignals:
    SIGINT, SIGTERM = 2, 15

    def __init__(self):
        self.handlers: dict[int, object] = {}
        self.installed = threading.Event()

    def signal(self, sig, handler):
        self.handlers[sig] = handler
        if len(self.handlers) >= 2:
            self.installed.set()
        return None


def test_serve_forever_blocks_until_a_signal_then_shuts_down_cleanly():
    svc, io, llm = contract()
    svc.start(instance_id="sig-1", reporter="ou_owner", inputs=INPUTS)
    sig = FakeSignals()
    srv, pumps = served(svc, signals=sig, log=lambda *a: None)
    rc: list[int] = []
    t = threading.Thread(target=lambda: rc.append(srv.serve_forever()), daemon=True)
    t.start()

    assert sig.installed.wait(5), "SIGINT / SIGTERM 都要装 handler"
    for _ in range(500):                       # 起 pump 排在 block 之前
        if pumps and pumps[0].keys:
            break
        time.sleep(0.01)
    assert pumps and pumps[0].keys, "block 之前必须已经在收事件了"
    sig.handlers[sig.SIGTERM](sig.SIGTERM, None)
    t.join(5)

    assert rc == [0] and pumps[0].stopped
    assert srv.report["instances"] == 1        # 起 pump 之前先对了账


def test_serve_forever_does_not_die_when_signal_handlers_cannot_be_installed():
    """非主线程装 handler 会 ValueError（真栈用 supervisor 起子线程时会遇到）。"""
    class Refusing(FakeSignals):
        def signal(self, sig, handler):
            self.handlers[sig] = handler
            if len(self.handlers) >= 2:
                self.installed.set()
            raise ValueError("signal only works in main thread")

    svc, io, llm = contract()
    sig = Refusing()
    srv, pumps = served(svc, signals=sig, log=lambda *a: None)
    rc: list[int] = []
    t = threading.Thread(target=lambda: rc.append(srv.serve_forever()), daemon=True)
    t.start()
    assert sig.installed.wait(5)
    for _ in range(200):
        if pumps:
            break
        time.sleep(0.01)
    srv.stop(close_db=False)
    t.join(5)
    assert rc == [0]


def test_a_pump_that_cannot_start_fails_loudly_instead_of_pretending_to_serve():
    svc, io, llm = contract()

    def broken(on_event, **kw):
        raise RuntimeError("lark-cli 不在 PATH 上")

    srv = LarkFlowServer(svc, pump_factory=broken, signals=FakeSignals(), log=lambda *a: None)
    rc: list[int] = []
    # 跑在带上限的线程里：起不了泵却 block 住，是这条要防的**退化形态**本身。
    # 直接调 serve_forever 的话，回归发生时测试会挂死而不是变红（变异实测过）。
    t = threading.Thread(target=lambda: rc.append(srv.serve_forever()), daemon=True)
    t.start()
    t.join(10)

    assert rc == [1], "起不了入站通道就该当场退出，绝不能阻塞在那儿装作在服务"
    assert not t.is_alive()


# ---------- 跨进程写同一个 SQLite ----------

def test_wal_and_busy_timeout_are_on_for_a_file_backed_db(tmp_path):
    conn = open_db(str(tmp_path / "x.sqlite"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_an_in_memory_db_is_left_alone(tmp_path):
    conn = open_db(":memory:")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "memory"
    conn.close()


def bounded(fn, *, within=10.0):
    """在带上限的线程里跑一段**可能永远等下去**的代码，返回 (结果, 异常)。

    凡是会去抢锁的断言都走它：「等待上限」那条防线一旦被去掉，直接调用会让测试**挂死**
    而不是变红（变异实测：去掉 deadline 判断后整个文件跑不完，CI 只会显示超时）。
    """
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(within)
    assert not t.is_alive(), f"{within:g}s 还没返回：一直等下去了（等待上限失效）"
    return box.get("value"), box.get("exc")


def expect_busy(acquire, *, within=10.0):
    """拿不到锁就该**报 LockBusy**，不是永远等下去。"""
    _, exc = bounded(acquire, within=within)
    assert isinstance(exc, LockBusy), f"期望 LockBusy，实际 {exc!r}"


def test_two_processes_cannot_touch_the_same_instance_at_once(tmp_path):
    """service 的 _thread_lock 只是进程内锁；serve 常驻 + 一次性 CLI 是两个进程。"""
    a = InstanceLocks(lock_dir_for(str(tmp_path / "db.sqlite")), timeout=0.2)
    b = InstanceLocks(lock_dir_for(str(tmp_path / "db.sqlite")), timeout=0.2)   # 另一个「进程」

    with a("inst-1"):
        expect_busy(lambda: b("inst-1").__enter__())
        with b("inst-2"):        # 别的实例互不干扰
            pass
    # 放开之后拿得到。走 bounded：一次**失败的**加锁若没把已拿到的那层回滚干净，这里会
    # 永远等下去，测试就成了挂死而不是变红（变异实测）。
    lock, exc = bounded(lambda: b("inst-1").__enter__(), within=5)
    assert exc is None, f"对方放手了还拿不到：{exc!r}"
    lock.__exit__(None, None, None)


def test_an_instance_id_that_is_not_a_filename_still_gets_its_own_lock(tmp_path):
    locks = InstanceLocks(lock_dir_for(str(tmp_path / "db.sqlite")), timeout=0.2)
    with locks("../../etc/passwd"):
        pass
    other = InstanceLocks(lock_dir_for(str(tmp_path / "db.sqlite")), timeout=0.05)
    with locks("含中文 / 斜杠"):
        expect_busy(lambda: other("含中文 / 斜杠").__enter__())
    assert not (tmp_path / ".." / ".." / "etc").exists()


def test_a_lock_held_by_a_real_other_process_is_seen(tmp_path):
    """线程级 flock 只证明一半：真跨进程也得挡住。"""
    path = tmp_path / "held.lock"
    ready = tmp_path / "ready"
    code = (
        "import fcntl,sys,time,pathlib\n"
        f"f=open({str(path)!r},'a+')\n"
        "fcntl.flock(f, fcntl.LOCK_EX)\n"
        f"pathlib.Path({str(ready)!r}).write_text('1')\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        for _ in range(500):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "子进程没能拿到锁"
        expect_busy(lambda: FileLock(str(path), timeout=0.2).acquire())
    finally:
        proc.kill()
        proc.wait(timeout=10)
    FileLock(str(path), timeout=1.0).acquire().release()   # 对方走了就拿得到


def test_a_second_connection_waits_for_the_writer_instead_of_erroring_out(tmp_path):
    """两个进程写同一个文件时，后到的那个必须**等**，不能当场 `database is locked`。

    （这条钉的是行为不是实现：busy_timeout 由 PRAGMA 与 `connect(timeout=)` 双保险给出。）
    """
    path = str(tmp_path / "busy.sqlite")
    a, b = open_db(path), open_db(path)
    try:
        a.execute("CREATE TABLE t(x)")
        a.commit()
        a.execute("BEGIN IMMEDIATE")            # a 拿住写锁
        a.execute("INSERT INTO t VALUES (1)")
        threading.Timer(0.3, a.commit).start()  # 0.3s 后才放手

        _, exc = bounded(lambda: (b.execute("INSERT INTO t VALUES (2)"), b.commit()), within=10)

        assert exc is None, f"后到的写没等就报错了：{exc!r}"
        assert b.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    finally:
        a.close()
        b.close()


def test_a_released_lock_can_be_taken_again_by_the_same_holder(tmp_path):
    """service 是**反复进出同一个锁对象**的（每实例缓存一把）。漏放一层就再也进不去。"""
    locks = InstanceLocks(lock_dir_for(str(tmp_path / "db.sqlite")), timeout=0.5)
    lock = locks("inst-1")
    for i in range(3):
        _, exc = bounded(lambda: lock.__enter__(), within=5)
        assert exc is None, f"第 {i + 1} 次进不去：上一次没放干净（{exc!r}）"
        lock.__exit__(None, None, None)


def test_a_second_daemon_refuses_to_start_on_the_same_database(tmp_path):
    db = str(tmp_path / "db.sqlite")
    first = daemon_lock_for(db)
    first.acquire(timeout=0)
    try:
        expect_busy(lambda: daemon_lock_for(db).acquire(timeout=0))
    finally:
        first.release()
    daemon_lock_for(db).acquire(timeout=0).release()


def test_a_db_that_refuses_wal_is_rejected_instead_of_quietly_running_unsafe(monkeypatch, tmp_path):
    """DB 放在网络盘上时 WAL 开不起来，而那里的 flock 同样不可靠：两条防线一起没了。

    宁可当场不跑，也不静默降级成「两个进程一起写、祈祷没事」。
    """
    class Cursor:
        def fetchone(self):
            return ("delete",)

    class Refuses:
        closed = False

        def execute(self, sql):
            return Cursor()

        def close(self):
            self.closed = True

    fake = Refuses()
    monkeypatch.setattr(store_module.sqlite3, "connect", lambda *a, **kw: fake)
    with pytest.raises(RuntimeError, match="WAL"):
        open_db(str(tmp_path / "x.sqlite"))
    assert fake.closed, "开不了 WAL 就别把连接留在那儿"


def test_the_service_takes_the_cross_process_lock_for_every_state_change(tmp_path):
    """光有锁没用，得真接进 service 的每一处写。"""
    taken: list[str] = []

    class Recording:
        def __init__(self, iid):
            self.iid = iid
            self.inner = threading.Lock()

        def __enter__(self):
            self.inner.acquire()
            taken.append(self.iid)
            return self

        def __exit__(self, *exc):
            self.inner.release()

    svc, io = build_service("contract", llm=CountingLLM({}),
                            deliverables=FakeDeliverableStore(),
                            lock_factory=Recording)
    svc.start(instance_id="lk-1", reporter="ou_owner", inputs=INPUTS)
    svc.resume_from_event({"key": CARD_ACTION, "operator_id": card_target(io, "legal_gate"),
                           "action_value": io.button_value("legal_gate", "通过")})
    svc.reconcile("lk-1")
    assert taken == ["lk-1", "lk-1", "lk-1"]


def test_the_default_service_is_still_a_plain_in_process_lock():
    svc, io, llm = contract()
    assert isinstance(svc._thread_lock("whatever"), type(threading.Lock()))


def test_lock_dirs_live_next_to_the_database_not_in_the_cwd(tmp_path):
    d = lock_dir_for(str(tmp_path / "sub" / "db.sqlite"))
    assert Path(d).parent == tmp_path / "sub"
    assert DEFAULT_LOCK_TIMEOUT > 0


# ---------- CLI ----------

def cli(argv, svc, **kw):
    from larkflow.__main__ import main
    return main(argv, factory=lambda ns: svc, **kw)


def test_cli_start_prints_the_instance_id_it_created(capsys, tmp_path):
    svc, io, llm = contract()
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "start", "--reporter", "ou_owner",
              "--input", "甲方=A", "--input", "乙方=B", "--id", "cli-1"], svc)
    out = capsys.readouterr().out
    assert rc == 0 and "cli-1" in out
    assert svc._values("cli-1")["meta"]["inputs"] == {"甲方": "A", "乙方": "B"}
    assert svc._values("cli-1")["meta"]["reporter"] == "ou_owner"


def test_cli_start_makes_up_an_id_when_you_do_not_give_one(capsys, tmp_path):
    svc, io, llm = contract()
    assert cli(["--db", str(tmp_path / "db.sqlite"), "start", "--reporter", "ou_o"], svc) == 0
    printed = capsys.readouterr().out.splitlines()[0].split()[-1]
    assert printed.startswith("lf-")
    assert svc.status(printed), "打印出来的 id 得真能查到实例"


def test_cli_rejects_an_input_pair_without_an_equals_sign(tmp_path):
    svc, io, llm = contract()
    with pytest.raises(SystemExit) as e:
        cli(["--db", str(tmp_path / "db.sqlite"), "start", "--reporter", "ou_o",
             "--input", "甲方"], svc)
    assert e.value.code == 2


def test_cli_status_shows_every_node_and_what_is_blocking(capsys, tmp_path):
    svc, io, llm = contract()
    svc.start(instance_id="st-1", reporter="ou_owner", inputs=INPUTS)
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "status", "st-1"], svc)
    out = capsys.readouterr().out
    assert rc == 0
    assert "biz_draft" in out and "finance_gate" in out


def test_cli_status_of_an_unknown_instance_exits_nonzero(capsys, tmp_path):
    svc, io, llm = contract()
    assert cli(["--db", str(tmp_path / "db.sqlite"), "status", "nope"], svc) == 1


def test_cli_pending_says_who_is_being_waited_on(capsys, tmp_path):
    svc, io, llm = contract()
    svc.start(instance_id="pd-1", reporter="ou_owner", inputs=INPUTS)
    assert cli(["--db", str(tmp_path / "db.sqlite"), "pending", "pd-1"], svc) == 0
    out = capsys.readouterr().out
    assert "finance_gate" in out and "legal_gate" in out


def test_cli_pending_can_be_read_through_one_persons_eyes(capsys, tmp_path):
    """不传 actor = 机制层全集（驾驶舱口径）；传了才按 ADR-023 过滤成他点得动的。"""
    svc, io, llm = contract()
    svc.start(instance_id="pa-1", reporter="ou_owner", inputs=INPUTS)
    assert cli(["--db", str(tmp_path / "db.sqlite"), "pending", "pa-1",
                "--actor", "ou_财务"], svc) == 0
    assert "finance_gate" in capsys.readouterr().out


def test_cli_unblock_is_exposed_and_reports_a_rejection_with_a_nonzero_exit(capsys, tmp_path):
    dag = [
        {"id": "draft", "label": "起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "p", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "chk", "label": "机检", "executor": "tool", "role": "gate", "deps": ["draft"],
         "approval_policy": "auto", "reopen_budget": 1,
         "tool": {"kind": "format_check", "args": {"required": ["永不出现"]}}},
    ]
    svc, io = build_service(dag, llm=CountingLLM({"w": "过不了"}))
    svc.start(instance_id="ub-1", reporter="ou_owner", inputs={})
    assert svc.blocked("ub-1") == ["chk"]
    db = str(tmp_path / "db.sqlite")

    assert cli(["--db", db, "unblock", "ub-1", "chk", "--by", "ou_owner",
                "--reason", "改了要素"], svc) == 0
    assert "chk" in capsys.readouterr().out
    # 不是 blocked 的节点解除不了 → 非零退出（脚本据此判断）
    assert cli(["--db", db, "unblock", "ub-1", "draft", "--by", "ou_owner",
                "--reason", "试试"], svc) == 1
    assert "not_blocked" in capsys.readouterr().out


def test_cli_unblock_demands_who_and_why():
    with pytest.raises(SystemExit) as e:
        from larkflow.__main__ import build_parser
        build_parser().parse_args(["unblock", "i", "n"])
    assert e.value.code == 2


def test_cli_reconcile_without_an_id_covers_every_instance(capsys, tmp_path):
    svc, io, llm = contract()
    for iid in ("rc-1", "rc-2"):
        svc.start(instance_id=iid, reporter="ou_owner", inputs=INPUTS)
    assert cli(["--db", str(tmp_path / "db.sqlite"), "reconcile"], svc) == 0
    out = capsys.readouterr().out
    assert "rc-1" in out and "rc-2" in out


def test_cli_reconcile_of_one_instance_does_not_touch_the_others(capsys, tmp_path):
    svc, io, llm = contract()
    for iid in ("ro-1", "ro-2"):
        svc.start(instance_id=iid, reporter="ou_owner", inputs=INPUTS)
    assert cli(["--db", str(tmp_path / "db.sqlite"), "reconcile", "ro-1"], svc) == 0
    out = capsys.readouterr().out
    assert "ro-1" in out and "ro-2" not in out


def test_cli_json_output_is_machine_readable(capsys, tmp_path):
    svc, io, llm = contract()
    svc.start(instance_id="js-1", reporter="ou_owner", inputs=INPUTS)
    assert cli(["--db", str(tmp_path / "db.sqlite"), "--json", "status", "js-1"], svc) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"]["biz_draft"] == "done"


def test_cli_serve_takes_the_daemon_lock_and_refuses_a_second_one(capsys, tmp_path):
    svc, io, llm = contract()
    db = str(tmp_path / "db.sqlite")
    calls: list[str] = []

    class FakeServer:
        def __init__(self, service, **kw):
            self.service = service

        def serve_forever(self):
            calls.append("served")
            return 0

    assert cli(["--db", db, "serve"], svc, server_factory=FakeServer) == 0
    assert calls == ["served"]

    held = daemon_lock_for(db)
    held.acquire(timeout=0)
    try:
        rc, exc = bounded(lambda: cli(["--db", db, "serve"], svc, server_factory=FakeServer))
        assert exc is None and rc == 1, f"第二个 daemon 该被当场拒掉，实际 {rc!r} / {exc!r}"
        assert "另一个" in capsys.readouterr().out
        assert calls == ["served"], "被拒的那次绝不能真去 serve"
    finally:
        held.release()


def test_cli_serve_really_runs_the_server_it_built_and_events_reach_the_engine(tmp_path):
    """把 CLI serve 这条路真跑一遍：单例锁 → 真 LarkFlowServer → 对账 → 收事件 → 干净退出。

    只用假 server 测的话，`_cmd_serve` 传给真 server 的那组 kwargs 从来没被验过
    （拼错一个关键字，daemon 起不来而测试全绿）。
    """
    svc, io, llm = contract()
    svc.start(instance_id="cs-1", reporter="ou_owner", inputs=INPUTS)
    made: list[LarkFlowServer] = []

    def factory(service, **kw):
        srv = LarkFlowServer(service, pump_factory=FakePump, signals=FakeSignals(),
                             log=lambda *a: None, **kw)      # 真类、真 kwargs
        made.append(srv)
        return srv

    rc: list[int] = []
    db = str(tmp_path / "db.sqlite")
    t = threading.Thread(daemon=True, target=lambda: rc.append(
        cli(["--db", db, "serve"], svc, server_factory=factory)))
    t.start()
    for _ in range(500):
        if made and made[0].pump is not None and made[0].pump.keys:
            break
        time.sleep(0.01)
    assert made and made[0].pump.keys == [CARD_ACTION, TASK_UPDATE]
    assert made[0].report["reconciled"] == ["cs-1"]

    made[0].pump.feed(CARD_ACTION, cli_card(io, "legal_gate", "通过"))
    assert svc.status("cs-1")["legal_gate"] == "done"

    made[0].stop(close_db=False)
    t.join(10)
    assert rc == [0] and not t.is_alive()
    daemon_lock_for(db).acquire(timeout=0).release()   # 退出时把单例锁放开了


def test_cli_without_a_subcommand_prints_help_and_exits_nonzero(capsys):
    from larkflow.__main__ import main
    assert main([], factory=lambda ns: None) == 2


def test_cli_never_reaches_for_the_real_stack_when_a_factory_is_injected(tmp_path, monkeypatch):
    """测试里绝不能真去建飞书应用；注入了 factory 就绝不该碰 build_real_service。"""
    import larkflow.app as app

    def forbidden(*a, **kw):
        raise AssertionError("测试绝不构造 build_real_service")

    monkeypatch.setattr(app, "build_real_service", forbidden)
    svc, io, llm = contract()
    svc.start(instance_id="nr-1", reporter="ou_owner", inputs=INPUTS)
    assert cli(["--db", str(tmp_path / "db.sqlite"), "status", "nr-1"], svc) == 0


def test_the_package_is_runnable_as_a_module():
    """`python -m larkflow --help` 得真跑得起来（打包 / systemd 都靠它）。"""
    root = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run([sys.executable, "-m", "larkflow", "--help"],
                          capture_output=True, text=True, timeout=120, cwd=root,
                          env={**os.environ, "PYTHONPATH": root})
    assert proc.returncode == 0
    for sub in ("serve", "start", "status", "pending", "edit", "escalations",
                "approve", "reject", "unblock", "reconcile"):
        assert sub in proc.stdout


# ---------- CLI：改图 / 审批（注入假 service） ----------

class CliService:
    """CLI 段专用的假 service：只记调用、按脚本回话，一行引擎逻辑都不跑。

    这几条命令测的是 CLI 自己的活（参数解析成什么、退出码给几、拒绝怎么回显），
    与引擎实现无关。绑真 service 会把 service 的签名变更算到 CLI 头上（红的不是
    CLI 的锅），而真栈那条路本来就绝不许在测试里构造（会真发飞书消息 / 真建文档）。
    """

    DAG = [{"id": "biz_draft", "label": "商务起草", "executor": "llm", "role": "produce",
            "deps": []},
           {"id": "legal_gate", "label": "法务审", "executor": "human", "role": "gate",
            "deps": ["biz_draft"]}]

    def __init__(self, dag=None, **scripted):
        self.calls: list[tuple] = []
        self.scripted = scripted
        self.dag = list(self.DAG if dag is None else dag)

    def _reply(self, name, default):
        r = self.scripted.get(name, default)
        if isinstance(r, BaseException):
            raise r
        return r

    def dag_of(self, instance_id):
        return self.dag

    def edit_graph(self, instance_id, ops, **kw):
        self.calls.append(("edit_graph", instance_id, ops, kw))
        return self._reply("edit_graph", {"edited": len(ops), "remapped": 0,
                                          "nodes": [n["id"] for n in self.dag]})

    def escalations(self, instance_id, node_id=None):
        self.calls.append(("escalations", instance_id, node_id))
        return self._reply("escalations", {})

    def pending_escalations(self, instance_id, node_id=None):
        self.calls.append(("pending_escalations", instance_id, node_id))
        return self._reply("pending_escalations", {})

    def approve_escalation(self, instance_id, gate_id, **kw):
        self.calls.append(("approve_escalation", instance_id, gate_id, kw))
        return self._reply("approve_escalation",
                           {"approved": True, "instance_id": instance_id, "node_id": gate_id,
                            "seq": kw.get("seq") or 1, "reopened": ["biz_draft"],
                            "by": kw.get("by")})

    def reject_escalation(self, instance_id, gate_id, **kw):
        self.calls.append(("reject_escalation", instance_id, gate_id, kw))
        return self._reply("reject_escalation",
                           {"rejected_request": True, "instance_id": instance_id,
                            "node_id": gate_id, "seq": kw.get("seq") or 1, "by": kw.get("by")})


# ---------- 端到端的真现场（Mock 飞书 + Stub LLM，绝不碰 build_real_service）----------
#
# 假 service 测得了「CLI 自己的活」，测不了「CLI 与引擎之间的契约」：stub 的形状是我写的，
# 我照着自己的实现写 stub，就只能验出我已经想到的事（candidates 那条实测崩，就是因为
# stub 喂的形状引擎从不产生）。所以每条跨层的路，至少留一条走真 service 的。

CROSS_GATES = [
    {"id": "a", "label": "甲写材料", "executor": "human", "role": "produce", "deps": [],
     "assignee_role": "甲", "signal": "task_complete", "deliverable": {"region": "whole"}},
    {"id": "b", "label": "AI 整合", "executor": "llm", "role": "produce", "deps": ["a"],
     "prompt": "整合", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "g", "label": "乙审", "executor": "human", "role": "gate", "deps": ["b"],
     "assignee_role": "乙", "signal": "card_action", "approval_policy": "single"},
    {"id": "side", "label": "丙审", "executor": "human", "role": "gate", "deps": ["b"],
     "assignee_role": "丙", "signal": "card_action", "approval_policy": "single"},
    {"id": "tail", "label": "收口", "executor": "tool", "role": "produce", "deps": ["g", "side"],
     "tool": {"kind": "noop"}},
]


def cross(iid):
    """跑到「乙审 / 丙审」两道门同时挂着的现场：乙打回甲的活会把还在等的丙一起卷进返工，
    于是乙那一下天然是**跨界打回**，落进 escalation。"""
    from larkflow.io.deliverable import Deliverable

    store = FakeDeliverableStore()
    svc, io = build_service(CROSS_GATES, llm=CountingLLM({"w": "正文"}), deliverables=store)
    svc.start(instance_id=iid, reporter="ou_owner", inputs={})
    p = next(x for x in svc.pending(iid) if x["node_id"] == "a")
    store.overwrite(Deliverable.from_dict(p["deliverable"]), content="材料")
    guid = next(t["guid"] for t in io.tasks.values() if t["summary"] == "甲写材料")
    svc.resume_from_event({"key": TASK_UPDATE, "event": {
        "task_guid": guid, "event_types": ["task_completed_update"]}})
    assert {x["node_id"] for x in svc.pending(iid)} == {"g", "side"}
    return svc, io


def push_back(svc, io, targets, comment="材料不全"):
    res = svc.resume_from_event({"key": CARD_ACTION, "operator_id": "ou_乙",
                                 "action_value": dict(io.button_value("g", "打回"),
                                                      reopen=list(targets), comment=comment)})
    assert res.get("seq"), res          # 真的落成了一笔申请，不是被别的规则挡掉
    return res


ESC = {"by": "ou_法务", "at": "2026-07-26T10:00:00+08:00", "from_node": "legal_gate",
       "targets": ["biz_draft"], "escalated": ["biz_draft"], "approvers": ["ou_owner", "财务"],
       "notified": ["ou_owner"], "collateral": ["legal_draft"], "comment": "价款条款改了",
       "attempt": 0, "seq": 2, "status": "pending", "effective_status": "pending"}

ADD_OP = [{"op": "add_node", "node": {"id": "extra", "label": "补充审阅", "executor": "human",
                                      "role": "gate", "deps": ["biz_draft"],
                                      "assignee_role": "法务"}}]


def test_cli_edit_hands_the_ops_and_the_audit_pair_to_the_engine(capsys, tmp_path):
    svc = CliService()
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "edit", "ed-1",
              "--ops", json.dumps(ADD_OP, ensure_ascii=False),
              "--by", "ou_owner", "--reason", "加一道审"], svc)
    assert rc == 0
    name, iid, ops, kw = svc.calls[0]
    assert (name, iid) == ("edit_graph", "ed-1") and ops == ADD_OP
    assert kw == {"by": "ou_owner", "reason": "加一道审"}
    assert "ed-1" in capsys.readouterr().out


def test_cli_edit_reads_ops_from_a_file_so_the_shell_cannot_eat_them(tmp_path):
    """ops 里全是中文 label，prompt 还可能含 `$`。逼人在命令行裸写 JSON = 重踩
    `source .env` 那个坑（shell 的引号剥离 / `$` 展开会把报文悄悄改坏）。"""
    ops = [{"op": "update_node", "id": "biz_draft",
            "set": {"prompt": "按 $价款 与 ${期限} 重写「商务条款」", "label": "商务起草 v2"}}]
    f = tmp_path / "ops.json"
    f.write_text(json.dumps(ops, ensure_ascii=False), encoding="utf-8")
    svc = CliService()
    assert cli(["--db", str(tmp_path / "db.sqlite"), "edit", "ed-2", "--ops", f"@{f}",
                "--by", "ou_owner", "--reason", "改提示词"], svc) == 0
    assert svc.calls[0][2] == ops, "文件里那份报文得一个字节不差地进引擎"


def test_cli_edit_reads_ops_from_stdin(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _io.StringIO(json.dumps(ADD_OP, ensure_ascii=False)))
    svc = CliService()
    assert cli(["--db", str(tmp_path / "db.sqlite"), "edit", "ed-3", "--ops", "-",
                "--by", "ou_owner", "--reason", "管道进来的"], svc) == 0
    assert svc.calls[0][2] == ADD_OP


@pytest.mark.parametrize("bad", [
    "{不是 JSON",                                  # 连 JSON 都不是
    '{"op": "add_node"}',                          # 是对象不是数组
    '["add_node"]',                                # 元素不是对象
    '[{"op": "rename_node", "id": "x"}]',          # op 不在受控活图的三条里
])
def test_cli_edit_refuses_a_malformed_ops_report_at_parse_time(bad, tmp_path):
    """形状错 = 参数错，退出码 2，且**一次都不碰 service**。"""
    svc = CliService()
    with pytest.raises(SystemExit) as e:
        cli(["--db", str(tmp_path / "db.sqlite"), "edit", "ed-4", "--ops", bad,
             "--by", "ou_owner", "--reason", "r"], svc)
    assert e.value.code == 2 and svc.calls == []


def test_cli_edit_does_not_second_guess_the_engine(tmp_path):
    """CLI 只校验形状。「这个节点是不是 pending」「会不会成环」一律留给引擎权威侧算，
    照红线「绝不信前端」的同一条理由：两处各判一次，早晚判出两套口径。"""
    frozen = [{"op": "remove_node", "id": "biz_draft"}]          # 假设它已经 done
    svc = CliService()
    assert cli(["--db", str(tmp_path / "db.sqlite"), "edit", "ed-5",
                "--ops", json.dumps(frozen), "--by", "ou_owner", "--reason", "r"], svc) == 0
    assert svc.calls[0][2] == frozen, "CLI 不许自己拦下来，得原样递到引擎"


@pytest.mark.parametrize("exc, code", [
    (GraphEditError("越过冻结线：biz_draft 已 done"), "illegal_edit"),
    (TemplateError("deps 悬挂: nope"), "invalid_graph"),
    (ExecutorError("tool 节点无可执行体: ['x']"), "unknown_executor"),
    (RoleError("角色解析不出来: 法务"), "unknown_role"),
])
def test_cli_edit_turns_an_engine_rejection_into_one_json_object(exc, code, capsys, tmp_path):
    """落到 main 的通吃 except 的话只打 stderr，`--json` 下 stdout 是空的，
    「stdout 必须是一个可 json.loads 的对象」这条契约当场破。"""
    svc = CliService(edit_graph=exc)
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "--json", "edit", "ed-6",
              "--ops", json.dumps(ADD_OP), "--by", "ou_owner", "--reason", "r"], svc)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["rejected"] == code and str(exc) in payload["error"]


@pytest.mark.parametrize("bad", [
    '[{"op": "add_node", "node": "x"}]',                      # node 不是对象 → apply_ops AttributeError
    '[{"op": "add_node", "node": ["x"]}]',
    '[{"op": "add_node", "node": {"label": "无 id"}}]',
    '[{"op": "add_node", "node": {"id": 7, "label": "整数 id"}}]',   # 脏 id 会进权威 dag
    '[{"op": "add_node", "node": {"id": "", "label": "空 id"}}]',
    '[{"op": "remove_node", "id": ["a"]}]',                   # apply_ops TypeError: unhashable
    '[{"op": "remove_node", "id": 7}]',
    '[{"op": "update_node", "id": "x", "set": "不是对象"}]',
    '[{"op": "update_node", "id": "x", "set": ["a"]}]',
])
def test_cli_edit_stops_a_structurally_broken_node_before_the_engine(bad, tmp_path):
    """报文形状错一律 rc=2、且碰都不碰 service。

    不挡的话，这些全在 `apply_ops` 里炸成 AttributeError / TypeError，两者都不在 edit 认领的
    异常里，于是落进通吃 except：`--json` 下 stdout 是空串，脚本读到空串只会崩在解析那行。
    整数 id 更狠，它一路畅通到权威 dag（实测），而 dag 是追加型，脏数据只能再发一次 edit 删。
    """
    svc = CliService()
    with pytest.raises(SystemExit) as e:
        cli(["--db", str(tmp_path / "db.sqlite"), "edit", "ed-8", "--ops", bad,
             "--by", "ou_owner", "--reason", "r"], svc)
    assert e.value.code == 2 and svc.calls == []


def test_cli_edit_keeps_a_dirty_id_out_of_the_authoritative_graph(tmp_path):
    """真 service 端到端：整数 id 此前真的进了权威 dag，`status` 的键成了 int 7，
    而 `--json` 投影里 nodes[].id 是数字 7、status 的键经 json.dumps 变成字符串 "7"，
    同一份报文里一个节点两种身份。"""
    svc, io, llm = contract()
    svc.start(instance_id="dirty-1", reporter="ou_owner", inputs=INPUTS)
    before = [n["id"] for n in svc.dag_of("dirty-1")]
    dirty = [{"op": "add_node", "node": {"id": 7, "label": "脏", "executor": "tool",
                                         "role": "produce", "deps": ["biz_draft"],
                                         "tool": {"kind": "noop"}}}]
    with pytest.raises(SystemExit) as e:
        cli(["--db", str(tmp_path / "db.sqlite"), "edit", "dirty-1",
             "--ops", json.dumps(dirty, ensure_ascii=False), "--by", "ou_owner",
             "--reason", "试"], svc)
    assert e.value.code == 2
    assert [n["id"] for n in svc.dag_of("dirty-1")] == before, "权威 dag 一个字节都不许动"


def test_cli_edit_reports_a_capability_boundary_as_json_too(capsys, tmp_path):
    """真 service 端到端：加一道会签门（v1.3 才实现）走的是 `UnsupportedInV1`，
    它继承 NotImplementedError，与 edit 认领的那四个类 issubclass 全 False。

    这条测试故意**不**照抄实现里的异常元组：照抄的话，结构上就不可能发现「漏了第 5 种」。
    """
    svc, io, llm = contract()
    svc.start(instance_id="cap-1", reporter="ou_owner", inputs=INPUTS)
    ops = [{"op": "add_node", "node": {"id": "multi", "label": "会签", "executor": "human",
                                       "role": "gate", "deps": ["merge"],
                                       "assignee_role": "法务", "signal": "card_action",
                                       "approval_policy": "all"}}]
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "--json", "edit", "cap-1",
              "--ops", json.dumps(ops, ensure_ascii=False), "--by", "ou_owner",
              "--reason", "加会签"], svc)
    payload = json.loads(capsys.readouterr().out)          # 此前这里是空串
    assert rc == 1 and payload["rejected"] == "unsupported_in_v1"
    assert "v1.3" in payload["error"]


def test_cli_edit_never_leaves_stdout_empty_under_json(capsys, tmp_path):
    """漏网异常也得出一个对象。引擎会长出新的异常类型，CLI 不该因此破契约。

    这条路**不许**说「改图被拒」：写回图与推进执行不是一步，漏网异常可能是图已经落库之后
    才抛的，那时候报「被拒」是骗人。落没落只能让人自己去 status 看，故 landed=unknown。
    """
    svc = CliService(edit_graph=ZeroDivisionError("引擎里冒出来的新玩意"))
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "--json", "edit", "ed-9",
              "--ops", json.dumps(ADD_OP), "--by", "ou_owner", "--reason", "r"], svc)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1 and payload["rejected"] == "engine_error"
    assert "ZeroDivisionError" in payload["error"] and payload["landed"] == "unknown"

    svc2 = CliService(edit_graph=ZeroDivisionError("同上"))
    assert cli(["--db", str(tmp_path / "db.sqlite"), "edit", "ed-10",
                "--ops", json.dumps(ADD_OP), "--by", "ou_owner", "--reason", "r"], svc2) == 1
    human = capsys.readouterr().out
    assert "被拒" not in human and "status ed-10" in human


@pytest.mark.parametrize("cmd, extra", [
    ("edit", ["--ops", json.dumps(ADD_OP), "--by", "ou_o", "--reason", "r"]),
    ("approve", ["g", "--by", "ou_o"]),
    ("reject", ["g", "--by", "ou_o"]),
])
def test_cli_lock_contention_stays_json_and_says_it_is_retryable(cmd, extra, capsys, tmp_path):
    """拿不到实例锁不是「引擎说不行」，是**可重试**。此前它只往 stdout 打一行裸文本，
    `--json` 下同样破契约；而 edit / approve / reject 是继 unblock 之后第一批写命令，
    这条路第一次被推到真实使用面上。"""
    svc = CliService(**{{"edit": "edit_graph", "approve": "approve_escalation",
                         "reject": "reject_escalation"}[cmd]: LockBusy("被另一个进程占着")})
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "--json", cmd, "lk-9", *extra], svc)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1 and payload["rejected"] == "lock_busy" and payload["retryable"] is True


def test_cli_edit_of_an_unknown_instance_says_so_like_every_other_command(capsys, tmp_path):
    """打错实例 id 是最常见的手误，别报成 illegal_edit（同一批改动里 escalations 报的是
    no_such_instance，两套口径会让人以为是自己的 ops 写错了）。"""
    svc = CliService(dag=[])
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "--json", "edit", "typo-1",
              "--ops", json.dumps(ADD_OP), "--by", "ou_owner", "--reason", "r"], svc)
    assert rc == 1 and json.loads(capsys.readouterr().out)["rejected"] == "no_such_instance"
    assert svc.calls == []


def test_cli_edit_reads_an_ops_file_that_starts_with_a_bom(tmp_path):
    """带 BOM 的 ops 文件（Windows / 某些编辑器另存）也得读得进来。
    用 utf-8 读的话 json 会报「Unexpected UTF-8 BOM」，虽然点了名，但让人改文件编码
    才能用一个 CLI，属于没必要的摩擦。"""
    f = tmp_path / "ops.json"
    f.write_text(json.dumps(ADD_OP, ensure_ascii=False), encoding="utf-8-sig")
    assert f.read_bytes()[:3] == b"\xef\xbb\xbf", "先确认这个文件真的带 BOM"
    svc = CliService()
    assert cli(["--db", str(tmp_path / "db.sqlite"), "edit", "bom-1", "--ops", f"@{f}",
                "--by", "ou_owner", "--reason", "带 BOM 的文件"], svc) == 0
    assert svc.calls[0][2] == ADD_OP


@pytest.mark.parametrize("code", ["unauthorized_edit", "missing_audit"])
def test_cli_edit_also_catches_the_rejections_that_come_back_as_a_dict(code, capsys, tmp_path):
    """service 有两条拒绝出口：校验失败抛异常，鉴权 / 缺审计回结构化拒绝。
    只认异常那条的话，`unauthorized_edit` 会被当成功打印并退出 0。"""
    svc = CliService(edit_graph={"rejected": code, "instance_id": "ed-7",
                                 "detail": "只有项目发起人能改图"})
    assert cli(["--db", str(tmp_path / "db.sqlite"), "edit", "ed-7",
                "--ops", json.dumps(ADD_OP), "--by", "ou_路人", "--reason", "r"], svc) == 1
    out = capsys.readouterr().out
    assert code in out and "只有项目发起人能改图" in out


def test_cli_edit_demands_who_and_why():
    with pytest.raises(SystemExit) as e:
        from larkflow.__main__ import build_parser
        build_parser().parse_args(["edit", "i", "--ops", json.dumps(ADD_OP)])
    assert e.value.code == 2


@pytest.mark.parametrize("argv", [
    ["edit", "i", "--ops", json.dumps(ADD_OP), "--by", "ou_o", "--reason", "r"],
    ["escalations", "i"],
    ["approve", "i", "n", "--by", "ou_o"],
    ["reject", "i", "n", "--by", "ou_o"],
])
def test_cli_new_subcommands_still_honour_the_global_flags(argv):
    """每个新子命令都必须走 sub.add_parser（parents=[common]）：裸 subs.add_parser 的话
    `--json` 当场 exit 2，且 `--db X` 会被子解析器的默认值覆盖回默认库（argparse 经典坑）。"""
    from larkflow.__main__ import build_parser
    ns = build_parser().parse_args(["--db", "/tmp/x.sqlite", *argv, "--json"])
    assert ns.db == "/tmp/x.sqlite" and ns.json is True


def test_cli_escalations_shows_who_should_decide(capsys, tmp_path):
    svc = CliService(pending_escalations={"legal_gate": [ESC]})
    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "es-1"], svc) == 0
    out = capsys.readouterr().out
    for must in ("legal_gate", "ou_owner", "财务", "ou_法务", "biz_draft", "价款条款改了", "2"):
        assert must in out, f"一眼看不出 {must!r}，审批人就不知道该拍什么板"
    assert svc.calls == [("pending_escalations", "es-1", None)]


def test_cli_escalations_says_so_when_nobody_is_waiting(capsys, tmp_path):
    svc = CliService()
    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "es-2"], svc) == 0
    assert "没有待拍板的申请" in capsys.readouterr().out


def test_cli_escalations_all_reads_the_whole_history(capsys, tmp_path):
    """默认只列待批；`--all` 才走全量历史（里面有随轮次作废的旧申请，别当待办用）。"""
    settled = {**ESC, "seq": 1, "status": "pending", "effective_status": "expired"}
    svc = CliService(escalations={"legal_gate": [settled, ESC]})
    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "es-3", "--all"], svc) == 0
    out = capsys.readouterr().out
    assert svc.calls == [("escalations", "es-3", None)], "--all 得读全量那口，不是待批那口"
    assert "expired" in out and "pending" in out


def test_cli_escalations_all_does_not_dress_a_verdict_up_as_a_request(capsys, tmp_path):
    """真 service 端到端：全量 log 里混着两类记录，靠 `kind` 分（request 缺省 / verdict）。

    不分 kind 地渲染的话，裁决行的 `by`（拍板人）会被标成「申请人」、`seq` 是 None 显示成
    `?`、`comment`（拍板附言）印成「理由」，count 还把裁决计成申请。审批人照着这份输出
    只会去拍一块已经拍过的板。
    """
    svc, io = cross("hist-1")
    push_back(svc, io, ["a"])
    push_back(svc, io, ["a", "b"], comment="连整合也重来")
    svc.approve_escalation("hist-1", "g", by="ou_甲", seq=1, comment="确实不全")
    raw = svc.escalations("hist-1")["g"]
    assert [r.get("kind", "request") for r in raw] == ["request", "request", "verdict"]

    rc = cli(["--db", str(tmp_path / "db.sqlite"), "--json", "escalations", "hist-1",
              "--all"], svc)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["count"] == 2, "count 只数申请，裁决不是待办也不是申请"
    assert payload["verdicts"] == 1

    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "hist-1", "--all"], svc) == 0
    out = capsys.readouterr().out
    assert "ou_甲 同意" in out, "裁决要挂在它 ref 指向的那笔申请下面，写清谁拍的板"
    assert "申请人：ou_甲" not in out, "拍板人不是申请人"
    assert "第 None 笔" not in out and "第 ? 笔" not in out
    assert "确实不全" in out


def test_cli_escalations_all_shows_the_derived_status_not_the_frozen_literal(capsys, tmp_path):
    """记录里的 `status` 字面量冻的是落库那一刻，**永远**是 pending（追加型 channel 没有
    UPDATE）。批过的那笔要显示 approved，随轮次作废的要显示 expired。"""
    svc, io = cross("hist-2")
    push_back(svc, io, ["a"])
    push_back(svc, io, ["a", "b"], comment="连整合也重来")
    svc.approve_escalation("hist-2", "g", by="ou_甲", seq=1)
    states = {r["seq"]: r["effective_status"]
              for r in svc.escalations("hist-2")["g"] if r.get("kind", "request") == "request"}
    assert states == {1: "approved", 2: "expired"}, states

    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "hist-2", "--all"], svc) == 0
    out = capsys.readouterr().out
    assert "[approved]" in out and "[expired]" in out
    assert "[pending]" not in out, "别再把那个恒为 pending 的字面量印出来"


def test_cli_escalations_default_view_is_unaffected_by_verdict_records(capsys, tmp_path):
    """默认视图走 pending_escalations，拍过板的那笔不该再出现。"""
    svc, io = cross("hist-3")
    push_back(svc, io, ["a"])
    push_back(svc, io, ["a", "b"], comment="连整合也重来")
    svc.approve_escalation("hist-3", "g", by="ou_甲", seq=1)
    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "hist-3"], svc) == 0
    assert "没有待拍板的申请" in capsys.readouterr().out, "同意会把这道门推进新一轮，旧申请全作废"


def test_cli_escalations_renders_an_orphan_verdict_instead_of_swallowing_it(capsys, tmp_path):
    """裁决的 ref 指不到申请（历史 / 数据异常）时也要露出来，别静默吞掉一条审计。"""
    svc = CliService(escalations={"g": [{"kind": "verdict", "ref": 9, "node_id": "g",
                                         "verdict": "rejected", "by": "ou_owner",
                                         "at": "2026-07-26T10:00:00+08:00"}]})
    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "orp-1", "--all"], svc) == 0
    out = capsys.readouterr().out
    assert "第 9 笔" in out and "ou_owner" in out


def test_cli_escalations_can_focus_one_node_and_takes_a_bare_list(capsys, tmp_path):
    """带 node_id 时 service 回的是 list 而不是 dict（现状如此）。CLI 两种都得吃得下，
    否则 `--node` 一加就 TypeError。"""
    svc = CliService(pending_escalations=[ESC])
    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "es-4",
                "--node", "legal_gate"], svc) == 0
    assert svc.calls == [("pending_escalations", "es-4", "legal_gate")]
    assert "legal_gate" in capsys.readouterr().out


def test_cli_escalations_json_is_one_object_keyed_by_node(capsys, tmp_path):
    svc = CliService(pending_escalations=[ESC])
    assert cli(["--db", str(tmp_path / "db.sqlite"), "--json", "escalations", "es-5",
                "--node", "legal_gate"], svc) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["escalations"]["legal_gate"][0]["seq"] == 2 and payload["count"] == 1


def test_cli_read_commands_also_keep_stdout_parseable_when_something_blows_up(capsys, tmp_path):
    """兜底那条（main 的通吃 except）自己也得守约。此前它只往 stderr 打一行，
    `--json` 的 stdout 是空串，而 escalations / approve / reject 都可能走到它。"""
    svc = CliService(pending_escalations=RuntimeError("state 读坏了"))
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "--json", "escalations", "boom-1"], svc)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1 and payload["rejected"] == "internal_error"
    assert "RuntimeError" in payload["error"] and payload["cmd"] == "escalations"


def test_cli_human_mode_still_keeps_crashes_on_stderr(capsys, tmp_path):
    """人类模式保持旧行为：错误走 stderr，不污染 stdout 管道。"""
    svc = CliService(pending_escalations=RuntimeError("state 读坏了"))
    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "boom-2"], svc) == 1
    cap = capsys.readouterr()
    assert cap.out == "" and "RuntimeError" in cap.err


def test_cli_escalations_of_an_unknown_instance_exits_nonzero(capsys, tmp_path):
    svc = CliService(dag=[])
    assert cli(["--db", str(tmp_path / "db.sqlite"), "escalations", "nope"], svc) == 1
    assert "nope" in capsys.readouterr().out


def test_cli_approve_settles_a_request_and_says_what_got_reopened(capsys, tmp_path):
    svc = CliService()
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "approve", "ap-1", "legal_gate",
              "--by", "ou_owner", "--seq", "2", "--comment", "同意返工"], svc)
    assert rc == 0
    name, iid, gate, kw = svc.calls[0]
    assert (name, iid, gate) == ("approve_escalation", "ap-1", "legal_gate")
    assert kw == {"by": "ou_owner", "seq": 2, "comment": "同意返工"}
    out = capsys.readouterr().out
    assert "biz_draft" in out and "ou_owner" in out


def test_cli_approve_without_a_seq_leaves_the_choice_to_the_engine(tmp_path):
    svc = CliService()
    assert cli(["--db", str(tmp_path / "db.sqlite"), "approve", "ap-2", "legal_gate",
                "--by", "ou_owner"], svc) == 0
    assert svc.calls[0][3] == {"by": "ou_owner", "seq": None, "comment": None}


def test_cli_reject_turns_a_request_down_and_still_exits_zero(capsys, tmp_path):
    """否决一笔申请是**成功执行**（rejected_request），别和 `rejected`（命令被拒）混了。"""
    svc = CliService()
    assert cli(["--db", str(tmp_path / "db.sqlite"), "reject", "rj-1", "legal_gate",
                "--by", "ou_owner", "--comment", "先别返工"], svc) == 0
    assert svc.calls[0][0] == "reject_escalation"
    assert "legal_gate" in capsys.readouterr().out


@pytest.mark.parametrize("result, hint", [
    ({"rejected": "unauthorized_approve", "instance_id": "x"}, "审批人"),
    ({"rejected": "self_approve", "instance_id": "x"}, "自己"),
    ({"rejected": "no_such_escalation", "instance_id": "x"}, "没有"),
    ({"rejected": "already_settled", "instance_id": "x"}, "拍过板"),
    ({"rejected": "illegal_reopen", "instance_id": "x"}, "打回"),
    ({"rejected": "missing_audit", "instance_id": "x"}, "审计"),
    ({"skipped": "stale", "instance_id": "x"}, "作废"),
])
def test_cli_approve_reports_every_structured_refusal_with_a_nonzero_exit(
        result, hint, capsys, tmp_path):
    svc = CliService(approve_escalation=result)
    assert cli(["--db", str(tmp_path / "db.sqlite"), "approve", "ap-3", "legal_gate",
                "--by", "ou_x"], svc) == 1
    assert hint in capsys.readouterr().out


def test_cli_approve_of_an_ambiguous_gate_tells_you_to_pick_a_seq(capsys, tmp_path):
    # candidates 是**纯 seq 列表**（引擎侧 `_pick_escalation` 的真形状）。此前这个 stub
    # 喂的是 [{"seq": …}]，引擎从不产生，于是测试全绿而真栈 100% AttributeError。
    svc = CliService(approve_escalation={"rejected": "ambiguous_escalation",
                                         "candidates": [2, 3]})
    assert cli(["--db", str(tmp_path / "db.sqlite"), "approve", "ap-4", "legal_gate",
                "--by", "ou_owner"], svc) == 1
    out = capsys.readouterr().out
    assert "--seq 2" in out and "--seq 3" in out


def test_cli_approve_survives_a_real_ambiguous_escalation(capsys, tmp_path):
    """真 service 端到端：同一道门挂两笔待批，approve 不给 seq。

    这条是 candidates 那个崩的唯一真守卫：形状由引擎产生，不是我写的 stub。
    """
    svc, io = cross("amb-1")
    push_back(svc, io, ["a"])
    push_back(svc, io, ["a", "b"], comment="连整合也重来")
    assert [r["seq"] for r in svc.pending_escalations("amb-1", "g")] == [1, 2]

    rc = cli(["--db", str(tmp_path / "db.sqlite"), "--json", "approve", "amb-1", "g",
              "--by", "ou_甲"], svc)
    payload = json.loads(capsys.readouterr().out)     # stdout 必须仍是一个对象
    assert rc == 1 and payload["rejected"] == "ambiguous_escalation"
    assert payload["candidates"] == [1, 2]

    assert cli(["--db", str(tmp_path / "db.sqlite"), "approve", "amb-1", "g",
                "--by", "ou_甲"], svc) == 1
    out = capsys.readouterr().out
    assert "--seq 1" in out and "--seq 2" in out, "人得看得出下一步该敲什么"


def test_cli_approve_then_picks_one_of_them_and_it_lands(capsys, tmp_path):
    """挑一笔批下去，打回真的落地（不只是打印一句好话）。"""
    svc, io = cross("amb-2")
    push_back(svc, io, ["a"])
    push_back(svc, io, ["a", "b"], comment="连整合也重来")
    assert cli(["--db", str(tmp_path / "db.sqlite"), "approve", "amb-2", "g",
                "--by", "ou_甲", "--seq", "1", "--comment", "同意"], svc) == 0
    assert svc.status("amb-2")["a"] == "pending", "甲的活得真的退回重做"
    assert "a" in capsys.readouterr().out


def test_cli_approve_json_stays_one_object_even_when_refused(capsys, tmp_path):
    svc = CliService(approve_escalation={"rejected": "self_approve", "instance_id": "ap-5"})
    assert cli(["--db", str(tmp_path / "db.sqlite"), "--json", "approve", "ap-5",
                "legal_gate", "--by", "ou_法务"], svc) == 1
    assert json.loads(capsys.readouterr().out)["rejected"] == "self_approve"


@pytest.mark.parametrize("cmd", ["approve", "reject"])
def test_cli_approve_and_reject_demand_who(cmd):
    """审计是不变量（照 unblock 的 missing_audit 先例）：没有 `--by` 连解析都不给过。"""
    from larkflow.__main__ import build_parser
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args([cmd, "i", "n"])
    assert e.value.code == 2
