"""Single audited gateway for policy, tools, redaction, and idempotency."""

from __future__ import annotations

import time
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import TypeAdapter

from repopilot.domain.models import (
    JsonValue,
    PolicyEffect,
    TaskEvent,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from repopilot.domain.ports import EventStore, ToolPolicy
from repopilot.tools.builtin import ToolNotFoundError, ToolRegistry
from repopilot.tools.redaction import Redactor

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class OperationInProgress(RuntimeError):
    """Raised when a durable intent has no committed outcome to replay."""


class SafeToolGateway:
    """Persist exactly one redacted result for every operation identity."""

    def __init__(
        self,
        *,
        store: EventStore,
        registry: ToolRegistry,
        policy: ToolPolicy,
        redactor: Redactor,
    ) -> None:
        self.store = store
        self.registry = registry
        self.policy = policy
        self.redactor = redactor

    def execute(
        self,
        task_id: str,
        worktree: Path,
        request: ToolRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        existing = self.store.get_operation(task_id, request.operation_id)
        if existing is not None:
            return self._replay(task_id, request, existing)

        # A changed filesystem must not turn an unfinished intent into a policy
        # denial. Reconcile uncertain effects before even evaluating a retry.
        if any(
            event.event_type == "tool.started" and event.operation_id == request.operation_id
            for event in self.store.events(task_id)
        ):
            raise OperationInProgress("persisted intent has no result; outcome is indeterminate")

        started = time.monotonic()
        decision = self.policy.decide(request)
        if decision.effect is PolicyEffect.DENY:
            result = ToolResult(
                operation_id=request.operation_id,
                tool_name=request.name,
                status=ToolStatus.DENIED,
                error=decision.reason,
                duration_ms=self._elapsed(started),
            )
            return self._commit_result(task_id, request, result, "tool.denied")
        if decision.effect is PolicyEffect.ASK and not self._approved(task_id, request):
            result = ToolResult(
                operation_id=request.operation_id,
                tool_name=request.name,
                status=ToolStatus.APPROVAL_REQUIRED,
                error=decision.reason,
                duration_ms=self._elapsed(started),
            )
            redacted = self._redact_result(result)
            snapshot = self.store.get_task(task_id)
            event = self._event(
                task_id,
                snapshot,
                request,
                redacted,
                "tool.approval_requested",
            )
            self.store.save_pending_approval(
                task_id,
                request,
                event,
                expected_sequence=snapshot.sequence,
            )
            return redacted

        snapshot = self.store.get_task(task_id)
        intent = self._intent_event(task_id, snapshot, request)
        claimed = self.store.claim_operation(
            task_id,
            request,
            intent,
            expected_sequence=snapshot.sequence,
        )
        if not claimed:
            committed = self.store.get_operation(task_id, request.operation_id)
            if committed is not None:
                return self._replay(task_id, request, committed)
            raise OperationInProgress(
                "operation has a persisted intent but no result; outcome is indeterminate"
            )
        try:
            result = self.registry.get(request.name).execute(
                worktree, request, timeout_seconds=timeout_seconds
            )
        except OperationInProgress:
            raise
        except (OSError, RuntimeError, ToolNotFoundError, ValueError) as error:
            result = ToolResult(
                operation_id=request.operation_id,
                tool_name=request.name,
                status=ToolStatus.FAILED,
                error=str(error),
                duration_ms=self._elapsed(started),
            )
        event_type = "tool.completed" if result.status is ToolStatus.SUCCEEDED else "tool.failed"
        return self._commit_result(task_id, request, result, event_type)

    def _commit_result(
        self,
        task_id: str,
        request: ToolRequest,
        result: ToolResult,
        event_type: str,
    ) -> ToolResult:
        redacted = self._redact_result(result)
        snapshot = self.store.get_task(task_id)
        event = self._event(task_id, snapshot, request, redacted, event_type)
        self.store.commit_operation(
            task_id,
            request,
            redacted,
            event,
            expected_sequence=snapshot.sequence,
        )
        return redacted

    def _replay(
        self,
        task_id: str,
        request: ToolRequest,
        existing: ToolResult,
    ) -> ToolResult:
        snapshot = self.store.get_task(task_id)
        replay_event = self._event(task_id, snapshot, request, existing, "tool.replayed")
        self.store.commit_operation(
            task_id,
            request,
            existing,
            replay_event,
            expected_sequence=snapshot.sequence,
        )
        return existing

    def _approved(self, task_id: str, request: ToolRequest) -> bool:
        for event in reversed(self.store.events(task_id)):
            if event.event_type != "task.approved":
                continue
            if (
                event.payload.get("operation_id") == request.operation_id
                and event.payload.get("arguments_digest") == request.arguments_digest
            ):
                return True
        return False

    def _redact_result(self, result: ToolResult) -> ToolResult:
        output = _JSON.validate_python(self.redactor.redact(result.output))
        error = self.redactor.redact(result.error) if result.error else None
        return result.model_copy(update={"output": output, "error": cast(str | None, error)})

    def _event(
        self,
        task_id: str,
        snapshot: object,
        request: ToolRequest,
        result: ToolResult,
        event_type: str,
    ) -> TaskEvent:
        from repopilot.domain.models import TaskSnapshot

        task = cast(TaskSnapshot, snapshot)
        arguments = _JSON.validate_python(self.redactor.redact(request.arguments))
        assert isinstance(arguments, dict)
        return TaskEvent(
            event_id=str(uuid4()),
            task_id=task_id,
            sequence=task.sequence + 1,
            event_type=event_type,
            from_state=task.state,
            to_state=task.state,
            operation_id=request.operation_id,
            payload={
                "operation_id": request.operation_id,
                "tool_name": request.name,
                "status": result.status.value,
                "arguments_digest": request.arguments_digest,
                "arguments": arguments,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
                **(
                    {"approval_preview": result.error}
                    if result.status is ToolStatus.APPROVAL_REQUIRED
                    else {}
                ),
            },
        )

    @staticmethod
    def _intent_event(task_id: str, snapshot: object, request: ToolRequest) -> TaskEvent:
        from repopilot.domain.models import TaskSnapshot

        task = cast(TaskSnapshot, snapshot)
        return TaskEvent(
            event_id=str(uuid4()),
            task_id=task_id,
            sequence=task.sequence + 1,
            event_type="tool.started",
            from_state=task.state,
            to_state=task.state,
            operation_id=request.operation_id,
            payload={
                "operation_id": request.operation_id,
                "tool_name": request.name,
                "arguments_digest": request.arguments_digest,
            },
        )

    @staticmethod
    def _elapsed(started: float) -> int:
        return max(0, round((time.monotonic() - started) * 1000))
