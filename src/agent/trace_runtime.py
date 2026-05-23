"""Trace aggregation runtime with bounded memory accounting."""

import copy
import json
import threading
import time
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    MutableMapping,
    Optional,
)


DEFAULT_TRACE_MEMORY_LIMIT_BYTES = 1024 * 1024


class TraceOutcome(Enum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class TraceAggregationError(RuntimeError):
    """Base error for terminal trace aggregation outcomes."""

    def __init__(self, message: str, outcome: Dict[str, Any]):
        super().__init__(message)
        self.outcome = copy.deepcopy(outcome)


class TraceMemoryLimitExceeded(TraceAggregationError):
    """Raised when trace aggregation would exceed its memory budget."""


class TraceAggregationCancelled(TraceAggregationError):
    """Raised when a cancelled run receives new trace aggregation work."""


class TraceAggregationRuntime:
    """Aggregate each run once while enforcing a hard memory limit."""

    def __init__(
        self,
        max_bytes: int = DEFAULT_TRACE_MEMORY_LIMIT_BYTES,
        outcome_store: Optional[MutableMapping[str, Dict[str, Any]]] = None,
        clock: Callable[[], float] = time.time,
    ):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self._outcomes = outcome_store if outcome_store is not None else {}
        self._clock = clock
        self._lock = threading.RLock()

    def aggregate(
        self,
        run_id: str,
        events: Iterable[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Aggregate events or replay the run's terminal outcome."""
        self._validate_run_id(run_id)
        with self._lock:
            existing = self._outcomes.get(run_id)
            if existing is not None:
                return self._terminal_response(existing)

            bytes_used = 0
            accepted_events: List[Dict[str, Any]] = []
            for attempted_count, event in enumerate(events, start=1):
                event_copy = self._copy_event(event)
                event_size = self.estimate_event_size(event_copy)
                next_bytes = bytes_used + event_size
                if next_bytes > self.max_bytes:
                    outcome = self._record_outcome(
                        run_id,
                        TraceOutcome.REJECTED,
                        reason="trace aggregation memory limit exceeded",
                        bytes_used=bytes_used,
                        event_count=len(accepted_events),
                        attempted_event_count=attempted_count,
                    )
                    raise TraceMemoryLimitExceeded(outcome["reason"], outcome)
                accepted_events.append(event_copy)
                bytes_used = next_bytes

            outcome = self._record_outcome(
                run_id,
                TraceOutcome.COMPLETED,
                reason="completed",
                bytes_used=bytes_used,
                event_count=len(accepted_events),
                attempted_event_count=len(accepted_events),
                events=accepted_events,
            )
            return copy.deepcopy(outcome)

    def cancel(self, run_id: str, reason: str = "cancelled") -> Dict[str, Any]:
        """Persist cancellation as the one terminal outcome for a run."""
        self._validate_run_id(run_id)
        with self._lock:
            existing = self._outcomes.get(run_id)
            if existing is not None:
                return copy.deepcopy(existing)
            outcome = self._record_outcome(
                run_id,
                TraceOutcome.CANCELLED,
                reason=reason,
                bytes_used=0,
                event_count=0,
                attempted_event_count=0,
            )
            return copy.deepcopy(outcome)

    def get_outcome(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            outcome = self._outcomes.get(run_id)
            return copy.deepcopy(outcome) if outcome is not None else None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            by_status: Dict[str, int] = {}
            for outcome in self._outcomes.values():
                status = outcome["status"]
                by_status[status] = by_status.get(status, 0) + 1
            return {
                "max_bytes": self.max_bytes,
                "terminal_count": len(self._outcomes),
                "by_status": by_status,
            }

    @staticmethod
    def estimate_event_size(event: Dict[str, Any]) -> int:
        payload = json.dumps(
            event,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return len(payload.encode("utf-8"))

    def _record_outcome(
        self,
        run_id: str,
        status: TraceOutcome,
        *,
        reason: str,
        bytes_used: int,
        event_count: int,
        attempted_event_count: int,
        events: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        outcome: Dict[str, Any] = {
            "run_id": run_id,
            "status": status.value,
            "reason": reason,
            "bytes_used": bytes_used,
            "memory_limit_bytes": self.max_bytes,
            "event_count": event_count,
            "attempted_event_count": attempted_event_count,
            "recorded_at": self._clock(),
        }
        if events is not None:
            outcome["events"] = copy.deepcopy(events)
        self._outcomes[run_id] = outcome
        return outcome

    @staticmethod
    def _copy_event(event: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(event, dict):
            raise TypeError("trace event must be a dictionary")
        return copy.deepcopy(event)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")

    @staticmethod
    def _terminal_response(outcome: Dict[str, Any]) -> Dict[str, Any]:
        status = outcome["status"]
        copied = copy.deepcopy(outcome)
        if status == TraceOutcome.COMPLETED.value:
            return copied
        if status == TraceOutcome.CANCELLED.value:
            raise TraceAggregationCancelled(copied["reason"], copied)
        raise TraceMemoryLimitExceeded(copied["reason"], copied)
