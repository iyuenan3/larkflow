"""Fast visual feedback for interactive Feishu cards."""
from __future__ import annotations

from datetime import timedelta
from typing import Any


CARD_FEEDBACK_FALLBACK = timedelta(seconds=10)


def processing_card(*, title: str, content: str) -> dict[str, Any]:
    """Replace live controls with a visible, non-clickable processing state."""

    if not title.strip() or not content.strip():
        raise ValueError("processing card title and content are required")
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": title.strip()},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "<text_tag color='blue'>处理中</text_tag> "
                        f"{content.strip()}\n\n请稍候，处理结果会更新在本卡片中。"
                    ),
                }
            ]
        },
    }


def rejected_card(*, title: str, content: str) -> dict[str, Any]:
    """Render a terminal rejection without leaving stale controls behind."""

    if not title.strip() or not content.strip():
        raise ValueError("rejected card title and content are required")
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": title.strip()},
            "template": "orange",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": content.strip(),
                }
            ]
        },
    }


__all__ = ["CARD_FEEDBACK_FALLBACK", "processing_card", "rejected_card"]
