"""Workflow Manager — Defines and executes multi-step agent workflows."""

import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import uuid4

logger = logging.getLogger(__name__)


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        return self in {
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
        }


class WorkflowDependencyError(ValueError):
    """Raised when a workflow dependency graph cannot be executed safely."""


class WorkflowStep:
    def __init__(
        self,
        name: str,
        handler: Callable,
        retries: int = 0,
        timeout: int = 300,
        dependencies: Optional[List[str]] = None,
    ):
        self.id = str(uuid4())
        self.name = name
        self.handler = handler
        self.retries = retries
        self.timeout = timeout
        self.dependencies = list(dependencies or [])
        self.status = StepStatus.PENDING
        self.result: Any = None
        self.error: Optional[str] = None


class Workflow:
    def __init__(self, name: str, description: str = ""):
        self.id = str(uuid4())
        self.name = name
        self.description = description
        self.steps: List[WorkflowStep] = []
        self._step_map: Dict[str, WorkflowStep] = {}
        self.status = StepStatus.PENDING
        self.audit_records: List[Dict[str, Any]] = []

    def add_step(self, step: WorkflowStep) -> "Workflow":
        if step.id in self._step_map:
            raise WorkflowDependencyError("duplicate workflow step id")
        self.steps.append(step)
        self._step_map[step.id] = step
        return self

    def add_step_with_dependencies(
        self,
        step: WorkflowStep,
        dependencies: List[WorkflowStep],
    ) -> "Workflow":
        step.dependencies = [dependency.id for dependency in dependencies]
        return self.add_step(step)

    def get_step(self, step_id: str) -> Optional[WorkflowStep]:
        return self._step_map.get(step_id)

    def validate_dependencies(self) -> List[str]:
        errors: List[str] = []

        for step in self.steps:
            for dependency_id in step.dependencies:
                if dependency_id not in self._step_map:
                    errors.append("unknown dependency")

        visiting: Set[str] = set()
        visited: Set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                errors.append("cyclic dependency")
                return
            if step_id in visited:
                return
            visiting.add(step_id)
            step = self._step_map.get(step_id)
            if step is not None:
                for dependency_id in step.dependencies:
                    if dependency_id in self._step_map:
                        visit(dependency_id)
            visiting.remove(step_id)
            visited.add(step_id)

        for step in self.steps:
            visit(step.id)
        return errors

    def dependency_decision(self, step: WorkflowStep) -> Dict[str, Any]:
        failed_dependencies = []
        waiting_dependencies = []

        for dependency_id in step.dependencies:
            dependency = self._step_map.get(dependency_id)
            if dependency is None:
                return {
                    "allowed": False,
                    "decision": "reject_unknown_dependency",
                    "dependency_count": len(step.dependencies),
                }
            if dependency.status in {StepStatus.FAILED, StepStatus.SKIPPED}:
                failed_dependencies.append(dependency.id)
            elif dependency.status != StepStatus.COMPLETED:
                waiting_dependencies.append(dependency.id)

        if failed_dependencies:
            return {
                "allowed": False,
                "decision": "reject_failed_dependency",
                "dependency_count": len(step.dependencies),
                "failed_dependencies": failed_dependencies,
            }
        if waiting_dependencies:
            return {
                "allowed": False,
                "decision": "defer_dependency_not_ready",
                "dependency_count": len(step.dependencies),
                "waiting_dependencies": waiting_dependencies,
            }
        return {
            "allowed": True,
            "decision": "allow",
            "dependency_count": len(step.dependencies),
        }

    def record_dependency_decision(
        self,
        step: WorkflowStep,
        decision: Dict[str, Any],
    ) -> None:
        record = {
            "workflow_id": self.id,
            "step_id": step.id,
            "decision": decision["decision"],
            "dependency_count": decision.get("dependency_count", 0),
            "failed_dependencies": decision.get("failed_dependencies", []),
            "waiting_dependencies": decision.get("waiting_dependencies", []),
        }
        self.audit_records.append(record)
        logger.info(
            "workflow dependency decision workflow=%s step=%s decision=%s",
            self.id,
            step.id,
            decision["decision"],
        )


