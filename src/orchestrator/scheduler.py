"""Task Scheduler — Priority-based task queuing and dispatch."""

import heapq
import logging
import math
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class PriorityQueue:
    def __init__(self):
        self._queue = []
        self._counter = 0

    def push(self, item: Any, priority: int = 0) -> None:
        heapq.heappush(self._queue, (-priority, self._counter, item))
        self._counter += 1

    def pop(self) -> Optional[Any]:
        if self._queue:
            return heapq.heappop(self._queue)[2]
        return None

    def peek(self) -> Optional[Any]:
        if self._queue:
            return self._queue[0][2]
        return None

    def __len__(self) -> int:
        return len(self._queue)


class TaskScheduler:
    def __init__(self, visibility_timeout: float = 30.0):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, Dict[str, Any]] = {}
        self._in_flight: Dict[str, Dict] = {}
        self._max_retries = 3
        self._visibility_timeout = visibility_timeout
        self._visibility_extensions: Dict[str, Dict[str, float]] = {}
        self._audit_log: List[Dict[str, Any]] = []

    def _audit(self, event: str, task_id: str, **details: Any) -> None:
        safe_details = {
            key: value
            for key, value in details.items()
            if key not in {"payload", "secret", "token", "lease_token"}
        }
        record = {
            "event": event,
            "task_id": task_id,
            "at": time.time(),
            "details": safe_details,
        }
        self._audit_log.append(record)
        logger.info("queue visibility decision", extra={"audit": record})

    def audit_log(self) -> List[Dict[str, Any]]:
        return [record.copy() for record in self._audit_log]

    def _queue_task(self, task: Dict, queue: str, priority: int = 0) -> str:
        task_id = task.setdefault("id", str(uuid4()))
        task.setdefault("enqueued_at", time.time())
        task.setdefault("retries", 0)
        task["queue"] = queue
        task["priority"] = priority
        task.pop("lease_token", None)
        task.pop("visibility_deadline", None)
        task.pop("dequeued_at", None)
        self._visibility_extensions.pop(task_id, None)

        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)
        return task_id

    def enqueue(
        self,
        task: Dict,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        return self._queue_task(task, queue, priority)

    def schedule(
        self,
        task: Dict,
        delay: float,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        task_id = task.setdefault("id", str(uuid4()))
        task.setdefault("retries", 0)
        task["queue"] = queue
        task["priority"] = priority
        self._scheduled[task_id] = {
            "task": task,
            "run_at": time.time() + delay,
            "queue": queue,
            "priority": priority,
        }
        return task_id

    def _promote_due_scheduled(self, now: float) -> None:
        due = [
            tid
            for tid, item in self._scheduled.items()
            if item["run_at"] <= now
        ]
        for task_id in due:
            item = self._scheduled.pop(task_id)
            self._queue_task(item["task"], item["queue"], item["priority"])
            self._audit(
                "scheduled_task_promoted",
                task_id,
                queue=item["queue"],
            )

    def reap_expired_visibility(self, queue: Optional[str] = None) -> int:
        """Return expired in-flight tasks to their queue for another worker."""
        now = time.time()
        expired = [
            (task_id, task)
            for task_id, task in self._in_flight.items()
            if task.get("visibility_deadline", 0) <= now
            and (queue is None or task.get("queue") == queue)
        ]
        for task_id, task in expired:
            self._in_flight.pop(task_id, None)
            self._queue_task(
                task,
                task.get("queue", "default"),
                task.get("priority", 0),
            )
            self._audit(
                "visibility_timeout_expired",
                task_id,
                queue=task.get("queue", "default"),
            )
        return len(expired)

    async def dequeue(
        self,
        queue: str = "default",
        timeout: float = 1.0,
        visibility_timeout: Optional[float] = None,
    ) -> Optional[Dict]:
        now = time.time()
        self._promote_due_scheduled(now)
        self.reap_expired_visibility(queue)

        if queue in self._queues and len(self._queues[queue]) > 0:
            task = self._queues[queue].pop()
            if task:
                lease_seconds = self._visibility_timeout
                if visibility_timeout is not None:
                    lease_seconds = visibility_timeout
                lease_token = str(uuid4())
                task["lease_token"] = lease_token
                task["visibility_deadline"] = now + lease_seconds
                task["dequeued_at"] = now
                task["queue"] = queue
                self._in_flight[task["id"]] = task
                self._audit(
                    "task_dequeued",
                    task["id"],
                    queue=queue,
                    visibility_deadline=task["visibility_deadline"],
                )
                return task
        return None

    def extend_visibility(
        self,
        task_id: str,
        lease_token: str,
        extension: float,
        request_id: Optional[str] = None,
    ) -> bool:
        """Extend an active task lease for a long-running agent."""
        if extension <= 0 or not math.isfinite(extension):
            self._audit(
                "visibility_extension_rejected",
                task_id,
                reason="invalid_extension",
            )
            return False

        task = self._in_flight.get(task_id)
        if not task:
            self._audit(
                "visibility_extension_rejected",
                task_id,
                reason="not_in_flight",
            )
            return False
        if task.get("lease_token") != lease_token:
            self._audit(
                "visibility_extension_rejected",
                task_id,
                reason="stale_lease",
            )
            return False

        now = time.time()
        if task.get("visibility_deadline", 0) <= now:
            self.reap_expired_visibility(task.get("queue"))
            self._audit(
                "visibility_extension_rejected",
                task_id,
                reason="expired",
            )
            return False

        if request_id:
            extension_cache = self._visibility_extensions.setdefault(
                task_id,
                {},
            )
            if request_id in extension_cache:
                task["visibility_deadline"] = max(
                    task["visibility_deadline"],
                    extension_cache[request_id],
                )
                self._audit(
                    "visibility_extension_replayed",
                    task_id,
                    queue=task.get("queue"),
                    visibility_deadline=task["visibility_deadline"],
                )
                return True

        task["visibility_deadline"] = (
            max(task["visibility_deadline"], now) + extension
        )
        if request_id:
            self._visibility_extensions[task_id][request_id] = (
                task["visibility_deadline"]
            )
        self._audit(
            "visibility_extended",
            task_id,
            queue=task.get("queue"),
            visibility_deadline=task["visibility_deadline"],
        )
        return True

    def _active_task_for_ack(
        self,
        task_id: str,
        lease_token: Optional[str],
        action: str,
    ) -> Optional[Dict]:
        task = self._in_flight.get(task_id)
        if not task:
            self._audit(f"{action}_rejected", task_id, reason="not_in_flight")
            return None
        if lease_token is not None and task.get("lease_token") != lease_token:
            self._audit(f"{action}_rejected", task_id, reason="stale_lease")
            return None
        if task.get("visibility_deadline", 0) <= time.time():
            self.reap_expired_visibility(task.get("queue"))
            self._audit(f"{action}_rejected", task_id, reason="expired")
            return None
        return task

    def complete(
        self,
        task_id: str,
        lease_token: Optional[str] = None,
    ) -> bool:
        task = self._active_task_for_ack(task_id, lease_token, "complete")
        if not task:
            return False
        self._in_flight.pop(task_id, None)
        self._visibility_extensions.pop(task_id, None)
        self._audit("task_completed", task_id, queue=task.get("queue"))
        return True

    def fail(
        self,
        task_id: str,
        queue: str = "default",
        lease_token: Optional[str] = None,
    ) -> bool:
        task = self._active_task_for_ack(task_id, lease_token, "fail")
        if not task:
            return False
        self._in_flight.pop(task_id, None)
        task["retries"] += 1
        if task["retries"] < self._max_retries:
            self._queue_task(task, queue, priority=task.get("priority", 0))
            self._audit("task_requeued_after_failure", task_id, queue=queue)
            return True
        self._visibility_extensions.pop(task_id, None)
        self._audit("task_failed_permanently", task_id, queue=queue)
        return False

# 2019-04-25T08:37:12 update

# 2019-06-04T16:40:00 update

# 2019-07-11T12:01:28 update

# 2019-08-02T12:20:21 update

# 2019-08-23T10:38:50 update

# 2019-10-31T13:55:52 update

# 2019-11-04T20:12:32 update

# 2019-12-13T12:22:36 update

# 2020-02-01T10:32:37 update

# 2020-02-26T09:44:38 update

# 2020-03-09T19:00:55 update

# 2020-05-01T18:40:34 update

# 2020-05-12T15:10:31 update

# 2020-06-30T13:24:19 update

# 2020-09-22T16:00:45 update

# 2020-10-20T10:52:48 update

# 2020-10-21T12:18:08 update

# 2020-11-06T12:35:01 update

# 2020-12-09T08:09:33 update

# 2021-01-07T08:20:36 update

# 2021-10-02T15:23:16 update

# 2021-10-06T16:14:57 update

# 2021-10-06T09:27:41 update

# 2021-11-19T08:37:40 update

# 2022-03-01T16:39:54 update

# 2022-05-26T13:43:07 update

# 2022-06-02T10:50:58 update

# 2022-06-14T10:46:48 update

# 2022-07-31T16:44:34 update

# 2022-08-30T18:20:12 update

# 2022-11-04T14:47:03 update

# 2022-12-06T10:36:49 update

# 2022-12-22T13:21:12 update

# 2022-12-26T12:24:50 update

# 2023-03-09T08:09:55 update

# 2023-05-01T10:07:37 update

# 2023-06-08T14:32:15 update

# 2023-07-14T17:24:18 update

# 2023-12-14T08:38:31 update

# 2024-02-20T13:43:58 update

# 2024-03-24T08:52:42 update

# 2024-03-28T15:27:17 update

# 2024-03-29T18:10:33 update

# 2024-04-15T20:18:31 update

# 2024-05-27T13:11:52 update

# 2024-05-27T16:42:56 update

# 2024-06-20T13:03:45 update

# 2024-06-28T12:32:58 update

# 2024-07-10T14:10:16 update

# 2024-07-26T14:18:59 update

# 2024-08-12T08:21:05 update

# 2024-08-21T16:58:40 update

# 2024-09-27T19:54:30 update

# 2024-10-21T13:47:42 update

# 2024-11-11T09:19:27 update

# 2024-12-24T08:23:41 update

# 2025-02-14T10:35:15 update

# 2025-03-31T18:09:40 update

# 2025-06-21T17:32:49 update

# 2025-07-21T16:52:28 update

# 2025-08-20T19:45:16 update

# 2025-11-04T18:54:24 update

# 2025-12-09T20:17:36 update

# 2026-01-12T15:42:32 update

# 2026-01-23T14:41:20 update

# 2026-03-18T14:43:07 update

# 2026-04-13T11:43:19 update
