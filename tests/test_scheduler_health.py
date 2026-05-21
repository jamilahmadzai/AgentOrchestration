import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

from src.orchestrator.health import check_scheduler_dependencies


ROOT = Path(__file__).resolve().parents[1]


def _tcp_server():
    ready = threading.Event()
    done = threading.Event()

    def serve(server):
        ready.set()
        try:
            connection, _ = server.accept()
            connection.close()
        finally:
            server.close()
            done.set()

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    thread = threading.Thread(target=serve, args=(server,), daemon=True)
    thread.start()
    ready.wait(timeout=1)
    return server.getsockname()[1], done


def test_scheduler_health_allows_memory_defaults():
    healthy, payload = check_scheduler_dependencies(env={})

    assert healthy
    assert payload["status"] == "healthy"
    details = {
        entry["name"]: entry["detail"]
        for entry in payload["dependencies"]
    }
    assert details == {
        "queue": "not configured",
        "storage": "not configured",
    }


def test_scheduler_health_uses_explicit_empty_env(monkeypatch):
    monkeypatch.setenv("SCHEDULER_REQUIRE_QUEUE", "true")

    healthy, payload = check_scheduler_dependencies(env={})

    assert healthy
    queue = next(
        entry for entry in payload["dependencies"]
        if entry["name"] == "queue"
    )
    assert queue["detail"] == "not configured"


def test_scheduler_health_fails_required_missing_dependency():
    healthy, payload = check_scheduler_dependencies(
        env={"SCHEDULER_REQUIRE_QUEUE": "true"}
    )

    assert not healthy
    assert payload["status"] == "unhealthy"
    queue = next(
        entry for entry in payload["dependencies"]
        if entry["name"] == "queue"
    )
    assert queue["detail"] == "required dependency is not configured"


def test_scheduler_health_reflects_tcp_dependency_failure():
    healthy, payload = check_scheduler_dependencies(
        env={
            "SCHEDULER_QUEUE_TCP": "127.0.0.1:1",
            "SCHEDULER_REQUIRE_QUEUE": "true",
            "SCHEDULER_HEALTH_TIMEOUT": "0.1",
        }
    )

    assert not healthy
    queue = next(
        entry for entry in payload["dependencies"]
        if entry["name"] == "queue"
    )
    assert queue["kind"] == "tcp"
    assert not queue["ok"]


def test_scheduler_health_reflects_storage_dependency_failure():
    healthy, payload = check_scheduler_dependencies(
        env={
            "SCHEDULER_STORAGE_TCP": "127.0.0.1:1",
            "SCHEDULER_REQUIRE_STORAGE": "true",
            "SCHEDULER_HEALTH_TIMEOUT": "0.1",
        }
    )

    assert not healthy
    storage = next(
        entry for entry in payload["dependencies"]
        if entry["name"] == "storage"
    )
    assert storage["kind"] == "tcp"
    assert not storage["ok"]


def test_scheduler_health_reflects_tcp_dependency_success():
    port, done = _tcp_server()

    healthy, payload = check_scheduler_dependencies(
        env={
            "SCHEDULER_QUEUE_TCP": f"127.0.0.1:{port}",
            "SCHEDULER_REQUIRE_QUEUE": "true",
            "SCHEDULER_HEALTH_TIMEOUT": "1",
        }
    )

    assert healthy
    assert done.wait(timeout=1)
    queue = next(
        entry for entry in payload["dependencies"]
        if entry["name"] == "queue"
    )
    assert queue["detail"] == "reachable"


def test_scheduler_health_command_exits_nonzero_when_unhealthy():
    result = subprocess.run(
        [sys.executable, "-m", "src.orchestrator.health"],
        cwd=ROOT,
        env={"SCHEDULER_REQUIRE_QUEUE": "true", "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "unhealthy"


def test_scheduler_image_and_compose_wire_healthchecks():
    dockerfile = (ROOT / "infra/Dockerfile.scheduler").read_text()
    compose = (ROOT / "infra/docker-compose.yml").read_text()

    assert "HEALTHCHECK" in dockerfile
    assert "python -m src.orchestrator.health" in dockerfile
    assert "src.orchestrator.scheduler_worker" in dockerfile
    assert "condition: service_healthy" in compose
    assert "SCHEDULER_QUEUE_TCP: queue:6379" in compose
    assert "SCHEDULER_STORAGE_TCP: storage:5432" in compose
