"""Minimal bounded agent loop: decide, execute safely, return paired evidence."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from repopilot.agent.actions import (
    ActionKind,
    AgentAction,
    AgentContext,
    ContextBudgetExceeded,
    FinalAnswer,
    ToolExchange,
)
from repopilot.domain.models import ToolRequest, ToolResult, ToolStatus


class Runtime(Protocol):
    def next_action(self, context: AgentContext) -> AgentAction: ...


class ToolGateway(Protocol):
    def execute(
        self,
        task_id: str,
        worktree: Path,
        request: ToolRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult: ...


class AgentLoopError(RuntimeError):
    """Base class for bounded-loop termination without a final answer."""


class StepBudgetExceeded(AgentLoopError):
    pass


class TimeBudgetExceeded(AgentLoopError):
    pass


class RepeatedToolCall(AgentLoopError):
    pass


class AgentCancelled(AgentLoopError):
    pass


class MinimalAgentLoop:
    """Give a runtime iterative tool use while the host retains every boundary."""

    def __init__(
        self,
        *,
        max_steps: int,
        max_context_chars: int = 24_000,
        max_repeated_calls: int = 2,
        max_elapsed_seconds: float = 300,
    ) -> None:
        if min(max_steps, max_context_chars, max_repeated_calls) <= 0:
            raise ValueError("loop budgets must be positive")
        self.max_steps = max_steps
        self.max_context_chars = max_context_chars
        self.max_repeated_calls = max_repeated_calls
        self.max_elapsed_seconds = max_elapsed_seconds

    def run(
        self,
        task_id: str,
        runtime: Runtime,
        gateway: ToolGateway,
        worktree: Path,
        context: AgentContext,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> FinalAnswer:
        started = time.monotonic()

        def remaining_time() -> float:
            remaining = self.max_elapsed_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeBudgetExceeded("agent loop exceeded its elapsed-time budget")
            return remaining

        fingerprints: dict[tuple[str, str], int] = {}
        for exchange in context.exchanges:
            fingerprint = (exchange.request.name, exchange.request.arguments_digest)
            fingerprints[fingerprint] = fingerprints.get(fingerprint, 0) + 1
        for step in range(1, self.max_steps + 1):
            if cancelled and cancelled():
                raise AgentCancelled("task was cancelled before the next model action")
            remaining_time()
            context = context.compact(self.max_context_chars)
            action = runtime.next_action(context)
            remaining = remaining_time()
            if action.kind is ActionKind.FINAL:
                assert action.final_text is not None
                return FinalAnswer(text=action.final_text, steps=step, context=context)
            request = action.to_tool_request()
            fingerprint = (request.name, request.arguments_digest)
            repetitions = fingerprints.get(fingerprint, 0) + 1
            fingerprints[fingerprint] = repetitions
            if repetitions > self.max_repeated_calls:
                raise RepeatedToolCall(
                    f"equivalent tool request repeated {repetitions} times: {request.name}"
                )
            result = gateway.execute(
                task_id,
                worktree,
                request,
                timeout_seconds=remaining,
            )
            remaining_time()
            context = context.with_exchange(ToolExchange(request=request, result=result))
            if result.status is ToolStatus.APPROVAL_REQUIRED:
                return FinalAnswer(text="approval required", steps=step, context=context)
        raise StepBudgetExceeded(f"agent did not finish within {self.max_steps} steps")


__all__ = [
    "AgentCancelled",
    "ContextBudgetExceeded",
    "MinimalAgentLoop",
    "RepeatedToolCall",
    "StepBudgetExceeded",
    "TimeBudgetExceeded",
]
