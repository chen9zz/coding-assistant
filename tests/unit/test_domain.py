from pathlib import Path

import pytest
from pydantic import ValidationError

from repopilot.domain.models import TaskSpec, TaskState, ToolRequest
from repopilot.domain.state_machine import transition_allowed


def test_task_spec_rejects_a_shell_command_string() -> None:
    """Turning argv into a shell string would reopen command-injection risk."""
    with pytest.raises(ValidationError):
        TaskSpec(
            repository=Path("/tmp/repo"),
            issue="Fix division by zero",
            test_command="pytest",  # type: ignore[arg-type]
        )


def test_state_machine_rejects_created_to_testing_transition() -> None:
    """Skipping indexing and planning would make task evidence untrustworthy."""
    assert transition_allowed(TaskState.CREATED, TaskState.TESTING) is False


def test_state_machine_allows_created_to_indexing_transition() -> None:
    """Removing the first legal transition would prevent every task from starting."""
    assert transition_allowed(TaskState.CREATED, TaskState.INDEXING) is True


def test_tool_arguments_digest_uses_canonical_json() -> None:
    """A non-canonical digest would break idempotency across JSON key orderings."""
    request = ToolRequest(
        operation_id="op-1",
        name="read_file",
        arguments={"path": "a.py"},
    )
    assert request.arguments_digest == (
        "87b2ac5b5ac97b3f01fbc4196d05ac56a5d4f76a7ee77d28aca26f54310ebe6e"
    )


def test_tool_arguments_digest_is_independent_of_mapping_order() -> None:
    """Reordering equivalent model arguments must not execute the tool twice."""
    left = ToolRequest(
        operation_id="op-1",
        name="search",
        arguments={"query": "needle", "path": "src"},
    )
    right = ToolRequest(
        operation_id="op-1",
        name="search",
        arguments={"path": "src", "query": "needle"},
    )
    assert left.arguments_digest == right.arguments_digest


@pytest.mark.parametrize("pattern", ["/etc/**", "../outside/**", "src/../../outside"])
def test_task_spec_rejects_unsafe_allowed_path_patterns(pattern: str) -> None:
    """Structured scope patterns must remain relative to the isolated worktree."""
    with pytest.raises(ValidationError):
        TaskSpec(
            repository=Path("/tmp/repo"),
            issue="Fix the scoped file",
            test_command=("pytest",),
            allowed_paths=(pattern,),
        )
