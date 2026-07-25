"""策展模板目录：**只有 yaml，没有 Python**。

tool 节点的确定性动作由 `tool: {kind, args}` 从内置能力库（`larkflow/engine/tools.py`）
选取，故新增一个业务场景 = 新增一个 yaml 文件，不改任何代码（ADR-026）。
真正一次性的确定性代码仍可按 node id 注入 handler 当逃生舱，但那是例外、不是常态。
"""
