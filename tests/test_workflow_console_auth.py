"""Feishu OAuth and browser-session tests for the central workbench."""
from __future__ import annotations

from http.cookies import SimpleCookie
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from larkflow.workflow.console import (
    ConsolePrincipal,
    ConsoleReadService,
    InvalidConsoleCredentialError,
)
from larkflow.workflow.console_auth import (
    ConsoleOAuthError,
    FeishuConsoleOAuthFlow,
    FeishuOAuthClient,
    FeishuOAuthIdentity,
    InMemoryConsoleSessionAuthenticator,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from larkflow.workflow.console_http import ConsoleHttpApplication
from larkflow.workflow.repository import InMemoryWorkflowRepository


APP_ID = "cli_test_console"
APP_SECRET = "test-console-app-secret"
FEISHU_TENANT_KEY = "tenant-key-test"
WORKFLOW_TENANT = "tenant_console"
OWNER = "ou_owner_console"
PUBLIC_ORIGIN = "https://larkflow.example.test"


class FakeIdentityProvider:
    def __init__(self, identity: FeishuOAuthIdentity) -> None:
        self.identity = identity
        self.calls: list[dict[str, str]] = []

    def exchange_code(self, code, *, code_verifier, redirect_uri):
        self.calls.append(
            {
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            }
        )
        return self.identity


def _cookie_pair(header: str, name: str) -> str:
    cookie = SimpleCookie()
    cookie.load(header)
    return f"{name}={cookie[name].value}"


def _flow(
    *,
    provider: FakeIdentityProvider | None = None,
    sessions: InMemoryConsoleSessionAuthenticator | None = None,
    clock=lambda: 1_000.0,
) -> tuple[
    FeishuConsoleOAuthFlow,
    FakeIdentityProvider,
    InMemoryConsoleSessionAuthenticator,
]:
    provider = provider or FakeIdentityProvider(
        FeishuOAuthIdentity(
            tenant_key=FEISHU_TENANT_KEY,
            open_id=OWNER,
        )
    )
    sessions = sessions or InMemoryConsoleSessionAuthenticator(clock=clock)
    return (
        FeishuConsoleOAuthFlow(
            app_id=APP_ID,
            public_base_url=PUBLIC_ORIGIN,
            workflow_tenant_id=WORKFLOW_TENANT,
            allowed_feishu_tenant_key=FEISHU_TENANT_KEY,
            identity_provider=provider,
            sessions=sessions,
            clock=clock,
        ),
        provider,
        sessions,
    )


def test_feishu_oauth_client_uses_official_v2_and_discards_the_user_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/authen/v2/oauth/token":
            body = json.loads(request.content)
            assert body == {
                "grant_type": "authorization_code",
                "client_id": APP_ID,
                "client_secret": APP_SECRET,
                "code": "single-use-code",
                "redirect_uri": f"{PUBLIC_ORIGIN}/console/auth/callback",
                "code_verifier": "v" * 64,
            }
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "access_token": "user-access-token-for-test",
                    "expires_in": 7200,
                    "token_type": "Bearer",
                },
            )
        assert request.url.path == "/open-apis/authen/v1/user_info"
        assert request.headers["Authorization"] == (
            "Bearer user-access-token-for-test"
        )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {
                    "open_id": OWNER,
                    "tenant_key": FEISHU_TENANT_KEY,
                    "name": "not persisted",
                },
            },
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as http_client:
        client = FeishuOAuthClient(
            app_id=APP_ID,
            app_secret=APP_SECRET,
            client=http_client,
        )
        identity = client.exchange_code(
            "single-use-code",
            code_verifier="v" * 64,
            redirect_uri=f"{PUBLIC_ORIGIN}/console/auth/callback",
        )

    assert identity == FeishuOAuthIdentity(
        tenant_key=FEISHU_TENANT_KEY,
        open_id=OWNER,
    )
    assert [request.url.host for request in requests] == [
        "open.feishu.cn",
        "open.feishu.cn",
    ]
    assert "user-access-token-for-test" not in repr(client.__dict__)


