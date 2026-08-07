"""Feishu OAuth and opaque browser sessions for the central console."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from hashlib import sha256
from http.cookies import CookieError, SimpleCookie
import base64
import secrets
from threading import Lock
import time
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from .console import (
    ConsolePrincipal,
    InvalidConsoleCredentialError,
)


FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
SESSION_COOKIE_NAME = "__Host-larkflow_console_session"
OAUTH_STATE_COOKIE_NAME = "__Host-larkflow_console_oauth_state"


class ConsoleOAuthError(RuntimeError):
    """Raised when a login request cannot be completed safely."""


class ConsoleOAuthAccessDeniedError(ConsoleOAuthError):
    """Raised when the user declines the Feishu authorization request."""


class ConsoleOAuthProviderError(ConsoleOAuthError):
    """Raised when Feishu does not return a valid identity."""


@dataclass(frozen=True)
class FeishuOAuthIdentity:
    tenant_key: str
    open_id: str

    def __post_init__(self) -> None:
        if not self.tenant_key.strip() or not self.open_id.strip():
            raise ValueError("Feishu identity requires tenant_key and open_id")


class FeishuIdentityProvider(Protocol):
    def exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
        redirect_uri: str,
    ) -> FeishuOAuthIdentity:
        """Exchange a single-use OAuth code for a verified Feishu identity."""


class FeishuOAuthClient:
    """Use OAuth v3, then discard the user access token after identity lookup."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not app_id.strip() or not app_secret.strip():
            raise ValueError("Feishu OAuth requires app_id and app_secret")
        self.app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        self._client = client

    def exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
        redirect_uri: str,
    ) -> FeishuOAuthIdentity:
        code = code.strip()
        if not code or len(code) > 1_024:
            raise ConsoleOAuthProviderError("Feishu authorization code is invalid")
        if not 43 <= len(code_verifier) <= 128:
            raise ConsoleOAuthProviderError("PKCE verifier is invalid")

        owned_client = self._client is None
        client = self._client or httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )
        context = client if owned_client else nullcontext(client)
        try:
            with context as active_client:
                token_response = active_client.post(
                    FEISHU_TOKEN_URL,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    json={
                        "grant_type": "authorization_code",
                        "client_id": self.app_id,
                        "client_secret": self._app_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "code_verifier": code_verifier,
                    },
                )
                token_document = _json_object(token_response)
                access_token = token_document.get("access_token")
                if (
                    token_response.status_code != 200
                    or token_document.get("code") != 0
                    or not isinstance(access_token, str)
                    or not access_token.strip()
                ):
                    raise ConsoleOAuthProviderError(
                        "Feishu did not issue a user access token"
                    )

                user_response = active_client.get(
                    FEISHU_USER_INFO_URL,
                    headers={"Authorization": f"Bearer {access_token.strip()}"},
                )
                user_document = _json_object(user_response)
                user = user_document.get("data")
                if (
                    user_response.status_code != 200
                    or user_document.get("code") != 0
                    or not isinstance(user, dict)
                ):
                    raise ConsoleOAuthProviderError(
                        "Feishu did not return a valid user identity"
                    )
                tenant_key = user.get("tenant_key")
                open_id = user.get("open_id")
                if not isinstance(tenant_key, str) or not isinstance(open_id, str):
                    raise ConsoleOAuthProviderError(
                        "Feishu user identity is incomplete"
                    )
                return FeishuOAuthIdentity(
                    tenant_key=tenant_key.strip(),
                    open_id=open_id.strip(),
                )
        except ConsoleOAuthProviderError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ConsoleOAuthProviderError(
                "Feishu OAuth request failed"
            ) from exc


@dataclass(frozen=True)
class _StoredSession:
    principal: ConsolePrincipal
    created_at: float
    expires_at: float


