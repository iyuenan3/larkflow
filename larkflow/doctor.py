"""起服务之前，把**能在本机查出来的**问题一次查完。

为什么要有这个：部署这件事今天是「上机现敲、错了现查」。到客户现场现调试，第一印象就没了。
`larkflow doctor` 把「起不来 / 起来了但静默不干活」的已知成因逐条查一遍，绿了再 `serve`。

三条设计约束：

1. **只查、不改、不发**。全部是本地读或 lark-cli 的只读探针（`--version` / `auth status` /
   `event status`），绝不建文档、绝不发消息。可以在客户机器上当着人的面跑。
2. **绝不打印凭证**。LLM 只报 host / model / key 长度，飞书凭证根本不经过我们（在 lark-cli
   自己的存储里）。`base_url` 的 host **故意**打出来：客户问「这玩意往外发什么」时，那正是
   要给他看的东西。
3. **分三档**：`fail` = 现在起就会坏；`warn` = 起得来但有已知风险；`ok` = 查过了。
   退出码只看有没有 `fail`（warn 不挡部署，否则人会养成忽略它的习惯）。

**lark-cli 的两种输出信封**（实测，`io/cli.py` 的契约只覆盖了一半）：普通命令成功是
`{"ok":true,"data":{…}}`，而 `auth status` / `event status` 这类自省命令成功时**直接吐裸对象**
（`{"appId":…}` / `{"apps":[…]}`），失败才回 `{"ok":false,"error":{…}}`。拿 `run_cli` 去解会
被「返回非成功信封」挡下来，所以这里自己解，两种形状都认。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import NamedTuple
from urllib.parse import urlparse

from .config import RoleError, RoleResolver, deliverable_folder_token, load_llm_roles
from .engine.support import UnsupportedInV1, assert_v1_supported
from .engine.tools import TOOL_KINDS
from .model import load_template
from .model.template import TemplateError, validate_template
from .store import daemon_lock_for, open_db, resolve_db_path

OK, WARN, FAIL = "ok", "warn", "fail"


class Check(NamedTuple):
    name: str
    level: str
    detail: str
    fix: str = ""


def _default_runner(argv: list[str], *, timeout: int = 30) -> tuple[int, str, str]:
    from .io.cli import QUIET_ENV
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **QUIET_ENV})
    except FileNotFoundError:
        return 127, "", "not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"超时（{timeout}s）"
    return p.returncode, p.stdout, p.stderr


def _probe(runner, argv: list[str]) -> tuple[dict | None, str]:
    """跑一条只读探针。返回 (data, 错误说明)；两种成功信封都认（见模块 docstring）。"""
    rc, out, err = runner(argv)
    if rc != 0:
        try:
            e = (json.loads(err or out or "{}") or {}).get("error") or {}
        except json.JSONDecodeError:
            e = {}
        msg = e.get("message") or (err or out).strip().splitlines()[0:1] or [f"退出码 {rc}"]
        return None, msg if isinstance(msg, str) else msg[0]
    try:
        obj = json.loads(out or "{}")
    except json.JSONDecodeError:
        return None, f"输出不是 JSON：{(out or '').strip()[:120]}"
    if not isinstance(obj, dict):
        return None, "输出不是 JSON 对象"
    return (obj.get("data") if obj.get("ok") is True else obj), ""


# ---------- 逐项检查 ----------

def check_lark_cli(runner) -> list[Check]:
    if not shutil.which("lark-cli"):
        return [Check("lark-cli", FAIL, "PATH 上找不到 lark-cli",
                      "npm i -g @larksuite/cli（出入站全走它，没有它整个服务是聋哑的）")]
    rc, out, err = runner(["lark-cli", "--version"])
    ver = (out or err).strip().splitlines()[0] if (out or err) else "?"
    return [Check("lark-cli", OK if rc == 0 else FAIL, ver)]


def check_profile(runner, profile: str | None, environ: dict) -> list[Check]:
    """profile 认哪个飞书 app，以及 bot 身份能不能用。

    **`--profile` 必填**是这里最要紧的一条：为空时 lark-cli 整条命令不带 `--profile`，
    于是静默连到默认 app。单租户下这最多是发错测试群；一台机器上跑多家客户时，它就是
    **默认路径上的跨租户串号**，不报错、不留痕。

    `LARKFLOW_APP_ID` 是给多租户部署用的**钉子**：写在每家自己的 .env 里，doctor 拿它
    和 profile 真正解析出来的 appId 对一遍。复制 systemd 单元时改漏 profile 名是真会发生
    的事，而「profile 名写错」lark-cli 会 fail loud，「写成另一家的合法 profile 名」不会。
    """
    if not profile:
        return [Check("飞书 profile", FAIL, "没有指定 profile（LARK_PROFILE / --profile）",
                      "为空时 lark-cli 会连默认 app，多租户下就是跨租户串号")]
    data, err = _probe(runner, ["lark-cli", "--profile", profile, "auth", "status", "--json"])
    if data is None:
        return [Check("飞书 profile", FAIL, f"profile「{profile}」不可用：{err}",
                      "lark-cli profile list 看有哪些；没有就 lark-cli config init --new")]
    app_id = data.get("appId") or "?"
    out = [Check("飞书 profile", OK, f"{profile} → app {app_id}")]

    expected = (environ.get("LARKFLOW_APP_ID") or "").strip()
    if not expected:
        out.append(Check("app 钉子", WARN, "没配 LARKFLOW_APP_ID，不校验 profile 连的是哪个 app",
                         f"在 .env 里写 LARKFLOW_APP_ID={app_id}，多租户下这条防的是串号"))
    elif expected != app_id:
        out.append(Check("app 钉子", FAIL,
                         f"profile「{profile}」连的是 {app_id}，而 .env 声明的是 {expected}",
                         "改错一个就是在动另一家客户的数据。核对 LARK_PROFILE 与 LARKFLOW_APP_ID"))
    else:
        out.append(Check("app 钉子", OK, f"profile 与 LARKFLOW_APP_ID 对得上（{app_id}）"))

    bot = (data.get("identities") or {}).get("bot") or {}
    if bot.get("status") == "ready":
        out.append(Check("bot 身份", OK, "ready（卡片回调只有 bot 收得到）"))
    else:
        out.append(Check("bot 身份", FAIL,
                         f"bot 身份不可用：{bot.get('message') or bot.get('status') or '未知'}",
                         "lark-cli auth login；注意 auth status 是纯本地判断，它 ready 不代表凭证还有效"))
    return out


def check_identity(environ: dict) -> list[Check]:
    ident = environ.get("LARKFLOW_IDENTITY") or "bot"
    if ident == "bot":
        return [Check("身份", OK, "bot")]
    return [Check("身份", FAIL, f"LARKFLOW_IDENTITY={ident}",
                  "卡片回调只有 bot 收得到，配成 user 时按钮点击一条都收不到且不报错")]


def check_event_channel(runner, profile: str | None) -> list[Check]:
    """入站通道现在是什么状态。**not_running 在 serve 起来之前是正常的**，所以只 warn。"""
    if not profile:
        return []
    data, err = _probe(runner, ["lark-cli", "--profile", profile, "event", "status", "--json"])
    if data is None:
        return [Check("入站通道", WARN, f"查不到状态：{err}")]
    apps = data.get("apps") or []
    running = [a for a in apps if a.get("running")]
    if running:
        return [Check("入站通道", OK, f"已在跑（{', '.join(a.get('app_id', '?') for a in running)}）")]
    return [Check("入站通道", WARN, "当前没有事件订阅在跑",
                  "serve 起来之前这是正常的；serve 起来之后还是这个，说明长连接没建起来")]


def check_db(db_path: str | None) -> list[Check]:
    """DB 能不能开、开出来的是不是本地盘、有没有别的 daemon 已经占着。"""
    path = resolve_db_path(db_path)
    out: list[Check] = []
    try:
        conn = open_db(path)
        conn.close()
        out.append(Check("SQLite", OK, f"{path}（WAL 可用）"))
    except Exception as exc:
        return [Check("SQLite", FAIL, f"{path}：{type(exc).__name__}: {exc}",
                      "DB 必须放本地盘：网络盘上 WAL 与 flock 都不可靠，多进程写会丢更新")]
    lock = daemon_lock_for(path)
    try:
        lock.acquire(timeout=0)
        lock.release()
        out.append(Check("daemon 单例锁", OK, "空闲（现在可以起 serve）"))
    except Exception:
        out.append(Check("daemon 单例锁", WARN, "已被占用：这个库上已经有一个 serve 在跑",
                         "同一个 DB 只允许一个 daemon。要重启先停掉那个"))
    return out


def check_template(name: str | None) -> tuple[list[dict], list[Check]]:
    try:
        dag = load_template(name or "contract")
        validate_template(dag)
        assert_v1_supported(dag)
    # 兜到 OSError：doctor 的价值就是「配置有问题时它还能说人话」，它自己抛栈是最糟的失败方式。
    # 真机上撞过一次 `PermissionError: 'contract'`（`Path.exists()` 不吞 EACCES，见 template._readable）。
    except (TemplateError, UnsupportedInV1, OSError, ValueError) as exc:
        return [], [Check("模板", FAIL, f"{name}：{type(exc).__name__}: {exc}")]
    missing = [n["id"] for n in dag if n["executor"] == "tool"
               and (n.get("tool") or {}).get("kind") not in TOOL_KINDS]
    checks = [Check("模板", OK, f"{name or 'contract'}：{len(dag)} 个节点")]
    if missing:
        checks.append(Check("tool 能力", FAIL, f"这些 tool 节点没有可执行体：{missing}",
                            f"声明 tool.kind ∈ {sorted(TOOL_KINDS)} 即可，无需写 Python"))
    return dag, checks


def check_roles(dag: list[dict], environ: dict) -> list[Check]:
    """派单对象。真栈 strict：宁可起不来，也不把 `ou_法务` 这种假 open_id 发给飞书。"""
    try:
        resolver = RoleResolver.from_env(environ, strict=True)
    except RoleError as exc:
        return [Check("角色映射", FAIL, str(exc),
                      "LARKFLOW_ROLES 是 JSON；别用 `source .env`，shell 会把引号吃掉")]
    try:
        resolver.validate_coverage(dag)
    except RoleError as exc:
        return [Check("角色映射", FAIL, str(exc), "见 .env.example 的 LARKFLOW_ROLES")]
    return [Check("角色映射", OK, f"{len(resolver.mapping)} 个角色，模板要的都配了")]


def check_llm(dag: list[dict], environ: dict) -> list[Check]:
    """LLM 路由。打 host 与 model，**绝不打 key**。

    host 是故意露出来的：客户问「我的正文发到哪去」时，这一行就是答案。
    """
    roles = load_llm_roles(environ)
    if not roles:
        return [Check("LLM 路由", FAIL, "一个角色都没配全（三元组缺项的角色会被整个跳过）",
                      "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 至少配一组兜底")]
    lines = []
    for name in sorted(roles):
        cfg = roles[name]
        host = urlparse(cfg.get("base_url") or "").netloc or "?"
        n = len(cfg.get("fallbacks") or [])
        lines.append(f"{name}→{host}／{cfg.get('model')}"
                     f"（key {len(cfg.get('api_key') or '')} 字符{f'，备用 {n} 条' if n else ''}）")
    out = [Check("LLM 路由", OK, "；".join(lines))]

    wanted = {n.get("model_role") for n in dag if n.get("executor") == "llm" and n.get("model_role")}
    uncovered = sorted(r for r in wanted if r not in roles)
    if uncovered and "default" not in roles:
        out.append(Check("LLM 覆盖", FAIL, f"模板要的角色没配且没有 default 兜底：{uncovered}"))
    elif uncovered:
        out.append(Check("LLM 覆盖", WARN, f"这些角色回退到 default：{uncovered}",
                         "回退不报错，但它们会共用同一个模型与同一份配额"))
    return out


def check_deliverable_target(environ: dict) -> list[Check]:
    try:
        folder_token = deliverable_folder_token(environ)
    except ValueError as exc:
        return [Check("交付物落点", FAIL, str(exc), "删除旧变量，统一使用新变量")]
    if (environ.get("LARKFLOW_DELIVERABLE_FOLDER_TOKEN") or "").strip():
        return [Check("交付物落点", OK, "已指定飞书云文档父文件夹")]
    if folder_token:
        return [Check(
            "交付物落点", WARN,
            "仍在使用已弃用的 LARKFLOW_DRIVE_FOLDER",
            "迁移为 LARKFLOW_DELIVERABLE_FOLDER_TOKEN",
        )]
    return [Check(
        "交付物落点", WARN,
        "没配 LARKFLOW_DELIVERABLE_FOLDER_TOKEN，交付物会落在 bot 的个人空间根目录",
        "真项目里人会找不到文档；建一个文件夹并把 folder token 配上",
    )]


# ---------- 编排 ----------

def run_checks(*, environ: dict | None = None, db_path: str | None = None,
               template: str | None = None, profile: str | None = None,
               runner=None) -> list[Check]:
    environ = os.environ if environ is None else environ
    runner = runner or _default_runner
    profile = profile or environ.get("LARK_PROFILE")
    template = template or environ.get("LARKFLOW_TEMPLATE")

    checks = check_lark_cli(runner)
    checks += check_profile(runner, profile, environ)
    checks += check_identity(environ)
    checks += check_event_channel(runner, profile)
    checks += check_db(db_path or environ.get("LARKFLOW_DB"))
    dag, tpl_checks = check_template(template)
    checks += tpl_checks
    if dag:
        checks += check_roles(dag, environ)
        checks += check_llm(dag, environ)
    checks += check_deliverable_target(environ)
    return checks


def verdict(checks: list[Check]) -> str:
    if any(c.level == FAIL for c in checks):
        return FAIL
    return WARN if any(c.level == WARN for c in checks) else OK
