import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.middleware import (
    RequestContextMiddleware,
    correlation_scope_registry,
    get_request_context,
)


def build_client(raise_server_exceptions=True):
    correlation_scope_registry.clear()
    app = FastAPI()
    app.state.context_hits = 0
    app.add_middleware(RequestContextMiddleware)

    @app.get("/context")
    async def context():
        app.state.context_hits += 1
        current = get_request_context()
        return {
            "correlation_id": current.correlation_id if current else None,
            "request_id": current.request_id if current else None,
            "tenant_id": current.tenant_id if current else None,
            "role": current.role if current else None,
            "scope_hash": current.scope_hash if current else None,
        }

    @app.get("/boom")
    async def boom():
        raise RuntimeError("handler failed")

    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_normal_request_sets_headers_and_resets_context():
    client = build_client()

    response = client.get(
        "/context",
        headers={
            "X-Correlation-ID": "corr-a",
            "X-Request-ID": "req-a",
            "X-Tenant-ID": "tenant-a",
            "X-Role": "admin",
        },
    )

    payload = response.json()
    assert payload["correlation_id"] == "corr-a"
    assert payload["request_id"] == "req-a"
    assert payload["tenant_id"] == "tenant-a"
    assert payload["role"] == "admin"
    assert response.headers["X-Correlation-ID"] == "corr-a"
    assert response.headers["X-Request-ID"] == "req-a"
    assert response.headers["X-Context-Decision"] == "accepted"
    assert response.headers["X-Context-Scope"] == payload["scope_hash"]
    assert "tenant-a" not in response.headers["X-Context-Scope"]
    assert get_request_context() is None


def test_same_correlation_id_is_allowed_inside_same_tenant_scope():
    client = build_client()
    headers = {
        "X-Correlation-ID": "shared-corr",
        "X-Tenant-ID": "tenant-a",
        "X-Role": "viewer",
    }

    first = client.get("/context", headers=headers)
    second = client.get("/context", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        first.headers["X-Context-Scope"]
        == second.headers["X-Context-Scope"]
    )


def test_rejects_same_correlation_id_crossing_tenants_before_handler():
    client = build_client()

    first = client.get(
        "/context",
        headers={
            "X-Correlation-ID": "shared-corr",
            "X-Tenant-ID": "tenant-a",
            "X-Role": "viewer",
        },
    )
    second = client.get(
        "/context",
        headers={
            "X-Correlation-ID": "shared-corr",
            "X-Tenant-ID": "tenant-b",
            "X-Role": "viewer",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.text == "Correlation ID is already bound to another context"
    assert second.headers["X-Correlation-ID"] == "shared-corr"
    assert second.headers["X-Context-Decision"] == "rejected"
    assert (
        first.headers["X-Context-Scope"]
        != second.headers["X-Context-Scope"]
    )
    assert get_request_context() is None


def test_rejected_workspace_mismatch_is_sanitized_and_skips_handler(caplog):
    client = build_client()

    with caplog.at_level(logging.WARNING, logger="src.api.middleware"):
        response = client.get(
            "/context",
            headers={
                "X-Correlation-ID": "corr-mismatch",
                "X-Tenant-ID": "tenant-secret-a",
                "X-Workspace-ID": "tenant-secret-b",
                "X-Role": "operator",
            },
        )

    assert response.status_code == 403
    assert response.text == "Tenant context mismatch"
    assert response.headers["X-Context-Decision"] == "rejected"
    assert "rejected request context mismatch" in caplog.text
    assert "tenant-secret-a" not in caplog.text
    assert "tenant-secret-b" not in caplog.text
    assert caplog.records[0].context_scope == response.headers[
        "X-Context-Scope"
    ]
    assert client.app.state.context_hits == 0
    assert get_request_context() is None


def test_rejected_unsupported_role_is_sanitized_and_skips_handler(caplog):
    client = build_client()

    with caplog.at_level(logging.WARNING, logger="src.api.middleware"):
        response = client.get(
            "/context",
            headers={
                "X-Correlation-ID": "corr-role",
                "X-Tenant-ID": "tenant-a",
                "X-Role": "superadmin-secret",
            },
        )

    assert response.status_code == 403
    assert response.text == "Unsupported request role"
    assert response.headers["X-Context-Decision"] == "rejected"
    assert "superadmin-secret" not in caplog.text
    assert client.app.state.context_hits == 0
    assert get_request_context() is None


def test_exception_path_returns_context_headers_and_resets_state():
    client = build_client(raise_server_exceptions=False)

    response = client.get(
        "/boom",
        headers={
            "X-Correlation-ID": "corr-error",
            "X-Request-ID": "req-error",
            "X-Tenant-ID": "tenant-a",
            "X-Role": "viewer",
        },
    )
    next_response = client.get(
        "/context",
        headers={
            "X-Correlation-ID": "corr-next",
            "X-Tenant-ID": "tenant-b",
            "X-Role": "viewer",
        },
    )

    assert response.status_code == 500
    assert response.headers["X-Correlation-ID"] == "corr-error"
    assert response.headers["X-Request-ID"] == "req-error"
    assert response.headers["X-Context-Decision"] == "failed"
    assert next_response.status_code == 200
    assert next_response.json()["tenant_id"] == "tenant-b"
    assert get_request_context() is None
