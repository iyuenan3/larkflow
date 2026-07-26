"""停订阅时要把**整棵子进程树**带走，否则每次停机都报一次假故障。

现场（2026-07-26 真机重启 daemon 时发现，两次退出逐字复现）：

    [17:43:29] 故障 drain: TimeoutError: 10.0s 内没排空在飞的事件，连接不关
    [17:43:29] 已停止（未排空）。事件 0 条（推进 0 / 跳过 0 / 故障 1）

一条事件都没有，却每次都判「没排空」。根因是 `lark-cli event consume` 是**两级进程**：

    node /opt/homebrew/bin/lark-cli …          ← Popen 拿到的就是它
      └─ /opt/homebrew/lib/…/bin/lark-cli …    ← 真正干活的，继承了 stdout/stderr

`proc.terminate()` 只杀得到第一级。孙进程还活着、还握着那两个管道，于是管道**永远不 EOF**，
`_pump` 与 `_drain_stderr` 两个线程一直阻塞在 `for line in proc.stdout` 上，`join(timeout)`
必然超时 → `drained=False` → 不关连接 + 记故障 + 退出码非 0。

后果不是「多打一行日志」：
  · **退出码永远非 0**，于是「这次停机到底干不干净」这个信号被恒定的噪声淹没，
    真出现半截写的那次也没人看得出来（这正是 v0.5.1 加这套判据要解决的问题，被自己废掉了）；
  · 连接不关那条保护逻辑每次都触发，SQLite 连接只能靠进程退出由 OS 收；
  · 孙进程要等 daemon 自己退出之后才跟着消失，中间有一段无主的时间。

修法：子进程起在**自己的进程组**里（`start_new_session=True`），停的时候按组发信号。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from larkflow.io.events import EventPump


# 一个「像 lark-cli 那样」的替身：自己派生一个孙进程，孙进程继承 stdout/stderr 并赖着不走。
PARENT = r"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c",
    "import time\ntime.sleep(300)"])          # 孙进程：继承管道，睡到天荒地老
sys.stderr.write("ready\n"); sys.stderr.flush()
try:
    time.sleep(300)
finally:
    pass
"""


class TreeSpawningPump(EventPump):
    """只替换**要跑哪条命令**，`Popen` 的参数原样走真实现。

    这一点是这份测试有没有用的关键：如果连 `Popen(...)` 一起替掉，就等于把要验的修复
    （`start_new_session`）烤进替身里，真实现漏了也测不出来。
    """

    def _argv(self, key: str) -> list[str]:
        return [sys.executable, "-c", PARENT]

    def _await_ready(self, proc, key):
        return           # 替身不走 lark-cli 那套 ready 协议


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def descendants(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-eo", "pid,ppid"], capture_output=True, text=True).stdout
    kids = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) == pid:
            kids.append(int(parts[0]))
    return kids


def test_stopping_the_pump_drains_even_when_the_child_spawned_a_grandchild():
    """这条如果红，看到的就是真机上那行「10s 内没排空」而事件数是 0。"""
    pump = TreeSpawningPump(lambda *a: None, identity="bot")
    pump.start(["k"])
    time.sleep(1.0)                       # 让替身把孙进程生出来
    proc = pump._procs[0]
    kids = descendants(proc.pid)
    assert kids, "先确认替身真的派生了孙进程，否则这条测试测了个寂寞"

    t0 = time.monotonic()
    pump.stop()
    drained = pump.join(10.0)
    spent = time.monotonic() - t0

    assert drained is True, f"没排空：泵线程被孙进程握着的管道卡住了（耗时 {spent:.1f}s）"
    assert spent < 8, f"就算最后排空了，也不该磨蹭 {spent:.1f}s"
    time.sleep(0.5)
    assert not any(alive(k) for k in kids), "孙进程必须一起带走，别留无主进程"


def test_the_child_runs_in_its_own_process_group():
    """按组发信号的前提。不单开会话的话，`killpg` 会打到**我们自己**头上。"""
    pump = TreeSpawningPump(lambda *a: None, identity="bot")
    pump.start(["k"])
    time.sleep(0.5)
    try:
        proc = pump._procs[0]
        assert os.getpgid(proc.pid) != os.getpgid(0), \
            "子进程与我们同组：按组杀会把当前进程一起带走"
        assert os.getpgid(proc.pid) == proc.pid, "它自己就是组长"
    finally:
        pump.stop()
        pump.join(5.0)


def test_a_process_that_died_on_its_own_does_not_make_stop_blow_up():
    """子进程早就没了（崩了 / 被人 kill 了），停机不许因此抛异常。"""
    pump = TreeSpawningPump(lambda *a: None, identity="bot")
    pump.start(["k"])
    time.sleep(0.5)
    proc = pump._procs[0]
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.3)

    pump.stop()                      # 不许抛
    assert pump.join(5.0) is True
