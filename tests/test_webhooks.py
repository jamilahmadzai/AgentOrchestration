from fastapi.testclient import TestClient

from src.api import routes
from src.api.server import create_app
from src.api.webhooks import WebhookRegistry, normalize_webhook_url


AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def client_with_fresh_webhooks():
    routes.webhooks = WebhookRegistry()
    return TestClient(create_app()), routes.webhooks


def register_endpoint(
    client,
    workspace_id="workspace-a",
    callback_url="https://hooks.example.com/events",
):
    response = client.post(
        "/api/v2/webhooks",
        headers=AUTH_HEADERS,
        json={"workspace_id": workspace_id, "callback_url": callback_url},
    )
    assert response.status_code == 201
    return response.json()


def test_normalize_webhook_url_canonicalizes_before_duplicate_checks():
    url = "HTTPS://Hooks.Example.COM:443/team/../events/?b=2&a=1#ignored"
    assert (
        normalize_webhook_url(url)
        == "https://hooks.example.com/events?a=1&b=2"
    )


def test_register_webhook_is_idempotent_per_workspace_after_normalization():
    client, service = client_with_fresh_webhooks()

    first = register_endpoint(
        client,
        callback_url=(
            "HTTPS://Hooks.Example.COM:443/team/../events/?b=2&a=1"
            "#ignored"
        ),
    )
    second_response = client.post(
        "/api/v2/webhooks",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "callback_url": "https://hooks.example.com/events?a=1&b=2",
        },
    )
    other_workspace = register_endpoint(
        client,
        workspace_id="workspace-b",
        callback_url="https://hooks.example.com/events?b=2&a=1",
    )

    assert second_response.status_code == 200
    second = second_response.json()
    assert first["endpoint_id"] == second["endpoint_id"]
    assert second["created"] is False
    assert (
        first["callback_url"]
        == "https://hooks.example.com/events?a=1&b=2"
    )
    assert "secret" not in first
    assert "normalized_url" not in first
    assert other_workspace["endpoint_id"] != first["endpoint_id"]
    assert service.endpoint_count() == 2


def test_disabled_webhook_can_be_replaced_without_reusing_state():
    client, service = client_with_fresh_webhooks()
    disabled = register_endpoint(
        client,
        callback_url="https://hooks.example.com/events/",
    )

    disable_response = client.post(
        f"/api/v2/webhooks/{disabled['endpoint_id']}/disable",
        headers=AUTH_HEADERS,
        json={"workspace_id": "workspace-a"},
    )
    replacement_response = client.post(
        "/api/v2/webhooks",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "callback_url": "https://HOOKS.example.com:443/events#ignored",
        },
    )
    replacement = replacement_response.json()
    delivery = client.post(
        f"/api/v2/webhooks/{replacement['endpoint_id']}/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "event_id": "evt-replacement",
            "payload": {},
        },
    )

    assert disable_response.status_code == 200
    assert replacement_response.status_code == 201
    assert replacement["endpoint_id"] != disabled["endpoint_id"]
    assert replacement["disabled"] is False
    assert delivery.status_code == 202
    assert service.endpoint_count() == 2
    assert service.delivery_count() == 1


def test_webhook_registration_requires_authorization_before_validation():
    client, service = client_with_fresh_webhooks()

    response = client.post(
        "/api/v2/webhooks",
        json={
            "workspace_id": "workspace-a",
            "callback_url": "https://10.0.0.5/internal",
        },
    )

    assert response.status_code == 401
    assert response.text == "Unauthorized"
    assert service.endpoint_count() == 0


def test_rejected_webhook_url_fails_before_state_mutation():
    client, service = client_with_fresh_webhooks()

    for callback_url in (
        "http://hooks.example.com/events",
        "https://localhost/internal",
        "https://10.0.0.5/internal",
        "https://user:token@hooks.example.com/events",
    ):
        response = client.post(
            "/api/v2/webhooks",
            headers=AUTH_HEADERS,
            json={"workspace_id": "workspace-a", "callback_url": callback_url},
        )
        assert response.status_code == 422

    assert service.endpoint_count() == 0


def test_delivery_accepts_valid_event_without_exposing_internal_fields():
    client, service = client_with_fresh_webhooks()
    endpoint = register_endpoint(client)

    response = client.post(
        f"/api/v2/webhooks/{endpoint['endpoint_id']}/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "event_id": "evt-1",
            "payload": {"secret": "do-not-echo"},
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["endpoint_id"] == endpoint["endpoint_id"]
    assert body["workspace_id"] == "workspace-a"
    assert body["event_id"] == "evt-1"
    assert body["status"] == "accepted"
    assert body["created"] is True
    assert "payload" not in body
    assert "secret" not in body
    assert "normalized_url" not in body
    assert "callback_url" not in body
    assert service.delivery_count() == 1


def test_delivery_retry_is_idempotent_by_endpoint_and_event_id():
    client, service = client_with_fresh_webhooks()
    endpoint = register_endpoint(client)
    url = f"/api/v2/webhooks/{endpoint['endpoint_id']}/deliveries"
    payload = {
        "workspace_id": "workspace-a",
        "event_id": "evt-retry",
        "payload": {"attempt": 1},
    }

    first = client.post(url, headers=AUTH_HEADERS, json=payload)
    retry = client.post(
        url,
        headers=AUTH_HEADERS,
        json={**payload, "payload": {"attempt": 2}},
    )

    assert first.status_code == 202
    assert retry.status_code == 200
    assert first.json()["delivery_id"] == retry.json()["delivery_id"]
    assert retry.json()["created"] is False
    assert service.delivery_count() == 1


def test_workspace_isolation_blocks_cross_workspace_delivery():
    client, service = client_with_fresh_webhooks()
    endpoint = register_endpoint(client, workspace_id="workspace-a")

    response = client.post(
        f"/api/v2/webhooks/{endpoint['endpoint_id']}/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-b",
            "event_id": "evt-1",
            "payload": {},
        },
    )

    assert response.status_code == 404
    assert service.delivery_count() == 0


def test_disabled_webhook_rejects_delivery_without_creating_record():
    client, service = client_with_fresh_webhooks()
    endpoint = register_endpoint(client)

    disabled = client.post(
        f"/api/v2/webhooks/{endpoint['endpoint_id']}/disable",
        headers=AUTH_HEADERS,
        json={"workspace_id": "workspace-a"},
    )
    delivery = client.post(
        f"/api/v2/webhooks/{endpoint['endpoint_id']}/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "event_id": "evt-disabled",
            "payload": {},
        },
    )

    assert disabled.status_code == 200
    assert disabled.json()["disabled"] is True
    assert delivery.status_code == 409
    assert service.delivery_count() == 0