class WorkflowManager:
    def __init__(self):
        self._workflows: Dict[str, Workflow] = {}

    def create_workflow(self, name: str, description: str = "") -> Workflow:
        workflow = Workflow(name, description)
        self._workflows[workflow.id] = workflow
        return workflow

    def register_workflow(self, workflow: Workflow) -> bool:
        errors = workflow.validate_dependencies()
        if errors:
            workflow.audit_records.append({
                "workflow_id": workflow.id,
                "decision": "reject_invalid_dependency_graph",
                "error_count": len(errors),
            })
            logger.warning("workflow rejected workflow=%s", workflow.id)
            return False
        self._workflows[workflow.id] = workflow
        return True

    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        return self._workflows.get(workflow_id)

    def list_workflows(self) -> List[Workflow]:
        return list(self._workflows.values())

    def delete_workflow(self, workflow_id: str) -> bool:
        return self._workflows.pop(workflow_id, None) is not None

    def execute_workflow(self, workflow_id: str) -> bool:
        workflow = self._workflows.get(workflow_id)
        if not workflow:
            return False

        if workflow.validate_dependencies():
            workflow.audit_records.append({
                "workflow_id": workflow.id,
                "decision": "reject_invalid_dependency_graph",
            })
            workflow.status = StepStatus.FAILED
            return False

        workflow.status = StepStatus.RUNNING
        executed_or_blocked = True
        failed = False

        while executed_or_blocked:
            executed_or_blocked = False
            for step in workflow.steps:
                if (
                    step.status.is_terminal
                    or step.status == StepStatus.RUNNING
                ):
                    continue

                decision = workflow.dependency_decision(step)
                if not decision["allowed"]:
                    workflow.record_dependency_decision(step, decision)
                    if decision["decision"] == "reject_failed_dependency":
                        step.status = StepStatus.SKIPPED
                        step.error = "dependency failed"
                        failed = True
                        executed_or_blocked = True
                    continue

                if step.dependencies:
                    workflow.record_dependency_decision(step, decision)

                executed_or_blocked = True
                step.status = StepStatus.RUNNING
                try:
                    result = step.handler()
                    step.result = result
                    step.status = StepStatus.COMPLETED
                except Exception:
                    step.error = "handler failed"
                    step.status = StepStatus.FAILED
                    workflow.audit_records.append({
                        "workflow_id": workflow.id,
                        "step_id": step.id,
                        "decision": "handler_failed",
                    })
                    failed = True

        pending = [
            step for step in workflow.steps
            if step.status == StepStatus.PENDING
        ]
        if pending:
            workflow.status = StepStatus.RUNNING
            return False

        workflow.status = StepStatus.FAILED if failed else StepStatus.COMPLETED
        return not failed

# 2019-03-27T19:58:07 update

# 2019-05-09T09:42:56 update

# 2019-12-03T10:07:42 update

# 2020-01-16T18:43:28 update

# 2020-03-20T10:40:15 update

# 2020-04-17T15:36:50 update

# 2020-05-04T14:44:01 update

# 2020-06-16T13:17:31 update

# 2020-08-05T17:00:24 update

# 2020-09-04T08:29:23 update

# 2020-09-09T17:52:02 update

# 2020-10-23T10:57:44 update

# 2020-12-05T20:55:47 update

# 2021-01-15T19:23:40 update

# 2021-02-03T20:43:12 update

# 2021-03-16T12:26:47 update

# 2021-04-20T14:33:28 update

# 2021-10-14T15:03:32 update

# 2021-10-21T17:24:55 update

# 2021-11-16T17:01:08 update

# 2021-11-22T09:51:21 update

# 2021-12-21T16:15:47 update

# 2022-03-23T16:52:27 update

# 2022-12-21T09:25:50 update

# 2023-01-09T09:55:25 update

# 2023-01-13T11:06:15 update

# 2023-01-26T11:00:59 update

# 2023-02-23T08:56:54 update

# 2023-05-17T08:07:16 update

# 2023-06-06T17:09:34 update

# 2023-06-13T10:35:28 update

# 2023-08-24T20:36:06 update

# 2023-10-30T19:10:13 update

# 2024-01-02T08:27:25 update

# 2024-01-24T12:13:15 update

# 2024-02-08T13:35:49 update

# 2024-05-07T16:09:24 update

# 2024-05-11T09:48:46 update

# 2024-05-21T19:25:41 update

# 2024-06-05T12:00:30 update

# 2024-06-25T09:40:26 update

# 2024-09-17T13:49:39 update

# 2024-10-14T17:39:35 update

# 2024-11-27T20:14:35 update

# 2024-12-25T19:31:41 update

# 2025-01-16T13:15:09 update

# 2025-02-05T14:06:59 update

# 2025-02-17T20:55:11 update

# 2025-04-30T19:36:53 update

# 2025-07-17T10:14:40 update

# 2025-08-29T12:13:15 update

# 2025-09-03T13:51:11 update

# 2025-09-19T16:08:24 update

# 2025-11-27T08:38:12 update

# 2026-01-27T13:23:38 update

# 2026-01-28T11:22:50 update
