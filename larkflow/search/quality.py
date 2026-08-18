"""Deterministic source-quality normalization and claim evidence checks."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import re
from urllib.parse import urlsplit, urlunsplit

from .contracts import SourceFreshness, SourceHealth
from .outbound import DisabledSafeOutboundFetcher, SafeOutboundFetcher


MAX_SOURCE_URL_CHARS = 4_096
MAX_SOURCE_TITLE_CHARS = 500
MAX_SOURCE_SNIPPET_CHARS = 4_000
MAX_CLAIM_TEXT_CHARS = 1_000
MAX_SUPPORTING_EXCERPT_CHARS = 1_000
MAX_CLAIMS = 50
CLAIM_ID_RE = re.compile(r"^C[1-9][0-9]{0,2}$")
SOURCE_AUTHORITIES = frozenset(
    {
        "government",
        "operator",
        "academic",
        "publisher",
        "vendor",
        "community",
        "unknown",
    }
)


@dataclass(frozen=True)
class SourceQualityPolicy:
    """Node-owned freshness policy; absence keeps freshness unknown."""

    as_of: date
    freshness_max_age_days: int | None = None

    def __post_init__(self) -> None:
        if self.freshness_max_age_days is not None and not (
            1 <= self.freshness_max_age_days <= 3_650
        ):
            raise ValueError("freshness_max_age_days must be between 1 and 3650")


def normalize_source_url(value: object) -> str | None:
    """Return a stable structural HTTP(S) URL without claiming reachability."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_SOURCE_URL_CHARS:
        return None
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    scheme = parsed.scheme.casefold()
    host = hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, host, path, parsed.query, ""))


def source_freshness(
    published_at: str | None,
    *,
    policy: SourceQualityPolicy,
) -> SourceFreshness:
    """Classify an ISO date only when the node states a freshness policy."""

    if policy.freshness_max_age_days is None or not published_at:
        return "unknown"
    parsed = _published_date(published_at)
    if parsed is None or parsed > policy.as_of:
        return "unknown"
    age_days = (policy.as_of - parsed).days
    return "current" if age_days <= policy.freshness_max_age_days else "stale"


def normalize_source_records(
    records: object,
    sources: Sequence[str],
    *,
    policy: SourceQualityPolicy,
    fetcher: SafeOutboundFetcher | None = None,
) -> tuple[dict[str, object], ...]:
    """Upgrade legacy records and attach independently observed quality."""

    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError("source records must be a sequence")
    expected_urls = tuple(normalize_source_url(url) for url in sources)
    if any(url is None for url in expected_urls) or len(set(expected_urls)) != len(
        expected_urls
    ):
        raise ValueError("source URLs are invalid or duplicated")
    by_url: dict[str, Mapping[str, object]] = {}
    for raw in records:
        if not isinstance(raw, Mapping):
            raise ValueError("source record must be an object")
        normalized_url = normalize_source_url(raw.get("source_url"))
        if normalized_url is None or normalized_url in by_url:
            raise ValueError("source record URL is invalid or duplicated")
        by_url[normalized_url] = raw
    if set(by_url) != set(expected_urls):
        raise ValueError("source records do not match cited sources")

    outbound = fetcher or DisabledSafeOutboundFetcher()
    enabled, unavailable_reason = outbound.capability()
    normalized: list[dict[str, object]] = []
    for url in expected_urls:
        assert url is not None
        raw = by_url[url]
        title = raw.get("title")
        snippet = raw.get("snippet")
        published_at = raw.get("published_at")
        published_status = raw.get("published_at_status")
        if not isinstance(title, str) or len(title) > MAX_SOURCE_TITLE_CHARS:
            raise ValueError("source title is invalid")
        if not isinstance(snippet, str) or len(snippet) > MAX_SOURCE_SNIPPET_CHARS:
            raise ValueError("source snippet is invalid")
        if published_status not in {"known", "unknown"}:
            raise ValueError("published_at_status is invalid")
        if published_status == "known":
            if not isinstance(published_at, str) or not published_at.strip():
                raise ValueError("known published time is missing")
            published_value: str | None = published_at.strip()
        else:
            if published_at is not None:
                raise ValueError("unknown published time must be null")
            published_value = None

        authority = raw.get("authority", "unknown")
        authority_basis = raw.get("authority_basis", "not_assessed")
        if authority not in SOURCE_AUTHORITIES:
            raise ValueError("source authority category is invalid")
        if not isinstance(authority_basis, str) or not authority_basis.strip():
            raise ValueError("source authority basis is invalid")
        if authority != "unknown" and authority_basis == "domain_heuristic":
            raise ValueError("domain heuristics cannot establish source authority")

        health: SourceHealth = "unknown"
        health_reason = unavailable_reason
        if enabled:
            observation = outbound.check(url=url)
            if observation.health not in {"reachable", "unreachable", "unknown"}:
                raise ValueError("source health observation is invalid")
            if observation.final_url is not None:
                final_url = normalize_source_url(observation.final_url)
                if final_url != url:
                    raise ValueError("source health redirect left the canonical URL")
            health = observation.health
            health_reason = observation.reason
        normalized.append(
            {
                "title": title.strip(),
                "snippet": snippet.strip(),
                "source_url": url,
                "published_at": published_value,
                "published_at_status": published_status,
                "url_status": "valid",
                "health": health,
                "health_reason": health_reason,
                "freshness": source_freshness(published_value, policy=policy),
                "authority": authority,
                "authority_basis": authority_basis.strip(),
                "support": "unknown",
            }
        )
    return tuple(normalized)


