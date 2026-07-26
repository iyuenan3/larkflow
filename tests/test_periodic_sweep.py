"""定期对账（真栈实测：任务事件这条推送根本不到，轮询是它唯一可靠的机制）。

现场证据链：卡片事件通；任务事件在 **bot 身份 + pre-consume 正常 + websocket 已连 +
app 建的任务 + app 完成 + 甚至把 app 加成 follower** 的情况下，一条都收不到。
于是 ADR-038 的轮询不再是「安全网」，而是 `task_complete` 节点**唯一**能依靠的通道，
只在启动时跑一次远远不够：人交了卷，引擎要等到下次重启才知道。
"""
from __future__ import annotations

import threading

from larkflow.serve import LarkFlowServer


class _Svc:
    def __init__(self):
        self.graph = object()
        self.seen: list[str] = []
        self.gate = threading.Event()

    def finished(self, iid):
        return False

    def reconcile(self, iid):
        self.seen.append(iid)
        self.gate.set()
        return {"reconciled": iid, "errors": []}


def server(monkeypatch, svc, **kw):
    monkeypatch.setattr("larkflow.serve.list_instances", lambda g: (["i1"], None))
    return LarkFlowServer(svc, pump_factory=lambda *a, **k: _Pump(), signals=None, **kw)


class _Pump:
    def start(self, keys):
        pass

    def stop(self):
        pass

    def join(self, timeout=None):
        return True


def test_the_sweep_keeps_running_after_startup(monkeypatch):
    svc = _Svc()
    s = server(monkeypatch, svc, sweep_seconds=0.05)
    s.start_sweeper()
    assert svc.gate.wait(3), "定期对账没跑起来"
    s.stop()
    assert svc.seen, svc.seen


def test_a_sweep_that_blows_up_does_not_kill_the_loop(monkeypatch):
    """一个坏实例不许让后续所有轮次都停摆（与启动对账同款纪律）。"""
    svc = _Svc()
    calls = {"n": 0}

    def flaky(iid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("坏了")
        svc.gate.set()
        return {"reconciled": iid, "errors": []}

    svc.reconcile = flaky
    s = server(monkeypatch, svc, sweep_seconds=0.05)
    s.start_sweeper()
    assert svc.gate.wait(3), "第一轮炸了之后应当还有第二轮"
    s.stop()


def test_stopping_the_server_stops_the_sweeper(monkeypatch):
    svc = _Svc()
    s = server(monkeypatch, svc, sweep_seconds=0.05)
    s.start_sweeper()
    assert svc.gate.wait(3)
    s.stop()
    n = len(svc.seen)
    threading.Event().wait(0.3)
    assert len(svc.seen) == n, "停了就不该再扫"


def test_it_can_be_turned_off(monkeypatch):
    svc = _Svc()
    s = server(monkeypatch, svc, sweep_seconds=0)
    s.start_sweeper()
    threading.Event().wait(0.2)
    s.stop()
    assert svc.seen == [], "配 0 = 关掉（推送可靠的租户不必付这份开销）"
