import threading

import pytest

from src.agent.trace_runtime import (
    TraceAggregationCancelled,
    TraceAggregationRuntime,
    TraceMemoryLimitExceeded,
)


def test_aggregate_records_completed_terminal_outcome():
    runtime = TraceAggregationRuntime(max_bytes=512, clock=lambda: 123.0)

    outcome = runtime.aggregate(
        "run-1",
        [{"span": "start"}, {"span": "finish", "tokens": 10}],
    )

    assert outcome["status"] == "completed"
    assert outcome["event_count"] == 2
    assert outcome["attempted_event_count"] == 2
    assert outcome["bytes_used"] <= runtime.max_bytes
    assert outcome["recorded_at"] == 123.0
    assert runtime.get_outcome("run-1") == outcome


def test_completed_retry_is_idempotent_and_does_not_mutate_terminal_state():
    runtime = TraceAggregationRuntime(max_bytes=512)
    outcome = runtime.aggregate("run-1", [{"span": "start"}])

    retry = runtime.aggregate("run-1", [{"span": "different"}])
    assert retry == outcome

    retry["events"][0]["span"] = "mutated"
    assert runtime.get_outcome("run-1") == outcome


def test_memory_limit_records_single_rejected_outcome_before_retry():
    runtime = TraceAggregationRuntime(max_bytes=42)

    with pytest.raises(TraceMemoryLimitExceeded) as first:
        runtime.aggregate(
            "run-2",
            [{"span": "small"}, {"span": "x" * 200}],
        )

    first_outcome = first.value.outcome
    assert first_outcome["status"] == "rejected"
    assert first_outcome["event_count"] == 1
    assert first_outcome["attempted_event_count"] == 2
    assert first_outcome["bytes_used"] <= runtime.max_bytes
    assert "events" not in first_outcome

    with pytest.raises(TraceMemoryLimitExceeded) as retry:
        runtime.aggregate("run-2", [{"span": "retry"}])

    assert retry.value.outcome == first_outcome
    assert runtime.snapshot()["terminal_count"] == 1


def test_rejected_terminal_outcome_can_be_shared_across_runtime_instances():
    store = {}
    runtime = TraceAggregationRuntime(max_bytes=24, outcome_store=store)

    with pytest.raises(TraceMemoryLimitExceeded) as rejected:
        runtime.aggregate("run-3", [{"span": "x" * 200}])

    restarted_runtime = TraceAggregationRuntime(
        max_bytes=24,
        outcome_store=store,
    )
    with pytest.raises(TraceMemoryLimitExceeded) as retry:
        restarted_runtime.aggregate("run-3", [{"span": "small"}])

    assert retry.value.outcome == rejected.value.outcome


def test_cancelled_run_rejects_later_aggregation_without_new_state():
    runtime = TraceAggregationRuntime(max_bytes=512)

    cancelled = runtime.cancel("run-4", reason="worker cancelled")
    assert runtime.cancel("run-4", reason="different") == cancelled

    with pytest.raises(TraceAggregationCancelled) as retry:
        runtime.aggregate("run-4", [{"span": "late"}])

    assert retry.value.outcome == cancelled
    assert runtime.snapshot() == {
        "max_bytes": 512,
        "terminal_count": 1,
        "by_status": {"cancelled": 1},
    }


def test_concurrent_memory_limit_retries_keep_one_terminal_outcome():
    runtime = TraceAggregationRuntime(max_bytes=24)
    barrier = threading.Barrier(4)
    outcomes = []

    def attempt():
        barrier.wait()
        with pytest.raises(TraceMemoryLimitExceeded) as rejected:
            runtime.aggregate("run-5", [{"span": "x" * 200}])
        outcomes.append(rejected.value.outcome)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 4
    assert all(outcome == outcomes[0] for outcome in outcomes)
    assert runtime.snapshot() == {
        "max_bytes": 24,
        "terminal_count": 1,
        "by_status": {"rejected": 1},
    }


def test_invalid_limits_and_run_ids_are_rejected_before_work():
    with pytest.raises(ValueError):
        TraceAggregationRuntime(max_bytes=0)

    runtime = TraceAggregationRuntime()
    with pytest.raises(ValueError):
        runtime.aggregate(" ", [{"span": "start"}])
    with pytest.raises(TypeError):
        runtime.aggregate("run-6", ["not-a-dict"])
