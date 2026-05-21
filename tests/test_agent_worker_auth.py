import base64
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from src.api.routes import registry
from src.api.server import create_app


SECRET = "test-service-secret"


def _b64(data):
    encoded = base64.urlsafe_b64encode(data).decode("ascii")
    return encoded.rstrip("=")


def service_token(**overrides):
    now = int(time.time())
    payload = {
        "sub": "worker-service",
        "aud": "agent-workers",
        "scope": "agent:worker",
        "workspace_role": "operator",
        "iat": now,
        "exp": now + 300,
        "jti": "token-1",
    }
    payload.update(overrides)
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join(
        [
            _b64(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(
        SECRET.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_b64(signature)}"


def auth_header(token=None):
    return {"Authorization": f"Bearer {token or service_token()}"}


def reset_registry():
    registry._agents.clear()
    registry._index.clear()


def make_client(monkeypatch, revoked=""):
    monkeypatch.setenv("AO_SERVICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("AO_REVOKED_SERVICE_JTIS", revoked)
    reset_registry()
    return TestClient(create_app())


def register_agent(client, **kwargs):
    params = {"name": "build-worker", "agent_type": "worker.processor"}
    params.update(kwargs.pop("params", {}))
    return client.post("/api/v2/agents", params=params, **kwargs)


def test_missing_worker_credentials_are_denied_before_handler(monkeypatch):
    client = make_client(monkeypatch)

    response = register_agent(client)

    assert response.status_code == 401
    assert registry.count() == 0


def test_malformed_worker_token_is_denied_before_handler(monkeypatch):
    client = make_client(monkeypatch)

    response = register_agent(client, headers=auth_header("not-a-jwt"))

    assert response.status_code == 401
    assert registry.count() == 0


def test_anonymous_worker_token_is_denied_before_handler(monkeypatch):
    client = make_client(monkeypatch)

    response = register_agent(
        client,
        headers=auth_header(service_token(sub="")),
    )

    assert response.status_code == 401
    assert registry.count() == 0


def test_wrong_audience_is_denied_before_handler(monkeypatch):
    client = make_client(monkeypatch)

    response = register_agent(
        client,
        headers=auth_header(service_token(aud="public-api")),
    )

    assert response.status_code == 401
    assert registry.count() == 0


def test_expired_token_is_denied_before_handler(monkeypatch):
    client = make_client(monkeypatch)
    now = int(time.time())

    response = register_agent(
        client,
        headers=auth_header(service_token(iat=now - 600, exp=now - 1)),
    )

    assert response.status_code == 401
    assert registry.count() == 0


def test_revoked_token_is_denied_before_handler(monkeypatch):
    client = make_client(monkeypatch, revoked="revoked-token")

    response = register_agent(
        client,
        headers=auth_header(service_token(jti="revoked-token")),
    )

    assert response.status_code == 401
    assert registry.count() == 0


def test_insufficient_scope_is_denied_before_handler(monkeypatch):
    client = make_client(monkeypatch)

    response = register_agent(
        client,
        headers=auth_header(service_token(scope="agent:read")),
    )

    assert response.status_code == 403
    assert registry.count() == 0


def test_insufficient_workspace_role_is_denied_before_handler(monkeypatch):
    client = make_client(monkeypatch)

    response = register_agent(
        client,
        headers=auth_header(service_token(workspace_role="viewer")),
    )

    assert response.status_code == 403
    assert registry.count() == 0


def test_authorized_bearer_principal_can_complete_worker_flow(monkeypatch):
    client = make_client(monkeypatch)

    created = register_agent(client, headers=auth_header())
    agent_id = created.json()["agent_id"]
    started = client.post(
        f"/api/v2/agents/{agent_id}/start",
        headers=auth_header(service_token(jti="token-2")),
    )
    listed = client.get(
        "/api/v2/agents",
        headers=auth_header(service_token(jti="token-3")),
    )

    assert created.status_code == 200
    assert started.status_code == 200
    assert listed.status_code == 200
    assert listed.json()["agents"][0]["id"] == agent_id
    assert registry.get(agent_id)["status"] == "running"


def test_session_cookie_uses_same_worker_auth_policy(monkeypatch):
    client = make_client(monkeypatch)
    client.cookies.set("ao_session", service_token(jti="session-token"))

    response = register_agent(client)

    assert response.status_code == 200
    assert registry.count() == 1
