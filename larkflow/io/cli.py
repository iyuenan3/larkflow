"""lark-cli 子进程调用层（出口全走 lark-cli，不引入飞书 SDK，ADR-005）。

JSON 契约（lark-shared 内嵌 skill，已核对 v1.2.x，不靠 --help 猜）：
  成功 → stdout `{"ok": true, "identity": …, "data": {…}}`，退出码 0
  失败 → stderr `{"ok": false, "error": {type, subtype, code, message, hint, …}}`，退出码非 0
  **判成功只看 ok==true / 退出码**，绝不看 `code==0`（成功信封根本没有顶层 code，
  按老格式判会把所有成功调用误判为失败，进而绕过幂等逻辑重复创建）。
  退出码 10 = 高风险写操作要确认（不自动补 --yes，交给人）。

另：机器读 JSON 时关掉更新 / skills 提示，避免 `_notice` 混进输出。
"""
from __future__ import annotations

import json
import os
import subprocess

CONFIRMATION_EXIT = 10
QUIET_ENV = {
    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
}


class LarkCliError(RuntimeError):
    """lark-cli 调用失败（带结构化 error 信封）。"""

    def __init__(self, message: str, *, error: dict | None = None, argv: list[str] | None = None):
        super().__init__(message)
        self.error = error or {}
        self.argv = argv or []


def run_cli(argv: list[str], *, stdin: str | None = None, timeout: int = 120) -> dict:
    """跑一条 lark-cli 命令，返回成功信封里的 data。"""
    proc = subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **QUIET_ENV},
    )
    return parse_result(argv, proc.returncode, proc.stdout, proc.stderr)


def parse_result(argv: list[str], returncode: int, stdout: str, stderr: str) -> dict:
    """把 (退出码, stdout, stderr) 解成 data，或抛带 hint 的 LarkCliError。"""
    if returncode != 0:
        err = _load(stderr).get("error") or {}
        if returncode == CONFIRMATION_EXIT or err.get("subtype") == "confirmation_required":
            raise LarkCliError(
                f"lark-cli 要求人工确认高风险操作: {err.get('action') or ' '.join(argv[:3])}"
                f"（risk={err.get('risk')}）；不自动补 --yes",
                error=err, argv=argv,
            )
        raise LarkCliError(
            f"lark-cli 失败({returncode}) {err.get('type', '')}/{err.get('subtype', '')}: "
            f"{err.get('message') or stderr.strip()[:400]}"
            + (f"｜hint: {err['hint']}" if err.get("hint") else ""),
            error=err, argv=argv,
        )
    env = _load(stdout)
    if env.get("ok") is not True:
        raise LarkCliError(f"lark-cli 返回非成功信封: {stdout.strip()[:400]}", argv=argv)
    return env.get("data") or {}


def _load(raw: str) -> dict:
    try:
        obj = json.loads(raw or "{}")
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}
