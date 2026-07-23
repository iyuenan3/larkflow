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
        self.conn.commit()

    def put(self, c: Correlation) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO correlations VALUES (?,?,?,?,?)",
            (c.external_id, c.thread_id, c.interrupt_id, c.node_id, c.kind),
        )
        self.conn.commit()

    def get(self, external_id: str) -> Correlation | None:
        row = self.conn.execute(
            "SELECT external_id, thread_id, interrupt_id, node_id, kind FROM correlations WHERE external_id=?",
            (external_id,),
        ).fetchone()
        return Correlation(*row) if row else None
