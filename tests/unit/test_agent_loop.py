import time
from collections.abc import Callable
from pathlib import Path

import pytest

from repopilot.agent.actions import AgentAction, AgentContext, ToolExchange
from repopilot.agent.loop import (
    AgentCancelled,
    ContextBudgetExceeded,
    MinimalAgentLoop,
    RepeatedToolCall,
    StepBudgetExceeded,
    TimeBudgetExceeded,
)
from repopilot.agent.scripted import ScriptedRuntime
from repopilot.domain.models import ToolRequest, ToolResult, ToolStatus


class RecordingGateway:
    def __init__(self, output_factory: Callable[[ToolRequest], str] | None = None) -> None:
        self.requests: list[ToolRequest] = []
        self.output_factory = output_factory or (lambda request: f"read {request.name}")

    def execute(
        self,
        task_id: str,
        worktree: Path,
        request: ToolRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.requests.append(request)
        return ToolResult(
            operation_id=request.operation_id,
            tool_name=request.name,
            status=ToolStatus.SUCCEEDED,
            output={"content": self.output_factory(request)},
        )


def test_loop_passes_remaining_budget_to_network_runtime(tmp_path: Path) -> None:
    class DeadlineRuntime:
        def next_action(self, context: AgentContext) -> AgentAction:
            raise AssertionError("unbounded model call")

        def next_action_with_timeout(
            self, context: AgentContext, timeout_seconds: float
        ) -> AgentAction:
            assert 0 < timeout_seconds <= 1
            return AgentAction.final("bounded")

    answer = MinimalAgentLoop(max_steps=1, max_elapsed_seconds=1).run(
        "deadline", DeadlineRuntime(), RecordingGateway(), tmp_path, AgentContext.empty()
    )
    assert answer.text == "bounded"


def test_loop_pairs_tool_result_and_stops_on_structured_final(tmp_path: Path) -> None:
    """Dropping the paired result would make the model unaware of its own side effect."""
    runtime = ScriptedRuntime(
        [
            AgentAction.tool("read_file", {"path": "app.py"}, operation_id="op-1"),
            AgentAction.final("patch verified"),
        ]
    )
    gateway = RecordingGateway()

    answer = MinimalAgentLoop(max_steps=4).run(
        "task-1", runtime, gateway, tmp_path, AgentContext.empty(issue="fix app")
    )

    assert answer.text == "patch verified"
    assert answer.steps == 2
    exchange = runtime.contexts[1].exchanges[0]
    assert exchange.request.operation_id == "op-1"
    assert exchange.result.operation_id == "op-1"


def test_runtime_context_preserves_operator_constraints(tmp_path: Path) -> None:
    """Accepting constraints at the API but dropping them before the model is misleading."""
    runtime = ScriptedRuntime([AgentAction.final("bounded answer")])
    context = AgentContext.empty(
        issue="fix app",
        constraints=("Do not change the public API", "Only edit app.py"),
    )

    MinimalAgentLoop(max_steps=2).run(
        "task-1",
        runtime,
        RecordingGateway(),
        tmp_path,
        context,
    )

    assert runtime.contexts[0].constraints == context.constraints


def test_loop_raises_after_step_budget_without_a_final_action(tmp_path: Path) -> None:
    """An unbounded loop could spend indefinitely after repeated partial progress."""
    runtime = ScriptedRuntime(
        [
            AgentAction.tool("git_diff", {}, operation_id="op-1"),
            AgentAction.tool("git_diff", {}, operation_id="op-2"),
            AgentAction.final("unreachable"),
        ]
    )
    with pytest.raises(StepBudgetExceeded):
        MinimalAgentLoop(max_steps=2).run(
            "task-1", runtime, RecordingGateway(), tmp_path, AgentContext.empty()
        )


def test_loop_stops_repeated_equivalent_tool_requests(tmp_path: Path) -> None:
    """Changing only operation IDs must not evade the repeated-call circuit breaker."""
    runtime = ScriptedRuntime(
        [
            AgentAction.tool("read_file", {"path": "app.py"}, operation_id="op-1"),
            AgentAction.tool("read_file", {"path": "app.py"}, operation_id="op-2"),
            AgentAction.tool("read_file", {"path": "app.py"}, operation_id="op-3"),
        ]
    )
    with pytest.raises(RepeatedToolCall):
        MinimalAgentLoop(max_steps=5, max_repeated_calls=2).run(
            "task-1", runtime, RecordingGateway(), tmp_path, AgentContext.empty()
        )


def test_host_verification_can_rerun_after_each_successful_patch(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        [
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v1"
            ),
            AgentAction.tool("apply_patch", {"patch": "first"}, operation_id="p1"),
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v2"
            ),
            AgentAction.tool("apply_patch", {"patch": "second"}, operation_id="p2"),
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v3"
            ),
            AgentAction.final("verified"),
        ]
    )
    answer = MinimalAgentLoop(max_steps=6, verification_command=("python", "acceptance.py")).run(
        "repair", runtime, RecordingGateway(), tmp_path, AgentContext.empty()
    )
    assert answer.text == "verified"


