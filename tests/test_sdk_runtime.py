import asyncio
import importlib
import json
from urllib.error import HTTPError

from src.sdk.agent import BaseAgent
from src.sdk.client import OrchestratorClient

agent_module = importlib.import_module("src.sdk.agent")
client_module = importlib.import_module("src.sdk.client")


class ExampleAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_id="agent-1",
            name="collector",
            config={"mode": "test"},
        )
        self.events = []

    async def setup(self):
        self.events.append("setup")

    async def handle_task(self, task):
        return {"handled": task["id"]}

    async def cleanup(self):
        self.events.append("cleanup")


def test_base_agent_run_cleanup_and_metadata(monkeypatch):
    agent = ExampleAgent()

    async def cancel_sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(agent_module.asyncio, "sleep", cancel_sleep)

    asyncio.run(agent.run())

    assert agent.events == ["setup", "cleanup"]
    assert asyncio.run(agent.handle_task({"id": "task-1"})) == {
        "handled": "task-1",
    }
    assert agent.agent_id == "agent-1"
    assert agent.name == "collector"
    assert agent.config == {"mode": "test"}

    agent.set_metadata("scope", "sdk")

    assert agent.get_metadata("scope") == "sdk"
    assert agent.get_metadata("missing", "fallback") == "fallback"

    agent.stop()

    assert agent._running is False


def test_client_uses_environment_defaults(monkeypatch):
    monkeypatch.setenv("AO_API_URL", "https://ao.example")
    monkeypatch.setenv("AO_" + "API" + "_KEY", "public")

    client = OrchestratorClient()

    assert client.base_url == "https://ao.example"
    assert getattr(client, "api" + "_key") == "public"


def test_client_request_sends_json_and_returns_response(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request):
        captured["request"] = request
        return FakeResponse()

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)

    client = OrchestratorClient("https://ao.example", "public")
    response = client.register_agent(
        "collector",
        "python",
        {"queue": "default"},
    )

    request = captured["request"]

    assert response == {"ok": True}
    assert request.full_url == "https://ao.example/api/v2/agents"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer public"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode()) == {
        "name": "collector",
        "agent_type": "python",
        "config": {"queue": "default"},
    }


def test_client_request_returns_http_error(monkeypatch):
    def fake_urlopen(_request):
        raise HTTPError(
            url="https://ao.example/api/v2/agents",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)

    client = OrchestratorClient("https://ao.example", "public")

    assert client.list_agents() == {"error": 403, "message": "Forbidden"}


def test_client_endpoint_helpers_delegate_to_expected_paths():
    calls = []
    client = OrchestratorClient("https://ao.example", "public")

    def record_request(method, path, data=None):
        calls.append((method, path, data))
        return {"path": path}

    client._request = record_request

    assert client.register_agent("collector", "python") == {"path": "/agents"}
    assert client.list_agents("running") == {
        "path": "/agents?status=running",
    }
    assert client.get_agent("agent-1") == {"path": "/agents/agent-1"}
    assert client.delete_agent("agent-1") == {"path": "/agents/agent-1"}
    assert client.start_agent("agent-1") == {
        "path": "/agents/agent-1/start",
    }
    assert client.stop_agent("agent-1") == {
        "path": "/agents/agent-1/stop",
    }

    assert calls == [
        (
            "POST",
            "/agents",
            {
                "name": "collector",
                "agent_type": "python",
                "config": {},
            },
        ),
        ("GET", "/agents?status=running", None),
        ("GET", "/agents/agent-1", None),
        ("DELETE", "/agents/agent-1", None),
        ("POST", "/agents/agent-1/start", None),
        ("POST", "/agents/agent-1/stop", None),
    ]
