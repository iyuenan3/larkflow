"""Explicit, source-preserving public search adapters."""

from .contracts import (
    SearchCapability,
    SearchEvidenceMissingError,
    SearchProtocolError,
    SearchProvider,
    SearchProviderError,
    SearchResult,
    SearchSource,
    SearchTransportError,
    SearchUnavailableError,
    SearchUsage,
)
from .doubao import (
    DOUBAO_SEARCH_ENDPOINT,
    DOUBAO_SEARCH_PROVIDER,
    DoubaoSearchConfig,
    DoubaoSearchProvider,
    render_search_result,
)

__all__ = [
    "DOUBAO_SEARCH_ENDPOINT",
    "DOUBAO_SEARCH_PROVIDER",
    "DoubaoSearchConfig",
    "DoubaoSearchProvider",
    "SearchCapability",
    "SearchEvidenceMissingError",
    "SearchProtocolError",
    "SearchProvider",
    "SearchProviderError",
    "SearchResult",
    "SearchSource",
    "SearchTransportError",
    "SearchUnavailableError",
    "SearchUsage",
    "render_search_result",
]
