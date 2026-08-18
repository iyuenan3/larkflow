"""`larkflow doctor`：起服务之前把能在本机查的问题一次查完。

存在的理由是部署这件事今天没有可重复流程（ADR-007 的宿主至今没真部署过），而到客户现场
现调试等于把第一印象赌在运气上。doctor 要在**服务还装配不起来**的时候也能说清为什么。

三条设计约束，各自有测试钉着：
  ① 只读：全程不建文档、不发消息（`test_doctor_never_runs_a_write_command`）。
  ② 绝不打印凭证（`test_the_llm_api_key_never_appears_in_the_output`）。
  ③ warn 不挡部署，只有 fail 挡（否则人会养成忽略 warn 的习惯）。

全程 Stub：`runner` 与 `environ` 都注入，绝不构造 `build_real_service`（红线）。
"""
from __future__ import annotations

import json

import pytest

from larkflow.doctor import FAIL, OK, WARN, run_checks, verdict

APP = "cli_realapp0001"

# lark-cli 的自省命令成功时吐**裸对象**，不是 {ok,data} 信封（实测，见 doctor 模块 docstring）
AUTH_OK = json.dumps({"appId": APP, "brand": "feishu",
                      "identities": {"bot": {"status": "ready", "message": "Bot identity: ready"}}})
EVENT_IDLE = json.dumps({"apps": [{"app_id": APP, "status": "not_running", "running": False}]})

GOOD_ENV = {
    "LARKFLOW_ROLES": json.dumps({"法务": "ou_a", "财务": "ou_b", "负责人": "ou_c"}),
    "LLM_BASE_URL": "https://ark.example.com/api/v3",
    "LLM_API_KEY": "sk-this-must-never-be-printed",
    "LLM_MODEL": "doubao-pro",
    "LARKFLOW_DELIVERABLE_FOLDER_TOKEN": "fld_x",
    "LARKFLOW_APP_ID": APP,
}


class Runner:
    """记下每一条被跑过的 argv，好断言 doctor 真的只读。"""

    def __init__(self, replies: dict[str, tuple[int, str, str]] | None = None):
        self.seen: list[list[str]] = []
        self.replies = replies or {}

    def __call__(self, argv, **kw):
        self.seen.append(list(argv))
        for key, rv in self.replies.items():
            if key in " ".join(argv):
                return rv
        if "--version" in argv:
            return 0, "lark-cli version 1.0.77\n", ""
        if "auth" in argv:
            return 0, AUTH_OK, ""
        if "event" in argv:
            return 0, EVENT_IDLE, ""
        return 0, "{}", ""


def levels(checks, name):
    return [c.level for c in checks if c.name == name]


def detail(checks, name):
    """这一项**人能看到的全部文字**：CLI 把 detail 与 fix 都打出来，断言就该按同一个口径。"""
    return " ".join(f"{c.detail} {c.fix}" for c in checks if c.name == name)


def run(environ=None, *, runner=None, db=None, profile="larkflow", template="contract"):
    return run_checks(environ=dict(GOOD_ENV if environ is None else environ),
                      runner=runner or Runner(), db_path=str(db) if db else None,
                      profile=profile, template=template)


# ---------- 三条设计约束 ----------

def test_doctor_never_runs_a_write_command(tmp_path):
    """可以当着客户的面跑：只跑只读探针，绝不建文档 / 发消息 / 建待办。"""
    r = Runner()
    run(runner=r, db=tmp_path / "d.sqlite")
    flat = [" ".join(a) for a in r.seen]
    assert flat, "至少该探一次"
    for cmd in flat:
        assert not any(w in cmd for w in ("+create", "+overwrite", "messages-send",
                                          "+complete", "+patch", "+delete")), cmd
        assert any(w in cmd for w in ("--version", "auth status", "event status")), cmd


