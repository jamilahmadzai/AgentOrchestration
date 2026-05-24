"""Task Scheduler — Priority-based task queuing and dispatch."""

import heapq
import time
from collections import deque
from threading import RLock
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional
from uuid import uuid4


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
        *,
        clock: Callable[[], float] = time.time,
        health_retry_delay: float = 30.0,
        audit_limit: int = 100,
    ):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, Dict[str, Any]] = {}
        self._in_flight: Dict[str, Dict] = {}
        self._dependency_health: Dict[str, Dict[str, Any]] = {}
        self._queue_dependencies: Dict[str, List[str]] = {}
        self._audit: Deque[Dict[str, Any]] = deque(maxlen=audit_limit)
        self._max_retries = 3
        self._clock = clock
        self._health_retry_delay = health_retry_delay
        self._lock = RLock()

    def enqueue(
        self,
        task: Dict,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        with self._lock:
            return self._queue_task(
                task,
                queue,
                priority,
                preserve_id=False,
            )

    def schedule(
        self,
        task: Dict,
        delay: float,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        with self._lock:
            return self._schedule_task(
                task,
                delay,
                queue,
                priority,
                preserve_id=False,
            )

    def set_dependency_health(
        self,
        dependency: str,
        healthy: bool,
        reason: Optional[str] = None,
    ) -> None:
        name = self._normalize_dependency_name(dependency)
        safe_reason = self._safe_reason(reason)
        with self._lock:
            self._dependency_health[name] = {
                "healthy": bool(healthy),
                "reason": safe_reason,
                "updated_at": self._clock(),
            }
            self._record_audit(
                "dependency_health_changed",
                dependency=name,
                healthy=bool(healthy),
                reason=safe_reason,
            )

    def set_queue_dependencies(
        self,
        queue: str,
        dependencies: Iterable[str],
    ) -> None:
        with self._lock:
            self._queue_dependencies[queue] = self._normalize_dependencies(
                dependencies,
            )

    def audit_records(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._audit]

    def dependency_health_snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                dependency: dict(state)
                for dependency, state in self._dependency_health.items()
            }

    def scheduled_ids(self) -> List[str]:
        with self._lock:
            return list(self._scheduled.keys())

    def in_flight_ids(self) -> List[str]:
        with self._lock:
            return list(self._in_flight.keys())

    async def dequeue(
        self,
        queue: str = "default",
        timeout: float = 1.0,
    ) -> Optional[Dict]:
        with self._lock:
            self._move_due_tasks()

            if queue not in self._queues or len(self._queues[queue]) == 0:
                return None

            while len(self._queues[queue]) > 0:
                task = self._queues[queue].pop()
                if not task:
                    continue

                blocked = self._blocked_dependencies(task, queue)
                if blocked:
                    self._defer_for_health_gate(task, queue, blocked)
                    continue

                task_id = task["id"]
                previous_gate = task.get("health_gate", {})
                self._in_flight[task_id] = task
                if previous_gate.get("decision") == "deferred":
                    task["health_gate"] = {
                        "decision": "released",
                        "released_at": self._clock(),
                        "blocked_dependencies": previous_gate.get(
                            "blocked_dependencies",
                            [],
                        ),
                    }
                    self._record_audit(
                        "task_released_dependency_health_gate",
                        task_id=task_id,
                        queue=queue,
                        blocked_dependencies=previous_gate.get(
                            "blocked_dependencies",
                            [],
                        ),
                    )
                return task
            return None

    def complete(self, task_id: str) -> bool:
        with self._lock:
            return self._in_flight.pop(task_id, None) is not None

    def fail(self, task_id: str, queue: str = "default") -> bool:
        with self._lock:
            task = self._in_flight.pop(task_id, None)
            if task:
                task["retries"] += 1
                if task["retries"] < self._max_retries:
                    self._queue_task(
                        task,
                        queue,
                        priority=task.get("priority", 0),
                        preserve_id=True,
                    )
                    return True
            return False

    def _queue_task(
        self,
        task: Dict,
        queue: str,
        priority: int,
        *,
        preserve_id: bool,
    ) -> str:
        task_id = task.get("id") if preserve_id else None
        if not task_id:
            task_id = str(uuid4())
        task["id"] = task_id
        task["enqueued_at"] = self._clock()
        task["queue"] = queue
        task["priority"] = priority
        if not preserve_id or "retries" not in task:
            task["retries"] = 0

        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)
        return task_id

    def _schedule_task(
        self,
        task: Dict,
        delay: float,
        queue: str,
        priority: int,
        *,
        preserve_id: bool,
    ) -> str:
        task_id = task.get("id") if preserve_id else None
        if not task_id:
            task_id = str(uuid4())
        now = self._clock()
        due_at = now + max(delay, 0.0)

        task["id"] = task_id
        task["queue"] = queue
        task["priority"] = priority
        task["scheduled_at"] = now
        if not preserve_id or "retries" not in task:
            task["retries"] = 0

        self._scheduled[task_id] = {
            "task": task,
            "due_at": due_at,
            "queue": queue,
            "priority": priority,
        }
        return task_id

    def _move_due_tasks(self) -> None:
        now = self._clock()
        expired = [
            task_id
            for task_id, record in self._scheduled.items()
            if record["due_at"] <= now
        ]
        for task_id in expired:
            record = self._scheduled.pop(task_id)
            self._queue_task(
                record["task"],
                record["queue"],
                record["priority"],
                preserve_id=True,
            )

    def _blocked_dependencies(self, task: Dict, queue: str) -> List[str]:
        blocked: List[str] = []
        for dependency in self._task_dependencies(task, queue):
            state = self._dependency_health.get(dependency)
            if state and not state.get("healthy", True):
                blocked.append(dependency)
        return blocked

    def _task_dependencies(self, task: Dict, queue: str) -> List[str]:
        dependencies: List[str] = []
        dependencies.extend(self._queue_dependencies.get(queue, []))
        for key in (
            "required_dependencies",
            "external_dependencies",
            "health_dependencies",
        ):
            dependencies.extend(
                self._normalize_dependencies(task.get(key)),
            )
        return sorted(set(dependencies))

    def _defer_for_health_gate(
        self,
        task: Dict,
        queue: str,
        blocked: List[str],
    ) -> None:
        now = self._clock()
        retry_at = now + self._health_retry_delay
        gate = task.get("health_gate", {})
        deferrals = int(gate.get("deferrals", 0)) + 1
        task["health_gate"] = {
            "decision": "deferred",
            "reason": "dependency_unhealthy",
            "blocked_dependencies": blocked,
            "deferred_at": now,
            "retry_at": retry_at,
            "deferrals": deferrals,
        }
        self._scheduled[task["id"]] = {
            "task": task,
            "due_at": retry_at,
            "queue": queue,
            "priority": task.get("priority", 0),
        }
        self._record_audit(
            "task_deferred_dependency_health_gate",
            task_id=task["id"],
            queue=queue,
            blocked_dependencies=blocked,
            retry_at=retry_at,
            deferrals=deferrals,
        )

    def _record_audit(self, action: str, **metadata: Any) -> None:
        record = {
            "action": action,
            "at": self._clock(),
        }
        for key, value in metadata.items():
            record[key] = self._audit_value(value)
        self._audit.append(record)

    def _audit_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._safe_text(value)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        if isinstance(value, (list, tuple, set)):
            ordered = sorted(value, key=lambda item: str(item))
            return [
                self._audit_value(item)
                for item in ordered[:20]
            ]
        return self._safe_text(value)

    def _safe_reason(self, reason: Optional[str]) -> Optional[str]:
        if reason is None:
            return None
        text = self._safe_text(reason)
        lowered = text.lower()
        sensitive = ("secret", "token", "key", "password", "credential")
        if any(marker in lowered for marker in sensitive):
            return "redacted"
        return text

    def _safe_text(self, value: Any, limit: int = 160) -> str:
        text = str(value).replace("\n", " ").replace("\r", " ")
        if len(text) > limit:
            return f"{text[:limit]}..."
        return text

    def _normalize_dependency_name(self, dependency: Any) -> str:
        name = str(dependency).strip()
        if not name:
            raise ValueError("dependency name cannot be empty")
        return name

    def _normalize_dependencies(self, dependencies: Any) -> List[str]:
        if dependencies is None:
            return []
        if isinstance(dependencies, str):
            values = [dependencies]
        elif isinstance(dependencies, dict):
            values = dependencies.keys()
        else:
            try:
                values = list(dependencies)
            except TypeError:
                values = [dependencies]

        normalized = {
            str(value).strip()
            for value in values
            if str(value).strip()
        }
        return sorted(normalized)

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
