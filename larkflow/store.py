"""多进程共用同一个 SQLite 的那一套：WAL + busy_timeout + 跨进程实例锁。

**为什么必须正面处理**：`serve` 是常驻进程、一直握着 DB；`larkflow start / unblock /
reconcile` 是另一个进程，写的是同一个文件。`LarkFlowService._thread_lock` 只是**进程内**
锁，对另一个进程完全不设防，而驱动层每一次状态变更都是「读 state → 算 → update_state」的
读改写（ADR-028 的保值写回更是把「当前观测到的 status / outputs」原样带回去写）。两个进程
交错跑这段，后写的会把先写的静默覆盖：丢的是人刚点下的裁决，且事后无迹可查。

这层给的三件东西，缺一条前一条就不成立：

  ① `open_db`     WAL + busy_timeout。解决**SQLite 层**的并发：读不再被写堵住，写与写
                  互斥而不是当场 `database is locked` 报错。它**不解决**读改写丢更新
                  （那是应用层的事），单靠它就是「祈祷没事」。
  ② `InstanceLocks`  跨进程**建议锁**（flock），按 instance_id 一把，作为 `lock_factory`
                  注进 service，于是「进程内 per-instance 串行」这条既有不变量原样扩展到
                  跨进程。锁的临界区与 `_thread_lock` 严格同域，不多不少。
  ③ `daemon_lock_for` 单例锁：同一个 DB 只允许一个 `serve` 常驻。两个 daemon 各自订阅同
                  一条飞书事件流，会把同一次点击处理两遍，且互相把对方的推进拍打乱。

**保证了什么**：所有走这套 API 的进程，对同一个实例的状态变更严格串行；同一个 DB 只有一个
daemon；SQLite 层不再出现「database is locked」这类伪故障。
**没保证什么**：flock 是**建议锁**，不走这套 API 的进程（裸 sqlite3、别的版本、`--no-lock`）
照样能进来写；flock 在 NFS / SMB 上语义不可靠（本地盘才算数）；它不是事务，进程被 kill -9
时锁随 fd 释放，此刻的原子性由 SQLite 自己的事务负责；不保证公平 / 先来后到；对方握锁超过
timeout 时这边**报错**（LockBusy）而不是硬闯：宁可失败得响，不要静默覆盖。
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

try:                       # Windows 没有 fcntl：不静默降级成「没有锁」（那比没锁更危险）
    import fcntl
except ImportError:        # pragma: no cover - 目标宿主是 Ubuntu / macOS
    fcntl = None

# 一次性命令等 daemon 放手的默认上限。给得宽是因为 daemon 的临界区里可能真在跑 LLM /
# 建文档；给上限是因为「永远等下去」的 CLI 会让人以为是自己敲错了。
DEFAULT_LOCK_TIMEOUT = 120.0
_POLL = 0.05
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class LockBusy(RuntimeError):
    """拿不到跨进程锁：另一个 larkflow 进程正握着同一个实例（或同一个 DB）。"""


def open_db(path: str, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """打开引擎的 SQLite：多进程共用前提下该开的都开好。

    · `journal_mode=WAL` 写进文件本身（一次生效，长期有效），读写不再互斥。
    · `busy_timeout` 是**每连接**设置，故每次开连接都要设。这里显式写出来（`connect` 的
      `timeout=` 参数其实也在设它），是为了让这个值明摆着由我们定，而不是悄悄跟着标准库
      的默认值走：后到的那个写必须**等**，不能当场 `database is locked`。
    · `synchronous` 保持默认（FULL）：单一事实源丢不起最后几笔提交，这里不拿耐久性换吞吐。
    """
    conn = sqlite3.connect(path, check_same_thread=False, timeout=busy_timeout_ms / 1000)
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if path == ":memory:" or path.startswith("file::memory:"):
        return conn
    mode = (conn.execute("PRAGMA journal_mode=WAL").fetchone() or [""])[0]
    if str(mode).lower() != "wal":
        # 开不了 WAL 多半是文件在 NFS / SMB 上（那里 flock 的语义也不可靠）。这两件事
        # 一起失效 = 多进程写这个 DB 不再安全，宁可当场不跑，也不静默降级。
        conn.close()
        raise RuntimeError(
            f"{path} 开不了 WAL（当前 journal_mode={mode}）。引擎的 DB 必须放在本地盘："
            "网络文件系统上 WAL 与 flock 都不可靠，多进程写会丢更新。")
    return conn


def lock_dir_for(db_path: str) -> str:
    """锁目录跟着 DB 文件走（不是 cwd）：换 cwd 起 CLI 不该换出一套新锁。"""
    p = Path(db_path)
    return str(p.parent / (p.name + ".locks"))


def daemon_lock_for(db_path: str, *, timeout: float = 0.0) -> "FileLock":
    """serve 单例锁：同一个 DB 同时只允许一个常驻进程。"""
    return FileLock(str(Path(db_path).with_name(Path(db_path).name + ".serve.lock")),
                    timeout=timeout)


class FileLock:
    """一个文件上的排他 flock，可设等待上限。

    不用 `O_EXCL` 造锁文件：进程被 kill -9 之后那种锁会永远留在盘上，人得手动删。
    flock 由内核在 fd 关闭时释放，崩溃自愈。
    """

    def __init__(self, path: str, *, timeout: float = DEFAULT_LOCK_TIMEOUT, poll: float = _POLL):
        if fcntl is None:      # pragma: no cover
            raise RuntimeError(
                "跨进程锁需要 fcntl（Linux / macOS）。本平台没有，请只跑单进程，"
                "或显式关掉跨进程锁并自行保证同一时刻只有一个 larkflow 在写这个 DB。")
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._fd: int | None = None

    def acquire(self, timeout: float | None = None) -> "FileLock":
        wait = self.timeout if timeout is None else timeout
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusy(
                        f"{self.path} 被另一个 larkflow 进程握着，等了 {wait:g}s 仍拿不到。"
                        "先停掉 daemon（或等它忙完）再重试。") from exc
                time.sleep(self.poll)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


class _InstanceLock:
    """进程内锁 + 跨进程 flock 的复合体，形状与 `threading.Lock` 的 with 用法一致。

    两层都要：进程内那层让同实例的多个泵线程先在内存里排队（快、且 flock 在同进程不同
    fd 之间的语义各平台差异大，不拿它当进程内锁使）；文件那层才管跨进程。
    """

    def __init__(self, local: threading.Lock, file_lock: FileLock):
        self._local = local
        self._file = file_lock

    def __enter__(self):
        self._local.acquire()
        try:
            self._file.acquire()
        except BaseException:
            self._local.release()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._file.release()
        finally:
            self._local.release()


class InstanceLocks:
    """`lock_factory`：instance_id → 跨进程实例锁。注给 `LarkFlowService`。

    粒度按实例，与 service 既有的 per-thread_id 串行完全同域：不同实例照常并行，
    同一实例无论在哪个进程都排队。
    """

    def __init__(self, dir_path: str, *, timeout: float = DEFAULT_LOCK_TIMEOUT):
        self.dir = Path(dir_path)
        self.timeout = timeout
        self._locals: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @classmethod
    def for_db(cls, db_path: str, **kw) -> "InstanceLocks":
        return cls(lock_dir_for(db_path), **kw)

    def path_for(self, instance_id: str) -> str:
        """instance_id 是外部输入（CLI / 事件），绝不直接当文件名：`../` 会写出目录外。

        可读前缀只为运维好认，唯一性由整串 id 的 sha256 保证。
        """
        raw = str(instance_id)
        stem = _SAFE.sub("_", raw)[:40] or "instance"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return str(self.dir / f"{stem}-{digest}.lock")

    def __call__(self, instance_id: str) -> _InstanceLock:
        with self._guard:
            local = self._locals.get(instance_id)
            if local is None:
                local = self._locals[instance_id] = threading.Lock()
        return _InstanceLock(local, FileLock(self.path_for(instance_id), timeout=self.timeout))
