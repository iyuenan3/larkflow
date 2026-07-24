"""v1 运行时能力边界：schema 预留 ≠ 引擎实现了行为。

模板 schema 层放行 v1.1+ 的字段（vote / when / any|all|threshold / region=section），
好让生成器与前端照同一份契约产图；但引擎 v1 只实现其中一部分。装配期就把「声明了
引擎不会执行的语义」的模板挡下来，绝不静默按退化语义跑（那会静默产出错的流程）。

对应 ROADMAP：会签 / 投票 / 条件分支 = v1.3；子项目 = v1.2；共享协同 section = v2。
"""
from __future__ import annotations

from ..model.node import is_gate

V1_POLICIES = ("auto", "single")


class UnsupportedInV1(NotImplementedError):
    """模板用了 v1 引擎还没实现的语义。"""


def assert_v1_supported(dag: list[dict]) -> None:
    for n in dag:
        nid = n["id"]
        if is_gate(n) and n.get("approval_policy") not in V1_POLICIES:
            raise UnsupportedInV1(
                f"{nid} approval_policy={n.get('approval_policy')}：会签 / 投票阈值落 v1.3"
                f"（ADR-025）；v1 只实现 {V1_POLICIES}"
            )
        if n.get("vote"):
            raise UnsupportedInV1(f"{nid} 声明了 vote：多人节点 runtime 落 v1.3（ADR-025）")
        if n.get("when"):
            raise UnsupportedInV1(f"{nid} 声明了 when 守卫：条件分支 runtime 落 v1.3（ADR-025）")
        region = (n.get("deliverable") or {}).get("region", "whole")
        if region != "whole":
            raise UnsupportedInV1(
                f"{nid} deliverable.region={region}：共享协同 section 落 v2（ADR-018）"
            )
