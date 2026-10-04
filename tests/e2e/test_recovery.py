import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from repopilot.agent.actions import AgentAction, AgentContext
from repopilot.agent.scripted import ScriptedRuntime
from repopilot.application.orchestrator import (
    CrashAfterOperation,
    DurableTaskOrchestrator,
    InjectedCrash,
)
from repopilot.application.service import ApprovalRequestError, TaskApplicationService
from repopilot.domain.models import TaskEvent, TaskSpec, TaskState, ToolRequest, ToolStatus
from repopilot.infrastructure.git import GitWorktreeManager
from repopilot.infrastructure.sqlite import IdempotencyConflict, SQLiteEventStore
from repopilot.tools.builtin import build_builtin_registry
from repopilot.tools.gateway import OperationInProgress, SafeToolGateway
from repopilot.tools.policy import PolicyEngine
from repopilot.tools.redaction import Redactor
from repopilot.tools.repository import RepositoryMapper

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def current_value() -> str:
-    return "before"
+    return "after"
"""


def git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.email", "tests@example.com", cwd=repo)
    git("config", "user.name", "RepoPilot Tests", cwd=repo)
    (repo / "app.py").write_text('def current_value() -> str:\n    return "before"\n')
    (repo / "test_app.py").write_text(
        "from app import current_value\n\n"
        "def test_current_value() -> None:\n"
        '    assert current_value() == "after"\n'
    )
    git("add", ".", cwd=repo)
    git("commit", "-m", "seed bug", cwd=repo)
    return repo


def runtime_with_patch() -> ScriptedRuntime:
    return ScriptedRuntime(
        [
            AgentAction.tool("apply_patch", {"patch": PATCH}, operation_id="patch-1"),
            AgentAction.final("implemented and ready for verification"),
        ]
    )


def build_service(
    database: Path,
    worktrees: Path,
    task_id_factory: Callable[[], str] = lambda: "task-1",
) -> tuple[TaskApplicationService, SQLiteEventStore]:
    store = SQLiteEventStore(database)

    redactor = Redactor()

    def gateway_factory(worktree: Path, spec: TaskSpec) -> SafeToolGateway:
        return SafeToolGateway(
            store=store,
            registry=build_builtin_registry(),
            policy=PolicyEngine(
                worktree,
                allowed_commands={spec.test_command},
                allowed_paths=spec.allowed_paths,
                max_changed_files=spec.max_changed_files,
            ),
            redactor=redactor,
        )

    orchestrator = DurableTaskOrchestrator(
        store=store,
        worktrees=GitWorktreeManager(worktrees),
        mapper=RepositoryMapper(),
        gateway_factory=gateway_factory,
        redactor=redactor,
    )
    return TaskApplicationService(
        store,
        orchestrator,
        task_id_factory,
        redactor=redactor,
    ), store


def task_spec(source_repo: Path) -> TaskSpec:
    return TaskSpec(
        repository=source_repo,
        issue='Make current_value return "after"',
        test_command=(sys.executable, "-m", "pytest", "-q"),
        max_steps=4,
    )


def install_modify_command(source_repo: Path) -> list[str]:
    (source_repo / "modify.py").write_text(
        "from pathlib import Path\n"
        "Path('app.py').write_text('def current_value() -> str:\\n    return \\\"after\\\"\\n')\n"
    )
    git("add", "modify.py", cwd=source_repo)
    git("commit", "-m", "add bounded edit command", cwd=source_repo)
    return [sys.executable, "modify.py"]


def test_resume_does_not_repeat_a_committed_patch(source_repo: Path, tmp_path: Path) -> None:
    """A crash after commit must not apply the same side effect again on replay."""
    database = tmp_path / "state.db"
    worktrees = tmp_path / "worktrees"
    service, store = build_service(database, worktrees)
    task = service.create(task_spec(source_repo))

    with pytest.raises(InjectedCrash):
        service.run(
            task.task_id,
            runtime_with_patch(),
            fault=CrashAfterOperation("patch-1"),
        )

    crashed = store.get_task(task.task_id)
    assert crashed.state is TaskState.EXECUTING
    assert crashed.worktree is not None
    changed_once = (crashed.worktree / "app.py").read_text()
    assert changed_once.endswith('return "after"\n')

    reopened_service, reopened_store = build_service(database, worktrees)
    recovered = reopened_service.resume(task.task_id, runtime_with_patch())

    assert recovered.state is TaskState.SUCCEEDED
    assert (source_repo / "app.py").read_text().endswith('return "before"\n')
    assert (recovered.worktree / "app.py").read_text() == changed_once  # type: ignore[operator]
    patch_events = [
        event
        for event in reopened_store.events(task.task_id)
        if event.event_type == "tool.completed" and event.operation_id == "patch-1"
    ]
    assert len(patch_events) == 1


def test_resume_reconstructs_committed_context_instead_of_starting_empty(
    source_repo: Path,
    tmp_path: Path,
) -> None:
    """A runtime restarted after a committed tool call must see that paired exchange."""
    database = tmp_path / "context.db"
    worktrees = tmp_path / "context-worktrees"
    service, _ = build_service(database, worktrees)
    task = service.create(task_spec(source_repo))
    with pytest.raises(InjectedCrash):
        service.run(
            task.task_id,
            runtime_with_patch(),
            fault=CrashAfterOperation("patch-1"),
        )

    reopened, _ = build_service(database, worktrees)
    runtime = ScriptedRuntime([AgentAction.final("continue from persisted context")])
    result = reopened.resume(task.task_id, runtime)

    assert result.state is TaskState.SUCCEEDED
    assert len(runtime.contexts[0].exchanges) == 1
    exchange = runtime.contexts[0].exchanges[0]
    assert exchange.request.operation_id == "patch-1"
    assert exchange.result.status is ToolStatus.SUCCEEDED


def test_failed_verification_preserves_worktree_and_evidence(
    source_repo: Path, tmp_path: Path
) -> None:
    """A confident final message cannot override a failing deterministic test."""
    service, _ = build_service(tmp_path / "state.db", tmp_path / "worktrees")
    task = service.create(task_spec(source_repo))
    runtime = ScriptedRuntime([AgentAction.final("looks correct")])

    failed = service.run(task.task_id, runtime)

    assert failed.state is TaskState.FAILED
    assert failed.worktree is not None and failed.worktree.exists()


def test_final_answer_with_passing_tests_but_no_patch_is_not_resolved(tmp_path: Path) -> None:
    """A model's final answer cannot turn an unchanged repository into a resolved issue."""
    source = tmp_path / "passing-source"
    source.mkdir()
    git("init", "-b", "main", cwd=source)
    git("config", "user.email", "tests@example.com", cwd=source)
    git("config", "user.name", "RepoPilot Tests", cwd=source)
    (source / "app.py").write_text("def stable() -> bool:\n    return True\n")
    (source / "test_app.py").write_text(
        "from app import stable\n\n"
        "def test_stable() -> None:\n"
        "    assert stable() is True\n"
    )
    git("add", ".", cwd=source)
    git("commit", "-m", "passing baseline", cwd=source)
    service, store = build_service(tmp_path / "passing.db", tmp_path / "passing-worktrees")
    task = service.create(
        TaskSpec(
            repository=source,
            issue="Make a real, reviewable improvement",
            test_command=(sys.executable, "-m", "pytest", "-q"),
        )
    )

    result = service.run(task.task_id, ScriptedRuntime([AgentAction.final("already done")]))

    assert result.state is TaskState.FAILED
    assert any(
        event.event_type == "task.review_failed" and event.payload.get("reason") == "empty_diff"
        for event in store.events(task.task_id)
    )


