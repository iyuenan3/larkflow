"""larkflow CLI：真栈的唯一入口（`larkflow <子命令>` 或 `python -m larkflow`）。

    larkflow serve                       常驻：启动对账 + 起事件泵 + block（唯一的守护进程）
    larkflow start --template contract --reporter ou_xxx --input 甲方=某某
    larkflow status <实例>  /  pending <实例> [--actor ou_xxx]
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
import json
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .config import env, load_dotenv
from .serve import DEFAULT_EVENT_KEYS, LarkFlowServer
from .store import DEFAULT_LOCK_TIMEOUT, LockBusy, daemon_lock_for, resolve_db_path

MARK = {"done": "✅", "failed": "❌", "blocked": "⛔", "pending": "…", "skipped": "⊘"}


def _kv(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"要素得写成 k=v，收到的是 {raw!r}")
    k, v = raw.split("=", 1)
    if not k.strip():
        raise argparse.ArgumentTypeError(f"要素名不能为空：{raw!r}")
    return k.strip(), v


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
    subs = ap.add_subparsers(dest="cmd", metavar="{serve,start,status,pending,unblock,reconcile}")

    class sub:                      # 每个子命令都带上 common，少写一遍 parents=
        add_parser = staticmethod(
            lambda name, **kw: subs.add_parser(name, parents=[common], **kw))

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


def _new_instance_id() -> str:
    """实例 id：UTC+8 时间戳 + 随机尾巴。可排序、可读，且同一秒起两个也不撞。"""
    now = datetime.now(timezone(timedelta(hours=8)))
    return f"lf-{now:%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"


# ---------- 子命令 ----------

def _cmd_serve(ns, factory, server_factory) -> int:
    """常驻。先抢单例锁：两个 daemon 订同一条事件流会把同一次点击处理两遍。"""
    lock = daemon_lock_for(ns.db)
    try:
        lock.acquire(timeout=0)
    except LockBusy:
        print(f"另一个 larkflow serve 正在跑（同一个 DB {ns.db}）。先停掉它再起。")
        return 1
    try:
        server = server_factory(factory(ns),
                                event_keys=ns.event_key or list(DEFAULT_EVENT_KEYS),
                                identity=ns.identity, profile=ns.profile)
        return server.serve_forever()
    finally:
        lock.release()


def _cmd_start(ns, factory, server_factory) -> int:
    service = factory(ns)
    iid = ns.instance_id or _new_instance_id()
    inputs = dict(ns.inputs or [])
    service.start(instance_id=iid, reporter=ns.reporter, inputs=inputs, template=ns.template)
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


HANDLERS = {"serve": _cmd_serve, "start": _cmd_start, "status": _cmd_status,
            "pending": _cmd_pending, "unblock": _cmd_unblock, "reconcile": _cmd_reconcile}


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
    keys = load_dotenv(path)
    if keys:
        # 只报键名：这里面全是凭证。人一眼看得出配置到底生效没有。
        print(f"[env] 从 {path} 读入 {len(keys)} 个键：{' '.join(sorted(keys))}",
              file=sys.stderr)


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
        print(f"拿不到实例锁：{exc}")
        return 1
    except Exception as exc:
        print(f"出错了：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
