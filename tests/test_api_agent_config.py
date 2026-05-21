from fastapi.testclient import TestClient

from src.agent import AgentRegistry
from src.api import routes
from src.api.server import create_app


AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def make_client(monkeypatch):
    registry = AgentRegistry()
    monkeypatch.setattr(routes, "registry", registry)
    return TestClient(create_app()), registry


def test_update_agent_config_with_current_etag(monkeypatch):
    client, registry = make_client(monkeypatch)
    agent_id = registry.register("processor", "worker.processor", {"limit": 1})
    etag = registry.get(agent_id)["config_etag"]

    response = client.put(
        f"/api/v2/agents/{agent_id}/config",
        json={"config": {"limit": 2, "enabled": True}},
        headers={**AUTH_HEADERS, "If-Match": etag},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["config"] == {"limit": 2, "enabled": True}
    assert body["config_version"] == 2
    assert body["config_etag"] != etag
    assert registry.get(agent_id)["config"] == {"limit": 2, "enabled": True}


def test_stale_etag_does_not_overwrite_agent_config(monkeypatch):
    client, registry = make_client(monkeypatch)
    agent_id = registry.register("processor", "worker.processor", {"limit": 1})
    stale_etag = registry.get(agent_id)["config_etag"]

    first = client.put(
        f"/api/v2/agents/{agent_id}/config",
        json={"config": {"limit": 2}},
        headers={**AUTH_HEADERS, "If-Match": stale_etag},
    )
    assert first.status_code == 200

    second = client.put(
        f"/api/v2/agents/{agent_id}/config",
        json={"config": {"limit": 3}},
        headers={**AUTH_HEADERS, "If-Match": stale_etag},
    )

    assert second.status_code == 412
    assert registry.get(agent_id)["config"] == {"limit": 2}
    assert registry.get(agent_id)["config_version"] == 2


def test_unauthorized_config_update_is_rejected_before_mutation(monkeypatch):
    client, registry = make_client(monkeypatch)
    agent_id = registry.register("processor", "worker.processor", {"limit": 1})
    etag = registry.get(agent_id)["config_etag"]

    response = client.put(
        f"/api/v2/agents/{agent_id}/config",
        json={"config": {"limit": 2}},
        headers={"If-Match": etag},
    )

    assert response.status_code == 401
    assert registry.get(agent_id)["config"] == {"limit": 1}
    assert registry.get(agent_id)["config_etag"] == etag


def test_malformed_config_update_is_rejected_before_mutation(monkeypatch):
    client, registry = make_client(monkeypatch)
    agent_id = registry.register("processor", "worker.processor", {"limit": 1})
    etag = registry.get(agent_id)["config_etag"]

    response = client.put(
        f"/api/v2/agents/{agent_id}/config",
        json={"config": "not-an-object"},
        headers={**AUTH_HEADERS, "If-Match": etag},
    )

    assert response.status_code == 422
    assert registry.get(agent_id)["config"] == {"limit": 1}
    assert registry.get(agent_id)["config_etag"] == etag


def test_missing_etag_is_rejected_before_mutation(monkeypatch):
    client, registry = make_client(monkeypatch)
    agent_id = registry.register("processor", "worker.processor", {"limit": 1})
    etag = registry.get(agent_id)["config_etag"]

    response = client.put(
        f"/api/v2/agents/{agent_id}/config",
        json={"config": {"limit": 2}},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 428
    assert registry.get(agent_id)["config"] == {"limit": 1}
    assert registry.get(agent_id)["config_etag"] == etag