def test_approved_operation_is_bound_to_its_digest_and_executes_after_restart(
    source_repo: Path,
    tmp_path: Path,
) -> None:
    """Approval must authorize one exact request and survive service reconstruction."""
    secret = "sk-sensitive-approval-123"
    command = [*install_modify_command(source_repo), f"token={secret}"]
    database = tmp_path / "approval.db"
    worktrees = tmp_path / "approval-worktrees"
    service, store = build_service(database, worktrees)
    task = service.create(task_spec(source_repo))
    runtime = ScriptedRuntime(
        [
            AgentAction.tool(
                "run_command",
                {"argv": command},
                operation_id="approved-edit",
            ),
            AgentAction.final("edit complete"),
        ]
    )

    waiting = service.run(task.task_id, runtime)

    assert waiting.state is TaskState.WAITING_APPROVAL
    assert store.get_operation(task.task_id, "approved-edit") is None
    assert secret not in "\n".join(event.model_dump_json() for event in store.events(task.task_id))
    with pytest.raises(ApprovalRequestError, match="does not match"):
        service.approve(task.task_id, "unrelated-request")
    assert store.get_task(task.task_id).state is TaskState.WAITING_APPROVAL

    executing = service.approve(task.task_id, "approved-edit")
    assert executing.state is TaskState.EXECUTING

    reopened, reopened_store = build_service(database, worktrees)
    resumed = reopened.resume(
        task.task_id,
        ScriptedRuntime([AgentAction.final("approved edit was host-executed")]),
    )

    assert resumed.state is TaskState.SUCCEEDED
    result = reopened_store.get_operation(task.task_id, "approved-edit")
    assert result is not None and result.status is ToolStatus.SUCCEEDED
    assert all(
        secret.encode() not in path.read_bytes()
        for path in database.parent.glob(f"{database.name}*")
    )
    assert [
        event.event_type
        for event in reopened_store.events(task.task_id)
        if event.event_type in {"task.approved", "tool.started", "tool.completed"}
        and event.operation_id in {None, "approved-edit"}
    ].count("task.approved") == 1


