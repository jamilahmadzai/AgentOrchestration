import asyncio

from src.orchestrator.scheduler import TaskScheduler


class ManualClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def run(coro):
    return asyncio.run(coro)


def test_dependency_outage_defers_without_in_flight_or_new_id():
    clock = ManualClock()
    scheduler = TaskScheduler(clock=clock, health_retry_delay=5)
    scheduler.set_dependency_health(
        "billing-api",
        healthy=False,
        reason="token=fake-value",
    )
    task = {
        "type": "charge",
        "payload": {"card": "4111-1111-1111-1111"},
        "required_dependencies": ["billing-api"],
    }

    task_id = scheduler.enqueue(task, priority=7)
    result = run(scheduler.dequeue())

    assert result is None
    assert task["id"] == task_id
    assert task["priority"] == 7
    assert task_id in scheduler.scheduled_ids()
    assert task_id not in scheduler.in_flight_ids()
    assert task["health_gate"]["decision"] == "deferred"
    assert task["health_gate"]["blocked_dependencies"] == ["billing-api"]

    records = scheduler.audit_records()
    assert records[-1]["action"] == "task_deferred_dependency_health_gate"
    assert records[-1]["task_id"] == task_id
    assert records[-1]["blocked_dependencies"] == ["billing-api"]
    assert "payload" not in repr(records).lower()
    assert "4111-1111-1111-1111" not in repr(records)
    assert "fake-value" not in repr(records)


def test_recovered_dependency_dispatches_same_task_after_retry_delay():
    clock = ManualClock()
    scheduler = TaskScheduler(clock=clock, health_retry_delay=5)
    scheduler.set_dependency_health("redis", healthy=False)
    task_id = scheduler.enqueue(
        {"type": "sync", "required_dependencies": ["redis"]},
    )

    assert run(scheduler.dequeue()) is None
    clock.advance(4)
    assert run(scheduler.dequeue()) is None

    scheduler.set_dependency_health("redis", healthy=True)
    clock.advance(1)
    task = run(scheduler.dequeue())

    assert task is not None
    assert task["id"] == task_id
    assert task["health_gate"]["decision"] == "released"
    assert task_id in scheduler.in_flight_ids()
    assert scheduler.complete(task_id)


def test_queue_dependency_policy_defers_task_without_task_metadata():
    clock = ManualClock()
    scheduler = TaskScheduler(clock=clock, health_retry_delay=10)
    scheduler.set_queue_dependencies("payments", ["stripe"])
    scheduler.set_dependency_health("stripe", healthy=False)

    task_id = scheduler.enqueue({"type": "settle"}, queue="payments")

    assert run(scheduler.dequeue("payments")) is None
    assert task_id in scheduler.scheduled_ids()
    assert task_id not in scheduler.in_flight_ids()


def test_blocked_task_does_not_hold_ready_independent_work():
    clock = ManualClock()
    scheduler = TaskScheduler(clock=clock, health_retry_delay=20)
    scheduler.set_dependency_health("vector-db", healthy=False)
    blocked_id = scheduler.enqueue(
        {"type": "index", "external_dependencies": {"vector-db": "write"}},
        priority=10,
    )
    ready_id = scheduler.enqueue({"type": "local-cleanup"}, priority=1)

    assert run(scheduler.dequeue()) is None
    ready = run(scheduler.dequeue())

    assert blocked_id in scheduler.scheduled_ids()
    assert ready is not None
    assert ready["id"] == ready_id
    assert ready["type"] == "local-cleanup"


def test_scheduled_task_keeps_due_queue_priority_and_identity():
    clock = ManualClock()
    scheduler = TaskScheduler(clock=clock)
    task_id = scheduler.schedule(
        {"type": "daily-report"},
        delay=3,
        queue="reports",
        priority=9,
    )

    assert run(scheduler.dequeue("reports")) is None
    clock.advance(3)
    task = run(scheduler.dequeue("reports"))

    assert task is not None
    assert task["id"] == task_id
    assert task["queue"] == "reports"
    assert task["priority"] == 9


def test_health_audit_is_bounded_and_sanitizes_reasons():
    clock = ManualClock()
    scheduler = TaskScheduler(clock=clock, audit_limit=2)

    scheduler.set_dependency_health("dep-0", healthy=False)
    scheduler.set_dependency_health(
        "dep-1",
        healthy=False,
        reason="password=hidden",
    )
    scheduler.set_dependency_health("dep-2", healthy=True)

    records = scheduler.audit_records()
    assert len(records) == 2
    assert records[0]["dependency"] == "dep-1"
    assert records[0]["reason"] == "redacted"
    assert records[1]["dependency"] == "dep-2"
    assert "hidden" not in repr(records)
