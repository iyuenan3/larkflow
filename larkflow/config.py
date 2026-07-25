"""配置 + 角色解析。凭证只从 env 读，绝不入库（红线）。"""
from __future__ import annotations

import os
import re

LLM_ROLE_RE = re.compile(r"^LLM_(?:(?P<role>[A-Z0-9_]+)_)?BASE_URL$")
DEFAULT_ROLE = "default"


class RoleResolver:
    """把模板里的 assignee_role（负责人/QA/财务/法务…）解析成飞书 open_id（assignee）。

    本地：无映射时回退 f"ou_{role}"（Mock 不校验真 id）。
    真飞书：传 mapping（role -> ou_xxx），或后续接通讯录查询（见 SPEC 待填）。
    """

    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping = mapping or {}

    def resolve(self, role: str, state: dict | None = None) -> str:
        if role in self.mapping:
            return self.mapping[role]
        return f"ou_{role}"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "RoleResolver":
        """`LARKFLOW_ROLE_<角色>=ou_xxx` → {角色: open_id}（角色名区分不了中文，故用别名 env）。"""
        environ = os.environ if environ is None else environ
        prefix = "LARKFLOW_ROLE_"
        return cls({k[len(prefix):]: v for k, v in environ.items()
                    if k.startswith(prefix) and v})


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