def test_approval_required_stops_before_the_runtime_can_propose_another_effect(
    source_repo: Path,
    tmp_path: Path,
) -> None:
    """An ASK decision is a suspension boundary, not another observation for the model."""
    command = install_modify_command(source_repo)
    service, _ = build_service(tmp_path / "pause.db", tmp_path / "pause-worktrees")
    task = service.create(task_spec(source_repo))
    runtime = ScriptedRuntime(
        [
            AgentAction.tool("run_command", {"argv": command}, operation_id="ask-first"),
            AgentAction.tool("apply_patch", {"patch": PATCH}, operation_id="must-not-run"),
            AgentAction.final("done"),
        ]
    )

    waiting = service.run(task.task_id, runtime)

    assert waiting.state is TaskState.WAITING_APPROVAL
    assert waiting.worktree is not None
    assert (waiting.worktree / "app.py").read_text().endswith('return "before"\n')
    assert len(runtime.contexts) == 1


def test_existing_operation_claim_is_a_conflict_not_a_terminal_task_failure(
    source_repo: Path,
    tmp_path: Path,
) -> None:
    """A second worker must not mark a normally in-flight operation as FAILED."""
    service, store = build_service(tmp_path / "claim.db", tmp_path / "claim-worktrees")
    task = service.create(task_spec(source_repo))
    executing = service.orchestrator._prepare(task)
    request = ToolRequest(
        operation_id="claimed-patch",
        name="apply_patch",
        arguments={"patch": PATCH},
    )
    intent = TaskEvent(
        event_id="claimed-intent",
        task_id=task.task_id,
        sequence=executing.sequence + 1,
        event_type="tool.started",
        from_state=TaskState.EXECUTING,
        to_state=TaskState.EXECUTING,
        operation_id=request.operation_id,
        payload={
            "operation_id": request.operation_id,
            "tool_name": request.name,
            "arguments_digest": request.arguments_digest,
        },
    )
    assert store.claim_operation(
        task.task_id,
        request,
        intent,
        expected_sequence=executing.sequence,
    )

    with pytest.raises(OperationInProgress):
        service.resume(
            task.task_id,
            ScriptedRuntime(
                [
                    AgentAction.tool(
                        "apply_patch",
                        {"patch": PATCH},
                        operation_id="claimed-patch",
                    ),
                    AgentAction.final("done"),
                ]
            ),
        )

    assert store.get_task(task.task_id).state is TaskState.EXECUTING


def test_changed_arguments_invalidate_an_earlier_approval(
    source_repo: Path,
    tmp_path: Path,
) -> None:
    """Approval of one digest must not authorize a different command with the same ID."""
    command = install_modify_command(source_repo)
    service, store = build_service(tmp_path / "changed.db", tmp_path / "changed-worktrees")
    task = service.create(task_spec(source_repo))
    original = AgentAction.tool(
        "run_command",
        {"argv": command},
        operation_id="approved-edit",
    )
    waiting = service.run(task.task_id, ScriptedRuntime([original, AgentAction.final("done")]))
    assert waiting.state is TaskState.WAITING_APPROVAL
    service.approve(task.task_id, "approved-edit")

    changed = AgentAction.tool(
        "run_command",
        {"argv": [sys.executable, "different.py"]},
        operation_id="approved-edit",
    )
    with pytest.raises(IdempotencyConflict):
        service.resume(
            task.task_id,
            ScriptedRuntime([changed, AgentAction.final("done")]),
        )

    current = store.get_task(task.task_id)
    assert current.state is TaskState.EXECUTING
    approved_result = store.get_operation(task.task_id, "approved-edit")
    assert approved_result is not None and approved_result.status is ToolStatus.SUCCEEDED
    assert current.worktree is not None
    assert not (current.worktree / "different.py").exists()
    requested_digests = [
        event.payload.get("arguments_digest")
        for event in store.events(task.task_id)
        if event.event_type == "task.approval_requested"
    ]
    assert requested_digests == [original.to_tool_request().arguments_digest]


def test_successful_terminal_task_resume_is_idempotent(source_repo: Path, tmp_path: Path) -> None:
    """Resuming a finished task must not create a second worktree or rerun tools."""
    service, store = build_service(tmp_path / "state.db", tmp_path / "worktrees")
    task = service.create(task_spec(source_repo))
    succeeded = service.run(task.task_id, runtime_with_patch())
    event_count = len(store.events(task.task_id))

    resumed = service.resume(task.task_id, runtime_with_patch())

    assert resumed == succeeded
    assert len(store.events(task.task_id)) == event_count