@pytest.mark.parametrize("patch_status", [ToolStatus.FAILED, ToolStatus.DENIED])
def test_failed_patch_does_not_reset_verification_repeat_guard(
    tmp_path: Path, patch_status: ToolStatus
) -> None:
    class Gateway(RecordingGateway):
        def execute(self, task_id, worktree, request, *, timeout_seconds=None):
            result = super().execute(task_id, worktree, request, timeout_seconds=timeout_seconds)
            return (
                result.model_copy(update={"status": patch_status})
                if request.name == "apply_patch"
                else result
            )

    runtime = ScriptedRuntime(
        [
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v1"
            ),
            AgentAction.tool("apply_patch", {"patch": "bad"}, operation_id="p1"),
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v2"
            ),
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v3"
            ),
        ]
    )
    gateway = Gateway()
    with pytest.raises(RepeatedToolCall):
        MinimalAgentLoop(max_steps=5, verification_command=("python", "acceptance.py")).run(
            "repair", runtime, gateway, tmp_path, AgentContext.empty()
        )
    assert len(gateway.requests) == 3


def test_other_command_repeats_are_not_reset_by_patches(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        [
            AgentAction.tool("run_command", {"argv": ["npm", "test"]}, operation_id="v1"),
            AgentAction.tool("apply_patch", {"patch": "first"}, operation_id="p1"),
            AgentAction.tool("run_command", {"argv": ["npm", "test"]}, operation_id="v2"),
            AgentAction.tool("apply_patch", {"patch": "second"}, operation_id="p2"),
            AgentAction.tool("run_command", {"argv": ["npm", "test"]}, operation_id="v3"),
        ]
    )
    with pytest.raises(RepeatedToolCall):
        MinimalAgentLoop(max_steps=6, verification_command=("python", "acceptance.py")).run(
            "repair", runtime, RecordingGateway(), tmp_path, AgentContext.empty()
        )


def test_resume_reconstructs_verification_invalidation_from_persisted_results(
    tmp_path: Path,
) -> None:
    context = AgentContext.empty()
    for index, name in enumerate(["run_command", "run_command", "apply_patch"]):
        request = ToolRequest(
            operation_id=f"old-{index}",
            name=name,
            arguments={"argv": ["python", "acceptance.py"]}
            if name == "run_command"
            else {"patch": "successful edit"},
        )
        context = context.with_exchange(
            ToolExchange(
                request=request,
                result=ToolResult(
                    operation_id=request.operation_id, tool_name=name, status=ToolStatus.SUCCEEDED
                ),
            )
        )
    runtime = ScriptedRuntime(
        [
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="new"
            ),
            AgentAction.final("verified after resume"),
        ]
    )
    result = MinimalAgentLoop(max_steps=2, verification_command=("python", "acceptance.py")).run(
        "repair", runtime, RecordingGateway(), tmp_path, context
    )
    assert result.text == "verified after resume"


