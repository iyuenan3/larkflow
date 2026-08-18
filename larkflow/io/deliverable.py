"""交付物 I/O：`(容器, region)` 统一飞书云文档 handle（ADR-016）。

交付物 = 带 type 的飞书 handle（doc token / 云盘 file token）。**内容在飞书**（投影），
引擎只存指针 + 元数据；版本靠飞书原生（稳定 handle + overwrite + 飞书 history），
引擎不自建版本。

两实现：
  FakeDeliverableStore  内存，本地 e2e 零依赖（overwrite 保 handle 不变、留版本供断言）。
  CliDeliverableIO      真飞书原生 Docx（`docs +create/+update/+fetch`）。

v1 只做 `region="whole"`（独立 doc 拓扑）；`{"section": …}` 属 v2 共享协同（ADR-018），
schema 层已放行、这里显式 NotImplementedError，绝不静默降级。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .cli import run_cli

WHOLE = "whole"
DOCX_IDEM_PREFIX = "docx:"


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

    def __init__(self, *, doc_type: str = "docx"):
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
    """真飞书交付物：原生 Docx（v1 = 独立 doc·whole）。

    命令与返回字段按内嵌 skill 核对（`lark-cli skills read lark-doc`），不猜 flag：
      docs +create --doc-format markdown --title … --content - [--parent-token …]
      docs +update --doc <t> --command overwrite --doc-format markdown --content -
      docs +fetch  --doc <t> --doc-format markdown
    正文一律走 stdin（`--content -`）：避免超长 argv 与 shell 转义（skill 明示推荐）。

    **幂等**：`docs +create` 没有 --idempotency-key（task/im 有），崩溃重跑会多建一份
    文档。故本地记 idem_key → document_id（idem_store，随 checkpointer 同一个 SQLite 走），
    重放直接返回旧 handle。全文更新复用同一 document_id，飞书保留原生版本历史。

    升级前已经登记的 Markdown handle 继续用原命令读取和覆盖；新的幂等值带 ``docx:``
    前缀，未带前缀的历史值按 Markdown 解释，避免把旧 file_token 当成 document_id。
    """

    def __init__(self, *, identity: str = "bot", profile: str | None = None,
                 folder_token: str | None = None, idem_store=None, runner=run_cli):
        self.identity = identity
        self.profile = profile
        self.folder_token = folder_token   # Docx 父文件夹；省略则建到云空间根目录
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
            return _cached_handle(cached, region=region)

        args = [
            "docs", "+create",
            "--doc-format", "markdown",
            "--title", title,
            "--content", "-",
        ]
        if self.folder_token:
            args += ["--parent-token", self.folder_token]
        data = self._run(args, stdin=content)
        document = _document_payload(data)
        token = document.get("document_id") or ""
        if not token:
            raise LarkDeliverableError(f"docs +create 未返回 document_id: {data}")
        if self.idem:
            self.idem.put(idem_key, f"{DOCX_IDEM_PREFIX}{token}")
        return Deliverable(type="docx", token=token,
                           url=document.get("url") or _docx_url(token),
                           region=region)

    def overwrite(self, handle: Deliverable, *, content: str) -> Deliverable:
        if handle.type == "markdown":
            self._run([
                "markdown", "+overwrite",
                "--file-token", handle.token,
                "--content", "-",
            ], stdin=content)
            return handle
        if handle.type != "docx":
            raise LarkDeliverableError(f"不支持覆盖 {handle.type} 交付物")
        self._run([
            "docs", "+update",
            "--doc", handle.token,
            "--command", "overwrite",
            "--doc-format", "markdown",
            "--content", "-",
        ], stdin=content)
        return handle   # handle 不变，版本由飞书原生留痕

    def fetch(self, handle: Deliverable) -> str:
        if handle.type == "markdown":
            data = self._run([
                "markdown", "+fetch",
                "--file-token", handle.token,
            ])
            content = data.get("content")
            if not isinstance(content, str):
                raise LarkDeliverableError(f"markdown +fetch 未返回正文: {data}")
            return content
        if handle.type != "docx":
            raise LarkDeliverableError(f"不支持读取 {handle.type} 交付物")
        data = self._run([
            "docs", "+fetch",
            "--doc", handle.token,
            "--doc-format", "markdown",
        ])
        document = _document_payload(data)
        content = document.get("content")
        if not isinstance(content, str):
            raise LarkDeliverableError(f"docs +fetch 未返回文档正文: {data}")
        return content


class LarkDeliverableError(RuntimeError):
    """交付物读写返回了不该有的形状。"""


def md_name(title: str) -> str:
    """兼容旧 Markdown 交付后端的文件名规范。"""
    safe = re.sub(r"[/\\\n\r\t]+", "_", (title or "deliverable").strip()) or "deliverable"
    return safe if safe.endswith(".md") else f"{safe}.md"


def _document_payload(data: dict) -> dict:
    document = data.get("document")
    if not isinstance(document, dict):
        nested = data.get("data")
        document = nested.get("document") if isinstance(nested, dict) else None
    return document if isinstance(document, dict) else {}


def _cached_handle(value: str, *, region) -> Deliverable:
    if value.startswith(DOCX_IDEM_PREFIX):
        token = value[len(DOCX_IDEM_PREFIX):]
        return Deliverable(type="docx", token=token, url=_docx_url(token), region=region)
    return Deliverable(type="markdown", token=value, url=_md_url(value), region=region)


def _docx_url(token: str) -> str:
    # +create 通常回带租户域名 URL；缓存命中时只剩 token，使用可打开的 Docx 形态兜底。
    return f"https://feishu.cn/docx/{token}"


def _md_url(token: str) -> str:
    return f"https://feishu.cn/file/{token}"
