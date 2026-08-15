"""配置 + 角色解析。凭证只从 env 读，绝不入库（红线）。"""
from __future__ import annotations

import json
import os
import pathlib
import re
from typing import NamedTuple

# 角色主配置。**必须排除 BACKUP 段**：`LLM_WRITER_BACKUP_BASE_URL` 长得就像
# 「角色 writer_backup 的主配置」，不排除就会凭空多出一个没人用的角色，而备用线路
# 静默失效、配置看起来还完全正常。
#
# 两个前瞻各挡一半（原来只有后一个，真栈上加第三条线路时漏了前一半）：
#   · `(?!BACKUP\d*_)`    挡**不带角色名**的 `LLM_BACKUP_BASE_URL` / `LLM_BACKUP2_BASE_URL`，
#                          它们是**默认角色的备用**，role 组会把它们读成叫 backup / backup2 的角色。
#   · `(?!.*_BACKUP\d*_)` 挡**带角色名**的 `LLM_WRITER_BACKUP_BASE_URL`。
# 只写后一个时前一半漏网，因为那条要求 BACKUP 前面有下划线，而 `LLM_BACKUP_…` 里没有。
LLM_ROLE_RE = re.compile(
    r"^LLM_(?:(?P<role>(?!BACKUP\d*_)(?!.*_BACKUP\d*_)[A-Z0-9_]+)_)?BASE_URL$")
# 备用线路：BACKUP / BACKUP2 / BACKUP3…，按序号排队。
LLM_BACKUP_RE = re.compile(
    r"^LLM_(?:(?P<role>[A-Z0-9_]+)_)?BACKUP(?P<idx>\d*)_"
    r"(?P<field>BASE_URL|API_KEY|MODEL|TIMEOUT|WEB_SEARCH_CAPABILITY)$")
DEFAULT_ROLE = "default"
_FIELDS = {"BASE_URL": "base_url", "API_KEY": "api_key", "MODEL": "model",
           "TIMEOUT": "timeout", "WEB_SEARCH_CAPABILITY": "web_search_capability"}


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

    def roles_of(self, open_id: str) -> set[str]:
        """**反向**解析：这个 open_id 担了哪些角色（ADR-023 的权限层要它）。

        一对多：同一个人可以同时是「财务」和「法务」，故返回集合，绝不假设一对一。
        非 strict 时与 `resolve` 的本地回退**严格对称**：`resolve(role)` 会回退成 `ou_<role>`，
        所以 `ou_<role>` 也反解成 `role`，**但仅限该角色没有真映射**。少这个条件就有冒名
        顶替：「财务」已配成 `ou_fin` 时，谁拿着 `ou_财务` 都能反解出「财务」。
        strict（真栈）下只认真映射，认不出来就是认不出来。
        """
        if not open_id:
            return set()
        hit = {r for r, oid in self.mapping.items() if oid == open_id}
        if hit or self.strict:
            return hit
        if isinstance(open_id, str) and open_id.startswith("ou_"):
            role = open_id[3:]
            if role and role not in self.mapping:
                return {role}
        return set()

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
    backups = _load_backups(environ)
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
        # 主配置不全 → 整个角色跳过（既有语义），它的备用也一并丢掉：没有主线路的
        # 「备用」不该自己上位，那会让一份写漏的配置静默跑在人没打算用的供应商上。
        primary = {"base_url": base_url, "api_key": api_key, "model": model}
        secs = _timeout(environ.get(f"{prefix}TIMEOUT"))
        if secs:
            primary["timeout"] = secs
        capability = environ.get(f"{prefix}WEB_SEARCH_CAPABILITY")
        if capability is not None:
            primary["web_search_capability"] = _web_search_capability(capability)
        chain = []
        for _, override in sorted((backups.get(raw or "") or {}).items()):
            backup = dict(primary, **override)
            route_changed = any(
                field in override and override[field] != primary[field]
                for field in ("base_url", "model")
            )
            if route_changed and "web_search_capability" not in override:
                backup.pop("web_search_capability", None)
            chain.append(backup)
        roles[role] = {**primary, "fallbacks": chain} if chain else primary
    return roles


