import asyncio
import inspect

from src.sdk.decorators import on_event, task


def _discover_task_handlers(cls):
    return {
        name: member.__task_config__
        for name, member in inspect.getmembers(cls, predicate=callable)
        if hasattr(member, "__task_config__")
    }


def _discover_event_handlers(cls):
    return {
        name: member.__event_handler__
        for name, member in inspect.getmembers(cls, predicate=callable)
        if hasattr(member, "__event_handler__")
    }


def test_task_metadata_is_attached_to_returned_wrapper_only():
    async def handler():
        return "done"

    decorated = task(name="sync", retries=2, timeout=15)(handler)

    assert decorated is not handler
    assert decorated.__wrapped__ is handler
    assert not hasattr(handler, "__task_config__")
    assert decorated.__task_config__ == {
        "name": "sync",
        "retries": 2,
        "timeout": 15,
    }
    assert asyncio.run(decorated()) == "done"


def test_class_level_task_discovery_sees_decorated_wrapper_metadata():
    class Worker:
        @task(name="refresh-user", retries=1, timeout=20)
        async def refresh(self, user_id):
            return user_id

        async def helper(self):
            return "not a task"

    assert _discover_task_handlers(Worker) == {
        "refresh": {
            "name": "refresh-user",
            "retries": 1,
            "timeout": 20,
        }
    }
    assert asyncio.run(Worker().refresh("user-123")) == "user-123"


def test_task_metadata_uses_function_name_without_mutating_original():
    class Worker:
        @task(timeout=5)
        async def reconcile(self):
            return "ok"

    assert Worker.reconcile.__task_config__ == {
        "name": "reconcile",
        "retries": 0,
        "timeout": 5,
    }
    assert not hasattr(Worker.reconcile.__wrapped__, "__task_config__")


def test_task_configs_are_independent_between_wrappers():
    @task(name="first", retries=1)
    async def first():
        return "first"

    @task(name="second", retries=2)
    async def second():
        return "second"

    assert first.__task_config__ is not second.__task_config__
    assert first.__task_config__["name"] == "first"
    assert second.__task_config__["name"] == "second"


def test_event_metadata_uses_same_wrapper_introspection_pattern():
    class Worker:
        @on_event("task.created")
        async def created(self, payload):
            return payload["id"]

    assert _discover_event_handlers(Worker) == {"created": "task.created"}
    assert not hasattr(Worker.created.__wrapped__, "__event_handler__")
    assert asyncio.run(Worker().created({"id": "task-1"})) == "task-1"
