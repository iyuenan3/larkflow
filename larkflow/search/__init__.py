"""Explicit, source-preserving public search adapters."""

from .contracts import (
    SearchCapability,
    SearchEvidenceMissingError,
    SearchQuotaExhaustedError,
    SearchRateLimitedError,
    SearchProtocolError,
    SearchProvider,
    SearchProviderError,
    SearchResult,
    SearchSource,
    SearchTransportError,
    SearchTimeoutError,
    SearchUnavailableError,
    SearchUsage,
    SearchSourcesUnavailableError,
)
from .doubao import (
    DOUBAO_SEARCH_ENDPOINT,
    DOUBAO_SEARCH_PROVIDER,
    DoubaoSearchConfig,
    DoubaoSearchProvider,
    render_search_result,
)
from .outbound import (
    DisabledSafeOutboundFetcher,
    OutboundFetchResult,
    SafeOutboundFetcher,
)
from .quality import (
    SourceQualityPolicy,
    normalize_source_records,
    normalize_source_url,
    source_freshness,
    validate_claim_support,
)

__all__ = [
    "DOUBAO_SEARCH_ENDPOINT",
    "DOUBAO_SEARCH_PROVIDER",
    "DoubaoSearchConfig",
    "DoubaoSearchProvider",
    "SearchCapability",
    "SearchEvidenceMissingError",
    "SearchQuotaExhaustedError",
    "SearchRateLimitedError",
    "SearchProtocolError",
    "SearchProvider",
    "SearchProviderError",
    "SearchResult",
    "SearchSource",
    "SearchTransportError",
    "SearchTimeoutError",
    "SearchUnavailableError",
    "SearchUsage",
    "SearchSourcesUnavailableError",
    "DisabledSafeOutboundFetcher",
    "OutboundFetchResult",
    "SafeOutboundFetcher",
    "SourceQualityPolicy",
    "normalize_source_records",
    "normalize_source_url",
    "source_freshness",
    "validate_claim_support",
    "render_search_result",
]