def test_opaque_session_is_http_only_expiring_and_revocable():
    now = [1_000.0]
    sessions = InMemoryConsoleSessionAuthenticator(
        ttl_seconds=300,
        clock=lambda: now[0],
    )
    principal = ConsolePrincipal(WORKFLOW_TENANT, OWNER)
    credential = sessions.issue(principal)
    cookie_header = sessions.session_cookie(credential)
    request_headers = {
        "Cookie": _cookie_pair(cookie_header, SESSION_COOKIE_NAME),
    }

    assert "Secure" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header
    assert sessions.authenticate(request_headers) == principal
    context = sessions.authenticate_context(request_headers)
    assert context.principal == principal
    assert context.session_id is not None
    assert len(context.session_id) == 32
    assert credential not in repr(sessions.__dict__)

    collaborator = ConsolePrincipal(WORKFLOW_TENANT, "ou_collaborator_console")
    collaborator_credential = sessions.issue(collaborator)
    assert sessions.authenticate(
        {
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={collaborator_credential}"
            )
        }
    ) == collaborator
    assert sessions.authenticate(request_headers) == principal

    sessions.revoke(request_headers)
    with pytest.raises(InvalidConsoleCredentialError):
        sessions.authenticate(request_headers)

    expiring = sessions.issue(principal)
    now[0] += 301
    with pytest.raises(InvalidConsoleCredentialError):
        sessions.authenticate(
            {"Cookie": f"{SESSION_COOKIE_NAME}={expiring}"}
        )


def test_opaque_session_store_evicts_the_oldest_digest_at_its_bound():
    now = [1_000.0]
    sessions = InMemoryConsoleSessionAuthenticator(
        ttl_seconds=300,
        max_sessions=1,
        clock=lambda: now[0],
    )
    first = sessions.issue(ConsolePrincipal(WORKFLOW_TENANT, OWNER))
    now[0] += 1
    second_principal = ConsolePrincipal(
        WORKFLOW_TENANT,
        "ou_second_console",
    )
    second = sessions.issue(second_principal)

    with pytest.raises(InvalidConsoleCredentialError):
        sessions.authenticate(
            {"Cookie": f"{SESSION_COOKIE_NAME}={first}"}
        )
    assert sessions.authenticate(
        {"Cookie": f"{SESSION_COOKIE_NAME}={second}"}
    ) == second_principal


def test_oauth_flow_binds_state_uses_pkce_and_maps_only_the_allowed_tenant():
    flow, provider, sessions = _flow()
    start = flow.begin()
    authorization = urlsplit(start.location)
    query = parse_qs(authorization.query)
    state = query["state"][0]

    assert authorization.scheme == "https"
    assert authorization.netloc == "accounts.feishu.cn"
    assert query["client_id"] == [APP_ID]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [
        f"{PUBLIC_ORIGIN}/console/auth/callback"
    ]
    assert query["code_challenge_method"] == ["S256"]
    assert "scope" not in query
    assert "Secure" in start.state_cookie
    assert "HttpOnly" in start.state_cookie

    finish = flow.finish(
        code="single-use-code",
        state=state,
        error=None,
        headers={
            "Cookie": _cookie_pair(start.state_cookie, OAUTH_STATE_COOKIE_NAME),
        },
    )

    assert finish.location == "/console/"
    assert provider.calls[0]["code"] == "single-use-code"
    assert 43 <= len(provider.calls[0]["code_verifier"]) <= 128
    assert sessions.authenticate(
        {
            "Cookie": _cookie_pair(
                finish.session_cookie,
                SESSION_COOKIE_NAME,
            )
        }
    ) == ConsolePrincipal(WORKFLOW_TENANT, OWNER)

    with pytest.raises(ConsoleOAuthError):
        flow.finish(
            code="replayed-code",
            state=state,
            error=None,
            headers={
                "Cookie": _cookie_pair(
                    start.state_cookie,
                    OAUTH_STATE_COOKIE_NAME,
                ),
            },
        )
    assert len(provider.calls) == 1

    foreign = FakeIdentityProvider(
        FeishuOAuthIdentity(tenant_key="foreign-tenant", open_id=OWNER)
    )
    foreign_flow, _, _ = _flow(provider=foreign)
    foreign_start = foreign_flow.begin()
    foreign_state = parse_qs(urlsplit(foreign_start.location).query)["state"][0]
    with pytest.raises(ConsoleOAuthError, match="tenant"):
        foreign_flow.finish(
            code="foreign-code",
            state=foreign_state,
            error=None,
            headers={
                "Cookie": _cookie_pair(
                    foreign_start.state_cookie,
                    OAUTH_STATE_COOKIE_NAME,
                ),
            },
        )


