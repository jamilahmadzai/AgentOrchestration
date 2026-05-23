import asyncio
import base64
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from src.api.auth import (
    BROWSER_AUDIENCE,
    DEFAULT_JWT_SECRET,
    SERVICE_AUDIENCE,
    clear_revoked_tokens,
    parse_bearer_token,
    revoke_token,
)
from src.api.middleware import AuthMiddleware
from src.api.server import create_app


def _b64encode_json(value):
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _token(header=None, secret=DEFAULT_JWT_SECRET, **overrides):
    now = int(time.time())
    payload = {
        "sub": "user-1",
        "aud": BROWSER_AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "jti": f"token-{now}",
        "scope": "agents:read agents:write",
        "workspace_id": "workspace-1",
        "workspace_role": "operator",
    }
    payload.update(overrides)
    token_header = {"alg": "HS256", "typ": "JWT"}
    token_header.update(header or {})
    signing_input = (
        f"{_b64encode_json(token_header)}.{_b64encode_json(payload)}"
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return (
        f"{signing_input}."
        f"{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"
    )


def _headers(token=None, scheme="Bearer"):
    return {"Authorization": f"{scheme} {token or _token()}"}


def _request(path="/api/v2/agents", method="GET", headers=None):
    raw_headers = [
        (key.lower().encode("ascii"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": raw_headers,
        }
    )


def _run_middleware(request):
    state = {"called": False}

    async def call_next(received_request):
        state["called"] = True
        if (
            received_request.url.path.startswith("/api/v2")
            and received_request.url.path != "/api/v2/auth/token"
        ):
            assert received_request.state.principal.subject == "user-1"
        return Response(status_code=204)

    middleware = AuthMiddleware(None)
    response = asyncio.run(middleware.dispatch(request, call_next))
    return response, state


def setup_function():
    clear_revoked_tokens()


def test_parse_bearer_scheme_is_case_insensitive():
    token = _token()

    assert parse_bearer_token(f"Bearer {token}") == token
    assert parse_bearer_token(f"bearer {token}") == token
    assert parse_bearer_token(f"BEARER {token}") == token
    assert parse_bearer_token(f"bEaReR {token}") == token


def test_public_token_and_health_routes_bypass_auth():
    for path in ("/api/v2/auth/token", "/health"):
        response, state = _run_middleware(_request(path=path))

        assert response.status_code == 204
        assert state["called"] is True


def test_authorized_browser_and_service_audiences_are_accepted():
    for audience in (BROWSER_AUDIENCE, SERVICE_AUDIENCE):
        response, state = _run_middleware(
            _request(headers=_headers(_token(aud=audience)))
        )

        assert response.status_code == 204
        assert state["called"] is True


def test_list_audience_and_scope_claims_can_mutate():
    response, state = _run_middleware(
        _request(
            method="POST",
            headers=_headers(
                _token(
                    aud=[BROWSER_AUDIENCE, SERVICE_AUDIENCE],
                    scopes=["agents:write"],
                    workspace_role="operator",
                )
            ),
        )
    )

    assert response.status_code == 204
    assert state["called"] is True


def test_lowercase_and_mixed_case_bearer_reach_handler():
    for scheme in ("bearer", "BEARER", "bEaReR"):
        response, state = _run_middleware(
            _request(headers=_headers(scheme=scheme))
        )

        assert response.status_code == 204
        assert state["called"] is True


def test_malformed_or_non_bearer_headers_fail_before_handler():
    for authorization in ("", "Basic abc", "Bearer", "Bearer token extra"):
        response, state = _run_middleware(
            _request(headers={"Authorization": authorization})
        )

        assert response.status_code == 401
        assert state["called"] is False


def test_stale_revoked_anonymous_and_wrong_audience_fail_before_handler():
    now = int(time.time())
    revoked = _token(jti="revoked-token")
    revoke_token("revoked-token")
    cases = [
        (_token(iat=now - 7200, exp=now + 300), 401),
        (_token(exp=now - 1), 401),
        (_token(iat=now + 30), 401),
        (_token(nbf=now + 30), 401),
        (revoked, 401),
        (_token(revoked=True), 401),
        (_token(sub="anonymous"), 401),
        (_token(anonymous=True), 401),
        (_token(aud="unknown-client"), 401),
    ]

    for token, status_code in cases:
        response, state = _run_middleware(_request(headers=_headers(token)))

        assert response.status_code == status_code
        assert state["called"] is False


def test_env_revoked_token_id_fails_before_handler(monkeypatch):
    token = _token(jti="env-revoked-token")
    monkeypatch.setenv("AO_REVOKED_TOKEN_IDS", "env-revoked-token")

    response, state = _run_middleware(_request(headers=_headers(token)))

    assert response.status_code == 401
    assert state["called"] is False


def test_malformed_and_unsupported_tokens_fail_before_handler():
    cases = [
        "not-a-jwt",
        _token(header={"alg": "none"}),
        _token(secret="wrong-secret"),
    ]

    for token in cases:
        response, state = _run_middleware(_request(headers=_headers(token)))

        assert response.status_code == 401
        assert state["called"] is False


def test_insufficient_scope_or_role_fails_before_handler():
    cases = [
        (_token(scope="metrics:read"), "GET"),
        (_token(scope="agents:read"), "POST"),
        (_token(scope="agents:write", workspace_role="viewer"), "POST"),
        (_token(scope="agents:write", workspace_role="guest"), "POST"),
    ]

    for token, method in cases:
        response, state = _run_middleware(
            _request(method=method, headers=_headers(token))
        )

        assert response.status_code == 403
        assert state["called"] is False


def test_authorized_workspace_role_can_complete_mutating_workflow():
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/agents",
        params={"name": "worker-1", "agent_type": "worker.processor"},
        headers=_headers(_token(scope="agents:write", workspace_role="admin")),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "registered"


def test_signed_token_tampering_is_denied():
    token = _token()
    header, payload, signature = token.split(".")
    decoded = json.loads(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    decoded["workspace_role"] = "admin"
    tampered = f"{header}.{_b64encode_json(decoded)}.{signature}"

    response, state = _run_middleware(_request(headers=_headers(tampered)))

    assert response.status_code == 401
    assert state["called"] is False
