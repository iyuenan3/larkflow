"""关联表：external_id -> (thread_id, interrupt_id, node_id)。

发卡 / 建任务时写入（此刻已知 interrupt.id），飞书事件回来时按 external_id
反查该 resume 哪个实例的哪个中断。它是路由索引，不是真相源（可从 checkpointer
+ 已发对象重建）。卡片自描述（action_value 里带 thread/interrupt/node），任务只带
task_guid，故任务必须靠这张表回映射。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class Correlation:
    external_id: str
    thread_id: str
    interrupt_id: str
    node_id: str
    kind: str  # task | card


class Correlations:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS correlations (
                   external_id  TEXT PRIMARY KEY,
                   thread_id    TEXT NOT NULL,
                   interrupt_id TEXT NOT NULL,
                   node_id      TEXT NOT NULL,
                   kind         TEXT NOT NULL
               )"""
        )
        # 改图（update_state）会让**挂起中断换 id**（实测：连空更新也换）。已发出去的卡 /
        # 任务里嵌的是旧 id，不重绑就会变成「点了没反应」。这张表只记「改图导致的 id 迁移」，
        # 不覆盖打回（打回本就该出新单、旧卡该失效）。
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS interrupt_remap (
                   thread_id  TEXT NOT NULL,
                   old_id     TEXT NOT NULL,
                   new_id     TEXT NOT NULL,
                   PRIMARY KEY (thread_id, old_id)
               )"""
        )
        self.conn.commit()

    def put(self, c: Correlation) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO correlations VALUES (?,?,?,?,?)",
            (c.external_id, c.thread_id, c.interrupt_id, c.node_id, c.kind),
        )
        self.conn.commit()

    def put_remap(self, thread_id: str, old_id: str, new_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO interrupt_remap VALUES (?,?,?)", (thread_id, old_id, new_id)
        )
        self.conn.commit()

    def resolve_interrupt(self, thread_id: str, interrupt_id: str, *, is_live) -> str:
        """顺着改图迁移链找到当前还活着的中断 id（找不到就原样返回，交给陈旧判定）。"""
        seen, cur = {interrupt_id}, interrupt_id
        while not is_live(cur):
            row = self.conn.execute(
                "SELECT new_id FROM interrupt_remap WHERE thread_id=? AND old_id=?",
                (thread_id, cur),
            ).fetchone()
            if not row or row[0] in seen:
                return interrupt_id
            cur = row[0]
            seen.add(cur)
        return cur

    def idem_store(self) -> "IdemStore":
        """借同一个 SQLite 存幂等键（给没有 --idempotency-key 的 lark-cli 命令用）。"""
        return IdemStore(self.conn)

    def get(self, external_id: str) -> Correlation | None:
        row = self.conn.execute(
            "SELECT external_id, thread_id, interrupt_id, node_id, kind FROM correlations WHERE external_id=?",
            (external_id,),
        ).fetchone()
        return Correlation(*row) if row else None


class IdemStore:
    """幂等键 → 外部对象 id 的小 KV。

    `markdown +create` 没有 --idempotency-key（task / im 有），崩溃恢复重跑 super-step
    会多建一份文档。写入前先查这里，重放直接返回旧 handle。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS idem (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.conn.commit()

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM idem WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO idem VALUES (?,?)", (key, value))
        self.conn.commit()
