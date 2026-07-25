"""本地演示：用 Mock 飞书 + Stub LLM 把一张模板真跑一遍，你扮演所有的人。

    python -m larkflow.demo                    # 交互式，默认合同图
    python -m larkflow.demo --template hiring  # 换一张业务图（招聘接力）
    python -m larkflow.demo --auto             # 自动跑一遍「打回省算」的完整剧本

不联网、不碰飞书、不调真 LLM：飞书任务 / 卡片记在内存里，交付物是内存文档。
它跑的就是真引擎（同一份 build_service），只是把出口换成了替身。
"""
from __future__ import annotations

import argparse
import sys

from .app import build_service
from .io.deliverable import Deliverable, FakeDeliverableStore
from .io.events import CARD_ACTION, TASK_UPDATE
from .llm import LLMClient

MARK = {"done": "✅", "failed": "❌", "blocked": "⛔", "pending": "…", "skipped": "⊘"}


class DemoLLM(LLMClient):
    """产出可读的假正文：把「有没有收到打回意见」直接写进正文，好让你看见打回真的生效了。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.counts: dict[str, int] = {}

    def complete(self, *, prompt: str, model_role: str) -> str:
        self.counts[model_role] = n = self.counts.get(model_role, 0) + 1
        self.calls.append({"prompt": prompt, "model_role": model_role})
        fed = "上一轮打回意见" in prompt
        lines = [f"# {model_role} 产出 v{n}", ""]
        if fed:
            seg = prompt.split("## 上一轮打回意见（必须逐条处理）", 1)[1].split("\n\n", 1)[0]
            lines += ["> 本稿是**按打回意见改**出来的：" + seg.strip().replace("\n", "；"), ""]
        lines += ["一、价款：人民币 30 万元。", "二、期限：自签署日起 12 个月。",
                  "三、违约责任：逾期按日万分之五计。", "四、争议解决：提交甲方所在地法院。",
                  "五、初筛标准：技术栈匹配、年限达标。"]
        return "\n".join(lines)


def build(template: str):
    llm, store = DemoLLM(), FakeDeliverableStore()
    svc, io = build_service(template, llm=llm, deliverables=store)
    return svc, io, llm, store


# ---------- 展示 ----------

def show_status(svc, iid) -> None:
    status = svc.status(iid)
    cells = [f"{MARK.get(status.get(n['id'], 'pending'), '?')} {n['id']}" for n in svc.dag]
    print("  " + "   ".join(cells))
    stuck = svc.blocked(iid)
    if stuck:
        print(f"  ⛔ 已停下等人介入：{stuck}（反复打回仍不通过）")
        print(f"     解除：un {stuck[0]} <理由>  [+目标…]（人显式介入，追加一份打回预算）")


def show_pending(svc, iid) -> list[dict]:
    items = svc.pending(iid)
    if not items:
        print("  （没有人在等；全跑完或已停）")
        return items
    # 打回候选按**这一项的负责人**过滤（ADR-023）：驾驶舱的全集口径会让人点了才知道越权
    for p in items:
        role = p.get("assignee_role")
        if role and p.get("reopen_candidates") is not None:
            mine = {x["node_id"]: x for x in svc.pending(iid, actor=svc.resolver.resolve(role))}
            p.update({k: v for k, v in (mine.get(p["node_id"]) or {}).items()
                      if k in ("reopen_candidates", "reopen_escalation")})
    for i, p in enumerate(items, 1):
        kind = "门禁" if p.get("role") == "gate" else "产出"
        print(f"  {i}) {p['node_id']}  {p.get('label')}  [{kind}] 派给 {p.get('assignee_role')}")
        if p.get("deliverable_url"):
            print(f"       你要写的：{p['deliverable_url']}")
        for u in p.get("upstream") or []:
            print(f"       待审：{u['label']} {u['url']}")
        for f in p.get("feedback") or []:
            print(f"       ⚠ 上一轮被「{f['label']}」打回：{f.get('comment') or '（未留言）'}")
        if p.get("reopen_candidates"):
            print(f"       可打回：{p['reopen_candidates']}")
        if p.get("reopen_escalation"):
            print(f"       需审批才打得回（会连累别人返工）：{p['reopen_escalation']}")
    return items


def show_docs(svc, store, iid, node_id: str | None = None) -> None:
    outs = svc.outputs(iid)
    for nid, out in outs.items():
        if node_id and nid != node_id:
            continue
        raw = out.get("deliverable")
        if not raw:
            continue
        h = Deliverable.from_dict(raw)
        print(f"\n  ── {nid} · {h.url} ──")
        for line in store.fetch(h).splitlines():
            print("  " + line)


# ---------- 驱动：替人点卡片 / 完成任务 ----------

def _find(items: list[dict], key: str) -> dict | None:
    if key.isdigit() and 1 <= int(key) <= len(items):
        return items[int(key) - 1]
    return next((p for p in items if p["node_id"] == key), None)


def act(svc, io, iid, target: dict, *, passed: bool, reopen=None, comment=None) -> dict:
    """把「人做了什么」翻译成一条飞书事件，走与真栈完全相同的入口。"""
    nid = target["node_id"]
    if target.get("signal") == "task_complete":
        guid = next((t["guid"] for t in reversed(list(io.tasks.values()))
                     if t["summary"] == target.get("label")), None)
        if guid is None:
            return {"skipped": "no-task"}
        return svc.resume_from_event({"key": TASK_UPDATE, "event": {
            "task_guid": guid, "event_types": ["task_completed_update"]}})
    av = dict(io.button_value(nid, "通过" if passed else "打回"))
    if not passed:
        if reopen:
            av["reopen"] = list(reopen)
        if comment:
            av["comment"] = comment
    # 谁收到这张卡，谁才点得动它：打回权限层（ADR-023）按这个身份判。
    # 写死一个占位 id 会让每一次打回都被判成陌生人越权。
    who = svc.resolver.resolve(target.get("assignee_role"))
    return svc.resume_from_event({"key": CARD_ACTION, "action_value": av, "operator_id": who})


def write_doc(svc, store, iid, target: dict, text: str) -> None:
    """模拟人在飞书文档里把内容写好（引擎只备容器，内容归人）。"""
    raw = target.get("deliverable")
    if not raw:
        print("  这个节点没有交付物，不用写")
        return
    store.overwrite(Deliverable.from_dict(raw), content=text)
    print(f"  已写入 {target['node_id']}（{len(text)} 字）")


# ---------- 自动剧本 ----------

def run_auto(template: str) -> None:
    svc, io, llm, store = build(template)
    iid = "demo-auto"
    inputs = {"甲方": "某某科技", "乙方": "某某咨询", "价款": "人民币 30 万元", "期限": "12 个月",
              "岗位": "后端工程师", "标题": "演示"}
    print(f"\n▶ 起项目（模板 {template}）")
    svc.start(instance_id=iid, reporter="ou_owner", inputs=inputs)
    show_status(svc, iid)
    items = show_pending(svc, iid)

    gates = [p for p in items if p.get("role") == "gate"]
    if len(gates) >= 2:
        print(f"\n▶ {gates[1]['node_id']} 放行")
        act(svc, io, iid, gates[1], passed=True)
        print(f"\n▶ {gates[0]['node_id']} 打回（带意见）")
        before = dict(llm.counts)
        outs_before = {n: o.get("deliverable") for n, o in svc.outputs(iid).items()}
        act(svc, io, iid, gates[0], passed=False, comment="账期与价款不符，请改")

        print(f"  LLM 调用：{before} → {llm.counts}")
        print("  证据（打回省算）：")
        for nid, raw in outs_before.items():
            if not raw:
                continue
            now = svc.outputs(iid).get(nid, {}).get("deliverable")
            vers = len(store.versions(Deliverable.from_dict(raw)))
            same = now == raw
            what = "重算了（同一 handle overwrite，飞书留版本）" if vers > 1 else "根本没重算，旧产出原样复用"
            print(f"    {nid:<14} handle {'不变' if same else '变了'}｜版本 {vers} 版｜{what}")
        show_status(svc, iid)

    # 剩下的一路走完：门禁一律放行，人工产出节点先写内容再完成
    for _ in range(40):
        items = svc.pending(iid)
        if not items:
            break
        p = items[0]
        if p.get("role") == "gate":
            print(f"\n▶ {p['node_id']} 放行")
            act(svc, io, iid, p, passed=True)
        else:
            print(f"\n▶ {p['node_id']} 由人写好并完成")
            write_doc(svc, store, iid, p,
                      "# 定稿\n一、价款：30 万元。\n二、期限：12 个月。\n"
                      "三、违约责任：按日万分之五。\n四、争议解决：甲方所在地法院。\n"
                      "五、初筛标准：技术栈匹配。\n")
            act(svc, io, iid, p, passed=True)

    print("\n▶ 收尾")
    show_status(svc, iid)
    print(f"\n  飞书任务 {len(io.tasks)} 条 / 卡片 {len(io.cards)} 张 / 通知 {len(io.notifications)} 条")
    print(f"  交付物 {len(store.docs)} 份，LLM 调用 {llm.counts}")
    show_docs(svc, store, iid, node_id=svc.dag[-1]["id"])


HELP = """
  s / status          看整张图的状态
  p / pending         看现在卡在谁手上（含链接、打回候选、上一轮意见）
  g / graph           看拓扑（节点 + 依赖）
  ok <n>              该项通过 / 完成          例：ok 1
  no <n> [目标…] [意见…]  打回                 例：no 1 biz_draft 账期不对
                      （越权会被拒；会连累别人返工的目标转成一笔审批申请，见 esc）
  esc                 看待批的跨界打回申请（ADR-023）
  w <n> <正文>        模拟人在飞书文档里写内容  例：w 1 这是我写的定稿
  doc [节点]          打印交付物正文
  add <id> <标签> after <上游>   运行中往图里加一个 AI 节点（受控活图）
  un <节点> <理由> [目标…]  解除 ⛔（人显式介入，追加一份打回预算，可连带解冻上游）
  h / help            帮助      q / quit      退出