def test_http_oauth_login_session_data_and_logout_are_same_origin():
    flow, provider, sessions = _flow()
    application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        sessions,
        oauth=flow,
    )

    auth = application.handle("GET", "/console/api/v1/auth")
    login = application.handle("GET", "/console/auth/login")
    state = parse_qs(urlsplit(login.headers["Location"]).query)["state"][0]
    state_cookie = _cookie_pair(
        login.headers["Set-Cookie"],
        OAUTH_STATE_COOKIE_NAME,
    )
    callback = application.handle(
        "GET",
        f"/console/auth/callback?code=once&state={state}",
        headers={"Cookie": state_cookie},
    )
    session_cookie = _cookie_pair(
        callback.headers["Set-Cookie"],
        SESSION_COOKIE_NAME,
    )
    listing = application.handle(
        "GET",
        "/console/api/v1/instances",
        headers={"Cookie": session_cookie},
    )
    authenticated = application.handle(
        "GET",
        "/console/api/v1/auth",
        headers={"Cookie": session_cookie},
    )
    logout = application.handle(
        "POST",
        "/console/auth/logout",
        headers={"Cookie": session_cookie},
    )
    after_logout = application.handle(
        "GET",
        "/console/api/v1/instances",
        headers={"Cookie": session_cookie},
    )

    assert json.loads(auth.body) == {
        "mode": "feishu",
        "authenticated": False,
        "admin": False,
        "login_url": "/console/auth/login",
        "logout_available": True,
        "capabilities": {
            "attachment_planning": False,
            "enterprise_knowledge_catalog": False,
        },
    }
    assert login.status == 302
    assert login.headers["Location"].startswith(
        "https://accounts.feishu.cn/open-apis/authen/v1/authorize?"
    )
    assert callback.status == 302
    assert callback.headers["Location"] == "/console/"
    assert listing.status == 200
    assert json.loads(authenticated.body)["authenticated"] is True
    assert logout.status == 204
    assert "Max-Age=0" in logout.headers["Set-Cookie"]
    assert after_logout.status == 401
    assert "WWW-Authenticate" not in after_logout.headers
    assert provider.calls[0]["code"] == "once"


def test_http_oauth_callback_rejects_state_drift_without_calling_feishu():
    flow, provider, sessions = _flow()
    application = ConsoleHttpApplication(
        ConsoleReadService(InMemoryWorkflowRepository()),
        sessions,
        oauth=flow,
    )
    login = application.handle("GET", "/console/auth/login")

    response = application.handle(
        "GET",
        "/console/auth/callback?code=once&state=attacker-state",
        headers={
            "Cookie": _cookie_pair(
                login.headers["Set-Cookie"],
                OAUTH_STATE_COOKIE_NAME,
            )
        },
    )

    assert response.status == 302
    assert response.headers["Location"] == "/console/?auth_error=login_failed"
    assert provider.calls == []


@pytest.mark.parametrize(
    "url",
    (
        "http://larkflow.example.test",
        "https://user@larkflow.example.test",
        "https://larkflow.example.test/prefix",
        "https://larkflow.example.test?token=bad",
    ),
)
def test_oauth_flow_requires_a_clean_https_origin(url):
    with pytest.raises(ValueError, match="HTTPS origin"):
        FeishuConsoleOAuthFlow(
            app_id=APP_ID,
            public_base_url=url,
            workflow_tenant_id=WORKFLOW_TENANT,
            allowed_feishu_tenant_key=FEISHU_TENANT_KEY,
            identity_provider=FakeIdentityProvider(
                FeishuOAuthIdentity(FEISHU_TENANT_KEY, OWNER)
            ),
            sessions=InMemoryConsoleSessionAuthenticator(),
        )