def test_the_llm_api_key_never_appears_in_the_output(tmp_path):
    checks = run(db=tmp_path / "d.sqlite")
    blob = json.dumps([c._asdict() for c in checks], ensure_ascii=False)
    assert "sk-this-must-never-be-printed" not in blob


def test_the_llm_host_is_printed_on_purpose(tmp_path):
    """客户会问「我的正文发到哪去」。host 与 model 是要给他看的，key 不是。"""
    checks = run(db=tmp_path / "d.sqlite")
    assert "ark.example.com" in detail(checks, "LLM 路由")
    assert "doubao-pro" in detail(checks, "LLM 路由")


def test_a_warning_does_not_block_deployment_but_a_failure_does():
    assert verdict([]) == OK
    assert verdict([_c(OK), _c(WARN)]) == WARN
    assert verdict([_c(OK), _c(WARN), _c(FAIL)]) == FAIL


def _c(level):
    from larkflow.doctor import Check
    return Check("x", level, "")


# ---------- 飞书身份（多租户下最要命的那一层） ----------

def test_a_missing_profile_is_fatal_not_a_warning(tmp_path):
    """profile 为空 = lark-cli 整条命令不带 --profile = 静默连默认 app。

    单租户下最多发错测试群；一台机器跑多家客户时这是**默认路径上的跨租户串号**。
    """
    checks = run(profile=None, db=tmp_path / "d.sqlite")
    assert levels(checks, "飞书 profile") == [FAIL]


def test_the_app_id_pin_catches_a_profile_pointing_at_someone_elses_app(tmp_path):
    """复制 systemd 单元时改漏 profile 名是真会发生的事。

    写错 profile 名 lark-cli 会 fail loud；写成**另一家的合法 profile 名**不会，
    只有这颗钉子拦得住。
    """
    env = dict(GOOD_ENV, LARKFLOW_APP_ID="cli_someoneelse")
    checks = run(env, db=tmp_path / "d.sqlite")
    assert levels(checks, "app 钉子") == [FAIL]
    assert "cli_someoneelse" in detail(checks, "app 钉子") and APP in detail(checks, "app 钉子")


def test_no_pin_configured_is_a_warning_with_the_exact_line_to_paste(tmp_path):
    env = {k: v for k, v in GOOD_ENV.items() if k != "LARKFLOW_APP_ID"}
    checks = run(env, db=tmp_path / "d.sqlite")
    assert levels(checks, "app 钉子") == [WARN]
    assert f"LARKFLOW_APP_ID={APP}" in detail(checks, "app 钉子"), "要能直接抄走"


def test_a_bot_identity_that_is_not_ready_is_fatal(tmp_path):
    bad = json.dumps({"appId": APP,
                      "identities": {"bot": {"status": "expired", "message": "Bot identity: expired"}}})
    r = Runner({"auth status": (0, bad, "")})
    checks = run(runner=r, db=tmp_path / "d.sqlite")
    assert levels(checks, "bot 身份") == [FAIL]


def test_a_profile_that_does_not_exist_is_reported_with_larks_own_message(tmp_path):
    err = json.dumps({"ok": False, "error": {"message": 'profile "nope" not found'}})
    r = Runner({"auth status": (1, "", err)})
    checks = run(runner=r, db=tmp_path / "d.sqlite")
    assert levels(checks, "飞书 profile") == [FAIL]
    assert "not found" in detail(checks, "飞书 profile")


def test_identity_must_be_bot_because_only_bot_gets_card_callbacks(tmp_path):
    checks = run(dict(GOOD_ENV, LARKFLOW_IDENTITY="user"), db=tmp_path / "d.sqlite")
    assert levels(checks, "身份") == [FAIL]


def test_an_idle_inbound_channel_is_only_a_warning(tmp_path):
    """serve 还没起时 not_running 是正常的，报红会让人学会忽略红色。"""
    checks = run(db=tmp_path / "d.sqlite")
    assert levels(checks, "入站通道") == [WARN]


