import asyncio

from src.agent.executor import AgentExecutor


def test_execute_returns_id_before_handler_finishes():
    asyncio.run(_assert_execute_returns_id_before_handler_finishes())


async def _assert_execute_returns_id_before_handler_finishes():
    executor = AgentExecutor(max_concurrent=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(agent_id, task):
        started.set()
        await release.wait()
        return "done"

    execution_id = await asyncio.wait_for(
        executor.execute("agent-1", {"id": "task-1"}, handler),
        timeout=0.05,
    )

    assert execution_id in executor._active_tasks
    assert executor.get_result(execution_id) is None
    await asyncio.wait_for(started.wait(), timeout=0.5)

    task_obj = executor._active_tasks[execution_id]
    release.set()
    await asyncio.wait_for(task_obj, timeout=0.5)

    result = executor.get_result(execution_id)
    assert result["execution_id"] == execution_id
    assert result["agent_id"] == "agent-1"
    assert result["task_id"] == "task-1"
    assert result["result"] == "done"
    assert execution_id not in executor._active_tasks


def test_background_execution_still_respects_max_concurrent():
    asyncio.run(_assert_background_execution_still_respects_max_concurrent())


async def _assert_background_execution_still_respects_max_concurrent():
    executor = AgentExecutor(max_concurrent=1)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first_handler(agent_id, task):
        first_started.set()
        await release_first.wait()
        return "first"

    async def second_handler(agent_id, task):
        second_started.set()
        return "second"

    first_id = await executor.execute(
        "agent-1", {"id": "first"}, first_handler
    )
    await asyncio.wait_for(first_started.wait(), timeout=0.5)

    second_id = await asyncio.wait_for(
        executor.execute("agent-1", {"id": "second"}, second_handler),
        timeout=0.05,
    )
    await asyncio.sleep(0.05)

    assert not second_started.is_set()
    assert first_id in executor._active_tasks
    assert second_id in executor._active_tasks

    first_task = executor._active_tasks[first_id]
    second_task = executor._active_tasks[second_id]
    release_first.set()
    await asyncio.wait_for(
        asyncio.gather(first_task, second_task),
        timeout=0.5,
    )

    assert second_started.is_set()
    assert executor.get_result(first_id)["result"] == "first"
    assert executor.get_result(second_id)["result"] == "second"


def test_cancel_active_execution_records_cancelled_result():
    asyncio.run(_assert_cancel_active_execution_records_cancelled_result())


async def _assert_cancel_active_execution_records_cancelled_result():
    executor = AgentExecutor(max_concurrent=1)
    started = asyncio.Event()

    async def handler(agent_id, task):
        started.set()
        await asyncio.sleep(10)

    execution_id = await executor.execute(
        "agent-1", {"id": "task-1"}, handler
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)

    task_obj = executor._active_tasks[execution_id]
    assert executor.cancel(execution_id)
    await asyncio.gather(task_obj, return_exceptions=True)

    assert executor.get_result(execution_id) == {"error": "cancelled"}
    assert execution_id not in executor._active_tasks


def test_cancel_queued_execution_before_handler_starts():
    asyncio.run(_assert_cancel_queued_execution_before_handler_starts())


async def _assert_cancel_queued_execution_before_handler_starts():
    executor = AgentExecutor(max_concurrent=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    queued_started = asyncio.Event()

    async def first_handler(agent_id, task):
        first_started.set()
        await release_first.wait()
        return "first"

    async def queued_handler(agent_id, task):
        queued_started.set()
        return "queued"

    first_id = await executor.execute(
        "agent-1", {"id": "first"}, first_handler
    )
    await asyncio.wait_for(first_started.wait(), timeout=0.5)
    queued_id = await executor.execute(
        "agent-1", {"id": "queued"}, queued_handler
    )

    queued_task = executor._active_tasks[queued_id]
    assert executor.cancel(queued_id)
    await asyncio.gather(queued_task, return_exceptions=True)

    assert not queued_started.is_set()
    assert executor.get_result(queued_id) == {"error": "cancelled"}
    assert queued_id not in executor._active_tasks

    first_task = executor._active_tasks[first_id]
    release_first.set()
    await asyncio.wait_for(first_task, timeout=0.5)
    assert executor.get_result(first_id)["result"] == "first"


def test_cancel_unknown_execution_returns_false():
    executor = AgentExecutor(max_concurrent=1)

    assert executor.cancel("missing-execution") is False


def test_shutdown_cancels_active_and_queued_executions():
    asyncio.run(_assert_shutdown_cancels_active_and_queued_executions())


async def _assert_shutdown_cancels_active_and_queued_executions():
    executor = AgentExecutor(max_concurrent=1)
    first_started = asyncio.Event()
    release_never = asyncio.Event()
    queued_started = asyncio.Event()

    async def first_handler(agent_id, task):
        first_started.set()
        await release_never.wait()

    async def queued_handler(agent_id, task):
        queued_started.set()

    first_id = await executor.execute(
        "agent-1", {"id": "first"}, first_handler
    )
    await asyncio.wait_for(first_started.wait(), timeout=0.5)
    queued_id = await executor.execute(
        "agent-1", {"id": "queued"}, queued_handler
    )

    await executor.shutdown()

    assert not queued_started.is_set()
    assert executor.get_result(first_id) == {"error": "cancelled"}
    assert executor.get_result(queued_id) == {"error": "cancelled"}
    assert executor._active_tasks == {}


def test_handler_error_is_recorded_without_leaving_active_task():
    asyncio.run(_assert_handler_error_is_recorded_without_active_task())


async def _assert_handler_error_is_recorded_without_active_task():
    executor = AgentExecutor(max_concurrent=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(agent_id, task):
        started.set()
        await release.wait()
        raise RuntimeError("sandbox failed")

    execution_id = await executor.execute(
        "agent-1", {"id": "task-1"}, handler
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)

    task_obj = executor._active_tasks[execution_id]
    release.set()
    await asyncio.wait_for(
        asyncio.gather(task_obj, return_exceptions=True),
        timeout=0.5,
    )

    assert executor.get_result(execution_id) == {"error": "sandbox failed"}
    assert execution_id not in executor._active_tasks