def validate_claim_support(
    claims: object,
    source_records: object,
) -> tuple[tuple[dict[str, str], ...], tuple[str, ...]]:
    """Bind claims to exact snippets from the current search Attempt."""

    violations: list[str] = []
    if not isinstance(source_records, Sequence) or isinstance(
        source_records, (str, bytes)
    ):
        return (), ("source_records 必须是数组",)
    snippets: dict[str, str] = {}
    for index, raw in enumerate(source_records):
        if not isinstance(raw, Mapping):
            violations.append(f"source_records[{index}] 结构无效")
            continue
        url = normalize_source_url(raw.get("source_url"))
        snippet = raw.get("snippet")
        if url is None or not isinstance(snippet, str) or not snippet.strip():
            violations.append(f"source_records[{index}] 缺少有效 URL 或证据片段")
            continue
        if url in snippets:
            violations.append(f"来源 URL 重复：{url}")
            continue
        snippets[url] = snippet
    if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
        return (), tuple((*violations, "claims 必须是数组"))
    if not 1 <= len(claims) <= MAX_CLAIMS:
        violations.append(f"claims 必须包含 1 到 {MAX_CLAIMS} 项")

    claim_ids: set[str] = set()
    results: list[dict[str, str]] = []
    expected_fields = {"claim_id", "text", "source_url", "supporting_excerpt"}
    for index, raw in enumerate(claims):
        location = f"claims[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != expected_fields:
            violations.append(f"{location} 结构无效")
            continue
        claim_id = raw.get("claim_id")
        text = raw.get("text")
        source_url = normalize_source_url(raw.get("source_url"))
        excerpt = raw.get("supporting_excerpt")
        if not isinstance(claim_id, str) or not CLAIM_ID_RE.fullmatch(claim_id):
            violations.append(f"{location}.claim_id 无效")
            continue
        if claim_id in claim_ids:
            violations.append(f"claim_id 重复：{claim_id}")
            continue
        claim_ids.add(claim_id)
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_CLAIM_TEXT_CHARS
        ):
            violations.append(f"{location}.text 无效")
        if source_url is None or source_url not in snippets:
            violations.append(f"{location}.source_url 不属于当前搜索 Attempt")
        if (
            not isinstance(excerpt, str)
            or not excerpt.strip()
            or len(excerpt) > MAX_SUPPORTING_EXCERPT_CHARS
        ):
            violations.append(f"{location}.supporting_excerpt 无效")
        elif source_url in snippets and excerpt not in snippets[source_url]:
            violations.append(f"{location}.supporting_excerpt 不是 provider 原文片段")
        results.append(
            {
                "claim_id": claim_id,
                "source_url": source_url or "",
                "supporting_excerpt": excerpt if isinstance(excerpt, str) else "",
                "support": "supported",
            }
        )
    if violations:
        return (), tuple(violations)
    return tuple(results), ()


def _published_date(value: str) -> date | None:
    candidate = value.strip()
    try:
        return date.fromisoformat(candidate[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
    except ValueError:
        return None
