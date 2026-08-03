"""Normalize timestamp shapes emitted by Feishu event adapters."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


MICROSECOND_THRESHOLD = 100_000_000_000_000


def feishu_event_time(value: Any, *, fallback: datetime) -> datetime:
    """Accept Feishu millisecond timestamps and lark-cli microseconds."""

    try:
        raw_timestamp = int(value)
    except (TypeError, ValueError):
        return fallback
    divisor = (
        1_000_000
        if abs(raw_timestamp) >= MICROSECOND_THRESHOLD
        else 1_000
    )
    try:
        return datetime.fromtimestamp(raw_timestamp / divisor, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return fallback


__all__ = ["feishu_event_time"]
