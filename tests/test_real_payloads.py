"""把**真实抓到的**飞书报文钉成测试。

这些不是我照文档编的形状，是 2026-07-26 用 `lark-cli event consume` 从真飞书接到的原文
（open_id / message_id / chat_id / token 已换成假值，字段结构一字未改）。

为什么值得单开一个文件：整条入站通道此前**零真实报文覆盖**，`normalize_event` 的解包逻辑
是照 lark-cli 内嵌 skill 的字段表写的。字段名错一个，进程照样活着、systemd 看不出问题，
但所有人的按钮都点不动。这类失败没有任何症状，只能靠真报文钉住。

抓法（复现用）：
    lark-cli --profile <p> event consume card.action.trigger --as bot --max-events 1 --timeout 180s
    # 另一头在飞书客户端里点卡片按钮
"""
from __future__ import annotations

import json

from larkflow.io.events import CARD_ACTION
from larkflow.serve import normalize_event

# 真实报文（2026-07-26 实测，飞书 · 长连接 · card.action.trigger · 卡片 2.0 callback 按钮）
# 敏感字段已脱敏：operator_id / message_id / chat_id / token / event_id 换成假值。
REAL_CARD_ACTION = {
    "type": "card.action.trigger",
    "event_id": "e0000000000000000000000000000000",
    "timestamp": "1785001477632461",
    "operator_id": "ou_00000000000000000000000000000000",
    "message_id": "om_00000000000000000000000000000000",
    "chat_id": "oc_00000000000000000000000000000000",
    "host": "im_message",
    "token": "c-0000000000000000000000000000000000000000",
    "action_tag": "button",
    # 关键：开发者自定义 value 被**序列化成 JSON 字符串**回传，不是对象
    "action_value": ('{"interrupt_id":"probe-int-1","node_id":"legal_gate",'
                     '"reopen":["biz_draft"],"thread_id":"probe-1","verdict":"fail"}'),
    "checked": False,
    "card_content": json.dumps({"schema": "2.0", "header": {}, "body": {}}, ensure_ascii=False),
}


def test_the_real_card_payload_is_flat_and_carries_operator_id_at_top_level():
    """身份在**顶层**，且是裸的 open_id 字符串（不是嵌套对象）。

    权限层整个建立在「actor 取自事件顶层的 operator_id、绝不信卡片封套」这条上（红线⑤）。
    真报文确认了这个形状：`action_value` 里塞什么都影响不到 `operator_id`。
    """
    assert REAL_CARD_ACTION["operator_id"].startswith("ou_")
    assert "operator_id" not in json.loads(REAL_CARD_ACTION["action_value"])


def test_normalize_unwraps_the_json_string_action_value():
    """不解开的话 `_route` 里的 `av.get(...)` 每次 AttributeError，整条卡片通道永久失聪。"""
    ev = normalize_event(CARD_ACTION, REAL_CARD_ACTION)
    assert isinstance(ev["action_value"], dict)
    assert ev["action_value"]["thread_id"] == "probe-1"
    assert ev["action_value"]["reopen"] == ["biz_draft"]
    assert ev["key"] == CARD_ACTION          # 路由键用我们订阅的那个，不让 payload 改写
    assert ev["operator_id"] == REAL_CARD_ACTION["operator_id"]


def test_a_real_click_routes_to_the_right_instance_node_and_actor():
    """端到端：真报文 → normalize → _route，落成引擎认识的四元组。"""
    from larkflow.app import build_service

    svc, _io = build_service("contract")
    route = svc._route(normalize_event(CARD_ACTION, REAL_CARD_ACTION))
    assert route["instance_id"] == "probe-1"
    assert route["interrupt_id"] == "probe-int-1"
    assert route["node_id"] == "legal_gate"
    assert route["actor"] == REAL_CARD_ACTION["operator_id"]
    assert route["value"]["passed"] is False          # verdict=fail
    assert route["value"]["reopen"] == ["biz_draft"]


def test_a_pass_click_drops_the_reopen_noise():
    payload = {**REAL_CARD_ACTION,
               "action_value": ('{"interrupt_id":"probe-int-1","node_id":"legal_gate",'
                                '"reopen":["biz_draft"],"thread_id":"probe-1","verdict":"pass"}')}
    from larkflow.app import build_service

    svc, _io = build_service("contract")
    route = svc._route(normalize_event(CARD_ACTION, payload))
    assert route["value"]["passed"] is True
    assert "reopen" not in route["value"], "放行时回传的 reopen 是噪音，不该被当成打回目标"


def test_a_malformed_action_value_does_not_take_the_pump_down():
    """脏输入（外部可构造）当空事件跳过，不抛给泵去记一笔故障。"""
    ev = normalize_event(CARD_ACTION, {**REAL_CARD_ACTION, "action_value": "不是 JSON"})
    assert ev["action_value"] == {}
    ev = normalize_event(CARD_ACTION, {**REAL_CARD_ACTION, "action_value": "[1,2,3]"})
    assert ev["action_value"] == {}, "顶层不是对象也得落成空 dict，别让 _route 拿到 list"