"""


def run_interactive(template: str) -> None:
    svc, io, llm, store = build(template)
    iid = "demo-1"
    svc.start(instance_id=iid, reporter="ou_owner",
              inputs={"甲方": "某某科技", "乙方": "某某咨询", "价款": "人民币 30 万元",
                      "期限": "12 个月", "岗位": "后端工程师", "标题": "演示"})
    print("\n飞流 · 本地演示（Mock 飞书 + Stub LLM，不联网）")
    print(f"模板 {template}｜实例 {iid}｜你来扮演所有的人。输入 h 看命令。\n")
    show_status(svc, iid)
    items = show_pending(svc, iid)

    while True:
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue
        cmd, *rest = raw.split()
        if cmd in ("q", "quit", "exit"):
            return
        if cmd in ("h", "help"):
            print(HELP)
        elif cmd in ("s", "status"):
            show_status(svc, iid)
        elif cmd in ("p", "pending"):
            items = show_pending(svc, iid)
        elif cmd in ("g", "graph"):
            for n in svc.dag:
                st = svc.status(iid).get(n["id"], "pending")
                print(f"  {MARK.get(st,'?')} {n['id']:<16} {n.get('label','')}"
                      f"  [{n['executor']}/{n['role']}]  ← {n.get('deps') or '—'}")
        elif cmd == "doc":
            show_docs(svc, store, iid, rest[0] if rest else None)
        elif cmd == "esc":
            log = svc.escalations(iid)
            if not log:
                print("  （没有待批的打回申请）")
            for nid, records in log.items():
                for r in records:
                    print(f"  {nid} #{r['seq']} [{r['status']}] {r['by']} 想打回 {r['escalated']}"
                          f"，会连累 {r['collateral']}；已通知 {r.get('notified') or r['approvers']}"
                          f"｜{r.get('comment') or '（未留言）'}")
        elif cmd in ("ok", "no", "w"):
            items = svc.pending(iid)
            if not rest or not (t := _find(items, rest[0])):
                print("  找不到这一项，先 p 看看待办编号")
                continue
            if cmd == "ok":
                print("  →", act(svc, io, iid, t, passed=True))
            elif cmd == "w":
                write_doc(svc, store, iid, t, " ".join(rest[1:]) or "（空）")
                continue
            else:
                # 候选 = 他直接打得回的 ∪ 要走审批的：后者照样让他点，点了会落一笔申请
                ids = {n["id"] for n in svc.dag}
                targets = [x for x in rest[1:] if x in ids]
                comment = " ".join(x for x in rest[1:] if x not in ids)
                print("  →", act(svc, io, iid, t, passed=False,
                                 reopen=targets or None, comment=comment or None))
            show_status(svc, iid)
            items = show_pending(svc, iid)
        elif cmd == "un" and rest:
            # 解除 blocked：目标（可选）取图里存在的节点 id，其余当理由
            ids = {n["id"] for n in svc.dag}
            targets = [x for x in rest[1:] if x in ids]
            reason = " ".join(x for x in rest[1:] if x not in ids) or "（未留言）"
            print("  →", svc.unblock(iid, rest[0], by="ou_owner", reason=reason,
                                     reopen=targets or None))
            show_status(svc, iid)
            items = show_pending(svc, iid)
        elif cmd == "add" and len(rest) >= 4 and rest[2] == "after":
            node = {"id": rest[0], "label": rest[1], "executor": "llm", "role": "produce",
                    "deps": [rest[3]], "prompt": "补一段说明", "model_role": "writer",
                    "deliverable": {"region": "whole"}}
            try:
                print("  →", svc.edit_graph(iid, [{"op": "add_node", "node": node}]))
                show_status(svc, iid)
            except Exception as exc:
                print(f"  改图被拒：{type(exc).__name__}: {exc}")
        else:
            print("  不认识这个命令，h 看帮助")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="飞流本地演示（不联网）")
    ap.add_argument("--template", default="contract", help="contract | defect | hiring | 模板名")
    ap.add_argument("--auto", action="store_true", help="自动跑一遍完整剧本（含打回省算）")
    args = ap.parse_args(argv)
    try:
        (run_auto if args.auto else run_interactive)(args.template)
    except Exception as exc:
        print(f"\n出错了：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
