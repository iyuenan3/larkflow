"""v1.0 第一个 win 的本地判定版：合同图端到端（headless，零外部依赖）。

一次跑通里同时证四件事（PRD / ROADMAP 的 win 条款）：
  ① 交付物真在图上流转（双起草 → 分头复核 → merge → 人定稿 → 机检 → 收口）
  ② 打回**可感知省算**：只重算被打回支，旁支 AI 长文不重跑、旧 handle 原样复用
  ③ auto 门自动放行 / 自动打回（格式检查），全程不问人
  ④ 运行中改图（受控活图）生效
"""
from larkflow.app import build_contract_service
from larkflow.io import FakeDeliverableStore
from larkflow.io.deliverable import Deliverable
from larkflow.io.events import CARD_ACTION, TASK_UPDATE
from support import CountingLLM, card_target

NODES = ["biz_draft", "legal_draft", "finance_gate", "legal_gate",
         "merge", "finalize", "checks", "close"]

INPUTS = {"甲方": "某某科技", "乙方": "某某咨询", "标的": "年度法律顾问服务",
          "价款": "人民币 30 万元", "期限": "12 个月"}

GOOD_CONTRACT = """# 服务合同
一、标的与价款：年度法律顾问服务，价款人民币 30 万元。
二、期限：自签署日起 12 个月。
三、违约责任：逾期付款按日万分之五计违约金。
四、争议解决：提交甲方所在地有管辖权的人民法院。
"""


def build():
    llm = CountingLLM({"writer": "商务条款", "legal": "法律条款", "editor": "合并稿"})
    store = FakeDeliverableStore()
    svc, io = build_contract_service(llm=llm, deliverables=store)
    return svc, io, llm, store


def card(io, node_id, label, *, operator=None, **override):
    """点某节点最新一张卡。operator 默认 = **收到这张卡的人**（真栈里只有他点得到它）。

    打回权限层（ADR-023）按 operator 判身份，写死一个占位 id 会让每次打回都被判成越权。
    """
    return {"key": CARD_ACTION, "action_value": dict(io.button_value(node_id, label), **override),
            "operator_id": operator or card_target(io, node_id)}


def task_done(io):
    guid = list(io.tasks.values())[-1]["guid"]
    return {"key": TASK_UPDATE, "event": {"task_guid": guid,
                                          "event_types": ["task_completed_update"]}}


def human_writes(svc, store, iid, text, node_id="finalize"):
    """模拟人在飞书文档里把定稿写好（引擎只备容器、内容归人）。"""
    waiting = next(p for p in svc.pending(iid) if p["node_id"] == node_id)
    store.overwrite(Deliverable.from_dict(waiting["deliverable"]), content=text)


def test_contract_flow_end_to_end_with_both_kinds_of_reopen():
    svc, io, llm, store = build()
    iid = "ct-1"
    svc.start(instance_id=iid, reporter="ou_owner", inputs=INPUTS)

    # ① 双起草并行跑完，两道分头门各自挂人
    st = svc.status(iid)
    assert st["biz_draft"] == "done" and st["legal_draft"] == "done"
    assert llm.counts == {"writer": 1, "legal": 1}
    assert {c["target"] for c in io.cards.values()} == {"ou_财务", "ou_法务"}
    assert "某某科技" in llm.prompt_of("writer", 0)          # 项目要素进了 prompt

    # ② 法务放行；财务打回商务支（默认目标 = 它把关的直接上游）
    svc.resume_from_event(card(io, "legal_gate", "通过"))
    legal_handle = svc.outputs(iid)["legal_draft"]["deliverable"]
    svc.resume_from_event(card(io, "finance_gate", "打回", comment="账期与价款不符"))

    assert llm.counts["writer"] == 2                          # 被打回支重算
    assert llm.counts["legal"] == 1                           # 旁支没动 = 省下一次 AI 长文
    assert "editor" not in llm.counts                         # merge 还没轮到
    assert svc.outputs(iid)["legal_draft"]["deliverable"] == legal_handle   # 旧 handle 复用
    assert svc.status(iid)["legal_gate"] == "done"            # 法务不用重审

    # 财务重新收到卡（打回后出新单）→ 放行 → merge 扇入 → 人定稿挂起
    svc.resume_from_event(card(io, "finance_gate", "通过"))
    assert llm.counts["editor"] == 1
    merged = llm.prompt_of("editor", 0)
    assert "商务条款 v2" in merged and "法律条款 v1" in merged  # 新商务 + 旧法律

    # ③ 人没改就点完成 → auto 格式检查机检不过 → 自动打回定稿（不问任何人）
    svc.resume_from_event(task_done(io))
    assert svc.outputs(iid)["checks"]["passed"] is False
    assert svc.outputs(iid)["checks"]["placeholders"]          # 占位符还在
    assert svc.status(iid)["finalize"] == "pending"

    # 人这次真写了 → 完成 → 机检通过 → 收口
    human_writes(svc, store, iid, GOOD_CONTRACT)
    svc.resume_from_event(task_done(io))

    status = svc.status(iid)
    assert all(status.get(n) == "done" for n in NODES), status
    assert svc.outputs(iid)["checks"]["passed"] is True
    assert any(n["target"] == "ou_owner" for n in io.notifications)

    # 收口小结带全部交付物链接；5 个 produce 节点 = 5 份交付物，两次打回没新建任何一份
    summary = store.fetch(Deliverable.from_dict(svc.outputs(iid)["close"]["deliverable"]))
    assert "商务条款起草" in summary and "负责人定稿" in summary
    assert len(store.docs) == 5, store.docs


