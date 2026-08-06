"""larkflow CLI：真栈的唯一入口（`larkflow <子命令>` 或 `python -m larkflow`）。

    larkflow serve                       常驻：启动对账 + 起事件泵 + block（唯一的守护进程）
    larkflow start --template contract --reporter ou_xxx --input 甲方=某某
    larkflow status <实例>  /  pending <实例> [--actor ou_xxx]
    larkflow edit <实例> --ops @ops.json --by ou_xxx --reason "加一道审"   运行中改图
    larkflow escalations <实例> [--node <节点>] [--all]   谁在等谁拍板
    larkflow approve / reject <实例> <节点> --by ou_xxx [--seq N]   拍板一笔打回申请
    larkflow unblock <实例> <节点> --by ou_xxx --reason "改了要素"
    larkflow reconcile [实例]            手动对账（省略 = 全部）

**多进程写同一个 SQLite**：`serve` 常驻持有 DB，而这里每条一次性命令都是另一个进程。
service 的 `_thread_lock` 只是进程内锁，故真栈一律走 `store.InstanceLocks` 把同一实例的
状态变更跨进程串起来（保证与不保证见 `store.py` 顶部）。`serve` 另外持有一把单例锁：
同一个 DB 只允许一个常驻进程。

演示 / 本地把玩走 `python -m larkflow.demo`（Mock 飞书 + Stub LLM，不联网），与这里无关。
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from functools import partial
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .config import RoleError, env, load_dotenv
from .doctor import run_checks, verdict
from .engine.executors import ExecutorError
from .engine.livegraph import ADD, OPS, UPDATE, GraphEditError
from .engine.support import UnsupportedInV1
from .model.template import TemplateError
from .serve import DEFAULT_EVENT_KEYS, LarkFlowServer
from .service import InstanceExists
from .store import DEFAULT_LOCK_TIMEOUT, LockBusy, daemon_lock_for, resolve_db_path

MARK = {"done": "✅", "failed": "❌", "blocked": "⛔", "pending": "…", "skipped": "⊘"}


def _kv(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"要素得写成 k=v，收到的是 {raw!r}")
    k, v = raw.split("=", 1)
    if not k.strip():
        raise argparse.ArgumentTypeError(f"要素名不能为空：{raw!r}")
    return k.strip(), v


def _ops(raw: str) -> list[dict]:
    """改图报文：字面 JSON / `@路径` / `-`（读 stdin）。三种来源缺一不可。

    **只逼人在命令行裸写 JSON 是重踩 `source .env` 那个坑**：ops 里全是中文 label，
    prompt 还常含 `$`，一过 shell 就被引号剥离 / `$` 展开悄悄改坏（见 `_preload_env`）。
    `@file` 与 `-` 让报文一个字节不差地进来。

    这里**只校验形状**：合法 JSON / 是数组 / 元素是对象 / op ∈ OPS / 每种 op 的必填字段
    是不是该有的类型。**语义一概不判**（节点是不是 pending、会不会成环、tool 有没有可执行
    体），那些留给引擎权威侧算，两处各判一次早晚判出两套口径（红线「绝不信前端」同一条理由）。
    空数组也照放，让引擎那一条「ops 为空」的规则保持单一出处。

    形状这一层必须挡在这里，理由是实测出来的三条，都不是洁癖：
      · `node` 不是对象时，`apply_ops` 的 `node.get("id")` 抛 AttributeError；`id` 给数组时
        `nid not in index` 抛 TypeError（unhashable）。两者都不是引擎认领的拒绝，会落进
        通吃 except，`--json` 下 stdout 是空串。
      · **id 不是字符串会一路畅通进权威 dag**：`apply_ops` 只判 `if not nid`（`7` 是 truthy），
        `_validate_shape` 只判 `"id" in n`。实测整数 id 的节点真的跑了起来，`status` 的键是
        int 7，而 JSON 投影里 `nodes[].id` 是数字、`status` 的键经 json.dumps 变成 "7"，
        同一份报文里一个节点两种身份。dag 是追加型，脏数据只能再发一次 edit 删。
    """
    if raw == "-":
        raw = sys.stdin.read()
    elif raw.startswith("@"):
        try:
            # utf-8-sig 而不是 utf-8：带 BOM 的文件（Windows / 某些编辑器另存的）用 utf-8 读
            # 会在开头留一个 ﻿，json 只会说「Expecting value: line 1 column 1」，
            # 而肉眼看文件完全正常。utf-8-sig 对不带 BOM 的 UTF-8 也照样读。
            raw = Path(raw[1:]).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise argparse.ArgumentTypeError(f"读不了 ops 文件 {raw[1:]}：{exc.strerror or exc}")
    try:
        ops = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"ops 不是合法 JSON：{exc}")
    if not isinstance(ops, list):
        raise argparse.ArgumentTypeError(f"ops 得是一个数组，收到的是 {type(ops).__name__}")
    for i, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            raise argparse.ArgumentTypeError(f"第 {i} 条 op 得是对象，收到的是 {type(op).__name__}")
        kind = op.get("op")
        if kind not in OPS:
            # 只回显 op 字段本身，不回显整条报文：里面可能有大段 prompt，糊满一屏反而看不见错在哪
            raise argparse.ArgumentTypeError(
                f"第 {i} 条的 op 得是 {list(OPS)} 之一，收到的是 {kind!r}")
        if kind == ADD:
            node = op.get("node")
            if not isinstance(node, dict):
                raise argparse.ArgumentTypeError(
                    f"第 {i} 条 add_node 的 node 得是对象，收到的是 {type(node).__name__}")
            _node_id(node.get("id"), f"第 {i} 条 add_node 的 node.id")
        else:
            _node_id(op.get("id"), f"第 {i} 条 {kind} 的 id")
            if kind == UPDATE and not isinstance(op.get("set"), dict):
                raise argparse.ArgumentTypeError(
                    f"第 {i} 条 update_node 的 set 得是对象，收到的是 {type(op.get('set')).__name__}")
    return ops


def _node_id(nid, where: str) -> None:
    """节点 id 必须是非空字符串。整条 CLI 只有这一处能挡住非字符串 id（见 `_ops` 的说明）。"""
    if not isinstance(nid, str) or not nid.strip():
        raise argparse.ArgumentTypeError(
            f"{where} 得是非空字符串，收到的是 {type(nid).__name__} {nid!r}")


def _add_global_flags(ap: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """全局开关。**两边都挂一份**：`larkflow status i1 --json` 是所有人的第一反应，
    只挂顶层的话 argparse 当场 exit 2「unrecognized arguments」。

    子解析器那份一律 `default=SUPPRESS`：不写这个的话，子解析器的默认值会在解析子命令时
    把顶层已经解析出来的值**覆盖回默认**（argparse 的经典坑），于是
    `larkflow --db X status i1` 里的 X 会被悄悄丢掉、连回默认库。
    """
    d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
    ap.add_argument("--db", default=d(env("LARKFLOW_DB") or None),
                    help="SQLite 文件（checkpointer + 关联表 + 幂等表）；相对路径会落成绝对路径")
    ap.add_argument("--template", default=d(env("LARKFLOW_TEMPLATE", "contract")),
                    help="默认模板名（contract / defect / hiring / …）")
    ap.add_argument("--profile", default=d(env("LARK_PROFILE")), help="lark-cli profile")
    ap.add_argument("--identity", default=d(env("LARKFLOW_IDENTITY", "bot")),
                    help="lark-cli 身份（bot / user）")
    ap.add_argument("--lock-timeout", type=float, default=d(DEFAULT_LOCK_TIMEOUT),
                    help="等另一个 larkflow 进程放开这个实例的上限（秒）")
    ap.add_argument("--json", action="store_true", default=d(False),
                    help="输出 JSON（给脚本读）")
    ap.add_argument("--env-file", default=d(".env"),
                    help="从哪个文件读环境变量（默认 ./.env；**别用 source**，shell 会吃掉引号）")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="larkflow", description="飞流：飞书原生的交付物流转工作流引擎")
    _add_global_flags(ap)
    common = argparse.ArgumentParser(add_help=False)
    _add_global_flags(common, suppress=True)
    subs = ap.add_subparsers(
        dest="cmd",
        metavar="{doctor,serve,start,status,pending,edit,escalations,approve,reject,unblock,reconcile}")

    class sub:                      # 每个子命令都带上 common，少写一遍 parents=
        add_parser = staticmethod(
            lambda name, **kw: subs.add_parser(name, parents=[common], **kw))

    sub.add_parser("doctor", help="起服务之前把能在本机查的问题一次查完（只读，不发消息）")

    p = sub.add_parser("serve", help="常驻：启动对账 + 起事件泵 + block 到收到信号")
    p.add_argument("--event-key", action="append", default=None,
                   help=f"订阅的 EventKey（可重复，默认 {list(DEFAULT_EVENT_KEYS)}）")

    p = sub.add_parser("start", help="起一个实例")
    p.add_argument("--reporter", required=True, help="发起人 open_id（owner 权限据它判）")
    p.add_argument("--input", action="append", dest="inputs", type=_kv, default=None,
                   metavar="k=v", help="项目要素（可重复）")
    p.add_argument("--id", dest="instance_id", default=None, help="实例 id（省略则自动生成）")

    p = sub.add_parser("status", help="看整张图的状态")
    p.add_argument("instance_id")

    p = sub.add_parser("pending", help="看现在卡在谁手上")
    p.add_argument("instance_id")
    p.add_argument("--actor", default=None, help="以谁的视角看（打回候选按 ADR-023 过滤）")

    p = sub.add_parser("edit", help="运行中改图（受控活图：只动 pending 子图）")
    p.add_argument("instance_id")
    p.add_argument("--ops", required=True, type=_ops,
                   metavar="SOURCE",
                   help="改图报文：字面 JSON / @文件 / -（读 stdin）；含中文与 $ 时用后两种")
    p.add_argument("--by", required=True, help="谁改的（进审计）")
    p.add_argument("--reason", required=True, help="为什么改（进审计）")

    p = sub.add_parser("escalations", help="看谁在等谁拍板（跨界打回的审批申请）")
    p.add_argument("instance_id")
    p.add_argument("--node", default=None, help="只看这道门的")
    p.add_argument("--all", action="store_true",
                   help="列全量历史（含已作废的旧申请）；默认只列还等着拍板的")

    for name, verb in (("approve", "同意"), ("reject", "否决")):
        p = sub.add_parser(name, help=f"{verb}一笔跨界打回申请")
        p.add_argument("instance_id")
        p.add_argument("node_id", help="提申请的那道门")
        p.add_argument("--by", required=True, help="谁拍的板（既是审计，也是引擎侧鉴权的依据）")
        p.add_argument("--seq", type=int, default=None,
                       help="第几笔（省略 = 该门当前唯一那笔待批；有多笔会让你补上）")
        p.add_argument("--comment", default=None, help=f"{verb}的理由（进审计、告诉申请人）")

    p = sub.add_parser("unblock", help="解除 ⛔（人显式介入，追加一份打回预算）")
    p.add_argument("instance_id")
    p.add_argument("node_id")
    p.add_argument("--by", required=True, help="谁解除的（进审计）")
    p.add_argument("--reason", required=True, help="为什么解除（进审计）")
    p.add_argument("--grant", type=int, default=1, help="追加几次打回预算")
    p.add_argument("--reopen", action="append", default=None,
                   help="连带解冻的祖先节点（可重复，须是这道门的传递祖先）")

    p = sub.add_parser("reconcile", help="手动对账（省略实例 = 全部）")
    p.add_argument("instance_id", nargs="?", default=None)
    return ap


# ---------- 装配 ----------

def _real_service(ns):
    """真栈：真飞书（lark-cli）+ 真 LLM + 文件 SQLite。测试绝不走这里。"""
    from .app import build_real_service
    service, _io = build_real_service(ns.template, db_path=ns.db, identity=ns.identity,
                                      profile=ns.profile, lock_timeout=ns.lock_timeout)
    return service


# ---------- 输出 ----------

def _emit(ns, human: str, payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2) if ns.json else human)


def _status_lines(service, instance_id: str) -> tuple[str, dict]:
    dag = service.dag_of(instance_id)
    status = service.status(instance_id)
    rows = [{"id": n["id"], "label": n.get("label", n["id"]),
             "status": status.get(n["id"], "pending"),
             "executor": n.get("executor"), "role": n.get("role"),
             "deps": list(n.get("deps") or [])} for n in dag]
    waiting = service.pending(instance_id)
    stuck = service.blocked(instance_id)
    lines = [f"实例 {instance_id}"]
    lines += [f"  {MARK.get(r['status'], '?')} {r['id']:<16} {r['label']}"
              f"  [{r['executor']}/{r['role']}]  ← {r['deps'] or '—'}" for r in rows]
    lines.append(f"  在等：{[p['node_id'] for p in waiting] or '（没有人在等）'}")
    if stuck:
        lines.append(f"  ⛔ 已停下等人介入：{stuck}"
                     f"（解除：larkflow unblock {instance_id} {stuck[0]} --by <你> --reason <理由>）")
    return "\n".join(lines), {"instance_id": instance_id, "nodes": rows,
                              "status": status, "blocked": stuck,
                              "pending": [p["node_id"] for p in waiting]}


def _pending_lines(items: list[dict], instance_id: str) -> str:
    if not items:
        return f"实例 {instance_id}：没有人在等（全跑完或已停）"
    out = [f"实例 {instance_id}：{len(items)} 项在等"]
    for i, p in enumerate(items, 1):
        kind = "门禁" if p.get("role") == "gate" else "产出"
        out.append(f"  {i}) {p['node_id']}  {p.get('label')}  [{kind}] 派给 {p.get('assignee_role')}")
        if p.get("deliverable_url"):
            out.append(f"       交付物：{p['deliverable_url']}")
        for u in p.get("upstream") or []:
            out.append(f"       待审：{u['label']} {u['url']}")
        for f in p.get("feedback") or []:
            out.append(f"       ⚠ 上一轮被「{f['label']}」打回：{f.get('comment') or '（未留言）'}")
        if p.get("reopen_candidates"):
            out.append(f"       可打回：{p['reopen_candidates']}")
        if p.get("reopen_escalation"):
            out.append(f"       需审批才打得回：{p['reopen_escalation']}")
    return "\n".join(out)


def _by_node(got, node_id: str | None) -> dict:
    """把 escalations / pending_escalations 的返回统一成 {节点: [申请…]}。

    带 node_id 时它们回的是**裸 list**、不带时回 dict（service 的现状）。不在这里收口的话，
    `--node` 一加下游 `.items()` 当场 TypeError，而这条路只有加了 `--node` 才走得到。
    """
    if isinstance(got, dict):
        return {k: list(v) for k, v in got.items() if v}
    return {node_id or "?": list(got)} if got else {}


def _requests_of(records) -> list[dict]:
    """log 里的**申请**。`kind` 缺省即申请（早于一键同意通道的历史记录没有这个字段）。"""
    return [r for r in records if r.get("kind", "request") == "request"]


def _verdict_line(v: dict) -> str:
    """一条裁决挂在它 `ref` 指向的申请下面。"""
    word = {"approved": "同意", "rejected": "驳回"}.get(v.get("verdict"), v.get("verdict"))
    line = f"       ↳ 已由 {v.get('by') or '未知'} {word}（{v.get('at') or '时间不详'}）"
    if v.get("comment"):
        line += f"：{v['comment']}"
    if v.get("reopened"):
        line += f"；已解冻重做 {v['reopened']}"
    return line


def _escalation_lines(groups: dict, instance_id: str, *, whole: bool) -> str:
    """一眼看得出：谁该拍板 / 谁提的 / 要打回谁 / 第几笔 / 为什么 / 后来谁拍的板。

    少任何一样，审批人拿到这份输出都还得再问一轮人，那这条通道就等于没通。

    **两类记录必须分开渲染**：全量 log 里混着申请（`kind` 缺省）与裁决（`kind == "verdict"`），
    后者的字段完全不同（`by` 是拍板人不是申请人、没有 `seq` 只有 `ref`、`comment` 是拍板附言）。
    混着印出来，等于告诉审批人「有一笔 ? 号申请等着你」，而那其实是他自己刚拍完的板。
    """
    total = sum(len(_requests_of(v)) for v in groups.values())
    if not total and not any(v for v in groups.values()):
        return (f"实例 {instance_id}：没有任何打回申请（这张图从没走过跨界打回）" if whole
                else f"实例 {instance_id}：没有待拍板的申请")
    out = [f"实例 {instance_id}：{total} 笔" + ("申请（全量历史，含已作废的）" if whole
                                               else "待拍板的申请")]
    for node_id, records in groups.items():
        verdicts = {v.get("ref"): v for v in records if v.get("kind") == "verdict"}
        for r in _requests_of(records):
            seq = r.get("seq")
            # 只认派生的 effective_status：记录里那个 `status` 字面量冻的是落库那一刻，
            # 追加型 channel 没有 UPDATE，所以它**永远**是 pending，印出来就是误导。
            state = r.get("effective_status") or ("pending" if not whole else "未知")
            out.append(f"  {node_id}  第 {seq} 笔  [{state}]  {r.get('at') or ''}")
            out.append(f"       申请人：{r.get('by') or '未知'}")
            out.append(f"       该谁拍板：{r.get('approvers') or '—'}"
                       + (f"（已通知 {r['notified']}）" if r.get("notified") else ""))
            out.append(f"       要打回：{r.get('escalated') or r.get('targets') or '—'}"
                       + (f"，连累 {r['collateral']} 一起返工" if r.get("collateral") else ""))
            if r.get("comment"):
                out.append(f"       理由：{r['comment']}")
            if r.get("notify_failed"):
                out.append(f"       ⚠ 没通知到：{r['notify_failed']}（他们并不知道有这笔申请）")
            if seq in verdicts:
                out.append(_verdict_line(verdicts.pop(seq)))
        # 剩下的是找不到申请的裁决（历史 / 数据异常）。静默吞掉就是丢一条审计，宁可露出来
        for ref, v in verdicts.items():
            out.append(f"  {node_id}  第 {ref} 笔的裁决（对应的申请不在这份记录里）")
            out.append(_verdict_line(v))
    if not whole:
        out.append(f"  拍板：larkflow approve {instance_id} <节点> --by <你> [--seq N]"
                   f"（否决换 reject）")
    return "\n".join(out)


# 拒绝码 → 一句人话。只给码的话，拿到 `unauthorized_approve` 的人第一反应是「系统坏了」，
# 而这几种全是**正常的业务拒绝**，得当场说清下一步该干什么。
ESCALATION_HINTS = {
    "unauthorized_approve": "你不在这道门的审批人名单里（拍板权在引擎侧算，不看调用方说自己是谁）",
    "self_approve": "自己提的申请不能自己批，得找名单上的另一个人",
    "no_such_escalation": "这道门没有这笔待批申请（编号写错，或它已随新一轮作废）",
    "already_settled": "这笔申请已经拍过板了（同意 / 否决都只算一次）",
    "illegal_reopen": "要打回的目标已经不合法了（图改过 / 目标已重跑），这笔批不下去",
    "missing_audit": "缺审计信息：谁拍的板必须记下来",
    "ambiguous_escalation": "这道门有多笔待批，得用 --seq 指定是哪一笔",
    "stale": "这笔申请已随新一轮作废（门重跑过，旧申请自动失效），不用再拍板",
}


def _settle_lines(ns, result: dict, *, ok: bool) -> str:
    where = (f"实例 {result.get('instance_id') or ns.instance_id} · "
             f"{result.get('node_id') or ns.node_id} · 第 {result.get('seq')} 笔")
    if not ok:
        # 兜底 "unknown_result"：service 回了个读不懂的形状时，「没拍成（None）」看着像 CLI 自己的
        # bug，会把人引到错的方向；给个明确的码 + 原样回显报文，让人一眼看出是上游变了契约
        code = result.get("rejected") or result.get("skipped") or "unknown_result"
        lines = [f"没拍成（{code}）："
                 + ESCALATION_HINTS.get(code, json.dumps(result, ensure_ascii=False))]
        # candidates 是**纯 seq 列表**（引擎侧 `_pick_escalation: [r.get("seq") for r in live]`）。
        # 当 dict 遍历过一版，实测 100% AttributeError 并把 `--json` 的 stdout 打空；
        # 而 stub 喂的正是那个引擎从不产生的形状，于是测试还全绿。别再猜形状。
        for seq in result.get("candidates") or []:
            lines.append(f"    --seq {seq}")
        if result.get("candidates"):
            lines.append(f"    看每一笔是谁提的、要打回什么："
                         f"larkflow escalations {ns.instance_id} --node {ns.node_id}")
        return "\n".join(lines)
    if result.get("approved"):
        return (f"已同意打回（{where}），拍板人 {result.get('by') or ns.by}；"
                f"已解冻重做：{result.get('reopened') or '—'}")
    return f"已否决这笔打回申请（{where}），拍板人 {result.get('by') or ns.by}；图没有任何变化"


def _new_instance_id() -> str:
    """实例 id：UTC+8 时间戳 + 随机尾巴。可排序、可读，且同一秒起两个也不撞。"""
    now = datetime.now(timezone(timedelta(hours=8)))
    return f"lf-{now:%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"


# ---------- 子命令 ----------

_DOCTOR_MARK = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}


def _cmd_doctor(ns, factory, server_factory, checker=run_checks) -> int:
    """只读体检。**不经过 factory**：它要在「服务还装配不起来」的时候也能给出诊断，
    而 `build_real_service` 恰恰会因为角色没配全之类的原因在装配期直接抛。
    """
    checks = checker(db_path=ns.db, template=ns.template, profile=ns.profile)
    lines = [f"{_DOCTOR_MARK[c.level]} {c.name}：{c.detail}"
             + (f"\n     ↳ {c.fix}" if c.fix else "") for c in checks]
    v = verdict(checks)
    tail = {"ok": "可以起 serve 了。", "warn": "能起，但上面几条 ⚠️ 值得先看一眼。",
            "fail": "现在起会坏，先修掉 ❌ 那几条。"}[v]
    _emit(ns, "\n".join(lines) + f"\n\n{tail}",
          {"verdict": v, "checks": [c._asdict() for c in checks]})
    return 1 if v == "fail" else 0


def _cmd_serve(ns, factory, server_factory) -> int:
    """常驻。先抢单例锁：两个 daemon 订同一条事件流会把同一次点击处理两遍。"""
    lock = daemon_lock_for(ns.db)
    try:
        lock.acquire(timeout=0)
    except LockBusy:
        print(f"另一个 larkflow serve 正在跑（同一个 DB {ns.db}）。先停掉它再起。")
        return 1
    try:
        event_keys = list(ns.event_key or DEFAULT_EVENT_KEYS)
        if _target_im_commands_enabled():
            from .workflow.im_commands import IM_MESSAGE_EVENT
            from .workflow.role_bindings import CARD_ACTION_EVENT

            if IM_MESSAGE_EVENT not in event_keys:
                event_keys.append(IM_MESSAGE_EVENT)
            if CARD_ACTION_EVENT not in event_keys:
                event_keys.append(CARD_ACTION_EVENT)
        server = server_factory(factory(ns),
                                event_keys=event_keys,
                                identity=ns.identity, profile=ns.profile,
                                event_observers=_target_event_observers(
                                    identity=ns.identity,
                                    profile=ns.profile,
                                ))
        return server.serve_forever()
    finally:
        lock.release()


def _target_event_observers(*, identity: str = "bot", profile: str | None = None):
    dsn = env("LARKFLOW_TARGET_INBOX_DSN")
    tenant_id = env("LARKFLOW_TARGET_TENANT")
    if not dsn and not tenant_id:
        return ()
    if not dsn or not tenant_id:
        raise RuntimeError(
            "LARKFLOW_TARGET_INBOX_DSN and LARKFLOW_TARGET_TENANT must be set together"
        )
    from .workflow.cli import JsonLogger
    from .workflow.im_commands import (
        HumanDecisionActionInboxBridge,
        IMEventInboxBridge,
        RecoveryActionInboxBridge,
    )
    from .workflow.inbound import TaskEventInboxBridge
    from .workflow.migrate import postgres_connection_factory
    from .workflow.postgres import (
        PostgresIMCommandStore,
        PostgresWorkflowInbox,
        PostgresWorkflowRepository,
    )
    from .workflow.projection import FEISHU_DECISION_CARD_KIND
    from .workflow.role_bindings import RoleBindingActionInboxBridge
    from .io import CliLarkIO
    from .io.cli import run_cli

    connection_factory = postgres_connection_factory(dsn)
    observers = [
        TaskEventInboxBridge(
            PostgresWorkflowInbox(connection_factory),
            tenant_id=tenant_id,
        )
    ]
    if _target_im_commands_enabled():
        im_store = PostgresIMCommandStore(connection_factory)
        projection_store = PostgresWorkflowRepository(connection_factory)

        def resolve_human_decision_binding(
            message_id: str,
        ) -> Mapping[str, object] | None:
            projection = projection_store.get_projection_by_external_id(
                tenant_id,
                FEISHU_DECISION_CARD_KIND,
                message_id,
            )
            if projection is None:
                return None
            binding = projection.state.get("decision_binding")
            return binding if isinstance(binding, Mapping) else None

        card_io = CliLarkIO(
            identity=identity,
            profile=profile,
            runner=partial(run_cli, timeout=3),
        )
        feedback_logger = JsonLogger()
        observers.append(
            IMEventInboxBridge(
                im_store,
                tenant_id=tenant_id,
            )
        )
        observers.append(
            RoleBindingActionInboxBridge(
                im_store,
                tenant_id=tenant_id,
                card_updater=card_io.update_card,
                feedback_reporter=feedback_logger,
            )
        )
        observers.append(
            RecoveryActionInboxBridge(
                im_store,
                tenant_id=tenant_id,
                card_updater=card_io.update_card,
                feedback_reporter=feedback_logger,
            )
        )
        observers.append(
            HumanDecisionActionInboxBridge(
                im_store,
                tenant_id=tenant_id,
                card_updater=card_io.update_card,
                feedback_reporter=feedback_logger,
                decision_binding_resolver=resolve_human_decision_binding,
            )
        )
    return tuple(observers)


def _target_im_commands_enabled() -> bool:
    value = env("LARKFLOW_TARGET_ENABLE_IM_COMMANDS", "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise RuntimeError("LARKFLOW_TARGET_ENABLE_IM_COMMANDS must be a boolean")


def _cmd_start(ns, factory, server_factory) -> int:
    service = factory(ns)
    iid = ns.instance_id or _new_instance_id()
    inputs = dict(ns.inputs or [])
    try:
        service.start(instance_id=iid, reporter=ns.reporter, inputs=inputs, template=ns.template)
    except InstanceExists as exc:
        # 与 status / pending / edit 的 no_such_instance 是同一句口径的反面，同样要自己
        # 认领：落到 main 那条通吃 except 上会变成 internal_error，脚本分不出「我 --id
        # 传重了」和「引擎炸了」，而前者是 --id 唯一的常见手误。
        _emit(ns, str(exc), {"rejected": "instance_exists", "instance_id": iid})
        return 1
    human, payload = _status_lines(service, iid)
    _emit(ns, f"已起实例 {iid}\n{human}", {"started": iid, **payload})
    return 0


def _cmd_status(ns, factory, server_factory) -> int:
    service = factory(ns)
    if not service.dag_of(ns.instance_id):
        _emit(ns, f"实例不存在：{ns.instance_id}", {"rejected": "no_such_instance",
                                                   "instance_id": ns.instance_id})
        return 1
    human, payload = _status_lines(service, ns.instance_id)
    _emit(ns, human, payload)
    return 0


def _cmd_pending(ns, factory, server_factory) -> int:
    service = factory(ns)
    if not service.dag_of(ns.instance_id):
        _emit(ns, f"实例不存在：{ns.instance_id}", {"rejected": "no_such_instance",
                                                   "instance_id": ns.instance_id})
        return 1
    items = service.pending(ns.instance_id, actor=ns.actor)
    _emit(ns, _pending_lines(items, ns.instance_id),
          {"instance_id": ns.instance_id, "actor": ns.actor, "pending": items})
    return 0


# 改图的五种「引擎说不行」。抛的是异常不是结构化拒绝（edit_graph 的现状），故这里逐一
# 认领并翻成拒绝码：落到 main 那条通吃 except 的话只往 stderr 打一行，`--json` 下 stdout
# 是空的，「--json 的 stdout 必须是一个可 json.loads 的对象」当场破，脚本读到空串就崩。
# `UnsupportedInV1` 继承 NotImplementedError，与另外四个类没有任何继承关系，只能单列：
# 它是**默认路径**上的（加一道会签门 / 给节点加 when 守卫都撞它），一漏就是最常见的那条。
_EDIT_REJECTS = ((GraphEditError, "illegal_edit"), (TemplateError, "invalid_graph"),
                 (ExecutorError, "unknown_executor"), (RoleError, "unknown_role"),
                 (UnsupportedInV1, "unsupported_in_v1"))
_EDIT_ERRORS = tuple(t for t, _ in _EDIT_REJECTS)


def _cmd_edit(ns, factory, server_factory) -> int:
    service = factory(ns)
    # 与 status / pending / escalations 同一句口径：打错实例 id 是最常见的手误，报成
    # illegal_edit 会让人以为是自己的 ops 写错了，去改一份本来没问题的报文。
    if not service.dag_of(ns.instance_id):
        _emit(ns, f"实例不存在：{ns.instance_id}", {"rejected": "no_such_instance",
                                                   "instance_id": ns.instance_id})
        return 1
    try:
        result = service.edit_graph(ns.instance_id, ns.ops, by=ns.by, reason=ns.reason)
    except _EDIT_ERRORS as exc:
        code = next(c for t, c in _EDIT_REJECTS if isinstance(exc, t))
        _emit(ns, f"改图被拒（{code}）：{exc}",
              {"rejected": code, "error": str(exc), "instance_id": ns.instance_id, "by": ns.by})
        return 1
    except LockBusy:
        raise                      # 锁争用是可重试的，交给 main 统一标出来，别混进 engine_error
    except Exception as exc:
        # 兜底：认领清单是**会过时**的（引擎长出新异常类型时，上面那份不会自己更新）。
        # 契约「--json 的 stdout 是一个对象」不该跟着一起失效，所以这里无条件兜一层。
        # 措辞不说「被拒」：漏网异常可能是在图**已经写回**之后才抛的（写回与推进不是一步），
        # 那时候说「改图被拒」就是骗人。只说没正常完成，并让人自己去 status 看落没落。
        _emit(ns, f"改图没能正常完成（engine_error）：{type(exc).__name__}: {exc}\n"
                  f"  这次改动到底落没落，以 larkflow status {ns.instance_id} 为准。",
              {"rejected": "engine_error", "error": f"{type(exc).__name__}: {exc}",
               "landed": "unknown", "instance_id": ns.instance_id, "by": ns.by})
        return 1
    # service 有**两条**拒绝出口：校验失败抛异常（上面那段），鉴权 / 缺审计回结构化拒绝。
    # 只认异常那条的话，`unauthorized_edit` 会被当成功打印并退出 0，脚本据此以为图改成了。
    code = result.get("rejected") or result.get("skipped")
    if code:
        _emit(ns, f"改图被拒（{code}）："
                  + (result.get("detail") or result.get("error")
                     or json.dumps(result, ensure_ascii=False)), result)
        return 1
    remapped = result.get("remapped")
    _emit(ns, f"已改图 {ns.instance_id}：{result.get('edited')} 条 ops 生效，"
              f"现在的节点 {result.get('nodes')}"
              + (f"（{remapped} 个挂起节点重新派了单）" if remapped else ""),
          {**result, "instance_id": ns.instance_id, "by": ns.by})
    return 0


def _cmd_escalations(ns, factory, server_factory) -> int:
    service = factory(ns)
    if not service.dag_of(ns.instance_id):
        _emit(ns, f"实例不存在：{ns.instance_id}", {"rejected": "no_such_instance",
                                                   "instance_id": ns.instance_id})
        return 1
    # 默认走 pending：全量历史里有随轮次作废的旧申请，当待办看会让人去拍早就没用的板
    read = service.escalations if ns.all else service.pending_escalations
    groups = _by_node(read(ns.instance_id, ns.node), ns.node)
    # count 只数**申请**：`--all` 的 log 里混着裁决记录，把它们计进去会让「还有几笔要处理」
    # 这个最常被脚本读的数字凭空变大。裁决另计一个数，两者都别丢。
    counts = {"count": sum(len(_requests_of(v)) for v in groups.values()),
              "verdicts": sum(len(v) - len(_requests_of(v)) for v in groups.values())}
    _emit(ns, _escalation_lines(groups, ns.instance_id, whole=ns.all),
          {"instance_id": ns.instance_id, "node_id": ns.node, "all": ns.all,
           **counts, "escalations": groups})
    return 0


def _cmd_settle(ns, factory, *, approve: bool) -> int:
    """approve / reject 共用一条路：两者只差调哪个方法、成功时说哪句话。

    合法性一律在引擎侧算（谁有拍板权、这笔是不是还活着），CLI 不预判、也不重算。
    """
    service = factory(ns)
    call = service.approve_escalation if approve else service.reject_escalation
    result = call(ns.instance_id, ns.node_id, by=ns.by, seq=ns.seq, comment=ns.comment)
    # 正着判成功，别用「没有 rejected 就是成了」：否决一笔申请的成功回执是 rejected_request，
    # 与命令被拒的 rejected 只差一个后缀，反着判就会把拒绝当成功、退出码给 0。
    ok = bool(result.get("approved") or result.get("rejected_request"))
    _emit(ns, _settle_lines(ns, result, ok=ok), result)
    return 0 if ok else 1


def _cmd_approve(ns, factory, server_factory) -> int:
    return _cmd_settle(ns, factory, approve=True)


def _cmd_reject(ns, factory, server_factory) -> int:
    return _cmd_settle(ns, factory, approve=False)


def _cmd_unblock(ns, factory, server_factory) -> int:
    service = factory(ns)
    result = service.unblock(ns.instance_id, ns.node_id, by=ns.by, reason=ns.reason,
                             grant=ns.grant, reopen=ns.reopen)
    ok = "unblocked" in result
    _emit(ns, ("已解除 " if ok else "解除被拒 ") + json.dumps(result, ensure_ascii=False), result)
    return 0 if ok else 1


def _cmd_reconcile(ns, factory, server_factory) -> int:
    service = factory(ns)
    if ns.instance_id:
        if not service.dag_of(ns.instance_id):
            _emit(ns, f"实例不存在：{ns.instance_id}", {"rejected": "no_such_instance",
                                                       "instance_id": ns.instance_id})
            return 1
        result = service.reconcile(ns.instance_id)
        _emit(ns, f"已对账 {ns.instance_id}"
                  + (f"，仍有失败：{result['errors']}" if result.get("errors") else ""), result)
        return 1 if result.get("errors") else 0
    # 省略实例 = 全部：与 serve 启动时走的是同一条路（同一份容错、同一份跳过规则）
    server = LarkFlowServer(service, log=lambda msg: None)
    report = server.startup_reconcile()
    human = [f"实例 {report['instances']} 个"
             f"｜已对账 {report['reconciled']}｜已完成 {report['finished']}"]
    human += [f"  对账失败 {f['instance_id']}: {f['error']}" for f in report["failed"]]
    human += [f"  {iid} 派单仍有失败：{errs}" for iid, errs in report["errors"].items()]
    _emit(ns, "\n".join(human), report)
    return 1 if (report["failed"] or report["errors"]) else 0


HANDLERS = {"doctor": _cmd_doctor, "serve": _cmd_serve, "start": _cmd_start, "status": _cmd_status,
            "pending": _cmd_pending, "edit": _cmd_edit, "escalations": _cmd_escalations,
            "approve": _cmd_approve, "reject": _cmd_reject,
            "unblock": _cmd_unblock, "reconcile": _cmd_reconcile}


def _preload_env(argv) -> None:
    """在 build_parser **之前**加载 `.env`：`--db` 之类的默认值是在建 parser 时从
    环境变量取的，晚一步就读不到。`--env-file` 自己也是命令行参数，故这里先手工扫一遍
    argv（鸡生蛋问题，不值得为它引入两段式解析）。

    绝不让人用 `source .env`：那会走 shell 的引号剥离 / `$` 展开，把 JSON 和含 `$`
    的 key 悄悄改坏（见 config.load_dotenv）。
    """
    path = ".env"
    for i, a in enumerate(argv or []):
        if a == "--env-file" and i + 1 < len(argv):
            path = argv[i + 1]
        elif a.startswith("--env-file="):
            path = a.split("=", 1)[1]
    loaded = load_dotenv(path)
    if loaded.set:
        # 只报键名：这里面全是凭证。人一眼看得出配置到底生效没有。
        print(f"[env] 从 {path} 读入 {len(loaded.set)} 个键：{' '.join(sorted(loaded.set))}",
              file=sys.stderr)
    if loaded.skipped:
        # 这一条比上一条重要：静默的话，「shell 里留着一份被 source 弄坏的值」会表现成
        # 「文件明明配对了却一直报错」，而且完全没有线索指向 shell（实测踩过）。
        print(f"[env] {len(loaded.skipped)} 个键已被环境变量占用、文件里的值**未生效**："
              f"{' '.join(sorted(loaded.skipped))}\n"
              f"      如果是早先 `source .env` 留下的，那些值已被 shell 的引号剥离弄坏，"
              f"请开一个干净终端，或 unset 掉它们。", file=sys.stderr)


def main(argv=None, *, factory=None, server_factory=LarkFlowServer) -> int:
    _preload_env(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not getattr(ns, "cmd", None):
        parser.print_help()
        return 2
    # 绝对化后回显：运维一眼看得出自己连的是哪个库（默认库不随 cwd 漂移，见 store 顶部）
    ns.db = resolve_db_path(getattr(ns, "db", None))
    try:
        return HANDLERS[ns.cmd](ns, factory or _real_service, server_factory)
    except LockBusy as exc:
        # 锁争用与「引擎说不行」是两回事：这条**过一会儿再来就成了**。退出码仍是 1（三档
        # 契约不为它开第四档），但 JSON 里标出 retryable，脚本才分得开该重试还是该报警。
        # edit / approve / reject 是继 unblock 之后第一批写命令，这条路是从它们开始才真的
        # 常走（读命令拿不到锁的窗口小得多）。
        _emit(ns, f"拿不到实例锁：{exc}。另一个 larkflow 进程正在动这个实例，稍后重试即可。",
              {"rejected": "lock_busy", "error": str(exc), "retryable": True,
               "cmd": ns.cmd, "instance_id": getattr(ns, "instance_id", None)})
        return 1
    except Exception as exc:
        # `--json` 下 stdout 必须仍是一个可 json.loads 的对象：只往 stderr 打的话，脚本读到
        # 空串会崩在解析那一行，拿不到任何线索。人类模式保持旧行为（错误走 stderr，
        # 不污染 stdout 管道）。只报类型与消息，绝不把 ns / env 倒出来（里面有凭证）。
        payload = {"rejected": "internal_error", "error": f"{type(exc).__name__}: {exc}",
                   "cmd": ns.cmd}
        if getattr(ns, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"出错了：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
