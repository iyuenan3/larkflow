"""配置 + 角色解析。凭证只从 env 读，绝不入库（红线）。"""
from __future__ import annotations

import json
import os
import re

LLM_ROLE_RE = re.compile(r"^LLM_(?:(?P<role>[A-Z0-9_]+)_)?BASE_URL$")
DEFAULT_ROLE = "default"


class RoleResolver:
    """把模板里的 assignee_role（负责人/QA/财务/法务…）解析成飞书 open_id（assignee）。

    本地：无映射时回退 f"ou_{role}"（Mock 不校验真 id，测试才这么跑）。
    真飞书：**strict=True**，未配置的角色直接抛。否则会把 `ou_法务` 这种假 open_id 发给
    飞书，报错发生在人工节点挂起那一刻、信息是 lark-cli 的「无效 open_id」，排查时根本
    看不出是角色没配。
    """

    def __init__(self, mapping: dict[str, str] | None = None, *, strict: bool = False):
        self.mapping = mapping or {}
        self.strict = strict

    def resolve(self, role: str, state: dict | None = None) -> str:
        if role in self.mapping:
            return self.mapping[role]
        if self.strict:
            raise RoleError(
                f"assignee_role「{role}」没有配置对应的飞书 open_id"
                f"（已配置：{sorted(self.mapping)}；见 .env.example 的 LARKFLOW_ROLES）"
            )
        return f"ou_{role}"

    def validate_coverage(self, dag: list[dict]) -> None:
        """装配期自检：模板里出现的每个 assignee_role 都得能解析（与 tool 覆盖检查并列）。"""
        roles = {n.get("assignee_role") for n in dag if n.get("assignee_role")}
        for v in (n.get("vote") or {} for n in dag):
            roles |= set(v.get("voters") or [])
        missing = sorted(r for r in roles if r not in self.mapping)
        if missing and self.strict:
            raise RoleError(f"这些 assignee_role 没配 open_id: {missing}")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None, *, strict: bool = False) -> "RoleResolver":
        """两种写法都认（中文角色名当 env 变量名在 shell 里 export 不进去，故以 JSON 为主）：

            LARKFLOW_ROLES={"财务":"ou_x","法务":"ou_y"}     # 主
            LARKFLOW_ROLE_FINANCE=ou_x                        # ASCII 别名，辅
        """
        environ = os.environ if environ is None else environ
        mapping: dict[str, str] = {}
        raw = environ.get("LARKFLOW_ROLES")
        if raw:
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RoleError(f"LARKFLOW_ROLES 不是合法 JSON: {exc}") from exc
            if not isinstance(loaded, dict):
                raise RoleError("LARKFLOW_ROLES 必须是 {角色: open_id} 对象")
            mapping.update({str(k): str(v) for k, v in loaded.items()})
        prefix = "LARKFLOW_ROLE_"
        mapping.update({k[len(prefix):]: v for k, v in environ.items()
                        if k.startswith(prefix) and v})
        return cls(mapping, strict=strict)


class RoleError(RuntimeError):
    """派单对象解析不出来（真栈下绝不静默伪造 open_id）。"""


def load_llm_roles(environ: dict[str, str] | None = None) -> dict[str, dict]:
    """按角色前缀装配 LLM 路由表（ADR-017）。

        LLM_BASE_URL / LLM_API_KEY / LLM_MODEL                 → 角色 default
        LLM_<ROLE>_BASE_URL / _API_KEY / _MODEL                → 角色 <role>（小写）

    模板节点的 `model_role` 命中哪个角色就用哪组 (base_url, api_key, model)；
    未命中回退 default。三元组缺项的角色直接跳过（宁可少一个角色，不带半截配置上路）。
    """
    environ = os.environ if environ is None else environ
    roles: dict[str, dict] = {}
    for key, base_url in environ.items():
        m = LLM_ROLE_RE.match(key)
        if not m or not base_url:
            continue
        raw = m.group("role")
        role = raw.lower() if raw else DEFAULT_ROLE
        prefix = f"LLM_{raw}_" if raw else "LLM_"
        api_key, model = environ.get(f"{prefix}API_KEY"), environ.get(f"{prefix}MODEL")
        if not api_key or not model:
            continue
        roles[role] = {"base_url": base_url, "api_key": api_key, "model": model}
    return roles


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"缺少必需环境变量 {key}（凭证走 env，不入库）")
    return val