def test_verification_invalidation_does_not_extend_total_step_budget(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        [
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v1"
            ),
            AgentAction.tool("apply_patch", {"patch": "edit"}, operation_id="p1"),
            AgentAction.final("must not run"),
        ]
    )
    with pytest.raises(StepBudgetExceeded):
        MinimalAgentLoop(max_steps=2, verification_command=("python", "acceptance.py")).run(
            "repair", runtime, RecordingGateway(), tmp_path, AgentContext.empty()
        )
    assert len(runtime.contexts) == 2


def test_replaying_a_successful_patch_cannot_reset_verification_again(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        [
            AgentAction.tool("apply_patch", {"patch": "edit"}, operation_id="patch-one"),
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v1"
            ),
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v2"
            ),
            AgentAction.tool("apply_patch", {"patch": "edit"}, operation_id="patch-one"),
            AgentAction.tool(
                "run_command", {"argv": ["python", "acceptance.py"]}, operation_id="v3"
            ),
            AgentAction.final("must not succeed"),
        ]
    )
    gateway = RecordingGateway()
    with pytest.raises(RepeatedToolCall):
        MinimalAgentLoop(max_steps=6, verification_command=("python", "acceptance.py")).run(
            "repair", runtime, gateway, tmp_path, AgentContext.empty()
        )
    assert len(gateway.requests) == 4


def test_loop_honours_cancellation_before_next_model_action(tmp_path: Path) -> None:
    """Ignoring cancellation would continue code changes after a human stops the task."""
    runtime = ScriptedRuntime([AgentAction.final("must not run")])
    with pytest.raises(AgentCancelled):
        MinimalAgentLoop(max_steps=2).run(
            "task-1",
            runtime,
            RecordingGateway(),
            tmp_path,
            AgentContext.empty(),
            cancelled=lambda: True,
        )
    assert runtime.contexts == []


def test_runtime_returning_after_deadline_cannot_trigger_a_tool(tmp_path: Path) -> None:
    """A slow model response must be checked before its requested side effect executes."""

    class SlowRuntime:
        def next_action(self, context: AgentContext) -> AgentAction:
            time.sleep(0.03)
            return AgentAction.tool("git_diff", {}, operation_id="too-late")

    gateway = RecordingGateway()
    with pytest.raises(TimeBudgetExceeded):
        MinimalAgentLoop(max_steps=2, max_elapsed_seconds=0.01).run(
            "task-1", SlowRuntime(), gateway, tmp_path, AgentContext.empty()
        )
    assert gateway.requests == []


def test_context_compaction_preserves_request_result_pairs() -> None:
    """Compacting only one side would produce an invalid tool-calling transcript."""
    context = AgentContext.empty(issue="fix app")
    for number in range(4):
        request = ToolRequest(
            operation_id=f"op-{number}",
            name="read_file",
            arguments={"path": f"file-{number}.py"},
        )
        context = context.with_exchange(
            ToolExchange(
                request=request,
                result=ToolResult(
                    operation_id=request.operation_id,
                    tool_name=request.name,
                    status=ToolStatus.SUCCEEDED,
                    output={"content": "x" * 800},
                ),
            )
        )

    compacted = context.compact(max_chars=2_500, keep_recent=1)

    assert len(compacted.model_dump_json()) <= 2_500
    assert len(compacted.exchanges) == 4
    assert all(
        exchange.request.operation_id == exchange.result.operation_id
        for exchange in compacted.exchanges
    )
    assert compacted.exchanges[0].compacted is True
    assert compacted.exchanges[-1].compacted is False


def test_context_raises_when_fixed_metadata_exceeds_budget() -> None:
    """Silently exceeding a hard context limit would fail later at the model boundary."""
    context = AgentContext.empty(issue="x" * 2_000)
    with pytest.raises(ContextBudgetExceeded):
        context.compact(max_chars=512)
