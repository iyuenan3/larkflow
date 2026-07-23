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
import subprocess
import threading
from typing import Callable

CARD_ACTION = "card.action.trigger"
TASK_UPDATE = "task.task.update_user_access_v2"
READY_PREFIX = "[event] ready"


class EventPump:
    def __init__(self, on_event: Callable[[str, dict], None], *, identity: str = "bot", profile: str | None = None):
        self.on_event = on_event
        self.identity = identity
        self.profile = profile
        self._procs: list[subprocess.Popen] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self, event_keys: list[str]) -> None:
        for key in event_keys:
            proc = self._spawn(key)
            self._await_ready(proc, key)
            t = threading.Thread(target=self._pump, args=(proc, key), daemon=True)
            t.start()
            self._procs.append(proc)
            self._threads.append(t)

    def _spawn(self, key: str) -> subprocess.Popen:
        base = ["lark-cli"]
        if self.profile:
            base += ["--profile", self.profile]
        # 保留 stdin=PIPE 不关 → 无界订阅不因 EOF 退出（研究坑）
        return subprocess.Popen(
            base + ["event", "consume", key, "--as", self.identity],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

    def _await_ready(self, proc: subprocess.Popen, key: str) -> None:
        for line in proc.stderr:  # 阻塞直到 ready 标记，绝不 sleep
            if line.startswith(READY_PREFIX):
                return
        raise RuntimeError(f"event consume {key} 未就绪即退出")

    def _pump(self, proc: subprocess.Popen, key: str) -> None:
        for line in proc.stdout:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.on_event(key, obj)

    def stop(self) -> None:
        self._stop.set()
        for proc in self._procs:
            proc.terminate()
