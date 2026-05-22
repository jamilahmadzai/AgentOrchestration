from fastapi.testclient import TestClient

from src.api.server import create_app
from src.api.webhooks import webhook_store


AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def setup_function():
    webhook_store.reset()


def _client():
    return TestClient(create_app())


def _create_subscription(
    client,
    workspace_id="workspace-a",
    event_types=None,
    target_url=None,
):
    response = client.post(
        "/api/v2/webhooks/subscriptions",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": workspace_id,
            "target_url": target_url or "https://hooks.example.test/agents",
            "event_types": event_types or ["agent.started", "task.completed"],
            "secret": "signing-secret",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_subscription_create_rejects_invalid_event_types_before_persistence():
    client = _client()

    response = client.post(
        "/api/v2/webhooks/subscriptions",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "target_url": "https://hooks.example.test/agents",
            "event_types": ["agent.started", "internal.audit.dump"],
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_event_types"
    assert detail["invalid_event_types"] == ["internal.audit.dump"]

    list_response = client.get(
        "/api/v2/webhooks/subscriptions",
        headers=AUTH_HEADERS,
        params={"workspace_id": "workspace-a"},
    )
    assert list_response.status_code == 200
    assert list_response.json()["subscriptions"] == []


def test_valid_delivery_is_idempotent_and_sanitized():
    client = _client()
    subscription = _create_subscription(client)

    delivery_payload = {
        "workspace_id": "workspace-a",
        "subscription_id": subscription["id"],
        "event_type": "agent.started",
        "delivery_id": "delivery-1",
        "payload": {"agent_id": "agent-1", "internal_token": "do-not-return"},
    }
    first = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json=delivery_payload,
    )
    replay = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json=delivery_payload,
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    first_body = first.json()
    replay_body = replay.json()
    assert replay_body == first_body
    assert first_body["attempts"] == 1
    assert "payload" not in first_body
    assert "target_url" not in first_body
    assert "secret" not in first_body
    assert "callback_url" not in first_body
    assert "endpoint_version" not in first_body
    assert not any(key.startswith("_") for key in first_body)

    changed_payload = {**delivery_payload, "payload": {"agent_id": "agent-2"}}
    conflict = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json=changed_payload,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "delivery_id_conflict"


def test_delivery_rejects_invalid_or_unsubscribed_events_without_records():
    client = _client()
    subscription = _create_subscription(client, event_types=["agent.started"])

    invalid = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "subscription_id": subscription["id"],
            "event_type": "internal.audit.dump",
            "delivery_id": "bad-event",
            "payload": {"secret": "must-not-persist"},
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["error"] == "invalid_event_type"

    unsubscribed = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "subscription_id": subscription["id"],
            "event_type": "task.completed",
            "delivery_id": "wrong-event",
            "payload": {"task_id": "task-1"},
        },
    )
    assert unsubscribed.status_code == 400
    assert (
        unsubscribed.json()["detail"]["error"]
        == "event_type_not_allowed_for_subscription"
    )

    deliveries = client.get(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        params={"workspace_id": "workspace-a"},
    )
    assert deliveries.status_code == 200
    assert deliveries.json()["deliveries"] == []


def test_workspace_isolation_for_subscription_and_delivery_access():
    client = _client()
    subscription = _create_subscription(client, workspace_id="workspace-a")

    cross_workspace_delivery = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-b",
            "subscription_id": subscription["id"],
            "event_type": "agent.started",
            "delivery_id": "delivery-1",
            "payload": {},
        },
    )
    assert cross_workspace_delivery.status_code == 404

    delivery = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "subscription_id": subscription["id"],
            "event_type": "agent.started",
            "delivery_id": "delivery-1",
            "payload": {},
        },
    )
    assert delivery.status_code == 201

    denied = client.get(
        f"/api/v2/webhooks/deliveries/{delivery.json()['id']}",
        headers=AUTH_HEADERS,
        params={"workspace_id": "workspace-b"},
    )
    assert denied.status_code == 404


def test_retry_is_idempotent_and_rejects_disabled_or_rotated_endpoints():
    client = _client()
    subscription = _create_subscription(client)
    delivery = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "subscription_id": subscription["id"],
            "event_type": "agent.started",
            "delivery_id": "delivery-1",
            "payload": {"agent_id": "agent-1"},
        },
    ).json()

    first_retry = client.post(
        f"/api/v2/webhooks/deliveries/{delivery['id']}/retry",
        headers=AUTH_HEADERS,
        json={"workspace_id": "workspace-a", "retry_id": "retry-1"},
    )
    duplicate_retry = client.post(
        f"/api/v2/webhooks/deliveries/{delivery['id']}/retry",
        headers=AUTH_HEADERS,
        json={"workspace_id": "workspace-a", "retry_id": "retry-1"},
    )
    assert first_retry.status_code == 200
    assert duplicate_retry.status_code == 200
    assert first_retry.json()["attempts"] == 2
    assert duplicate_retry.json()["attempts"] == 2

    rotated = client.patch(
        f"/api/v2/webhooks/subscriptions/{subscription['id']}",
        headers=AUTH_HEADERS,
        params={"workspace_id": "workspace-a"},
        json={"target_url": "https://hooks.example.test/rotated"},
    )
    assert rotated.status_code == 200

    stale_retry = client.post(
        f"/api/v2/webhooks/deliveries/{delivery['id']}/retry",
        headers=AUTH_HEADERS,
        json={"workspace_id": "workspace-a", "retry_id": "retry-2"},
    )
    assert stale_retry.status_code == 409
    assert stale_retry.json()["detail"]["error"] == "endpoint_rotated"

    rotated_delivery = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "subscription_id": subscription["id"],
            "event_type": "agent.started",
            "delivery_id": "delivery-1",
            "payload": {"agent_id": "agent-1"},
        },
    )
    assert rotated_delivery.status_code == 201
    assert rotated_delivery.json()["id"] != delivery["id"]
    assert "endpoint_version" not in rotated_delivery.json()

    disabled = client.patch(
        f"/api/v2/webhooks/subscriptions/{subscription['id']}",
        headers=AUTH_HEADERS,
        params={"workspace_id": "workspace-a"},
        json={"enabled": False},
    )
    assert disabled.status_code == 200

    disabled_delivery = client.post(
        "/api/v2/webhooks/deliveries",
        headers=AUTH_HEADERS,
        json={
            "workspace_id": "workspace-a",
            "subscription_id": subscription["id"],
            "event_type": "agent.started",
            "delivery_id": "delivery-disabled",
            "payload": {},
        },
    )
    assert disabled_delivery.status_code == 409
    assert (
        disabled_delivery.json()["detail"]["error"]
        == "subscription_disabled"
    )
