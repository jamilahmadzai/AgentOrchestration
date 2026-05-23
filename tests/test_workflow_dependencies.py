import logging

from src.orchestrator.workflow import (
    StepStatus,
    Workflow,
    WorkflowManager,
    WorkflowStep,
)


def test_fan_in_join_waits_for_dependencies_and_runs_once():
    manager = WorkflowManager()
    workflow = manager.create_workflow("fan-in-success")
    calls = []

    left = WorkflowStep("left", lambda: calls.append("left") or "left-ok")
    right = WorkflowStep("right", lambda: calls.append("right") or "right-ok")
    join = WorkflowStep("join", lambda: calls.append("join") or "join-ok")

    workflow.add_step(join)
    workflow.add_step(left)
    workflow.add_step(right)
    join.dependencies = [left.id, right.id]

    assert manager.execute_workflow(workflow.id)
    assert calls == ["left", "right", "join"]
    assert left.status == StepStatus.COMPLETED
    assert right.status == StepStatus.COMPLETED
    assert join.status == StepStatus.COMPLETED
    assert workflow.status == StepStatus.COMPLETED
    assert any(
        record["decision"] == "defer_dependency_not_ready"
        for record in workflow.audit_records
    )
    assert any(
        record["decision"] == "allow"
        and record["step_id"] == join.id
        for record in workflow.audit_records
    )


def test_sequential_workflow_without_dependencies_still_runs_in_order():
    manager = WorkflowManager()
    workflow = manager.create_workflow("sequential")
    calls = []

    workflow.add_step(
        WorkflowStep("first", lambda: calls.append("first") or "first-ok")
    )
    workflow.add_step(
        WorkflowStep("second", lambda: calls.append("second") or "second-ok")
    )

    assert manager.execute_workflow(workflow.id)
    assert calls == ["first", "second"]
    assert [step.status for step in workflow.steps] == [
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
    ]
    assert workflow.audit_records == []


def test_failed_dependency_skips_fan_in_without_reviving_failure(caplog):
    manager = WorkflowManager()
    workflow = manager.create_workflow("fan-in-failure")
    calls = []

    def fail_dependency():
        calls.append("left")
        raise RuntimeError("private downstream token")

    left = WorkflowStep("left", fail_dependency)
    right = WorkflowStep("right", lambda: calls.append("right") or "right-ok")
    join = WorkflowStep("join", lambda: calls.append("join") or "join-ok")

    workflow.add_step(left)
    workflow.add_step(right)
    workflow.add_step_with_dependencies(join, [left, right])

    with caplog.at_level(logging.INFO, logger="src.orchestrator.workflow"):
        assert not manager.execute_workflow(workflow.id)

    assert calls == ["left", "right"]
    assert left.status == StepStatus.FAILED
    assert left.error == "handler failed"
    assert right.status == StepStatus.COMPLETED
    assert join.status == StepStatus.SKIPPED
    assert join.error == "dependency failed"
    assert workflow.status == StepStatus.FAILED
    assert "private downstream token" not in caplog.text
    failed_decision = [
        record for record in workflow.audit_records
        if record["decision"] == "reject_failed_dependency"
    ][0]
    assert failed_decision["step_id"] == join.id
    assert failed_decision["failed_dependencies"] == [left.id]


def test_failed_dependency_skips_multiple_downstream_joins():
    manager = WorkflowManager()
    workflow = manager.create_workflow("fan-in-multiple")
    calls = []

    def fail_dependency():
        calls.append("source")
        raise RuntimeError("do not log this")

    source = WorkflowStep("source", fail_dependency)
    first_join = WorkflowStep("first-join", lambda: calls.append("first"))
    second_join = WorkflowStep("second-join", lambda: calls.append("second"))

    workflow.add_step(source)
    workflow.add_step_with_dependencies(first_join, [source])
    workflow.add_step_with_dependencies(second_join, [source])

    assert not manager.execute_workflow(workflow.id)
    assert calls == ["source"]
    assert source.status == StepStatus.FAILED
    assert first_join.status == StepStatus.SKIPPED
    assert second_join.status == StepStatus.SKIPPED
    skipped = [
        record for record in workflow.audit_records
        if record["decision"] == "reject_failed_dependency"
    ]
    assert [record["step_id"] for record in skipped] == [
        first_join.id,
        second_join.id,
    ]


def test_running_dependency_defers_join_and_preserves_pending_state():
    manager = WorkflowManager()
    workflow = manager.create_workflow("fan-in-defer")

    dependency = WorkflowStep("dependency", lambda: "already-running")
    dependency.status = StepStatus.RUNNING
    join = WorkflowStep("join", lambda: "should-not-run")

    workflow.add_step(dependency)
    workflow.add_step_with_dependencies(join, [dependency])

    assert not manager.execute_workflow(workflow.id)
    assert dependency.status == StepStatus.RUNNING
    assert join.status == StepStatus.PENDING
    assert workflow.status == StepStatus.RUNNING
    assert workflow.audit_records[-1]["decision"] == (
        "defer_dependency_not_ready"
    )
    assert workflow.audit_records[-1]["waiting_dependencies"] == [
        dependency.id
    ]


def test_register_workflow_rejects_missing_dependency_before_execution():
    manager = WorkflowManager()
    workflow = Workflow("missing-dependency")
    join = WorkflowStep("join", lambda: "should-not-run")
    join.dependencies = ["missing-step"]
    workflow.add_step(join)

    assert not manager.register_workflow(workflow)
    assert workflow.id not in {item.id for item in manager.list_workflows()}
    assert workflow.audit_records == [
        {
            "workflow_id": workflow.id,
            "decision": "reject_invalid_dependency_graph",
            "error_count": 1,
        }
    ]


def test_register_workflow_rejects_dependency_cycle():
    manager = WorkflowManager()
    workflow = Workflow("cycle")
    first = WorkflowStep("first", lambda: "first")
    second = WorkflowStep("second", lambda: "second")
    workflow.add_step(first)
    workflow.add_step(second)
    first.dependencies = [second.id]
    second.dependencies = [first.id]

    assert not manager.register_workflow(workflow)
    assert workflow.audit_records[-1]["decision"] == (
        "reject_invalid_dependency_graph"
    )
    assert workflow.audit_records[-1]["error_count"] == 1


def test_terminal_dependency_status_is_not_revived():
    manager = WorkflowManager()
    workflow = manager.create_workflow("terminal-state")
    failed = WorkflowStep("failed", lambda: "must-not-run")
    failed.status = StepStatus.FAILED
    failed.error = "existing failure"
    join = WorkflowStep("join", lambda: "must-not-run")

    workflow.add_step(failed)
    workflow.add_step_with_dependencies(join, [failed])

    assert not manager.execute_workflow(workflow.id)
    assert failed.status == StepStatus.FAILED
    assert failed.error == "existing failure"
    assert join.status == StepStatus.SKIPPED
    assert workflow.status == StepStatus.FAILED
