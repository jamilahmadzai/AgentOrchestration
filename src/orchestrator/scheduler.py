"""Task Scheduler — Priority-based task queuing and dispatch."""

import heapq
import time
from threading import RLock
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from src.common.metrics import metrics


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
    def __init__(
        self,
        max_retries: int = 3,
        poison_redelivery_delay: float = 30.0,
    ):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, Dict[str, Any]] = {}
        self._in_flight: Dict[str, Dict] = {}
        self._queued_task_ids: Set[str] = set()
        self._terminal: Dict[str, Dict[str, Any]] = {}
        self._audit_events: List[Dict[str, Any]] = []
        self._max_retries = max_retries
        self._poison_redelivery_delay = poison_redelivery_delay
        self._lock = RLock()

    def enqueue(
        self,
        task: Dict,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        with self._lock:
            task_id = task.get("id") or str(uuid4())
            task["id"] = task_id
            task["enqueued_at"] = time.time()
            task["retries"] = int(task.get("retries", 0))
            task["priority"] = priority

            self._enqueue_existing(task, queue, priority, reason="new")
            return task_id

    def schedule(
        self,
        task: Dict,
        delay: float,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        with self._lock:
            task_id = task.get("id") or str(uuid4())
            task["id"] = task_id
            task["retries"] = int(task.get("retries", 0))
            task["priority"] = priority
            task["state"] = "scheduled"
            self._scheduled[task_id] = {
                "due_at": time.time() + delay,
                "task": task,
                "queue": queue,
                "priority": priority,
            }
            self._audit(
                task_id,
                "scheduled",
                queue=queue,
                reason="delayed",
                retries=task["retries"],
            )
            return task_id

    async def dequeue(
        self,
        queue: str = "default",
        timeout: float = 1.0,
    ) -> Optional[Dict]:
        del timeout
        with self._lock:
            now = time.time()
            expired = [
                tid for tid, item in self._scheduled.items()
                if item["due_at"] <= now
            ]
            for tid in expired:
                scheduled = self._scheduled.pop(tid)
                self._enqueue_existing(
                    scheduled["task"],
                    scheduled["queue"],
                    scheduled["priority"],
                    reason="scheduled_due",
                )

            queue_obj = self._queues.get(queue)
            while queue_obj and len(queue_obj) > 0:
                task = queue_obj.pop()
                if not task:
                    continue
                task_id = task["id"]
                if task_id not in self._queued_task_ids:
                    continue
                self._queued_task_ids.remove(task_id)

                if task_id in self._terminal:
                    self._audit(
                        task_id,
                        "claim_rejected",
                        queue=queue,
                        reason="terminal",
                    )
                    metrics.increment("scheduler.claim_rejected.terminal")
                    continue
                if task_id in self._in_flight:
                    self._audit(
                        task_id,
                        "claim_rejected",
                        queue=queue,
                        reason="already_in_flight",
                    )
                    metrics.increment("scheduler.claim_rejected.in_flight")
                    continue

                task["state"] = "in_flight"
                task["last_claimed_at"] = now
                self._in_flight[task_id] = task
                self._audit(
                    task_id,
                    "claimed",
                    queue=queue,
                    retries=task.get("retries", 0),
                )
                return task
            return None

    def complete(self, task_id: str) -> bool:
        with self._lock:
            task = self._in_flight.pop(task_id, None)
            if not task:
                self._audit(
                    task_id,
                    "ack_rejected",
                    reason=self._missing_task_reason(task_id),
                )
                metrics.increment("scheduler.ack_rejected")
                return False

            task["state"] = "completed"
            self._terminal[task_id] = {
                "state": "completed",
                "retries": task.get("retries", 0),
                "completed_at": time.time(),
            }
            self._audit(task_id, "completed", retries=task.get("retries", 0))
            metrics.increment("scheduler.completed")
            return True

    def fail(
        self,
        task_id: str,
        queue: str = "default",
        reason: str = "failure",
    ) -> bool:
        with self._lock:
            task = self._in_flight.pop(task_id, None)
            if not task:
                self._audit(
                    task_id,
                    "ack_rejected",
                    reason=self._missing_task_reason(task_id),
                )
                metrics.increment("scheduler.ack_rejected")
                return False

            task["retries"] = int(task.get("retries", 0)) + 1
            retries = task["retries"]
            if retries >= self._max_retries:
                task["state"] = "failed"
                self._terminal[task_id] = {
                    "state": "failed",
                    "reason": self._safe_reason(reason),
                    "retries": retries,
                    "failed_at": time.time(),
                }
                self._audit(
                    task_id,
                    "failed_terminal",
                    queue=queue,
                    reason=reason,
                    retries=retries,
                )
                metrics.increment("scheduler.failed_terminal")
                return False

            priority = int(task.get("priority", 0))
            if reason == "worker_crash_loop":
                task["state"] = "scheduled"
                self._scheduled[task_id] = {
                    "due_at": time.time() + self._poison_redelivery_delay,
                    "task": task,
                    "queue": queue,
                    "priority": priority,
                }
                self._audit(
                    task_id,
                    "redelivery_deferred",
                    queue=queue,
                    reason=reason,
                    retries=retries,
                )
                metrics.increment("scheduler.redelivery_deferred.poison")
                return True

            return self._enqueue_existing(
                task,
                queue,
                priority,
                reason="retry",
            )

    def task_state(self, task_id: str) -> Optional[str]:
        with self._lock:
            if task_id in self._terminal:
                return self._terminal[task_id]["state"]
            if task_id in self._in_flight:
                return "in_flight"
            if task_id in self._scheduled:
                return "scheduled"
            if task_id in self._queued_task_ids:
                return "queued"
            return None

    def audit_events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [event.copy() for event in self._audit_events]

    def _enqueue_existing(
        self,
        task: Dict,
        queue: str,
        priority: int,
        reason: str,
    ) -> bool:
        task_id = task["id"]
        if task_id in self._terminal:
            self._audit(
                task_id,
                "enqueue_rejected",
                queue=queue,
                reason="terminal",
            )
            metrics.increment("scheduler.enqueue_rejected.terminal")
            return False
        if task_id in self._in_flight:
            self._audit(
                task_id,
                "enqueue_rejected",
                queue=queue,
                reason="already_in_flight",
            )
            metrics.increment("scheduler.enqueue_rejected.in_flight")
            return False
        if task_id in self._queued_task_ids:
            self._audit(
                task_id,
                "enqueue_rejected",
                queue=queue,
                reason="already_queued",
            )
            metrics.increment("scheduler.enqueue_rejected.duplicate")
            return False

        task["queue"] = queue
        task["priority"] = priority
        task["state"] = "queued"
        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)
        self._queued_task_ids.add(task_id)
        self._audit(
            task_id,
            "queued",
            queue=queue,
            reason=reason,
            retries=task.get("retries", 0),
        )
        metrics.increment("scheduler.queued")
        return True

    def _audit(
        self,
        task_id: str,
        action: str,
        queue: Optional[str] = None,
        reason: Optional[str] = None,
        retries: Optional[int] = None,
    ) -> None:
        event: Dict[str, Any] = {
            "task_id": task_id,
            "action": action,
            "at": time.time(),
        }
        if queue is not None:
            event["queue"] = queue
        if reason is not None:
            event["reason"] = self._safe_reason(reason)
        if retries is not None:
            event["retries"] = retries
        self._audit_events.append(event)

    def _missing_task_reason(self, task_id: str) -> str:
        if task_id in self._terminal:
            return "terminal"
        if task_id in self._queued_task_ids:
            return "queued_not_claimed"
        if task_id in self._scheduled:
            return "scheduled_not_claimed"
        return "unknown"

    def _safe_reason(self, reason: str) -> str:
        safe_reasons = {
            "already_in_flight",
            "already_queued",
            "delayed",
            "failure",
            "new",
            "queued_not_claimed",
            "retry",
            "scheduled_due",
            "scheduled_not_claimed",
            "terminal",
            "unknown",
            "worker_crash_loop",
        }
        return reason if reason in safe_reasons else "failure"

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