def test_task_deadline_is_persistent_across_service_restart(
    source_repo: Path, tmp_path: Path
) -> None:
    """Reconstructing the service must not reset a task's wall-clock budget."""
    database = tmp_path / "deadline.db"
    worktrees = tmp_path / "deadline-worktrees"
    service, _ = build_service(database, worktrees)
    task = service.create(
        task_spec(source_repo).model_copy(update={"timeout_seconds": 1})
    )
    time.sleep(1.05)
    reopened, store = build_service(database, worktrees)
    runtime = ScriptedRuntime([AgentAction.final("must not run")])

    result = reopened.resume(task.task_id, runtime)

    assert result.state is TaskState.FAILED
    assert runtime.contexts == []
    assert any(
        event.event_type == "task.failed"
        and event.payload.get("error_type") == "TimeBudgetExceeded"
        for event in store.events(task.task_id)
    )


def test_task_deadline_clamps_the_deterministic_test_process(
    source_repo: Path, tmp_path: Path
) -> None:
    """A one-second task cannot inherit the command tool's 120-second default."""
    (source_repo / "slow_test.py").write_text("import time\ntime.sleep(2)\n")
    git("add", "slow_test.py", cwd=source_repo)
    git("commit", "-m", "add slow verification fixture", cwd=source_repo)
    service, store = build_service(tmp_path / "slow.db", tmp_path / "slow-worktrees")
    task = service.create(
        TaskSpec(
            repository=source_repo,
            issue="Make a bounded change",
            test_command=(sys.executable, "slow_test.py"),
            timeout_seconds=1,
        )
    )

    started = time.monotonic()
    result = service.run(task.task_id, ScriptedRuntime([AgentAction.final("verify now")]))
    elapsed = time.monotonic() - started

    assert result.state is TaskState.FAILED
    assert elapsed < 1.8
    verification = store.get_operation(task.task_id, f"verify-{task.task_id}")
    assert verification is not None and verification.status is ToolStatus.FAILED
    assert isinstance(verification.output, dict)
    assert verification.output["timed_out"] is True


def test_structured_path_scope_is_enforced_before_patch_execution(
    source_repo: Path, tmp_path: Path
) -> None:
    """Advisory prose cannot override the host's machine-readable changed-path scope."""
    service, store = build_service(tmp_path / "scope.db", tmp_path / "scope-worktrees")
    task = service.create(
        task_spec(source_repo).model_copy(
            update={"allowed_paths": ("tests/**",), "max_changed_files": 1}
        )
    )

    result = service.run(task.task_id, runtime_with_patch())

    assert result.state is TaskState.FAILED
    denied = store.get_operation(task.task_id, "patch-1")
    assert denied is not None and denied.status is ToolStatus.DENIED
    assert result.worktree is not None
    assert (result.worktree / "app.py").read_text().endswith('return "before"\n')


def test_final_scope_rejects_staged_and_untracked_paths(
    source_repo: Path, tmp_path: Path
) -> None:
    """Final review must see more than the ordinary unstaged tracked diff."""
    service, store = build_service(tmp_path / "final-scope.db", tmp_path / "final-scope-wt")
    task = service.create(
        task_spec(source_repo).model_copy(
            update={"allowed_paths": ("app.py",), "max_changed_files": 1}
        )
    )
    executing = service.orchestrator._prepare(task)
    assert executing.worktree is not None

    class HiddenChangesRuntime:
        def next_action(self, context: AgentContext) -> AgentAction:
            if not context.exchanges:
                return AgentAction.tool(
                    "apply_patch", {"patch": PATCH}, operation_id="visible-patch"
                )
            (executing.worktree / "staged-outside.py").write_text("STAGED = True\n")
            git("add", "staged-outside.py", cwd=executing.worktree)
            (executing.worktree / "untracked-outside.py").write_text("UNTRACKED = True\n")
            return AgentAction.final("hide extra files")

    result = service.resume(task.task_id, HiddenChangesRuntime())

    assert result.state is TaskState.FAILED
    review = store.get_operation(task.task_id, f"diff-{task.task_id}")
    assert review is not None and isinstance(review.output, dict)
    assert set(review.output["changed_paths"]) == {
        "app.py",
        "staged-outside.py",
        "untracked-outside.py",
    }
    assert any(
        event.event_type == "task.review_failed"
        and "file limit" in str(event.payload.get("reason"))
        for event in store.events(task.task_id)
    )
