"""飞书事件入口：引擎 spawn `lark-cli event consume <EventKey>` 子进程读 NDJSON。

两个 EventKey（研究核实为静态常量，不需 dev app 上下文即可确定）：
  card.action.trigger              卡片按钮点击（仅 bot）
  task.task.update_user_access_v2  任务事件（完成 = event_types 含 task_completed_update）

子进程契约：stdout 逐行 NDJSON；stderr 先出 `[event] ready event_key=<k>`（阻塞等它
再读 stdout，不 sleep）；无界订阅要保持 stdin 不 EOF（这里保留 PIPE 不关）。

本地 e2e 不走此路径（用合成事件驱动 service.resume_from_event）；此模块供真飞书阶段。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from typing import Callable

CARD_ACTION = "card.action.trigger"
TASK_UPDATE = "task.task.update_user_access_v2"
READY_PREFIX = "[event] ready"


class EventPump:
    """飞书事件入口。**这是整个服务唯一的入站通道，它哑了产品就哑了**，故三条容错是必需的：

    ① 处理某条事件抛异常，绝不能终结泵线程（否则进程还活着、systemd 看不出问题，
       但从此所有实例的卡片点击与任务完成全部无人处理）。
    ② ready 之后 stderr 仍要有人读：子进程写满管道（约 64KB）就会永久阻塞，事件流静默停摆。
    ③ 子进程退了要带退避重启，并把故障喊出来。
    """

    def __init__(self, on_event: Callable[[str, dict], None], *, identity: str = "bot",
                 profile: str | None = None, on_error: Callable[[str, Exception], None] | None = None,
                 ready_timeout: float = 30.0, restart_backoff: float = 2.0, max_restarts: int = 5):
        self.on_event = on_event
        self.identity = identity
        self.profile = profile
        self.on_error = on_error or (lambda where, exc: None)
        self.ready_timeout = ready_timeout
        self.restart_backoff = restart_backoff
        self.max_restarts = max_restarts
        self._procs: list[subprocess.Popen] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self, event_keys: list[str]) -> None:
        for key in event_keys:
            self._start_one(key)

    def _start_one(self, key: str) -> None:
        proc = self._spawn(key)
        self._await_ready(proc, key)
        self._procs.append(proc)
        self._spawn_thread(self._drain_stderr, proc, key)   # ② 别让 stderr 写满管道
        self._spawn_thread(self._pump, proc, key)

    def _spawn_thread(self, fn, *args) -> None:
        t = threading.Thread(target=fn, args=args, daemon=True)
        t.start()
        self._threads.append(t)

    def _argv(self, key: str) -> list[str]:
        base = ["lark-cli"]
        if self.profile:
            base += ["--profile", self.profile]
        return base + ["event", "consume", key, "--as", self.identity]

    def _spawn(self, key: str) -> subprocess.Popen:
        # 保留 stdin=PIPE 不关 → 无界订阅不因 EOF 退出（研究坑）
        # **`start_new_session=True`**：子进程自成进程组，停机时才能按组把整棵树带走。
        # `lark-cli event consume` 是两级进程（`node …/bin/lark-cli` 再派生真正的 CLI），
        # 只 terminate 第一级的话，孙进程继续握着 stdout / stderr，管道**永远不 EOF**，
        # 泵线程一直阻塞在 `for line in proc.stdout`，于是 `join(timeout)` 必然超时：
        # 每次停机都判「没排空」、不关连接、退出码非 0，把「这次停机干不干净」这个信号
        # 淹在恒定噪声里（真机上两次重启逐字复现，事件数都是 0）。
        return subprocess.Popen(
            self._argv(key),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )

    def _await_ready(self, proc: subprocess.Popen, key: str) -> None:
        """阻塞等 ready 标记，绝不 sleep；但也不能无限等（子进程可能挂着不吭声）。"""
        result: dict = {}

        def wait():
            for line in proc.stderr:
                if line.startswith(READY_PREFIX):
                    result["ok"] = True
                    return

        t = threading.Thread(target=wait, daemon=True)
        t.start()
        t.join(self.ready_timeout)
        if not result.get("ok"):
            proc.terminate()
            raise RuntimeError(f"event consume {key} 未在 {self.ready_timeout}s 内就绪")

    def _drain_stderr(self, proc: subprocess.Popen, key: str) -> None:
        try:
            for _ in proc.stderr:      # 读掉就行，避免管道写满把子进程卡死
                if self._stop.is_set():
                    return
        except Exception:
            return

    def _pump(self, proc: subprocess.Popen, key: str) -> None:
        restarts = 0
        while not self._stop.is_set():
            for line in proc.stdout:
                if self._stop.is_set():
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    self.on_event(key, obj)
                except Exception as exc:      # ① 一条事件处理失败，绝不拖垮整条入站通道
                    self.on_error(f"on_event:{key}", exc)
            # stdout 到头 = 子进程没了：退避重启（③）
            if self._stop.is_set():
                return          # 正常收工（stop 会 terminate 子进程），别当故障喊
            if restarts >= self.max_restarts:
                self.on_error(f"consume:{key}", RuntimeError(
                    f"event consume {key} 退出且重启已达上限 {self.max_restarts}，入站通道已停"))
                return
            restarts += 1
            self.on_error(f"consume:{key}", RuntimeError(f"event consume {key} 退出，第 {restarts} 次重启"))
            if self._stop.wait(self.restart_backoff * restarts):
                return
            try:
                proc = self._spawn(key)
                self._await_ready(proc, key)
                self._procs.append(proc)
                self._spawn_thread(self._drain_stderr, proc, key)
            except Exception as exc:
                self.on_error(f"respawn:{key}", exc)
                return

    def stop(self) -> None:
        """停订阅：置位 + 终止 `event consume` **整棵进程树**。**不动正在处理的那条事件**
        （它可能正握着实例锁写 checkpointer），要等它请用 `join`。"""
        self._stop.set()
        for proc in self._procs:
            self._terminate_tree(proc)

    def _terminate_tree(self, proc) -> None:
        """按**进程组**结束，而不是只杀 Popen 拿到的那一个。

        `lark-cli event consume` 是两级进程（`node …/bin/lark-cli` 再派生真正的 CLI）。
        只 `terminate()` 第一级的话，孙进程继续握着 stdout / stderr，管道**永远不 EOF**，
        泵线程一直阻塞在 `for line in proc.stdout`，于是 `join(timeout)` 必然超时：每次
        停机都判「没排空」、不关连接、退出码非 0，把「这次停机干不干净」这个信号淹在恒定
        噪声里（真机上两次重启逐字复现，事件数都是 0；本地用一个自己派生孙进程的替身
        也稳定复现）。

        进程组是 `_spawn` 里 `start_new_session=True` 建的。拿不到 pid / 不是真进程
        （测试替身）时退回逐个 terminate，别让替身把停机路径搞崩。
        """
        def signal_group(sig) -> bool:
            pid = getattr(proc, "pid", None)
            if pid is None:
                return False
            try:
                os.killpg(os.getpgid(pid), sig)
                return True
            except (OSError, TypeError, AttributeError, ValueError):
                return False

        if not signal_group(signal.SIGTERM):
            try:
                proc.terminate()
            except Exception:
                pass
        wait = getattr(proc, "wait", None)
        if wait is None:
            return
        try:
            wait(timeout=5)
        except Exception:            # 赖着不走才升级到 kill，别一上来就硬杀
            if not signal_group(signal.SIGKILL):
                kill = getattr(proc, "kill", None)
                if kill is not None:
                    try:
                        kill()
                    except Exception:
                        pass

    def join(self, timeout: float | None = None) -> bool:
        """等泵线程真正退出（优雅停的第二步：让在飞的那条事件跑完）。

        线程都是 daemon，join 超时也不会挂住进程；超时后未完成的写由下次启动对账兜。

        **返回是否真排空了。** 调用方拿这个决定还能不能关连接：在飞的那条事件可能正握着
        实例锁写 checkpointer，这时候把 SQLite 连接关掉，等于把桌子从人手底下抽走。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in list(self._threads):
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            t.join(left)
        return not any(t.is_alive() for t in list(self._threads))
