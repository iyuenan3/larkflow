"""第二轮对抗 review 里**没进验伪预算**、事后自查发现属实的那批。

共同的坏味道有两种：
  ① **权威 state 里记了没发生的事**（假审计）：`notified` 在通知真发出去之前就写死。
  ② **失败被静默吞掉**：停机信号在对账循环里不看、排空超时照样关库、退出码照样 0。
两者都不丢数据、都不报错，所以测试全绿、真栈上却让人查无可查。
"""
from __future__ import annotations

import threading

from larkflow.app import build_service
from larkflow.config import RoleResolver
from larkflow.io.events import CARD_ACTION, TASK_UPDATE
from larkflow.serve import LarkFlowServer
from larkflow.store import InstanceLocks, resolve_db_path


# ---------- 图 ----------

def two_gates_over_one_draft() -> list[dict]:
    """一份 AI 稿 + 两道门：一道机检（预算 1，必卡死）、一道人复核。

    两道门共享上游 `a`，于是「人打回 a」会把已经 blocked 的机检门也一起重置回前沿。
    """
    return [
        {"id": "a", "label": "AI 起草", "executor": "llm", "role": "produce", "deps": [],
         "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
        {"id": "chk", "label": "机检", "executor": "tool", "role": "gate", "deps": ["a"],
         "approval_policy": "auto", "reopen_budget": 1,
         "tool": {"kind": "format_check", "args": {"required": ["价款"]}}},
        {"id": "h", "label": "人复核", "executor": "human", "role": "gate", "deps": ["a"],
         "assignee_role": "法务", "signal": "card_action", "approval_policy": "single"},
        {"id": "end", "label": "收口", "executor": "tool", "role": "produce",
         "deps": ["chk", "h"], "tool": {"kind": "noop"}},
    ]


def cross_lane() -> list[dict]:
    """两条并行支汇到乙的门：乙打回共同上游 `a`，重算集会把**丙**的活一起卷进去。

    注意不能只放一条支：那样「乙打回 a」= ADR-023 ② 的「最多回到上一个人工节点」，
    本来就允许、不走 escalation（第一版这张图就是这么写错的）。要连累的必须是**第三方**。
    """
    return [
        {"id": "a", "label": "共同上游", "executor": "human", "role": "produce", "deps": [],
         "assignee_role": "甲", "signal": "task_complete", "deliverable": {"region": "whole"}},
        {"id": "b", "label": "乙的活", "executor": "human", "role": "produce", "deps": ["a"],
         "assignee_role": "乙", "signal": "task_complete", "deliverable": {"region": "whole"}},
        {"id": "c", "label": "丙的活", "executor": "human", "role": "produce", "deps": ["a"],
         "assignee_role": "丙", "signal": "task_complete", "deliverable": {"region": "whole"}},
        {"id": "g", "label": "乙把关", "executor": "human", "role": "gate", "deps": ["b", "c"],
         "assignee_role": "乙", "signal": "card_action", "approval_policy": "single"},
    ]


ROLES = RoleResolver({"法务": "ou_falv", "甲": "ou_jia", "乙": "ou_yi", "丙": "ou_bing"})


def click(svc, io, nid, label, who, **extra):
    av = dict(io.button_value(nid, label))
    av.update(extra)
    return svc.resume_from_event(
        {"key": CARD_ACTION, "action_value": av, "operator_id": who})


# ---------- ① 假审计：notified 得是「真发出去了的」 ----------

def test_escalation_records_only_the_approvers_it_really_reached():
    """通知失败时，权威 state 不许留下「已通知」的假记录。

    审批人隔天来查「谁该拍板」，系统说通知过了、人却从没收到，这条审计记录比没有更坏：
    它会让人不再去追。`escalations` 是追加型 channel，写下去就改不了，所以顺序只能是
    「先发、后记」，记的是**发生过的事**。
    """
    svc, io = build_service(cross_lane(), resolver=ROLES)
    svc.start(instance_id="i1", reporter="ou_owner", inputs={})
    for _ in range(6):                       # 甲、乙、丙各自把活干完，走到乙把关那道门
        items = [p for p in svc.pending("i1") if p.get("signal") == "task_complete"]
        if not items:
            break
        p = items[0]
        guid = next(t["guid"] for t in reversed(list(io.tasks.values()))
                    if t["summary"] == p.get("label"))
        svc.resume_from_event({"key": TASK_UPDATE, "event": {
            "task_guid": guid, "event_types": ["task_completed_update"]}})

    # 审批人收到的是**审批卡**（ADR-043），所以失败要注在发卡这一路上；
    # 申请人的回执仍走 notify，别把那条也弄挂了。
    real_card = io.send_card
    io.send_card = lambda *, target, summary, buttons, idem_key: (
        (_ for _ in ()).throw(RuntimeError("飞书挂了")) if target == "ou_owner"
        else real_card(target=target, summary=summary, buttons=buttons, idem_key=idem_key))

    out = click(svc, io, "g", "打回", "ou_yi", reopen=["a"], comment="重来")
    assert out.get("escalated") == ["a"], out

    log = svc.escalations("i1")["g"]
    assert len(log) == 1
    rec = log[0]
    assert "ou_owner" not in (rec["notified"] or []), "通知失败的人不许出现在 notified 里"
    assert "ou_owner" in (rec.get("notify_failed") or []), "失败得留痕，不能悄悄消失"
    assert rec["approvers"], "approvers 存的是令牌，与投影是否送达无关，照旧要有"


def test_escalation_notified_holds_everyone_when_delivery_works():
    svc, io = build_service(cross_lane(), resolver=ROLES)
    svc.start(instance_id="i1", reporter="ou_owner", inputs={})
    for _ in range(6):
        items = [p for p in svc.pending("i1") if p.get("signal") == "task_complete"]
        if not items:
            break
        guid = next(t["guid"] for t in reversed(list(io.tasks.values()))
                    if t["summary"] == items[0].get("label"))
        svc.resume_from_event({"key": TASK_UPDATE, "event": {
            "task_guid": guid, "event_types": ["task_completed_update"]}})
    click(svc, io, "g", "打回", "ou_yi", reopen=["a"], comment="重来")
    rec = svc.escalations("i1")["g"][0]
    assert set(rec["notified"]) == {"ou_owner", "ou_jia"}
    assert "notify_failed" not in rec


# ---------- ② blocked 再次卡死要再喊人 ----------

def test_a_gate_that_blocks_again_in_a_new_round_tells_the_owner_again():
    """`blocked` 不是真终态：别的门打回共同祖先，就能把它不经解除地拖回前沿再跑一次。

    重跑仍不过 → 再次 blocked。旧幂等键只含「已解除次数」，而这条路一次解除都没花，
    于是第二次卡死被幂等表吞掉、发起人一无所知（改到本地永久幂等表之后是彻底静默）。
    判别式得用**轮次**：换了一轮就是一件新事。
    """
    svc, io = build_service(two_gates_over_one_draft(), resolver=ROLES)
    svc.start(instance_id="i1", reporter="ou_owner", inputs={})
    assert svc.blocked("i1") == ["chk"]
    first = [n for n in io.notifications if "机检" in (n.get("text") or "")]
    assert len(first) == 1

    out = click(svc, io, "h", "打回", "ou_falv", reopen=["a"], comment="重写")
    assert out.get("resumed"), out
    assert svc.blocked("i1") == ["chk"], "重跑仍不过，应当再次 blocked"

    again = [n for n in io.notifications if "机检" in (n.get("text") or "")]
    assert len(again) == 2, "新一轮又卡死了，发起人必须再被喊一次"


# ---------- ③ unblock 要么成，要么不花额度 ----------

def test_a_transient_failure_during_unblock_does_not_burn_the_grant():
    """解除的额度只有 3 次、且不可退。重试期间 LLM / 飞书抽一下就吃掉一次，很快就没了。

    额度是**为人准备的**，不该被基础设施的抖动花掉。审计仍然只追加（补一条退款记录），
    历史一条不改。
    """
    svc, io = build_service(two_gates_over_one_draft(), resolver=ROLES)
    svc.start(instance_id="i1", reporter="ou_owner", inputs={})
    assert svc.blocked("i1") == ["chk"]

    boom = {"n": 0}
    real_advance = svc._advance

    def flaky(instance_id, *a, **kw):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("LLM 502")
        return real_advance(instance_id, *a, **kw)

    svc._advance = flaky
    out = svc.unblock("i1", "chk", by="ou_owner", reason="改了要素", grant=1)
    svc._advance = real_advance
    assert out.get("rejected") == "unblock_failed", out
    assert out.get("refunded") is True

    log = svc.unblock_log("i1", "chk")
    assert any(r.get("refund") for r in log), "退款也是审计事件，要留痕"

    # 额度没被吃掉：三次真解除仍然做得完
    for i in range(3):
        r = svc.unblock("i1", "chk", by="ou_owner", reason=f"第 {i + 1} 次", grant=1)
        assert r.get("unblocked") == "chk", r
    assert svc.unblock("i1", "chk", by="ou_owner",
                       reason="第 4 次").get("rejected") == "unblock_exhausted"


# ---------- ④ DB 路径 ----------

def test_the_db_path_is_absolute_so_daemon_and_cli_cannot_drift_apart(tmp_path, monkeypatch):
    """默认路径是 cwd 相对的话，systemd 起的 daemon（WorkingDirectory=/）与你在 home
    敲的救场命令会**静默**落到两个不同的库：两边都「正常」，只是各看各的实例。
    """
    monkeypatch.delenv("LARKFLOW_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    a = resolve_db_path(None)
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    b = resolve_db_path(None)
    assert a == b, "默认库不许随 cwd 漂移"
    assert a.startswith("/"), a
    assert resolve_db_path("rel.sqlite").startswith("/"), "相对路径也要落成绝对路径"
    assert resolve_db_path("~/x.sqlite").startswith("/")


# ---------- ⑤ CLI 全局参数顺序 ----------

def test_global_flags_are_accepted_on_either_side_of_the_subcommand():
    """`larkflow status i1 --json` 是所有人的第一反应，argparse 默认会 exit 2。"""
    from larkflow.__main__ import build_parser
    p = build_parser()
    before = p.parse_args(["--json", "status", "i1"])
    after = p.parse_args(["status", "i1", "--json"])
    assert before.json and after.json
    assert p.parse_args(["--db", "/tmp/a.sqlite", "status", "i1"]).db == "/tmp/a.sqlite"
    assert p.parse_args(["status", "i1", "--db", "/tmp/a.sqlite"]).db == "/tmp/a.sqlite"


def test_a_subcommand_without_the_flag_does_not_wipe_the_global_one():
    """argparse 的经典坑：子解析器的默认值会把顶层已解析的值覆盖成 None。"""
    from larkflow.__main__ import build_parser
    ns = build_parser().parse_args(["--db", "/tmp/a.sqlite", "--json", "pending", "i1"])
    assert ns.db == "/tmp/a.sqlite" and ns.json is True


# ---------- ⑥ 停机信号 ----------

class _Svc:
    """只够 LarkFlowServer 用的替身。"""

    def __init__(self, ids, on_reconcile=None):
        self._ids = ids
        self.seen: list[str] = []
        self.on_reconcile = on_reconcile
        self.graph = object()

    def finished(self, iid):
        return False

    def reconcile(self, iid):
        self.seen.append(iid)
        if self.on_reconcile:
            self.on_reconcile(self, iid)
        return {"reconciled": iid, "errors": []}


def test_a_stop_signal_during_startup_reconcile_aborts_the_sweep(monkeypatch):
    """对账要挨个实例做 IO。收到 SIGTERM 后还把剩下几百个跑完，等于「停不下来」，
    而且停之前还会把泵起起来又立刻停掉。
    """
    svc = _Svc([f"i{n}" for n in range(10)])
    server = LarkFlowServer(svc, pump_factory=lambda *a, **kw: None, signals=None)
    monkeypatch.setattr("larkflow.serve.list_instances", lambda graph: (svc._ids, None))
    svc.on_reconcile = lambda s, iid: server._on_signal(15, None) if iid == "i2" else None

    report = server.startup_reconcile()
    assert len(svc.seen) < 10, "收到停机信号后不该把剩下的实例跑完"
    assert report.get("aborted") is True
    assert report["pending"], "没轮到的实例要报出来，别让人以为全对过账了"


def test_serve_forever_does_not_start_the_pump_if_it_was_told_to_stop(monkeypatch):
    started = []
    svc = _Svc(["i0"])
    server = LarkFlowServer(svc, pump_factory=lambda *a, **kw: started.append(1) or _StuckPump(),
                            signals=None)
    monkeypatch.setattr("larkflow.serve.list_instances", lambda graph: (svc._ids, None))
    svc.on_reconcile = lambda s, iid: server._on_signal(15, None)
    assert server.serve_forever() == 0
    assert not started, "已经要停了就别再起泵"


# ---------- ⑦ 排空超时 ----------

class _StuckPump:
    def __init__(self, *a, **kw):
        pass

    def start(self, keys):
        pass

    def stop(self):
        pass

    def join(self, timeout=None):
        return False        # 没排空：还有事件在飞


def test_the_db_stays_open_when_the_drain_times_out():
    """在飞的那条事件可能正握着实例锁写 checkpointer。join 超时了还去关连接，
    等于把桌子从人手底下抽走；而且 server 自认 errors=0、退出码 0，运维查不出所以然。
    """
    closed = []

    class _Conn:
        def close(self):
            closed.append(1)

    class _Owner:
        conn = _Conn()

    svc = _Svc(["i0"])
    svc.graph = type("G", (), {"checkpointer": _Owner()})()
    svc.corr = _Owner()
    server = LarkFlowServer(svc, pump_factory=_StuckPump, signals=None)
    server.start()
    server.stop(timeout=0.01)
    assert not closed, "没排空就不许关库"
    assert server.stats["errors"] >= 1
    assert any("drain" in e["where"] for e in server.errors)


def test_serve_forever_reports_a_dirty_exit(monkeypatch):
    svc = _Svc(["i0"])
    server = LarkFlowServer(svc, pump_factory=_StuckPump, signals=None)
    monkeypatch.setattr("larkflow.serve.list_instances", lambda graph: (svc._ids, None))
    threading.Timer(0.05, lambda: server._on_signal(15, None)).start()
    assert server.serve_forever(drain_timeout=0.01) == 1, "没干净收工就不该报 exit 0"


# ---------- ⑧ 锁文件路径逃逸 ----------

def test_lock_paths_cannot_escape_the_lock_dir(tmp_path):
    """instance_id 是外部输入（CLI 参数 / 飞书事件里的 thread_id）。"""
    locks = InstanceLocks.for_db(str(tmp_path / "db.sqlite"))
    root = tmp_path / "db.sqlite.locks"
    for evil in ("../../etc/passwd", "..", "/abs/path", "a/b/c", "..\\win", ""):
        p = locks.path_for(evil)
        assert p.startswith(str(root) + "/"), (evil, p)
        assert "/../" not in p and not p.endswith("/..")
    assert locks.path_for("x") != locks.path_for("y")
    assert locks.path_for("../x") != locks.path_for("x"), "消毒后不许把两个 id 撞成一个"


def test_lock_dir_sits_next_to_the_db(tmp_path):
    locks = InstanceLocks.for_db(str(tmp_path / "sub" / "db.sqlite"))
    assert locks.path_for("i1").startswith(str(tmp_path / "sub" / "db.sqlite.locks") + "/")
