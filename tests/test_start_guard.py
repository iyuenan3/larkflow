"""`start()` 打到一个已经存在的实例上（ADR-042 锁了前门，这扇后门一直开着）。

v0.7.0 给 `edit_graph` 补了 owner-only + 必署名 + `edits` 审计（ADR-042），理由逐字是
它「能直接删掉一道还在等的门，让流程静默放行」。`start()` 走的是另一条路，能做到**同一
件事**，而这三样防线它一样都没有：

  · `meta` 与 `dag` 是**无 reducer** 的通道（`engine/state.py`），`graph.invoke(state0)`
    对它们是整个替换而不是合并。
  · 于是 `larkflow start --id <既有实例> --reporter <我>` 一条命令就够：owner 换成我、
    项目要素换成我写的、整张图换成我给的（那道门可以干脆不在图里）。
  · `status` / `outputs` 用的是 merge reducer，`{}` 合并等于不动，所以实例**不会看起来
    像被重置**：它带着原来的进度、按新图继续跑到底。这比整个清空更难被发现。
  · `edits` 是空的。改图那条路留痕，这条路不留。

也就是说这不是「重复起实例会覆盖数据」这种手滑级问题，是 ADR-042 那条鉴权的完整旁路，
而 `--id` 就摆在 CLI 上（`__main__.py` 的 start 子命令）。

修法照 `store.py` 顶部那句口径：**宁可失败得响，不要静默覆盖**。两个约束：
  ① 判据复用 `dag_of`，不自己再写一遍（v0.7.0 的教训：同一件事只许有一把尺）。
  ② 必须在实例锁**内**判，否则两个进程同时 start 同一个 id 时两边都看到「不存在」。
"""
from __future__ import annotations

import json

import pytest

from larkflow.app import build_service
from larkflow.service import InstanceExists

DAG = [
    {"id": "draft", "label": "AI 起草", "executor": "llm", "role": "produce", "deps": [],
     "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "gate", "label": "法务复核", "executor": "human", "role": "gate", "deps": ["draft"],
     "assignee_role": "法务", "signal": "card_action", "approval_policy": "single"},
    {"id": "close", "label": "收口", "executor": "tool", "role": "produce", "deps": ["gate"],
     "tool": {"kind": "noop"}},
]

# 攻击者递的图：那道法务门直接不在里面。
NO_GATE = [
    {"id": "draft", "label": "AI 起草", "executor": "llm", "role": "produce", "deps": [],
     "prompt": "写", "model_role": "w", "deliverable": {"region": "whole"}},
    {"id": "close", "label": "收口", "executor": "tool", "role": "produce", "deps": ["draft"],
     "tool": {"kind": "noop"}},
]


def svc_at_gate(iid, **kw):
    """起一个实例，停在那道法务门上（= 真栈里「有人手里正拿着一张卡」的状态）。"""
    svc, io = build_service(DAG, **kw)
    svc.start(instance_id=iid, reporter="ou_owner", inputs={"甲方": "某某"})
    assert [p["node_id"] for p in svc.pending(iid)] == ["gate"]
    return svc, io


# ---------- 守卫本身 ----------

def test_starting_onto_a_live_instance_is_refused():
    svc, io = svc_at_gate("g-1")
    with pytest.raises(InstanceExists) as e:
        svc.start(instance_id="g-1", reporter="ou_attacker", inputs={}, template=NO_GATE)
    assert "g-1" in str(e.value)


def test_the_gate_that_someone_is_holding_a_card_for_is_still_there():
    """ADR-042 真正在乎的那件事：一道还在等的门不许被静默拿掉。

    修之前这里是 `close` 已经 done、实例跑完，而人手里那张卡指着一个不在图里的节点。
    """
    svc, io = svc_at_gate("g-2")
    with pytest.raises(InstanceExists):
        svc.start(instance_id="g-2", reporter="ou_attacker", inputs={}, template=NO_GATE)
    assert [n["id"] for n in svc.dag_of("g-2")] == ["draft", "gate", "close"]
    assert [p["node_id"] for p in svc.pending("g-2")] == ["gate"]
    assert svc.status("g-2").get("close") is None, "那道门还在等，收口不该已经发生"


def test_the_owner_does_not_change_hands():
    svc, io = svc_at_gate("g-3")
    with pytest.raises(InstanceExists):
        svc.start(instance_id="g-3", reporter="ou_attacker", inputs={"甲方": "我说了算"},
                  template=NO_GATE)
    meta = svc._values("g-3")["meta"]
    assert meta["reporter"] == "ou_owner"
    assert meta["inputs"] == {"甲方": "某某"}, "项目要素也不许被顺手改掉"