def test_running_edit_inserts_a_review_node_into_the_future():
    """④ 运行中改图：项目跑起来后临时加一道法务终审，下一次 dispatch 生效。"""
    svc, io, llm, store = build()
    iid = "ct-2"
    svc.start(instance_id=iid, reporter="ou_owner", inputs=INPUTS)
    old_card = io.button_value("legal_gate", "通过")

    svc.edit_graph(iid, [
        {"op": "add_node", "node": {
            "id": "final_legal", "label": "法务终审", "executor": "human", "role": "gate",
            "deps": ["finalize"], "assignee_role": "法务", "signal": "card_action",
            "approval_policy": "single"}},
        {"op": "update_node", "id": "checks", "set": {"deps": ["final_legal"]}},
    ], by="ou_owner", reason="测试改图")

    # 改图没废掉已经发出去的卡
    assert "resumed" in svc.resume_from_event(
        {"key": CARD_ACTION, "action_value": old_card, "operator_id": card_target(io, "legal_gate")})
    svc.resume_from_event(card(io, "finance_gate", "通过"))
    human_writes(svc, store, iid, GOOD_CONTRACT)
    svc.resume_from_event(task_done(io))

    # 新插的门真挡在机检之前
    assert svc.status(iid).get("checks", "pending") == "pending"
    svc.resume_from_event(card(io, "final_legal", "通过"))
    assert all(svc.status(iid).get(n) == "done" for n in NODES + ["final_legal"])


def test_gate_card_carries_a_link_to_what_is_being_reviewed():
    """gate 自己不产交付物：不带上游链接的话，审核人手里只有一张「通过 / 打回」的空卡。"""
    svc, io, llm, store = build()
    iid = "ct-4"
    svc.start(instance_id=iid, reporter="ou_owner", inputs=INPUTS)

    waiting = {p["node_id"]: p for p in svc.pending(iid)}
    biz_url = svc.outputs(iid)["biz_draft"]["deliverable"]["url"]
    assert waiting["finance_gate"]["upstream"] == [
        {"node_id": "biz_draft", "label": "商务条款起草", "url": biz_url}]

    sent = next(c for c in io.cards.values()
                if c["buttons"][0]["action_value"]["node_id"] == "finance_gate")
    assert "财务复核(商务条款)" in sent["summary"]
    assert biz_url in sent["summary"]        # 卡片正文里真带着要审的那份文档


def test_auto_gate_reopen_target_is_its_gated_upstream():
    svc, io, llm, store = build()
    iid = "ct-3"
    svc.start(instance_id=iid, reporter="ou_owner", inputs=INPUTS)
    svc.resume_from_event(card(io, "legal_gate", "通过"))
    svc.resume_from_event(card(io, "finance_gate", "通过"))

    human_writes(svc, store, iid, "空壳，没有必需条款")
    svc.resume_from_event(task_done(io))

    checks = svc.outputs(iid)["checks"]
    assert checks["passed"] is False and checks["missing"]
    # 只重开定稿这一支；合并稿与两份初稿都不重算
    assert llm.counts == {"writer": 1, "legal": 1, "editor": 1}
    assert svc.status(iid)["merge"] == "done"
