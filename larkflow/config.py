"""配置 + 角色解析。凭证只从 env 读，绝不入库（红线）。"""
from __future__ import annotations

import os


class RoleResolver:
    """把模板里的 role（负责人/QA/开发）解析成飞书 open_id（assignee）。

    本地：无映射时回退 f"ou_{role}"（Mock 不校验真 id）。
    真飞书：传 mapping（role -> ou_xxx），或后续接通讯录查询。
    """

    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping = mapping or {}

    def resolve(self, role: str, state: dict | None = None) -> str:
        if role in self.mapping:
            return self.mapping[role]
        return f"ou_{role}"


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"缺少必需环境变量 {key}（凭证走 env，不入库）")
    return val
