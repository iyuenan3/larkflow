"""Thin Doubao Search Custom API adapter for explicit public research."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
from urllib.parse import urlsplit

from .contracts import (
    SearchCapability,
    SearchEvidenceMissingError,
    SearchProviderError,
    SearchProtocolError,
    SearchQuotaExhaustedError,
    SearchRateLimitedError,
    SearchResult,
    SearchSource,
    SearchTransportError,
    SearchTimeoutError,
    SearchUnavailableError,
    SearchUsage,
)
from .quality import normalize_source_url


DOUBAO_SEARCH_ENDPOINT = "https://open.feedcoopapi.com/search_api/web_search"
DOUBAO_SEARCH_PROVIDER = "doubao_search_custom"
MAX_QUERY_CHARS = 100
DEFAULT_RESULT_COUNT = 10
MAX_RESULT_COUNT = 50
MAX_SNIPPET_CHARS = 4_000


@dataclass(frozen=True)
class DoubaoSearchConfig:
    """Server-owned route configuration. The API key never enters repr."""

    api_key: str = field(repr=False)
    bot_id: str | None = None
    api_id: str | None = None
    endpoint: str = DOUBAO_SEARCH_ENDPOINT
    timeout_seconds: float = 30.0
    result_count: int = DEFAULT_RESULT_COUNT

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise SearchUnavailableError("Doubao Search API key is not configured")
        if self.endpoint != DOUBAO_SEARCH_ENDPOINT:
            raise ValueError("Doubao Search endpoint is server-owned")
        if self.timeout_seconds <= 0:
            raise ValueError("Doubao Search timeout must be positive")
        if not 1 <= self.result_count <= MAX_RESULT_COUNT:
            raise ValueError("Doubao Search result count must be between 1 and 50")


class DoubaoSearchProvider:
    """Call the Custom API and normalize cited search evidence.

    This object has no repository, Lark credential, or DAG mutation capability.
    ``transport`` exists only as a narrow test seam and returns decoded JSON.
    """

    def __init__(
        self,
        config: DoubaoSearchConfig,
        *,
        transport: Callable[..., Mapping[str, object]] | None = None,
    ) -> None:
        self.config = config
        self._transport = transport

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
        *,
        transport: Callable[..., Mapping[str, object]] | None = None,
    ) -> DoubaoSearchProvider:
        return cls(
            DoubaoSearchConfig(
                api_key=environ.get("LARKFLOW_DOUBAO_SEARCH_API_KEY", "").strip(),
                bot_id=(
                    environ.get("LARKFLOW_DOUBAO_SEARCH_BOT_ID", "").strip()
                    or None
                ),
                api_id=(
                    environ.get("LARKFLOW_DOUBAO_SEARCH_API_ID", "").strip()
                    or None
                ),
            ),
            transport=transport,
        )

    @staticmethod
    def preflight(environ: Mapping[str, str]) -> SearchCapability:
        api_key = environ.get("LARKFLOW_DOUBAO_SEARCH_API_KEY", "").strip()
        resource_configured = bool(
            api_key
            or environ.get("LARKFLOW_DOUBAO_SEARCH_BOT_ID", "").strip()
            or environ.get("LARKFLOW_DOUBAO_SEARCH_API_ID", "").strip()
        )
        if not api_key:
            return SearchCapability(
                provider=DOUBAO_SEARCH_PROVIDER,
                configured=resource_configured,
                available=False,
                reason="api_key_missing",
            )
        return SearchCapability(
            provider=DOUBAO_SEARCH_PROVIDER,
            configured=True,
            available=True,
            reason="configured",
        )

    def capability(self) -> SearchCapability:
        return SearchCapability(
            provider=DOUBAO_SEARCH_PROVIDER,
            configured=True,
            available=True,
            reason="configured",
        )

    def search(self, *, query: str) -> SearchResult:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Doubao Search query is required")
        if len(normalized_query) > MAX_QUERY_CHARS:
            raise ValueError(
                f"Doubao Search query exceeds {MAX_QUERY_CHARS} characters"
            )
        body = {
            "Query": normalized_query,
            "SearchType": "web",
            "Count": self.config.result_count,
            "NeedSummary": True,
        }
        data = self._post(body)
        return _normalize_response(data, query=normalized_query)

    def _post(self, body: Mapping[str, object]) -> Mapping[str, object]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        if self._transport is not None:
            try:
                data = self._transport(
                    self.config.endpoint,
                    headers=headers,
                    json=dict(body),
                    timeout=self.config.timeout_seconds,
                )
            except SearchProviderError:
                raise
            except TimeoutError as exc:
                raise SearchTimeoutError("Doubao Search request timed out") from exc
            except Exception as exc:
                raise SearchTransportError(
                    f"Doubao Search request failed: {type(exc).__name__}"
                ) from exc
            if not isinstance(data, Mapping):
                raise SearchProtocolError("Doubao Search returned non-object JSON")
            return data

        try:
            import httpx

            with httpx.Client(
                timeout=self.config.timeout_seconds,
                trust_env=False,
            ) as client:
                response = client.post(
                    self.config.endpoint,
                    headers=headers,
                    content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                )
                if response.status_code == 429:
                    raise SearchRateLimitedError(
                        "Doubao Search rate limit was reached"
                    )
                response.raise_for_status()
                data = response.json()
        except SearchProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise SearchTimeoutError("Doubao Search request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise SearchTransportError(
                "Doubao Search returned an HTTP failure"
            ) from exc
        except httpx.TransportError as exc:
            raise SearchTransportError(
                "Doubao Search transport failed"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise SearchProtocolError(
                "Doubao Search returned invalid JSON"
            ) from exc
        if not isinstance(data, Mapping):
            raise SearchProtocolError("Doubao Search returned non-object JSON")
        return data


def _normalize_response(
    data: Mapping[str, object],
    *,
    query: str,
) -> SearchResult:
    _raise_response_error(data)
    result = data.get("Result")
    if not isinstance(result, Mapping):
        raise SearchProtocolError("Doubao Search response has no Result object")
    raw_results = result.get("WebResults")
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
        raise SearchProtocolError("Doubao Search response has no WebResults list")

    sources: list[SearchSource] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        if not isinstance(item, Mapping):
            continue
        url = normalize_source_url(item.get("Url"))
        if url is None or url in seen_urls:
            continue
        seen_urls.add(url)
        host = urlsplit(url).hostname or url
        title = _bounded_text(item.get("Title"), 500) or host
        snippet = _bounded_text(
            item.get("Summary") or item.get("Snippet") or item.get("Content"),
            MAX_SNIPPET_CHARS,
        )
        published_at = _published_at(item)
        sources.append(
            SearchSource(
                title=title,
                snippet=snippet,
                source_url=url,
                published_at=published_at,
                published_at_status=("known" if published_at else "unknown"),
            )
        )
    if not sources:
        raise SearchEvidenceMissingError(
            "Doubao Search returned no verifiable source URLs"
        )

    count = _non_negative_int(result.get("ResultCount"))
    time_cost = _non_negative_int(result.get("TimeCost"))
    metadata = data.get("ResponseMetadata")
    request_id = None
    if isinstance(metadata, Mapping):
        request_id = _bounded_text(
            metadata.get("RequestId") or metadata.get("RequestID"),
            200,
        ) or None
    return SearchResult(
        provider=DOUBAO_SEARCH_PROVIDER,
        query=query,
        sources=tuple(sources),
        usage=SearchUsage(
            result_count=count if count is not None else len(sources),
            time_cost_ms=time_cost,
            provider_request_id=request_id,
        ),
        error=None,
    )


def render_search_result(result: SearchResult) -> str:
    """Render evidence without asking the search service to synthesize claims."""

    lines = ["检索结果："]
    for index, source in enumerate(result.sources, 1):
        lines.append(f"{index}. {source.title}")
        if source.snippet:
            lines.append(f"   摘要：{source.snippet}")
        lines.append(f"   来源：{source.source_url}")
        lines.append(f"   发布时间：{source.published_at or '时间不明'}")
    return "\n".join(lines)


def _response_error_code(data: Mapping[str, object]) -> str | None:
    metadata = data.get("ResponseMetadata")
    if isinstance(metadata, Mapping):
        error = metadata.get("Error")
        if isinstance(error, Mapping):
            code = _bounded_scalar(error.get("Code") or error.get("CodeN"), 100)
            if code:
                return code
    error = data.get("Error")
    if isinstance(error, Mapping):
        code = _bounded_scalar(error.get("Code") or error.get("CodeN"), 100)
        if code:
            return code
    code = _bounded_scalar(data.get("Code") or data.get("CodeN"), 100)
    if code and code not in {"0", "200"}:
        return code
    return None


def _raise_response_error(data: Mapping[str, object]) -> None:
    code = _response_error_code(data)
    if not code:
        return
    normalized = code.casefold().replace("-", "_")
    if any(term in normalized for term in ("quota", "insufficient", "exhaust")):
        raise SearchQuotaExhaustedError("Doubao Search quota is exhausted")
    if any(term in normalized for term in ("429", "rate", "throttl", "too_many")):
        raise SearchRateLimitedError("Doubao Search rate limit was reached")
    raise SearchProtocolError("Doubao Search rejected the request")


def _published_at(item: Mapping[str, object]) -> str | None:
    for key in (
        "PublishTime",
        "PublishedTime",
        "PublishedAt",
        "DatePublished",
    ):
        value = _bounded_scalar(item.get(key), 100)
        if value:
            return value
    return None


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _bounded_scalar(value: object, limit: int) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float, str)):
        return str(value).strip()[:limit]
    return ""


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None
