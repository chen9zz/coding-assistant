"""Deterministic offline runtime for orchestration, safety, and recovery tests."""

from __future__ import annotations

from collections.abc import Iterable

from repopilot.agent.actions import AgentAction, AgentContext


class ScriptExhausted(RuntimeError):
    """Raised when an offline scenario has no structured terminal action."""


class ScriptedRuntime:
    """Return a fixed sequence while recording the exact contexts received."""

    def __init__(self, actions: Iterable[AgentAction]) -> None:
        self._actions = tuple(actions)
        self._index = 0
        self.contexts: list[AgentContext] = []

    def next_action(self, context: AgentContext) -> AgentAction:
        self.contexts.append(context)
        if self._index >= len(self._actions):
            raise ScriptExhausted("script ended without a final action")
        action = self._actions[self._index]
        self._index += 1
        return action

