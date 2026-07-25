"""交付物 I/O：`(容器, region)` 统一飞书文档 handle（ADR-016）。

交付物 = 带 type 的飞书 handle（doc token / 云盘 file token）。**内容在飞书**（投影），
引擎只存指针 + 元数据；版本靠飞书原生（稳定 handle + overwrite + 飞书 history），
引擎不自建版本。

两实现：
  FakeDeliverableStore  内存，本地 e2e 零依赖（overwrite 保 handle 不变、留版本供断言）。
  CliLarkIO             真飞书（`markdown +create/+overwrite/+fetch`），step 9 接。

v1 只做 `region="whole"`（独立 doc 拓扑）；`{"section": …}` 属 v2 共享协同（ADR-018），
schema 层已放行、这里显式 NotImplementedError，绝不静默降级。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .cli import run_cli

WHOLE = "whole"


@dataclass(frozen=True)
class Deliverable:
    """一个交付物 handle。以纯 dict 进 state（checkpointer 序列化友好）。"""

    type: str        # markdown | docx | file
    token: str       # 飞书 handle（doc token / 云盘 file token）
    url: str
    region: object = WHOLE   # "whole" | {"section": selector}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Deliverable":
        return cls(type=d["type"], token=d["token"], url=d.get("url", ""),
                   region=d.get("region", WHOLE))


def assert_v1_region(region) -> None:
    if region != WHOLE:
        raise NotImplementedError(
            f"v1 只支持 region=whole（独立 doc 拓扑）；section 共享协同属 v2（ADR-018）：{region}"
        )


class DeliverableIO:
    """produce 末步物化 / 下游消费的统一协议（ADR-016 产出·消费协议）。"""

    def create(self, *, title: str, content: str, region=WHOLE, idem_key: str) -> Deliverable:
        raise NotImplementedError

    def overwrite(self, handle: Deliverable, *, content: str) -> Deliverable:
        """重跑：handle 不变、飞书自动留版本。返回同一个 handle。"""
        raise NotImplementedError

    def fetch(self, handle: Deliverable) -> str:
        """下游 llm 消费上游正文。"""
        raise NotImplementedError


class FakeDeliverableStore(DeliverableIO):
    """内存实现。docs[token] = {"title", "region", "versions": [...]}。"""

    def __init__(self, *, doc_type: str = "markdown"):
        self.doc_type = doc_type
        self.docs: dict[str, dict] = {}
        self._idem: dict[str, str] = {}   # idem_key -> token
        self._seq = 0

    def create(self, *, title: str, content: str, region=WHOLE, idem_key: str) -> Deliverable:
        assert_v1_region(region)
        if idem_key in self._idem:
            return self._handle(self._idem[idem_key])
        self._seq += 1
        token = f"doc_{self._seq:04d}"
        self.docs[token] = {"title": title, "region": region, "versions": [content]}
        self._idem[idem_key] = token
        return self._handle(token)

    def overwrite(self, handle: Deliverable, *, content: str) -> Deliverable:
        doc = self.docs.get(handle.token)
        if doc is None:
            raise KeyError(f"交付物不存在: {handle.token}")
        doc["versions"].append(content)
        return self._handle(handle.token)

    def fetch(self, handle: Deliverable) -> str:
        doc = self.docs.get(handle.token)
        if doc is None:
            raise KeyError(f"交付物不存在: {handle.token}")
        return doc["versions"][-1]

    # ---- 测试辅助 ----
    def versions(self, handle: Deliverable) -> list[str]:
        return list(self.docs[handle.token]["versions"])

    def _handle(self, token: str) -> Deliverable:
        return Deliverable(type=self.doc_type, token=token,
                           url=f"https://example.feishu.cn/docx/{token}",
                           region=self.docs[token]["region"])


class CliDeliverableIO(DeliverableIO):
    """真飞书交付物：`lark-cli markdown +create/+overwrite/+fetch`（v1 = 独立 doc·whole）。

    命令与返回字段按内嵌 skill 核对（`lark-cli skills read lark-markdown`），不猜 flag：
      +create    --name <x.md> --content -（stdin）[--folder-token …] → data.file_token
      +overwrite --file-token <t> --content -（stdin）               → data.version
      +fetch     --file-token <t>                                    → data.content
    正文一律走 stdin（`--content -`）：避免超长 argv 与 shell 转义（skill 明示推荐）。

    **幂等**：`markdown +create` 没有 --idempotency-key（task/im 有），崩溃重跑会多建一份
    文档。故本地记 idem_key → file_token（idem_store，随 checkpointer 同一个 SQLite 走），
    重放直接返回旧 handle。overwrite 天然幂等（同内容覆盖同 handle）。
    """

    def __init__(self, *, identity: str = "bot", profile: str | None = None,
                 folder_token: str | None = None, idem_store=None, runner=run_cli):
        self.identity = identity
        self.profile = profile
        self.folder_token = folder_token   # 省略则建到云空间根目录
        self.idem = idem_store             # 有 get/put 的小 KV（见 io/correlations.py）
        self.runner = runner

    def _run(self, args: list[str], *, stdin: str | None = None) -> dict:
        base = ["lark-cli"]
        if self.profile:
            base += ["--profile", self.profile]
        return self.runner(base + args + ["--as", self.identity, "--json"], stdin=stdin)

    def create(self, *, title: str, content: str, region=WHOLE, idem_key: str) -> Deliverable:
        assert_v1_region(region)
        cached = self.idem.get(idem_key) if self.idem else None
        if cached:
            return Deliverable(type="markdown", token=cached, url=_md_url(cached), region=region)

        args = ["markdown", "+create", "--name", md_name(title), "--content", "-"]
        if self.folder_token:
            args += ["--folder-token", self.folder_token]
        data = self._run(args, stdin=content)
        token = data.get("file_token") or ""
        if not token:
            raise LarkDeliverableError(f"markdown +create 未返回 file_token: {data}")
        if self.idem:
            self.idem.put(idem_key, token)
        return Deliverable(type="markdown", token=token,
                           url=data.get("url") or data.get("file_url") or _md_url(token),
                           region=region)

    def overwrite(self, handle: Deliverable, *, content: str) -> Deliverable:
        self._run(["markdown", "+overwrite", "--file-token", handle.token, "--content", "-"],
                  stdin=content)
        return handle   # handle 不变，版本由飞书原生留痕

    def fetch(self, handle: Deliverable) -> str:
        data = self._run(["markdown", "+fetch", "--file-token", handle.token])
        return data.get("content", "")


class LarkDeliverableError(RuntimeError):
    """交付物读写返回了不该有的形状。"""


def md_name(title: str) -> str:
    """文件名必须显式带 .md 后缀（skill 硬约束），且不能带路径分隔符。"""
    safe = re.sub(r"[/\\\n\r\t]+", "_", (title or "deliverable").strip()) or "deliverable"
    return safe if safe.endswith(".md") else f"{safe}.md"


def _md_url(token: str) -> str:
    # +create 通常回带可打开 URL；缺省时按 Drive 文件 URL 形态兜底（域名待 dev app 核实）
    return f"https://feishu.cn/file/{token}"
