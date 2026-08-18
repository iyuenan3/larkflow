"""Provider-neutral contracts for explicit public-information search."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


PublishedAtStatus = Literal["known", "unknown"]


@dataclass(frozen=True)
class SearchCapability:
    """Static deployment preflight without performing a remote request."""

    provider: str
    configured: bool
    available: bool
    reason: str
    requires_source_urls: bool = True


@dataclass(frozen=True)
class SearchSource:
    """One provider result with a verifiable public source URL."""

    title: str
    snippet: str
    source_url: str
    published_at: str | None
    published_at_status: PublishedAtStatus

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "snippet": self.snippet,
            "source_url": self.source_url,
            "published_at": self.published_at,
            "published_at_status": self.published_at_status,
        }


@dataclass(frozen=True)
class SearchUsage:
    """Bounded provider observations safe to persist with a Tool Attempt."""

    result_count: int
    time_cost_ms: int | None = None
    provider_request_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "result_count": self.result_count,
            "time_cost_ms": self.time_cost_ms,
            "provider_request_id": self.provider_request_id,
        }


@dataclass(frozen=True)
class SearchResult:
    """Normalized search evidence before downstream Agent synthesis."""

    provider: str
    query: str
    sources: tuple[SearchSource, ...]
    usage: SearchUsage
    error: str | None = field(default=None)


class SearchProvider(Protocol):
    """Narrow read-only port used by one explicit ``web.search`` node."""

    def capability(self) -> SearchCapability:
        ...

    def search(self, *, query: str) -> SearchResult:
        ...


class SearchProviderError(RuntimeError):
    """A safe provider error that can become an Attempt error code."""

    error_code = "search_provider_error"


class SearchUnavailableError(SearchProviderError):
    error_code = "search_unavailable"


class SearchTransportError(SearchProviderError):
    error_code = "search_transport_error"


class SearchProtocolError(SearchProviderError):
    error_code = "search_protocol_error"


class SearchEvidenceMissingError(SearchProviderError):
    error_code = "search_evidence_missing"