def test_a_running_inbound_channel_is_reported_as_ok(tmp_path):
    live = json.dumps({"apps": [{"app_id": APP, "status": "running", "running": True}]})
    r = Runner({"event status": (0, live, "")})
    checks = run(runner=r, db=tmp_path / "d.sqlite")
    assert levels(checks, "入站通道") == [OK]


def test_both_lark_cli_envelope_shapes_are_understood(tmp_path):
    """自省命令成功吐裸对象，普通命令吐 {ok,data}。只认一种就会把成功判成失败。"""
    wrapped = json.dumps({"ok": True, "data": {"appId": APP,
                                               "identities": {"bot": {"status": "ready"}}}})
    r = Runner({"auth status": (0, wrapped, "")})
    checks = run(runner=r, db=tmp_path / "d.sqlite")
    assert levels(checks, "飞书 profile") == [OK]
    assert APP in detail(checks, "飞书 profile")


# ---------- 配置 ----------

def test_roles_the_template_needs_but_nobody_configured_are_fatal(tmp_path):
    env = {k: v for k, v in GOOD_ENV.items() if k != "LARKFLOW_ROLES"}
    checks = run(env, db=tmp_path / "d.sqlite")
    assert levels(checks, "角色映射") == [FAIL]


def test_roles_that_are_not_valid_json_say_so_instead_of_crashing(tmp_path):
    checks = run(dict(GOOD_ENV, LARKFLOW_ROLES="{法务:ou_a}"), db=tmp_path / "d.sqlite")
    assert levels(checks, "角色映射") == [FAIL]
    assert "JSON" in detail(checks, "角色映射")


def test_no_llm_configured_at_all_is_fatal(tmp_path):
    env = {k: v for k, v in GOOD_ENV.items() if not k.startswith("LLM_")}
    checks = run(env, db=tmp_path / "d.sqlite")
    assert levels(checks, "LLM 路由") == [FAIL]


def test_an_unknown_template_fails_and_skips_the_checks_that_need_it(tmp_path):
    checks = run(template="没有这个模板", db=tmp_path / "d.sqlite")
    assert levels(checks, "模板") == [FAIL]
    assert not levels(checks, "角色映射"), "模板都没有，角色覆盖无从谈起，不该报第二条噪声"


def test_a_missing_deliverable_folder_is_a_warning_because_it_still_runs(tmp_path):
    env = {
        k: v for k, v in GOOD_ENV.items()
        if k != "LARKFLOW_DELIVERABLE_FOLDER_TOKEN"
    }
    checks = run(env, db=tmp_path / "d.sqlite")
    assert levels(checks, "交付物落点") == [WARN]


def test_the_legacy_drive_folder_is_accepted_with_a_deprecation_warning(tmp_path):
    env = {
        k: v for k, v in GOOD_ENV.items()
        if k != "LARKFLOW_DELIVERABLE_FOLDER_TOKEN"
    }
    env["LARKFLOW_DRIVE_FOLDER"] = "fld_legacy"
    checks = run(env, db=tmp_path / "d.sqlite")
    assert levels(checks, "交付物落点") == [WARN]
    assert "已弃用" in detail(checks, "交付物落点")


def test_conflicting_deliverable_folder_variables_fail_closed(tmp_path):
    env = dict(GOOD_ENV, LARKFLOW_DRIVE_FOLDER="fld_other")
    checks = run(env, db=tmp_path / "d.sqlite")
    assert levels(checks, "交付物落点") == [FAIL]


# ---------- DB ----------

def test_the_database_check_reports_the_absolute_path_it_resolved(tmp_path):
    checks = run(db=tmp_path / "sub.sqlite")
    assert levels(checks, "SQLite") == [OK]
    assert str(tmp_path / "sub.sqlite") in detail(checks, "SQLite")


def test_a_database_that_cannot_be_opened_is_fatal(tmp_path):
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x")
    checks = run(db=blocker / "nested.sqlite")
    assert levels(checks, "SQLite") == [FAIL]


