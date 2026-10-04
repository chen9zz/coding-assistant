"""Durable deterministic lifecycle around a bounded agent runtime."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import TypeAdapter

from repopilot.agent.actions import AgentContext, ToolExchange
from repopilot.agent.loop import MinimalAgentLoop, Runtime, TimeBudgetExceeded, ToolGateway
from repopilot.domain.models import (
    AgentPlan,
    JsonValue,
    PlanStep,
    TaskEvent,
    TaskSnapshot,
    TaskSpec,
    TaskState,
    ToolRequest,
    ToolResult,
    ToolStatus,
    utc_now,
)
from repopilot.domain.ports import ContentRedactor, EventStore
from repopilot.domain.state_machine import transition_allowed
from repopilot.infrastructure.git import GitWorktreeManager
from repopilot.infrastructure.sqlite import IdempotencyConflict
from repopilot.tools.gateway import OperationInProgress, SafeToolGateway
from repopilot.tools.policy import change_scope_violation
from repopilot.tools.repository import RepositoryMapper

GatewayFactory = Callable[[Path, TaskSpec], SafeToolGateway]
_JSON_MAPPING: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


class InjectedCrash(RuntimeError):
    """Test-only simulation of process loss after a committed operation."""


@dataclass(slots=True)
class CrashAfterOperation:
    operation_id: str
    triggered: bool = False

    def after_operation(self, operation_id: str) -> None:
        if not self.triggered and operation_id == self.operation_id:
            self.triggered = True
            raise InjectedCrash(f"injected crash after committed operation {operation_id}")


class FaultingGateway:
    """Inject a crash only after the real gateway has committed its result."""

    def __init__(self, delegate: SafeToolGateway, fault: CrashAfterOperation) -> None:
        self.delegate = delegate
        self.fault = fault

    def execute(
        self,
        task_id: str,
        worktree: Path,
        request: ToolRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        result = self.delegate.execute(
            task_id, worktree, request, timeout_seconds=timeout_seconds
        )
        self.fault.after_operation(request.operation_id)
        return result


class DurableTaskOrchestrator:
    """Advance task stages from persisted state and deterministic evidence."""

    _TERMINAL = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})

    def __init__(
        self,
        *,
        store: EventStore,
        worktrees: GitWorktreeManager,
        mapper: RepositoryMapper,
        gateway_factory: GatewayFactory,
        redactor: ContentRedactor,
    ) -> None:
        self.store = store
        self.worktrees = worktrees
        self.mapper = mapper
        self.gateway_factory = gateway_factory
        self.redactor = redactor

    def run(
        self,
        task_id: str,
        runtime: Runtime,
        *,
        fault: CrashAfterOperation | None = None,
    ) -> TaskSnapshot:
        snapshot = self.store.get_task(task_id)
        if snapshot.state in self._TERMINAL:
            return snapshot
        try:
            snapshot = self._prepare(snapshot)
            if snapshot.state is TaskState.EXECUTING:
                snapshot = self._execute(snapshot, runtime, fault)
            if snapshot.state is TaskState.WAITING_APPROVAL:
                return snapshot
            if snapshot.state is TaskState.TESTING:
                snapshot = self._verify(snapshot)
            if snapshot.state is TaskState.REVIEWING:
                snapshot = self._review(snapshot)
            return snapshot
        except InjectedCrash:
            raise
        except (IdempotencyConflict, OperationInProgress):
            raise
        except Exception as error:
            current = self.store.get_task(task_id)
            if current.state not in self._TERMINAL and transition_allowed(
                current.state, TaskState.FAILED
            ):
                return self._transition(
                    current,
                    TaskState.FAILED,
                    "task.failed",
                    {"error_type": type(error).__name__, "error": str(error)[:1000]},
                )
            raise

    def _prepare(self, snapshot: TaskSnapshot) -> TaskSnapshot:
        if snapshot.state is TaskState.CREATED:
            self._remaining_seconds(snapshot)
            worktree = self.worktrees.create(snapshot.spec.repository, snapshot.task_id)
            self._remaining_seconds(snapshot)
            snapshot = self._transition(
                snapshot,
                TaskState.INDEXING,
                "task.indexing",
                {"worktree": str(worktree.path)},
            )
        if snapshot.state is TaskState.INDEXING:
            self._remaining_seconds(snapshot)
            assert snapshot.worktree is not None
            repo_map = self.mapper.build(snapshot.worktree, snapshot.spec.issue)
            self._remaining_seconds(snapshot)
            serialized = repo_map.model_dump_json()
            snapshot = self._transition(
                snapshot,
                TaskState.PLANNING,
                "task.planning",
                {
                    "repo_map_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
                    "file_count": len(repo_map.files),
                    "symbol_count": len(repo_map.symbols),
                },
            )
        if snapshot.state is TaskState.PLANNING:
            self._remaining_seconds(snapshot)
            plan = AgentPlan(
                summary=f"Resolve issue: {snapshot.spec.issue}",
                steps=(
                    PlanStep(
                        step_id="implement",
                        objective=snapshot.spec.issue,
                        verification=snapshot.spec.test_command,
                    ),
                ),
                risks=(
                    "Model output is untrusted until deterministic tests pass.",
                    *snapshot.spec.constraints,
                ),
            )
            snapshot = self._transition(
                snapshot,
                TaskState.EXECUTING,
                "task.executing",
                {"plan": self._json_mapping(plan.model_dump(mode="json"))},
            )
        return snapshot

    def _execute(
        self,
        snapshot: TaskSnapshot,
        runtime: Runtime,
        fault: CrashAfterOperation | None,
    ) -> TaskSnapshot:
        assert snapshot.worktree is not None
        concrete_gateway = self.gateway_factory(snapshot.worktree, snapshot.spec)
        gateway: ToolGateway = (
            FaultingGateway(concrete_gateway, fault) if fault is not None else concrete_gateway
        )
        pending = self._approved_pending_request(snapshot.task_id)
        if pending is not None:
            gateway.execute(
                snapshot.task_id,
                snapshot.worktree,
                pending,
                timeout_seconds=self._remaining_seconds(snapshot),
            )
        history = tuple(
            ToolExchange(request=request, result=result)
            for request, result in self.store.operation_history(snapshot.task_id)
        )
        remaining_steps = snapshot.spec.max_steps - len(history)
        if remaining_steps <= 0:
            raise RuntimeError("persisted tool history exhausted the task step budget")
        answer = MinimalAgentLoop(
            max_steps=remaining_steps,
            max_elapsed_seconds=self._remaining_seconds(snapshot),
        ).run(
            snapshot.task_id,
            runtime,
            gateway,
            snapshot.worktree,
            AgentContext.empty(
                issue=snapshot.spec.issue,
                constraints=snapshot.spec.constraints,
            ).model_copy(update={"exchanges": history}),
        )
        approval = next(
            (
                exchange
                for exchange in answer.context.exchanges
                if exchange.result.status is ToolStatus.APPROVAL_REQUIRED
            ),
            None,
        )
        current = self.store.get_task(snapshot.task_id)
        if approval is not None:
            return self._transition(
                current,
                TaskState.WAITING_APPROVAL,
                "task.approval_requested",
                {
                    "operation_id": approval.request.operation_id,
                    "arguments_digest": approval.request.arguments_digest,
                },
            )
        return self._transition(
            current,
            TaskState.TESTING,
            "task.testing",
            {"final_text": answer.text, "agent_steps": answer.steps},
        )

    def _approved_pending_request(self, task_id: str) -> ToolRequest | None:
        events = self.store.events(task_id)
        approved = next(
            (event for event in reversed(events) if event.event_type == "task.approved"),
            None,
        )
        if approved is None:
            return None
        operation_id = approved.payload.get("operation_id")
        arguments_digest = approved.payload.get("arguments_digest")
        if not isinstance(operation_id, str) or not isinstance(arguments_digest, str):
            raise RuntimeError("persisted approval identity is incomplete")
        if self.store.get_operation(task_id, operation_id) is not None:
            return None
        request = self.store.get_pending_approval(task_id, operation_id)
        if request is None:
            raise RuntimeError("approved operation has no matching persisted request")
        if request.arguments_digest != arguments_digest:
            raise RuntimeError("persisted approval digest does not match the approved request")
        return request

    def _verify(self, snapshot: TaskSnapshot) -> TaskSnapshot:
        assert snapshot.worktree is not None
        gateway = self.gateway_factory(snapshot.worktree, snapshot.spec)
        request = ToolRequest(
            operation_id=f"verify-{snapshot.task_id}",
            name="run_command",
            arguments={"argv": list(snapshot.spec.test_command)},
        )
        result = gateway.execute(
            snapshot.task_id,
            snapshot.worktree,
            request,
            timeout_seconds=self._remaining_seconds(snapshot),
        )
        self._remaining_seconds(snapshot)
        current = self.store.get_task(snapshot.task_id)
        if result.status is not ToolStatus.SUCCEEDED:
            return self._transition(
                current,
                TaskState.FAILED,
                "task.verification_failed",
                {"operation_id": request.operation_id, "status": result.status.value},
            )
        return self._transition(
            current,
            TaskState.REVIEWING,
            "task.reviewing",
            {"verification_operation_id": request.operation_id},
        )

    def _review(self, snapshot: TaskSnapshot) -> TaskSnapshot:
        assert snapshot.worktree is not None
        gateway = self.gateway_factory(snapshot.worktree, snapshot.spec)
        request = ToolRequest(
            operation_id=f"diff-{snapshot.task_id}",
            name="git_diff",
            arguments={},
        )
        result = gateway.execute(
            snapshot.task_id,
            snapshot.worktree,
            request,
            timeout_seconds=self._remaining_seconds(snapshot),
        )
        self._remaining_seconds(snapshot)
        current = self.store.get_task(snapshot.task_id)
        if result.status is not ToolStatus.SUCCEEDED:
            return self._transition(
                current,
                TaskState.FAILED,
                "task.review_failed",
                {"operation_id": request.operation_id},
            )
        output = result.output
        diff = output.get("diff") if isinstance(output, dict) else None
        if not isinstance(diff, str) or not diff.strip():
            return self._transition(
                current,
                TaskState.FAILED,
                "task.review_failed",
                {"operation_id": request.operation_id, "reason": "empty_diff"},
            )
        assert isinstance(output, dict)
        raw_paths = output.get("changed_paths")
        if not isinstance(raw_paths, list) or not all(
            isinstance(path, str) for path in raw_paths
        ):
            return self._transition(
                current,
                TaskState.FAILED,
                "task.review_failed",
                {"operation_id": request.operation_id, "reason": "missing_changed_paths"},
            )
        violation = change_scope_violation(
            tuple(cast(str, path) for path in raw_paths),
            allowed_paths=snapshot.spec.allowed_paths,
            max_changed_files=snapshot.spec.max_changed_files,
        )
        if violation is not None:
            return self._transition(
                current,
                TaskState.FAILED,
                "task.review_failed",
                {"operation_id": request.operation_id, "reason": violation},
            )
        serialized = result.model_dump_json()
        return self._transition(
            current,
            TaskState.SUCCEEDED,
            "task.succeeded",
            {
                "diff_operation_id": request.operation_id,
                "review_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
            },
        )

    def _transition(
        self,
        snapshot: TaskSnapshot,
        target: TaskState,
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> TaskSnapshot:
        sanitized_payload = self._json_mapping(self.redactor.redact(payload))
        event = TaskEvent(
            event_id=str(uuid4()),
            task_id=snapshot.task_id,
            sequence=snapshot.sequence + 1,
            event_type=event_type,
            from_state=snapshot.state,
            to_state=target,
            payload=sanitized_payload,
        )
        return self.store.append(event, expected_sequence=snapshot.sequence)

    @staticmethod
    def _remaining_seconds(snapshot: TaskSnapshot) -> float:
        elapsed = (utc_now() - snapshot.created_at).total_seconds()
        remaining = snapshot.spec.timeout_seconds - elapsed
        if remaining <= 0:
            raise TimeBudgetExceeded("task exceeded its persistent wall-clock deadline")
        return remaining

    @staticmethod
    def _json_mapping(value: object) -> dict[str, JsonValue]:
        return _JSON_MAPPING.validate_python(value)
