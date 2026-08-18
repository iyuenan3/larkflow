"""Narrow source-health port with no production network adapter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import SourceHealth


@dataclass(frozen=True)
class OutboundFetchResult:
    """Bounded observation from a policy-enforcing outbound adapter."""

    health: SourceHealth
    reason: str
    final_url: str | None = None


class SafeOutboundFetcher(Protocol):
    """Fetch one public URL only after enforcing network and redirect policy.

    An enabled adapter must reject non-HTTP(S), credential URLs, localhost,
    private and link-local destinations, DNS drift, unsafe redirects, login
    state, and oversized responses. This package intentionally provides no
    enabled network implementation yet.
    """

    def capability(self) -> tuple[bool, str]:
        ...

    def check(self, *, url: str) -> OutboundFetchResult:
        ...


class DisabledSafeOutboundFetcher:
    """Production-safe default that never performs an outbound request."""

    def capability(self) -> tuple[bool, str]:
        return False, "safe_outbound_fetcher_unavailable"

    def check(self, *, url: str) -> OutboundFetchResult:
        del url
        return OutboundFetchResult(
            health="unknown",
            reason="safe_outbound_fetcher_unavailable",
        )