def test_nothing_leaves_the_engine_before_the_refusal():
    """拒绝必须发生在任何外部动作之前：半执行的 start 比不执行更难收拾。

    （若守卫落在 `graph.invoke` 之后，卡已经发出去了，撤不回来。）
    """
    svc, io = svc_at_gate("g-4")
    cards, tasks = len(io.cards), len(getattr(io, "tasks", {}))
    with pytest.raises(InstanceExists):
        svc.start(instance_id="g-4", reporter="ou_attacker", inputs={}, template=NO_GATE)
    assert len(io.cards) == cards
    assert len(getattr(io, "tasks", {})) == tasks


def test_a_refused_start_leaves_no_audit_because_nothing_happened():
    """对照 `unblock` / `edit_graph`：**做成了**才留痕。这里什么都没做，不该造记录。"""
    svc, io = svc_at_gate("g-5")
    with pytest.raises(InstanceExists):
        svc.start(instance_id="g-5", reporter="ou_attacker", inputs={}, template=NO_GATE)
    assert svc.edit_log("g-5") == []


def test_an_id_nobody_used_still_starts_normally():
    """守卫不许误伤正路：这是唯一一条起实例的入口。"""
    svc, io = build_service(DAG)
    assert svc.start(instance_id="fresh-1", reporter="ou_owner", inputs={}) == "fresh-1"
    assert [p["node_id"] for p in svc.pending("fresh-1")] == ["gate"]


def test_a_finished_instance_is_still_an_instance():
    """跑完了也不等于这个 id 空出来了：历史是历史，不许被新实例覆写掉。"""
    svc, io = build_service([DAG[0]])
    svc.start(instance_id="done-1", reporter="ou_owner", inputs={})
    assert svc.status("done-1") == {"draft": "done"}
    with pytest.raises(InstanceExists):
        svc.start(instance_id="done-1", reporter="ou_attacker", inputs={})


# ---------- 两条实现约束（这两条挂掉 = 守卫在特定时序 / 特定改写下会失效） ----------

class RecordingLock:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self.log.append("enter")
        return self

    def __exit__(self, *a):
        self.log.append("exit")
        return False


def test_the_check_happens_inside_the_instance_lock():
    """判在锁外 = 两个进程同时 start 同一个 id 时两边都看到「不存在」，守卫形同虚设。

    真栈上这把锁是跨进程 flock（ADR-031），而 `larkflow start` 是一次性命令，两个人
    同时敲不是假想。
    """
    log: list[str] = []
    svc, io = svc_at_gate("g-lock", lock_factory=lambda iid: RecordingLock(log))
    log.clear()
    with pytest.raises(InstanceExists):
        svc.start(instance_id="g-lock", reporter="ou_attacker", inputs={})
    assert log == ["enter", "exit"], "拒绝这条路也必须是持着锁判的"


def test_the_check_reads_through_dag_of_not_a_second_ruler(monkeypatch):
    """「实例存不存在」只许有一把尺。

    CLI 的 status / pending / edit 一律用 `dag_of` 判 `no_such_instance`；守卫要是自己
    另写一个判据（比如读 `meta`），两把尺迟早分叉：一个 id 会同时是「查无此实例」和
    「已存在，不许起」。v0.7.0 那条教训（活性判据写了三处、漏改一处）就是这么来的。
    """
    svc, io = svc_at_gate("g-ruler")
    seen: list[str] = []
    real = svc.dag_of
    monkeypatch.setattr(svc, "dag_of", lambda iid: (seen.append(iid), real(iid))[1])
    with pytest.raises(InstanceExists):
        svc.start(instance_id="g-ruler", reporter="ou_attacker", inputs={})
    assert "g-ruler" in seen


# ---------- CLI ----------

def cli(argv, svc, **kw):
    from larkflow.__main__ import main
    return main(argv, factory=lambda ns: svc, **kw)


def test_cli_start_on_a_taken_id_rejects_with_a_code_not_a_traceback(capsys, tmp_path):
    svc, io = svc_at_gate("cli-dup")
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "start", "--reporter", "ou_attacker",
              "--id", "cli-dup"], svc)
    out = capsys.readouterr().out
    assert rc == 1
    assert "cli-dup" in out and "已存在" in out


def test_cli_start_rejection_keeps_json_stdout_parseable(capsys, tmp_path):
    """`--json` 的 stdout 必须仍是一个可 json.loads 的对象（v0.7.0 CLI 那批 finding 的口径）。

    落到 main 那条通吃 except 上的话拒绝码会变成 `internal_error`，脚本分不出「这是我
    自己传错了」还是「引擎炸了」。
    """
    svc, io = svc_at_gate("cli-dup-json")
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "--json", "start",
              "--reporter", "ou_attacker", "--id", "cli-dup-json"], svc)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["rejected"] == "instance_exists"
    assert payload["instance_id"] == "cli-dup-json"


def test_cli_start_still_works_on_a_fresh_id(capsys, tmp_path):
    svc, io = build_service(DAG)
    rc = cli(["--db", str(tmp_path / "db.sqlite"), "start", "--reporter", "ou_owner",
              "--id", "cli-fresh"], svc)
    assert rc == 0 and "cli-fresh" in capsys.readouterr().out
