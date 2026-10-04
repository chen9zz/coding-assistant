"""Fail-closed task lifecycle rules."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from repopilot.domain.models import TaskState

_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = MappingProxyType(
    {
        TaskState.CREATED: frozenset(
            {TaskState.INDEXING, TaskState.FAILED, TaskState.CANCELLED}
        ),
        TaskState.INDEXING: frozenset(
            {TaskState.PLANNING, TaskState.RETRYING, TaskState.FAILED, TaskState.CANCELLED}
        ),
        TaskState.PLANNING: frozenset(
            {
                TaskState.EXECUTING,
                TaskState.RETRYING,
                TaskState.WAITING_APPROVAL,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }
        ),
        TaskState.EXECUTING: frozenset(
            {
                TaskState.TESTING,
                TaskState.RETRYING,
                TaskState.WAITING_APPROVAL,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }
        ),
        TaskState.TESTING: frozenset(
            {TaskState.REVIEWING, TaskState.RETRYING, TaskState.FAILED, TaskState.CANCELLED}
        ),
        TaskState.REVIEWING: frozenset(
            {
                TaskState.SUCCEEDED,
                TaskState.EXECUTING,
                TaskState.RETRYING,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }
        ),
        TaskState.RETRYING: frozenset(
            {
                TaskState.INDEXING,
                TaskState.PLANNING,
                TaskState.EXECUTING,
                TaskState.TESTING,
                TaskState.REVIEWING,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }
        ),
        TaskState.WAITING_APPROVAL: frozenset(
            {TaskState.EXECUTING, TaskState.FAILED, TaskState.CANCELLED}
        ),
        TaskState.SUCCEEDED: frozenset(),
        TaskState.FAILED: frozenset(),
        TaskState.CANCELLED: frozenset(),
    }
)


def transition_allowed(current: TaskState, target: TaskState) -> bool:
    """Return whether a lifecycle move is explicitly allowed."""
    return target in _TRANSITIONS[current]


def legal_targets(current: TaskState) -> frozenset[TaskState]:
    """Expose an immutable target set for diagnostics and API clients."""
    return _TRANSITIONS[current]
