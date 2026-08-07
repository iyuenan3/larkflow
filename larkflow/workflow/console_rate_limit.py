"""Bounded in-memory request limiting for the public Console boundary."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import blake2s
import ipaddress
import math
import secrets
from threading import Lock
import time
from typing import Callable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ConsoleRateLimitDecision:
    allowed: bool
    policy: str
    retry_after_seconds: int = 0


@dataclass
class _Counter:
    tokens: float
    updated_at: float
    last_seen: float


class ConsoleRequestRateLimiter:
    """Apply bounded token buckets without storing raw client addresses."""

    def __init__(
        self,
        *,
        window_seconds: int = 60,
        requests_per_client: int = 300,
        auth_requests_per_client: int = 30,
        admin_writes_per_client: int = 30,
        global_requests: int = 3_000,
        max_client_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
        hash_key: bytes | None = None,
    ) -> None:
        _positive("window_seconds", window_seconds)
        _positive("requests_per_client", requests_per_client)
        _positive("auth_requests_per_client", auth_requests_per_client)
        _positive("admin_writes_per_client", admin_writes_per_client)
        _positive("global_requests", global_requests)
        _positive("max_client_keys", max_client_keys)
        if global_requests < max(
            requests_per_client,
            auth_requests_per_client,
            admin_writes_per_client,
        ):
            raise ValueError("global_requests must cover every per-client budget")
        if hash_key is not None and len(hash_key) < 16:
            raise ValueError("hash_key must contain at least 16 bytes")
        self.window_seconds = window_seconds
        self.requests_per_client = requests_per_client
        self.auth_requests_per_client = auth_requests_per_client
        self.admin_writes_per_client = admin_writes_per_client
        self.global_requests = global_requests
        self.max_client_keys = max_client_keys
        self.clock = clock
        self._hash_key = hash_key or secrets.token_bytes(32)
        self._lock = Lock()
        self._global: _Counter | None = None
        self._clients: OrderedDict[tuple[str, bytes], _Counter] = OrderedDict()

    def check(
        self,
        method: str,
        target: str,
        client_source: str,
    ) -> ConsoleRateLimitDecision:
        policy, client_limit = self._policy(method, target)
        now = self.clock()
        if not math.isfinite(now) or now < 0:
            raise ValueError("rate-limit clock must be a finite non-negative value")
        source_digest = self._source_digest(client_source)
        key = (policy, source_digest)
        with self._lock:
            if self._global is None:
                self._global = _Counter(
                    tokens=float(self.global_requests),
                    updated_at=now,
                    last_seen=now,
                )
            self._refill(self._global, self.global_requests, now)
            counter = self._clients.get(key)
            if counter is None:
                self._make_space()
                counter = _Counter(
                    tokens=float(client_limit),
                    updated_at=now,
                    last_seen=now,
                )
                self._clients[key] = counter
            else:
                self._refill(counter, client_limit, now)
                self._clients.move_to_end(key)
            counter.last_seen = now
            if self._global.tokens < 1:
                return ConsoleRateLimitDecision(
                    allowed=False,
                    policy="global",
                    retry_after_seconds=self._retry_after(
                        self._global,
                        self.global_requests,
                    ),
                )
            if counter.tokens < 1:
                return ConsoleRateLimitDecision(
                    allowed=False,
                    policy=policy,
                    retry_after_seconds=self._retry_after(counter, client_limit),
                )
            self._global.tokens -= 1
            self._global.last_seen = now
            counter.tokens -= 1
            return ConsoleRateLimitDecision(allowed=True, policy=policy)

    def _policy(self, method: str, target: str) -> tuple[str, int]:
        path = urlsplit(target).path
        if path.startswith("/console/auth/") or path == "/console/api/v1/auth":
            return "auth", self.auth_requests_per_client
        if method.upper() == "POST" and path.startswith("/console/api/v1/admin/"):
            return "admin_write", self.admin_writes_per_client
        return "read", self.requests_per_client

    def _source_digest(self, source: str) -> bytes:
        try:
            normalized = ipaddress.ip_address(source.strip()).compressed
        except ValueError:
            normalized = "unknown"
        return blake2s(
            normalized.encode("ascii"),
            key=self._hash_key,
            digest_size=16,
        ).digest()

    def _make_space(self) -> None:
        while len(self._clients) >= self.max_client_keys:
            self._clients.popitem(last=False)

    def _refill(self, counter: _Counter, capacity: int, now: float) -> None:
        elapsed = max(0.0, now - counter.updated_at)
        counter.tokens = min(
            float(capacity),
            counter.tokens + (elapsed * capacity / self.window_seconds),
        )
        counter.updated_at = now
        counter.last_seen = now

    def _retry_after(self, counter: _Counter, capacity: int) -> int:
        missing = max(0.0, 1.0 - counter.tokens)
        return max(1, math.ceil(missing * self.window_seconds / capacity))


def _positive(label: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
