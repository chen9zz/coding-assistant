"""Protocols that keep the application core independent of adapters."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from repopilot.domain.models import (
    ArtifactBundle,
    PolicyDecision,
    TaskEvent,
    TaskSnapshot,
    TaskSpec,
    ToolRequest,
    ToolResult,
)


class EventStore(Protocol):
    def create_task(self, task_id: str, spec: TaskSpec) -> TaskSnapshot: ...

    def get_task(self, task_id: str) -> TaskSnapshot: ...

    def list_tasks(self) -> list[TaskSnapshot]: ...

    def append(self, event: TaskEvent, expected_sequence: int) -> TaskSnapshot: ...

    def events(self, task_id: str, after: int = 0) -> list[TaskEvent]: ...

    def get_operation(self, task_id: str, operation_id: str) -> ToolResult | None: ...

    def operation_history(self, task_id: str) -> list[tuple[ToolRequest, ToolResult]]: ...

    def save_pending_approval(
        self,
        task_id: str,
        request: ToolRequest,
        event: TaskEvent,
        expected_sequence: int,
    ) -> TaskSnapshot: ...

    def get_pending_approval(
        self, task_id: str, operation_id: str
    ) -> ToolRequest | None: ...

    def claim_operation(
        self,
        task_id: str,
        request: ToolRequest,
        event: TaskEvent,
        expected_sequence: int,
    ) -> bool: ...

    def commit_operation(
        self,
        task_id: str,
        request: ToolRequest,
        result: ToolResult,
        event: TaskEvent,
        expected_sequence: int,
    ) -> TaskSnapshot: ...


class ToolPolicy(Protocol):
    def decide(self, request: ToolRequest) -> PolicyDecision: ...


class ToolExecutor(Protocol):
    name: str

    def execute(self, worktree: Path, arguments: dict[str, object]) -> ToolResult: ...


class AgentRuntime(Protocol):
    def next_action(self, context: object) -> object: ...


class Telemetry(Protocol):
    def emit(self, event_type: str, payload: dict[str, object]) -> None: ...


class ContentRedactor(Protocol):
    def redact(self, value: Any) -> Any: ...


class ArtifactWriter(Protocol):
    def write(self, task: TaskSnapshot, events: Sequence[TaskEvent]) -> ArtifactBundle: ...
