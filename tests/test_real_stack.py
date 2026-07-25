"""真实栈（lark-cli + 多角色 LLM）的可测部分：argv 拼装、JSON 信封、env 装配。

**不碰网络、不起子进程**：runner 注入替身。真 e2e 需要 dev 飞书应用 + 事件回调（ADR-008 /
DEPLOYMENT），停在那之前。这里钉死的是「flag 与返回字段按内嵌 skill 核对过」这件事，
以及那条最容易翻车的判据：成功只看 ok==true，绝不看 code==0。
"""
import sqlite3

import pytest

from larkflow.config import RoleResolver, load_llm_roles
from larkflow.io.cli import CONFIRMATION_EXIT, LarkCliError, parse_result
from larkflow.io.correlations import IdemStore
from larkflow.io.deliverable import CliDeliverableIO, Deliverable, md_name
from larkflow.io.lark_io import Button, CliLarkIO


class FakeRunner:
    """记录 argv / stdin，按预置返回 data。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, *, stdin=None, timeout=120):
        self.calls.append({"argv": argv, "stdin": stdin})
        return self.results.pop(0) if self.results else {}

    def argv(self, nth=-1) -> list[str]:
        return self.calls[nth]["argv"]

    def flag(self, name, nth=-1):
        argv = self.argv(nth)
        return argv[argv.index(name) + 1] if name in argv else None


# ---------- JSON 信封 ----------

def test_success_envelope_returns_data():
    assert parse_result(["lark-cli"], 0, '{"ok":true,"data":{"guid":"g1"}}', "") == {"guid": "g1"}


def test_legacy_code_zero_shape_is_not_treated_as_success():
    """老格式 {"code":0,"msg":"ok"} 没有 ok=true：误判成功会绕过幂等、重复创建。"""
    with pytest.raises(LarkCliError, match="非成功信封"):
        parse_result(["lark-cli"], 0, '{"code":0,"msg":"ok"}', "")


def test_error_envelope_surfaces_type_and_hint():
    err = ('{"ok":false,"error":{"type":"authorization","subtype":"missing_scope",'
           '"message":"no scope","hint":"run auth login"}}')
    with pytest.raises(LarkCliError, match="missing_scope.*no scope.*auth login"):
        parse_result(["lark-cli", "task", "+create"], 1, "", err)


def test_confirmation_exit_is_not_auto_confirmed():
    err = ('{"ok":false,"error":{"type":"confirmation","subtype":"confirmation_required",'
           '"risk":"high-risk-write","action":"drive +delete"}}')
    with pytest.raises(LarkCliError, match="人工确认"):
        parse_result(["lark-cli"], CONFIRMATION_EXIT, "", err)


# ---------- lark-cli 任务 / 卡片 ----------

def test_create_task_argv_and_guid():
    r = FakeRunner({"guid": "task-guid-1", "url": "https://x"})
    guid = CliLarkIO(runner=r).create_task(assignee="ou_a", summary="定稿", description="d",
                                           idem_key="k1")
    assert guid == "task-guid-1"                       # data.guid，不是 data.task.guid
    assert r.argv()[:3] == ["lark-cli", "task", "+create"]
    assert r.flag("--assignee") == "ou_a" and r.flag("--idempotency-key") == "k1"
    assert r.flag("--as") == "bot" and "--json" in r.argv()


def test_send_card_argv_and_message_id():
    r = FakeRunner({"message_id": "om_1"})
    msg = CliLarkIO(runner=r).send_card(target="ou_a", summary="审核",
                                        buttons=[Button("通过", {"verdict": "pass"})],
                                        idem_key="k2")
    assert msg == "om_1"                               # data.message_id
    assert r.flag("--user-id") == "ou_a" and r.flag("--msg-type") == "interactive"
    assert "打回" not in r.flag("--content")
    assert '"callback"' in r.flag("--content")         # 按钮回调塞的是自描述 action_value


def test_send_card_to_group_uses_chat_id():
    r = FakeRunner({"message_id": "om_2"})
    CliLarkIO(runner=r).send_card(target="oc_group", summary="s", buttons=[], idem_key="k")
    assert r.flag("--chat-id") == "oc_group" and "--user-id" not in r.argv()


# ---------- 交付物（markdown） ----------

def test_create_deliverable_argv_uses_stdin_and_md_suffix():
    r = FakeRunner({"file_token": "boxcn1", "url": "https://f/boxcn1"})
    io = CliDeliverableIO(runner=r, folder_token="fldcn_x")
    h = io.create(title="商务条款起草", content="# 正文", idem_key="wf-1:biz:create")

    assert h == Deliverable(type="markdown", token="boxcn1", url="https://f/boxcn1", region="whole")
    assert r.argv()[:3] == ["lark-cli", "markdown", "+create"]
    assert r.flag("--name") == "商务条款起草.md"        # 必须显式带 .md
    assert r.flag("--content") == "-" and r.calls[-1]["stdin"] == "# 正文"   # 正文走 stdin
    assert r.flag("--folder-token") == "fldcn_x"


def test_create_is_idempotent_through_local_store():
    """markdown +create 没有 --idempotency-key：崩溃重跑不能多建一份文档。"""
    conn = sqlite3.connect(":memory:")
    r = FakeRunner({"file_token": "boxcn1"}, {"file_token": "boxcn2"})
    io = CliDeliverableIO(runner=r, idem_store=IdemStore(conn))

    first = io.create(title="t", content="a", idem_key="same")
    second = io.create(title="t", content="a", idem_key="same")

    assert first.token == second.token == "boxcn1"
    assert len(r.calls) == 1                            # 第二次根本没出网


def test_overwrite_keeps_handle_and_fetch_reads_content():
    r = FakeRunner({"version": 2}, {"content": "远端正文"})
    io = CliDeliverableIO(runner=r)
    h = Deliverable(type="markdown", token="boxcn1", url="u", region="whole")

    assert io.overwrite(h, content="新正文") == h       # handle 不变，版本靠飞书原生
    assert r.flag("--file-token") == "boxcn1" and r.calls[-1]["stdin"] == "新正文"
    assert io.fetch(h) == "远端正文"
    assert r.argv()[:3] == ["lark-cli", "markdown", "+fetch"]


def test_md_name_is_sanitized():
    assert md_name("合同/初稿") == "合同_初稿.md"
    assert md_name("已带后缀.md") == "已带后缀.md"
    assert md_name("  ") == "deliverable.md"


def test_section_region_still_refused_on_real_stack():
    with pytest.raises(NotImplementedError, match="section"):
        CliDeliverableIO(runner=FakeRunner()).create(
            title="t", content="x", region={"section": "第三条"}, idem_key="k")


# ---------- 多角色 env 装配 ----------

def test_load_llm_roles_by_prefix():
    roles = load_llm_roles({
        "LLM_BASE_URL": "https://ark", "LLM_API_KEY": "k0", "LLM_MODEL": "m0",
        "LLM_WRITER_BASE_URL": "https://relay", "LLM_WRITER_API_KEY": "k1",
        "LLM_WRITER_MODEL": "m1",
        "LLM_LEGAL_BASE_URL": "https://direct", "LLM_LEGAL_MODEL": "m2",  # 缺 key → 跳过
        "UNRELATED": "x",
    })
    assert set(roles) == {"default", "writer"}
    assert roles["default"] == {"base_url": "https://ark", "api_key": "k0", "model": "m0"}
    assert roles["writer"]["model"] == "m1"


def test_role_resolver_from_env():
    r = RoleResolver.from_env({"LARKFLOW_ROLE_财务": "ou_fin", "OTHER": "x"})
    assert r.resolve("财务") == "ou_fin"
    assert r.resolve("法务") == "ou_法务"        # 未配置时回退，本地可跑
