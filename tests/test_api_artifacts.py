import asyncio

import pytest
from fastapi.testclient import TestClient

from src.api.artifacts import ArtifactIngestionService, artifact_service
from src.api.routes import registry
from src.api.server import create_app


@pytest.fixture(autouse=True)
def reset_artifact_state():
    registry._agents.clear()
    registry._index.clear()
    artifact_service.clear()


@pytest.fixture
def client():
    return TestClient(create_app())


def auth_headers(extra=None):
    headers = {"Authorization": "Bearer test-token"}
    if extra:
        headers.update(extra)
    return headers


def test_upload_artifact_records_metadata_for_authorized_agent(client):
    agent_id = registry.register("worker", "worker.processor")

    response = client.post(
        f"/api/v2/agents/{agent_id}/artifacts/output.log",
        content=b"artifact payload",
        headers=auth_headers({"Content-Type": "text/plain"}),
    )

    assert response.status_code == 200
    artifact = response.json()["artifact"]
    assert artifact["agent_id"] == agent_id
    assert artifact["name"] == "output.log"
    assert artifact["size"] == len(b"artifact payload")
    assert artifact["content_type"] == "text/plain"
    assert len(artifact["sha256"]) == 64
    assert artifact_service.count() == 1


def test_upload_artifact_requires_authorization_before_mutating(client):
    agent_id = registry.register("worker", "worker.processor")

    response = client.post(
        f"/api/v2/agents/{agent_id}/artifacts/output.log",
        content=b"artifact payload",
    )

    assert response.status_code == 401
    assert artifact_service.count() == 0


def test_malformed_artifact_name_route_fails_before_lookup_or_mutation(client):
    response = client.post(
        "/api/v2/agents/missing-agent/artifacts/bad%20name",
        content=b"artifact payload",
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert artifact_service.count() == 0


def test_declared_oversized_route_fails_before_lookup_or_mutation(client):
    service_limit = artifact_service.max_body_bytes
    artifact_service.max_body_bytes = 8
    try:
        response = client.post(
            "/api/v2/agents/missing-agent/artifacts/output.log",
            content=b"too large",
            headers=auth_headers(),
        )
    finally:
        artifact_service.max_body_bytes = service_limit

    assert response.status_code == 413
    assert artifact_service.count() == 0


def test_malformed_artifact_name_fails_before_agent_lookup_or_mutation():
    service = ArtifactIngestionService()
    lookups = []

    async def read_body():
        raise AssertionError("body should not be read for malformed requests")

    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            service.ingest(
                agent_id="agent-1",
                artifact_name="..",
                content_length="5",
                content_type="text/plain",
                read_body=read_body,
                agent_exists=lambda agent_id: lookups.append(agent_id) or True,
            )
        )

    assert getattr(exc_info.value, "status_code", None) == 400
    assert lookups == []
    assert service.count() == 0


def test_declared_oversized_upload_fails_before_agent_lookup_or_mutation():
    service = ArtifactIngestionService(max_body_bytes=8)
    lookups = []

    async def read_body():
        raise AssertionError(
            "body should not be read for declared oversized requests"
        )

    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            service.ingest(
                agent_id="agent-1",
                artifact_name="output.log",
                content_length="9",
                content_type="text/plain",
                read_body=read_body,
                agent_exists=lambda agent_id: lookups.append(agent_id) or True,
            )
        )

    assert getattr(exc_info.value, "status_code", None) == 413
    assert lookups == []
    assert service.count() == 0


def test_actual_oversized_upload_fails_without_mutating(client):
    service_limit = artifact_service.max_body_bytes
    artifact_service.max_body_bytes = 8
    agent_id = registry.register("worker", "worker.processor")
    try:
        response = client.post(
            f"/api/v2/agents/{agent_id}/artifacts/output.log",
            content=b"too large",
            headers=auth_headers(),
        )
    finally:
        artifact_service.max_body_bytes = service_limit

    assert response.status_code == 413
    assert artifact_service.count() == 0


def test_unknown_agent_fails_without_mutating(client):
    response = client.post(
        "/api/v2/agents/missing-agent/artifacts/output.log",
        content=b"artifact payload",
        headers=auth_headers(),
    )

    assert response.status_code == 404
    assert artifact_service.count() == 0