def test_a_daemon_already_holding_the_lock_is_a_warning_not_a_failure(tmp_path):
    """「已经有一个 serve 在跑」是运维要知道的事，但它不是配置错误。"""
    from larkflow.store import daemon_lock_for
    db = tmp_path / "held.sqlite"
    lock = daemon_lock_for(str(db))
    lock.acquire(timeout=0)
    try:
        checks = run(db=db)
    finally:
        lock.release()
    assert levels(checks, "daemon 单例锁") == [WARN]


# ---------- CLI ----------

def cli(argv, **kw):
    """`--env-file` 指到不存在的路径：不这么写就会去读开发机当前目录那份真 `.env`
    （见 conftest 的 `_no_env_leak`）。测试要的是一把与本机无关的尺子。"""
    from larkflow.__main__ import main
    return main(["--env-file", "/nonexistent/.env", *argv],
                factory=lambda ns: pytest.fail("doctor 不该构造 service"), **kw)


def test_cli_doctor_exits_nonzero_only_on_a_failure(capsys, tmp_path, monkeypatch):
    from larkflow import __main__ as m
    monkeypatch.setitem(m.HANDLERS, "doctor",
                        lambda ns, f, s: m._cmd_doctor(ns, f, s, checker=lambda **kw: [_c(WARN)]))
    assert cli(["--db", str(tmp_path / "d.sqlite"), "doctor"]) == 0
    monkeypatch.setitem(m.HANDLERS, "doctor",
                        lambda ns, f, s: m._cmd_doctor(ns, f, s, checker=lambda **kw: [_c(FAIL)]))
    assert cli(["--db", str(tmp_path / "d.sqlite"), "doctor"]) == 1


def test_cli_doctor_json_is_parseable_and_carries_the_verdict(capsys, tmp_path, monkeypatch):
    from larkflow import __main__ as m
    monkeypatch.setitem(m.HANDLERS, "doctor",
                        lambda ns, f, s: m._cmd_doctor(ns, f, s, checker=lambda **kw: [_c(FAIL)]))
    rc = cli(["--db", str(tmp_path / "d.sqlite"), "--json", "doctor"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1 and payload["verdict"] == FAIL
    assert payload["checks"] and "level" in payload["checks"][0]


# ---- 真机上撞出来的（cwd 不可读时 doctor 自己抛栈） ----

def test_a_template_name_still_resolves_when_the_cwd_is_unreadable(monkeypatch, tmp_path):
    """`sudo -u <另一个用户>` 从别人家目录里敲 doctor，cwd 对本进程 EACCES。

    `Path.exists()` 只吞 ENOENT/ENOTDIR/EBADF/ELOOP，**EACCES 往外抛**，于是
    `load_template("contract")` 抛 `PermissionError: 'contract'`，而它想说的只是
    「当前目录下没有这个文件，去内置模板目录找」。runbook 让人敲的正是这条命令。
    """
    from pathlib import Path

    from larkflow.model import load_template
    real = Path.exists

    def deny_relative(self):
        if not self.is_absolute():
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "exists", deny_relative)
    assert load_template("contract"), "内置模板必须仍然找得到"
    checks = run(db=tmp_path / "d.sqlite")
    assert levels(checks, "模板") == [OK]


def test_doctor_reports_instead_of_raising_when_the_template_layer_blows_up(monkeypatch, tmp_path):
    """doctor 自己抛栈是最糟的失败方式：它存在的全部意义就是配置坏了还能说人话。"""
    import larkflow.doctor as doc
    monkeypatch.setattr(doc, "load_template",
                        lambda n: (_ for _ in ()).throw(OSError(13, "Permission denied", "x")))
    checks = run(db=tmp_path / "d.sqlite")
    assert levels(checks, "模板") == [FAIL]
    # errno 13 的 OSError 会被 Python 自动实例化成 PermissionError 子类，报的就是真机上那个类名
    assert "PermissionError" in detail(checks, "模板")