class InMemoryConsoleSessionAuthenticator:
    """Keep opaque session credentials server-side for one Console process."""

    mode = "feishu"

    def __init__(
        self,
        *,
        ttl_seconds: int = 28_800,
        max_sessions: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds < 300 or ttl_seconds > 86_400:
            raise ValueError("console session TTL must be between 300 and 86400 seconds")
        if max_sessions < 1 or max_sessions > 100_000:
            raise ValueError("max_sessions must be between 1 and 100000")
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[str, _StoredSession] = {}
        self._lock = Lock()

    def issue(self, principal: ConsolePrincipal) -> str:
        now = self._clock()
        credential = secrets.token_urlsafe(48)
        digest = _credential_digest(credential)
        record = _StoredSession(
            principal=principal,
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._discard_expired(now)
            if len(self._sessions) >= self.max_sessions:
                oldest = min(
                    self._sessions,
                    key=lambda item: self._sessions[item].created_at,
                )
                self._sessions.pop(oldest, None)
            self._sessions[digest] = record
        return credential

    def authenticate(self, headers: Mapping[str, str]) -> ConsolePrincipal:
        credential = _cookie_value(headers, SESSION_COOKIE_NAME)
        if not credential:
            raise InvalidConsoleCredentialError("console session is missing")
        digest = _credential_digest(credential)
        now = self._clock()
        with self._lock:
            record = self._sessions.get(digest)
            if record is None or record.expires_at <= now:
                self._sessions.pop(digest, None)
                raise InvalidConsoleCredentialError("console session is invalid")
            return record.principal

    def revoke(self, headers: Mapping[str, str]) -> None:
        credential = _cookie_value(headers, SESSION_COOKIE_NAME)
        if not credential:
            return
        with self._lock:
            self._sessions.pop(_credential_digest(credential), None)

    def session_cookie(self, credential: str) -> str:
        return _cookie_header(
            SESSION_COOKIE_NAME,
            credential,
            max_age=self.ttl_seconds,
        )

    @staticmethod
    def clear_session_cookie() -> str:
        return _cookie_header(SESSION_COOKIE_NAME, "", max_age=0)

    def _discard_expired(self, now: float) -> None:
        expired = [
            digest
            for digest, record in self._sessions.items()
            if record.expires_at <= now
        ]
        for digest in expired:
            self._sessions.pop(digest, None)


@dataclass(frozen=True)
class _OAuthRequest:
    code_verifier: str
    expires_at: float


@dataclass(frozen=True)
class ConsoleOAuthStart:
    location: str
    state_cookie: str


@dataclass(frozen=True)
class ConsoleOAuthFinish:
    location: str
    session_cookie: str


class FeishuConsoleOAuthFlow:
    """Bind Feishu OAuth state to the browser and issue an Owner session."""

    def __init__(
        self,
        *,
        app_id: str,
        public_base_url: str,
        workflow_tenant_id: str,
        allowed_feishu_tenant_key: str,
        identity_provider: FeishuIdentityProvider,
        sessions: InMemoryConsoleSessionAuthenticator,
        state_ttl_seconds: int = 300,
        max_pending_states: int = 1_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not app_id.strip():
            raise ValueError("Feishu OAuth app_id is required")
        if not workflow_tenant_id.strip() or not allowed_feishu_tenant_key.strip():
            raise ValueError("console OAuth requires both tenant mappings")
        if state_ttl_seconds < 60 or state_ttl_seconds > 600:
            raise ValueError("OAuth state TTL must be between 60 and 600 seconds")
        if max_pending_states < 1 or max_pending_states > 10_000:
            raise ValueError("max_pending_states must be between 1 and 10000")

        self.app_id = app_id.strip()
        self.public_base_url = _https_origin(public_base_url)
        self.redirect_uri = f"{self.public_base_url}/console/auth/callback"
        self.workflow_tenant_id = workflow_tenant_id.strip()
        self.allowed_feishu_tenant_key = allowed_feishu_tenant_key.strip()
        self.identity_provider = identity_provider
        self.sessions = sessions
        self.state_ttl_seconds = state_ttl_seconds
        self.max_pending_states = max_pending_states
        self._clock = clock
        self._states: dict[str, _OAuthRequest] = {}
        self._lock = Lock()

    def begin(self) -> ConsoleOAuthStart:
        now = self._clock()
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        with self._lock:
            self._discard_expired_states(now)
            if len(self._states) >= self.max_pending_states:
                oldest = min(
                    self._states,
                    key=lambda item: self._states[item].expires_at,
                )
                self._states.pop(oldest, None)
            self._states[_credential_digest(state)] = _OAuthRequest(
                code_verifier=code_verifier,
                expires_at=now + self.state_ttl_seconds,
            )

        query = urlencode(
            {
                "client_id": self.app_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return ConsoleOAuthStart(
            location=f"{FEISHU_AUTHORIZE_URL}?{query}",
            state_cookie=_cookie_header(
                OAUTH_STATE_COOKIE_NAME,
                state,
                max_age=self.state_ttl_seconds,
            ),
        )

    def finish(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None,
        headers: Mapping[str, str],
    ) -> ConsoleOAuthFinish:
        returned_state = (state or "").strip()
        cookie_state = _cookie_value(headers, OAUTH_STATE_COOKIE_NAME)
        if (
            not returned_state
            or len(returned_state) > 512
            or not cookie_state
            or not secrets.compare_digest(returned_state, cookie_state)
        ):
            raise ConsoleOAuthError("OAuth state is invalid")

        digest = _credential_digest(returned_state)
        now = self._clock()
        with self._lock:
            request = self._states.pop(digest, None)
        if request is None or request.expires_at <= now:
            raise ConsoleOAuthError("OAuth state is missing or expired")
        if error is not None:
            if error == "access_denied":
                raise ConsoleOAuthAccessDeniedError("Feishu access was denied")
            raise ConsoleOAuthError("Feishu returned an OAuth error")
        if code is None or not code.strip():
            raise ConsoleOAuthError("Feishu authorization code is missing")

        identity = self.identity_provider.exchange_code(
            code.strip(),
            code_verifier=request.code_verifier,
            redirect_uri=self.redirect_uri,
        )
        if not secrets.compare_digest(
            identity.tenant_key,
            self.allowed_feishu_tenant_key,
        ):
            raise ConsoleOAuthError("Feishu tenant is not allowed")
        credential = self.sessions.issue(
            ConsolePrincipal(
                tenant_id=self.workflow_tenant_id,
                person_id=identity.open_id,
            )
        )
        return ConsoleOAuthFinish(
            location="/console/",
            session_cookie=self.sessions.session_cookie(credential),
        )

    def _discard_expired_states(self, now: float) -> None:
        expired = [
            digest
            for digest, request in self._states.items()
            if request.expires_at <= now
        ]
        for digest in expired:
            self._states.pop(digest, None)


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        document = response.json()
    except ValueError as exc:
        raise ConsoleOAuthProviderError(
            "Feishu returned an invalid JSON response"
        ) from exc
    if not isinstance(document, dict):
        raise ConsoleOAuthProviderError("Feishu returned an invalid response")
    return document


def _credential_digest(credential: str) -> str:
    return sha256(credential.encode("utf-8")).hexdigest()


def _cookie_value(headers: Mapping[str, str], name: str) -> str | None:
    raw = next(
        (value for key, value in headers.items() if key.lower() == "cookie"),
        "",
    )
    if not raw:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except CookieError:
        return None
    item = cookie.get(name)
    return item.value if item is not None and item.value else None


def _cookie_header(name: str, value: str, *, max_age: int) -> str:
    cookie = SimpleCookie()
    cookie[name] = value
    cookie[name]["path"] = "/"
    cookie[name]["secure"] = True
    cookie[name]["httponly"] = True
    cookie[name]["samesite"] = "Lax"
    cookie[name]["max-age"] = max_age
    return cookie.output(header="").strip()


def _https_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("console public base URL must be an HTTPS origin")
    return f"https://{parsed.netloc}"
