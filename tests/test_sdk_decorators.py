import asyncio
from enum import IntEnum

import pytest

from src.sdk.decorators import task


class RetryCount(IntEnum):
    TWO = 2


def test_task_default_config_is_attached_to_wrapper_only():
    async def handler():
        return "ok"

    decorated = task()(handler)

    assert not hasattr(handler, "__task_config__")
    assert decorated.__task_config__ == {
        "name": "handler",
        "retries": 0,
        "timeout": 300,
    }
    assert asyncio.run(decorated()) == "ok"


def test_task_accepts_and_normalizes_integral_retries():
    async def handler():
        return "ok"

    decorated = task(name="retryable", retries=RetryCount.TWO, timeout=10)(
        handler
    )

    assert not hasattr(handler, "__task_config__")
    assert decorated.__task_config__ == {
        "name": "retryable",
        "retries": 2,
        "timeout": 10,
    }


@pytest.mark.parametrize(
    "invalid_retries",
    [-1, -3, True, False, 1.5, "3", None],
)
def test_task_rejects_invalid_retry_counts_before_metadata(
    invalid_retries,
):
    async def handler():
        return "ok"

    with pytest.raises(ValueError, match="non-negative integer"):
        task(retries=invalid_retries)(handler)

    assert not hasattr(handler, "__task_config__")


def test_task_timeout_uses_validated_task_name():
    async def slow_handler():
        await asyncio.sleep(0.05)

    decorated = task(name="slow-task", timeout=0.001)(slow_handler)

    with pytest.raises(TimeoutError, match="slow-task timed out"):
        asyncio.run(decorated())
