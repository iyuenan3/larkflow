"""飞书 I/O 适配层（入口出口全走 lark-cli，不接飞书 SDK）。

抽象 LarkIO 两实现：
  MockLarkIO  内存记录建任务 / 发卡 / 通知，本地零依赖跑 e2e。
  CliLarkIO   shell 出 `lark-cli task/im`（入站事件在 events.py）。

所有写动作幂等（idem_key）：human 节点 resume 会重跑、飞书事件 at-least-once，
必须靠幂等键去重（lark-cli task/message 原生支持 --idempotency-key）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .cli import run_cli


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

    def update_card(self, *, token: str, card: dict) -> None:
        """用回调 token 把卡片换成「已处理」的样子（投影侧动作）。

        飞书的延迟更新：token 30 分钟内有效、最多用 2 次，且**只支持整张替换**。
        """
        raise NotImplementedError


class MockLarkIO(LarkIO):
    """本地内存实现。测试据此断言 + 造事件。"""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.cards: dict[str, dict] = {}
        self.notifications: list[dict] = []
        self.card_updates: list[dict] = []
        self._idem: dict[str, str] = {}   # idem_key -> external_id
        self._seq = 0

    def update_card(self, *, token: str, card: dict) -> None:
        self.card_updates.append({"token": token, "card": card})

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

    命令与返回字段按内嵌 skill 核对（`lark-cli skills read lark-task/lark-im`），不猜 flag：
      task +create --summary --description --assignee ou_ --idempotency-key --as bot → data.guid
      task +complete --task-id <guid>
      im +messages-send --user-id ou_/--chat-id oc_ --msg-type interactive --content <card2.0>
         --idempotency-key（同 key 1 小时内只发一条）→ data.message_id
    真飞书阶段接通；本地 e2e 不走此路径。
    """

    def __init__(self, *, identity: str = "bot", profile: str | None = None, runner=run_cli):
        self.identity = identity
        self.profile = profile
        self.runner = runner   # 可注入替身，便于对 argv 做单测

    def _run(self, args: list[str], *, stdin: str | None = None) -> dict:
        base = ["lark-cli"]
        if self.profile:
            base += ["--profile", self.profile]
        return self.runner(base + args + ["--json"], stdin=stdin)

    def create_task(self, *, assignee, summary, description, idem_key) -> str:
        data = self._run([
            "task", "+create", "--summary", summary, "--description", description,
            "--assignee", assignee, "--idempotency-key", idem_key, "--as", self.identity,
        ])
        return data.get("guid", "")

    def complete_task(self, task_guid, *, idem_key) -> None:
        self._run(["task", "+complete", "--task-id", task_guid, "--as", self.identity])

    def send_card(self, *, target, summary, buttons, idem_key) -> str:
        card = _card_2_0(summary, buttons)
        flag = "--chat-id" if target.startswith("oc_") else "--user-id"
        data = self._run([
            "im", "+messages-send", flag, target, "--msg-type", "interactive",
            "--content", json.dumps(card, ensure_ascii=False),
            "--idempotency-key", idem_key, "--as", self.identity,
        ])
        return data.get("message_id", "")

    def update_card(self, *, token: str, card: dict) -> None:
        self._run([
            "api", "POST", "/open-apis/interactive/v1/card/update", "--as", self.identity,
            "--data", json.dumps({"token": token, "card": card}, ensure_ascii=False),
        ])

    def notify(self, *, target, text, idem_key) -> None:
        flag = "--chat-id" if target.startswith("oc_") else "--user-id"
        self._run([
            "im", "+messages-send", flag, target, "--msg-type", "text",
            "--content", json.dumps({"text": text}, ensure_ascii=False),
            "--idempotency-key", idem_key, "--as", self.identity,
        ])


def settled_card(summary: str, verdict: str) -> dict:
    """已处理的卡：正文照旧 + 一行结论，**按钮全部撤掉**。

    撤按钮不是为了好看：留着就还能点，而点了只会静默 no-op（那一轮的中断早没了），
    人会以为「系统坏了」。撤掉之后，「不能点」本身就是反馈。
    """
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "larkflow"}, "template": "grey"},
        "body": {"elements": [
            {"tag": "markdown", "content": summary},
            {"tag": "hr"},
            {"tag": "markdown", "content": verdict},
        ]},
    }


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
