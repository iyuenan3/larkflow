"""飞书 I/O 适配层（入口出口全走 lark-cli，不接飞书 SDK）。

抽象 LarkIO 两实现：
  MockLarkIO  内存记录建任务 / 发卡 / 通知，本地零依赖跑 e2e。
  CliLarkIO   shell 出 `lark-cli task/im`（入站事件在 events.py）。

所有写动作幂等（idem_key）：human 节点 resume 会重跑、飞书事件 at-least-once，
必须靠幂等键去重（lark-cli task/message 原生支持 --idempotency-key）。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


@dataclass
class Button:
    """卡片按钮。action_value 会原样回传到 card.action.trigger 事件，
    塞路由键（thread_id / interrupt_id / node_id / verdict）实现自描述。"""
    label: str
    action_value: dict
    style: str = "default"  # default | primary_filled | danger_filled


class LarkIO:
    def create_task(self, *, assignee: str, summary: str, description: str, idem_key: str) -> str:
        raise NotImplementedError

    def complete_task(self, task_guid: str, *, idem_key: str) -> None:
        raise NotImplementedError

    def send_card(self, *, target: str, summary: str, buttons: list[Button], idem_key: str) -> str:
        raise NotImplementedError

    def notify(self, *, target: str, text: str, idem_key: str) -> None:
        raise NotImplementedError


class MockLarkIO(LarkIO):
    """本地内存实现。测试据此断言 + 造事件。"""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.cards: dict[str, dict] = {}
        self.notifications: list[dict] = []
        self._idem: dict[str, str] = {}   # idem_key -> external_id
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

    def create_task(self, *, assignee, summary, description, idem_key) -> str:
        if idem_key in self._idem:
            return self._idem[idem_key]
        guid = self._next("task")
        self.tasks[guid] = {
            "guid": guid, "assignee": assignee, "summary": summary,
            "description": description, "completed": False,
        }
        self._idem[idem_key] = guid
        return guid

    def complete_task(self, task_guid, *, idem_key) -> None:
        if task_guid in self.tasks:
            self.tasks[task_guid]["completed"] = True

    def send_card(self, *, target, summary, buttons, idem_key) -> str:
        if idem_key in self._idem:
            return self._idem[idem_key]
        msg_id = self._next("om")
        self.cards[msg_id] = {
            "message_id": msg_id, "target": target, "summary": summary,
            "buttons": [{"label": b.label, "action_value": b.action_value, "style": b.style} for b in buttons],
        }
        self._idem[idem_key] = msg_id
        return msg_id

    def notify(self, *, target, text, idem_key) -> None:
        if idem_key in self._idem:
            return
        self._idem[idem_key] = "sent"
        self.notifications.append({"target": target, "text": text})

    # ---- 测试辅助：拿某节点最新一张卡的某个按钮的 action_value ----
    def button_value(self, node_id: str, label: str) -> dict:
        for card in reversed(list(self.cards.values())):
            for b in card["buttons"]:
                if b["action_value"].get("node_id") == node_id and b["label"] == label:
                    return b["action_value"]
        raise KeyError(f"未找到 node={node_id} 按钮={label} 的卡片")


class CliLarkIO(LarkIO):
    """真飞书：shell `lark-cli`（出站）。凭证走 lark-cli 自身 auth/profile，不落这里。

    真飞书阶段接通；本地 e2e 不走此路径。命令形态见研究：
      task +create --summary --description --assignee ou_ --idempotency-key --as bot --json
      im   +messages-send --user-id ou_/--chat-id oc_ --msg-type interactive --content <card2.0>
    """

    def __init__(self, *, identity: str = "bot", profile: str | None = None):
        self.identity = identity
        self.profile = profile

    def _run(self, args: list[str]) -> dict:
        base = ["lark-cli"]
        if self.profile:
            base += ["--profile", self.profile]
        out = subprocess.run(base + args + ["--json"], capture_output=True, text=True, check=True)
        return json.loads(out.stdout or "{}")

    def create_task(self, *, assignee, summary, description, idem_key) -> str:
        res = self._run([
            "task", "+create", "--summary", summary, "--description", description,
            "--assignee", assignee, "--idempotency-key", idem_key, "--as", self.identity,
        ])
        return (res.get("task") or {}).get("guid", "")

    def complete_task(self, task_guid, *, idem_key) -> None:
        self._run(["task", "+complete", "--task-id", task_guid, "--as", self.identity])

    def send_card(self, *, target, summary, buttons, idem_key) -> str:
        card = _card_2_0(summary, buttons)
        flag = "--chat-id" if target.startswith("oc_") else "--user-id"
        res = self._run([
            "im", "+messages-send", flag, target, "--msg-type", "interactive",
            "--content", json.dumps(card, ensure_ascii=False),
            "--idempotency-key", idem_key, "--as", self.identity,
        ])
        return (res.get("data") or res).get("message_id", "")

    def notify(self, *, target, text, idem_key) -> None:
        flag = "--chat-id" if target.startswith("oc_") else "--user-id"
        self._run([
            "im", "+messages-send", flag, target, "--msg-type", "text",
            "--content", json.dumps({"text": text}, ensure_ascii=False),
            "--idempotency-key", idem_key, "--as", self.identity,
        ])


def _card_2_0(summary: str, buttons: list[Button]) -> dict:
    """把 Button 列成飞书卡片 2.0。callback 按钮的 value 原样回传为 action_value。"""
    style_map = {"primary_filled": "primary_filled", "danger_filled": "danger_filled", "default": "default"}
    btn_elems = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": b.label},
            "type": style_map.get(b.style, "default"),
            "behaviors": [{"type": "callback", "value": b.action_value}],
        }
        for b in buttons
    ]
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "larkflow"}, "template": "blue"},
        "body": {"elements": [
            {"tag": "markdown", "content": summary},
            {"tag": "column_set", "columns": [
                {"tag": "column", "elements": [e]} for e in btn_elems
            ]},
        ]},
    }
