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

from dataclasses import asdict, dataclass

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
