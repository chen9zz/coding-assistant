"""Public task use cases shared by CLI, API, and evaluation."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from repopilot.agent.loop import Runtime
from repopilot.application.orchestrator import CrashAfterOperation, DurableTaskOrchestrator
from repopilot.domain.models import ArtifactBundle, TaskEvent, TaskSnapshot, TaskSpec, TaskState
from repopilot.domain.ports import ArtifactWriter, ContentRedactor, EventStore


class ApprovalRequestError(RuntimeError):
    """Raised when an approval does not name the exact pending operation."""


class TaskApplicationService:
    def __init__(
        self,
        store: EventStore,
        orchestrator: DurableTaskOrchestrator,
        task_id_factory: Callable[[], str] | None = None,
        artifact_writer: ArtifactWriter | None = None,
        *,
        redactor: ContentRedactor,
    ) -> None:
        self.store = store
        self.orchestrator = orchestrator
        self.task_id_factory = task_id_factory or (lambda: str(uuid4()))
        self.artifact_writer = artifact_writer
        self.redactor = redactor

    def create(self, spec: TaskSpec) -> TaskSnapshot:
        redacted = TaskSpec.model_validate(self.redactor.redact(spec.model_dump(mode="python")))
        return self.store.create_task(self.task_id_factory(), redacted)

    def get(self, task_id: str) -> TaskSnapshot:
        return self.store.get_task(task_id)

    def list_tasks(self) -> list[TaskSnapshot]:
        return self.store.list_tasks()

    def events(self, task_id: str, after: int = 0) -> list[TaskEvent]:
        return self.store.events(task_id, after)

    def artifacts(self, task_id: str) -> ArtifactBundle:
        if self.artifact_writer is None:
            raise RuntimeError("artifact writer is not configured")
        task = self.store.get_task(task_id)
        return self.artifact_writer.write(task, self.store.events(task_id))

    def run(
        self,
        task_id: str,
        runtime: Runtime,
        *,
        fault: CrashAfterOperation | None = None,
    ) -> TaskSnapshot:
        return self.orchestrator.run(task_id, runtime, fault=fault)

    def resume(self, task_id: str, runtime: Runtime) -> TaskSnapshot:
        return self.orchestrator.run(task_id, runtime)

    def approve(self, task_id: str, request_id: str) -> TaskSnapshot:
        snapshot = self.store.get_task(task_id)
        if snapshot.state is not TaskState.WAITING_APPROVAL:
            if any(
                event.event_type == "task.approved"
                and event.payload.get("request_id") == request_id
                for event in self.store.events(task_id)
            ):
                return snapshot
            raise ApprovalRequestError("task has no matching pending approval request")
        pending = next(
            (
                event
                for event in reversed(self.store.events(task_id))
                if event.event_type == "task.approval_requested"
            ),
            None,
        )
        if pending is None:
            raise ApprovalRequestError("task has no persisted approval request")
        operation_id = pending.payload.get("operation_id")
        arguments_digest = pending.payload.get("arguments_digest")
        if request_id != operation_id:
            raise ApprovalRequestError("request ID does not match the pending operation")
        if not isinstance(operation_id, str) or not isinstance(arguments_digest, str):
            raise ApprovalRequestError("persisted approval request is incomplete")
        return self.orchestrator._transition(
            snapshot,
            TaskState.EXECUTING,
            "task.approved",
            {
                "request_id": request_id,
                "operation_id": operation_id,
                "arguments_digest": arguments_digest,
            },
        )