def _timeout(raw):
    """秒数，写坏了就当没配（退回默认值）。

    env 里手滑一个字不该让整个服务起不来：这是运维旋钮，不是业务契约。
    """
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def _web_search_capability(raw: object) -> str:
    """Fail closed unless the route explicitly promises Responses citations."""

    normalized = str(raw or "").strip().lower()
    if normalized == "responses_citations":
        return normalized
    return "unavailable"


def _load_backups(environ: dict[str, str]) -> dict[str, dict[int, dict]]:
    """扫出 {角色原文: {序号: 局部覆盖}}。

    **局部覆盖**是这里的关键：只写 `LLM_WRITER_BACKUP_API_KEY` 就等于「同一个供应商、
    同一个模型、换一把 key」（限流时最常见的用法）。要求人把三项重复填一遍，等于逼他
    复制粘贴，而且改主配置时一定会漏改备用的那一份。
    """
    out: dict[str, dict[int, dict]] = {}
    for key, val in environ.items():
        m = LLM_BACKUP_RE.match(key)
        if not m or not val:
            continue
        raw = m.group("role") or ""
        idx = int(m.group("idx") or 1)
        field = _FIELDS[m.group("field")]
        if field == "timeout":
            val = _timeout(val)      # env 全是字符串，这一个要落成数字
            if val is None:
                continue
        elif field == "web_search_capability":
            val = _web_search_capability(val)
        out.setdefault(raw, {}).setdefault(idx, {})[field] = val
    return out


class Loaded(NamedTuple):
    """`.env` 加载结果。`skipped` 是**关键的一半**：被环境变量占用的键，文件里的值没生效。

    只报 `set` 的话，「shell 里已有一份坏值」这个场景是**完全静默**的：11 个键全被占用时
    一行都不打，看起来就像 dotenv 没工作（实测踩过，坏值来自早先的 `source .env`）。
    """

    set: list[str]
    skipped: list[str]


def load_dotenv(path: str = ".env", *, environ: dict | None = None) -> "Loaded":
    """把 `.env` 读进环境变量，返回 (本次设置的键, 被占用而未生效的键)。只键名，值全是凭证。

    **为什么不让人用 `source .env`**（实测踩过）：`.env` 长得像 shell 赋值，但它不是
    shell 脚本。`source` 会做引号剥离、词分割、glob 展开、`$` 展开、反引号执行。
    实际后果：`LARKFLOW_ROLES={"法务":"ou_…"}` 被吃成 `{法务:ou_…}`，JSON 当场炸；
    含 `$` 的 api_key 会被悄悄改写成别的东西**而且不报错**。

    这里的规则明确、与 shell 无关：
      · `#` 开头整行 = 注释；空行跳过；允许 `export ` 前缀。
      · 值**整体**被一对 `'` 或 `"` 包住时剥掉那对引号，否则原样保留（含内部引号）。
      · 未被引号包裹时，「空白 + #」之后当行尾注释；紧贴的 `#` 是值的一部分。
      · `$` / 反引号一律是字面量。
      · **已存在的环境变量优先**：显式 export 是人当场的意图，文件不许盖回去。
    """
    environ = os.environ if environ is None else environ
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return Loaded([], [])
    done: list[str] = []
    skipped: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]                       # 整体包裹的引号是语法
        else:
            cut = _INLINE_COMMENT.search(val)     # 只有「空白 + #」才是行尾注释
            if cut:
                val = val[:cut.start()].rstrip()
        if key in environ:
            skipped.append(key)                   # 显式 export 优先，但要说出来
            continue
        environ[key] = val
        done.append(key)
    return Loaded(done, skipped)


_INLINE_COMMENT = re.compile(r"\s+#")


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"缺少必需环境变量 {key}（凭证走 env，不入库）")
    return val
