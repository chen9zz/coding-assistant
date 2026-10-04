import hashlib
import subprocess
from pathlib import Path

import pytest

from repopilot.domain.models import TaskSpec, ToolRequest, ToolResult, ToolStatus
from repopilot.infrastructure.sqlite import IdempotencyConflict, SQLiteEventStore
from repopilot.tools.builtin import GitDiffTool, ToolRegistry, build_builtin_registry
from repopilot.tools.gateway import OperationInProgress, SafeToolGateway
from repopilot.tools.policy import PolicyEngine
from repopilot.tools.redaction import Redactor

VALID_PATCH = """diff --git a/value.txt b/value.txt
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-before
+after
"""

ESCAPE_PATCH = """diff --git a/../escape.txt b/../escape.txt
--- a/../escape.txt
+++ b/../escape.txt
@@ -1 +1 @@
-private
+stolen
"""


class CountingEffectTool:
    name = "apply_patch"

    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self,
        worktree: Path,
        request: ToolRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls += 1
        (worktree / "effect-count.txt").write_text(str(self.calls))
        return ToolResult(
            operation_id=request.operation_id,
            tool_name=request.name,
            status=ToolStatus.SUCCEEDED,
        )


class CommitFailureStore:
    """Delegate reads/intents but fail the first result commit after the tool effect."""

    def __init__(self, delegate: SQLiteEventStore) -> None:
        self.delegate = delegate
        self.failed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)

    def commit_operation(self, *args: object, **kwargs: object) -> object:
        if not self.failed:
            self.failed = True
            raise RuntimeError("injected crash before result commit")
        return self.delegate.commit_operation(*args, **kwargs)  # type: ignore[arg-type]


def git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.email", "tests@example.com", cwd=repo)
    git("config", "user.name", "RepoPilot Tests", cwd=repo)
    (repo / "value.txt").write_text("before\n")
    (repo / "secret.txt").write_text("token=top-secret\n")
    git("add", ".", cwd=repo)
    git("commit", "-m", "initial", cwd=repo)
    return repo


@pytest.fixture
def store(tmp_path: Path, repository: Path) -> SQLiteEventStore:
    event_store = SQLiteEventStore(tmp_path / "state.db")
    event_store.create_task(
        "task-1",
        TaskSpec(
            repository=repository,
            issue="Change before to after",
            test_command=("python", "-m", "pytest"),
        ),
    )
    return event_store


@pytest.fixture
def gateway(repository: Path, store: SQLiteEventStore) -> SafeToolGateway:
    return SafeToolGateway(
        store=store,
        registry=build_builtin_registry(),
        policy=PolicyEngine(
            repository,
            allowed_commands={("python", "-m", "pytest")},
        ),
        redactor=Redactor(secrets={"top-secret"}),
    )


def test_gateway_replays_saved_patch_result_without_second_effect(
    gateway: SafeToolGateway,
    store: SQLiteEventStore,
    repository: Path,
) -> None:
    """Executing the same committed patch twice would corrupt recovery."""
    request = ToolRequest(
        operation_id="patch-1",
        name="apply_patch",
        arguments={"patch": VALID_PATCH},
    )

    first = gateway.execute("task-1", repository, request)
    second = gateway.execute("task-1", repository, request)

    assert first == second
    assert first.status is ToolStatus.SUCCEEDED
    assert (repository / "value.txt").read_text() == "after\n"
    assert [event.event_type for event in store.events("task-1")] == [
        "tool.started",
        "tool.completed",
        "tool.replayed",
    ]


def test_persisted_intent_prevents_reexecution_after_effect_before_commit(
    store: SQLiteEventStore,
    repository: Path,
) -> None:
    """An indeterminate effect must stop for reconciliation instead of running twice."""
    effect = CountingEffectTool()
    registry = ToolRegistry()
    registry.register(effect)
    request = ToolRequest(
        operation_id="effect-before-commit",
        name="apply_patch",
        arguments={"patch": VALID_PATCH},
    )
    crashing = SafeToolGateway(
        store=CommitFailureStore(store),  # type: ignore[arg-type]
        registry=registry,
        policy=PolicyEngine(repository),
        redactor=Redactor(),
    )

    with pytest.raises(RuntimeError, match="before result commit"):
        crashing.execute("task-1", repository, request)

    with pytest.raises(OperationInProgress, match="indeterminate"):
        SafeToolGateway(
            store=store,
            registry=registry,
            policy=PolicyEngine(repository),
            redactor=Redactor(),
        ).execute("task-1", repository, request)

    assert effect.calls == 1
    assert [event.event_type for event in store.events("task-1")] == ["tool.started"]


def test_reused_operation_id_with_new_arguments_is_rejected(
    gateway: SafeToolGateway,
    repository: Path,
) -> None:
    """Treating changed arguments as the same operation would break authorization identity."""
    first = ToolRequest(
        operation_id="read-1",
        name="read_file",
        arguments={"path": "value.txt"},
    )
    gateway.execute("task-1", repository, first)

    with pytest.raises(IdempotencyConflict):
        gateway.execute(
            "task-1",
            repository,
            first.model_copy(update={"arguments": {"path": "secret.txt"}}),
        )


def test_policy_denial_is_persisted_and_never_reaches_a_tool(
    gateway: SafeToolGateway,
    store: SQLiteEventStore,
    repository: Path,
) -> None:
    """An unrecorded denial would disappear from audit and be retried after restart."""
    request = ToolRequest(
        operation_id="read-outside",
        name="read_file",
        arguments={"path": "../outside.txt"},
    )

    result = gateway.execute("task-1", repository, request)

    assert result.status is ToolStatus.DENIED
    assert store.get_operation("task-1", "read-outside") == result
    assert store.events("task-1")[0].event_type == "tool.denied"


def test_patch_escape_is_denied_before_it_reaches_the_patch_tool(
    gateway: SafeToolGateway,
    repository: Path,
    tmp_path: Path,
) -> None:
    """Host containment must reject an escaping patch before any tool side effect."""
    outside = tmp_path / "escape.txt"
    outside.write_text("private\n")
    result = gateway.execute(
        "task-1",
        repository,
        ToolRequest(
            operation_id="patch-escape",
            name="apply_patch",
            arguments={"patch": ESCAPE_PATCH},
        ),
    )
    assert result.status is ToolStatus.DENIED
    assert "worktree" in (result.error or "").lower()
    assert outside.read_text() == "private\n"


def test_read_output_is_redacted_before_persistence(
    gateway: SafeToolGateway,
    store: SQLiteEventStore,
    repository: Path,
) -> None:
    """Persisting raw tool output would leak credentials even if telemetry is filtered."""
    result = gateway.execute(
        "task-1",
        repository,
        ToolRequest(
            operation_id="read-secret",
            name="read_file",
            arguments={"path": "secret.txt"},
        ),
    )
    persisted = store.get_operation("task-1", "read-secret")
    assert result.status is ToolStatus.SUCCEEDED
    assert "top-secret" not in result.model_dump_json()
    assert persisted == result


def test_diff_keeps_raw_integrity_hash_while_persisting_only_redacted_text(
    gateway: SafeToolGateway,
    store: SQLiteEventStore,
    repository: Path,
) -> None:
    """The public audit trail and exact local patch need different disclosure levels."""
    (repository / "value.txt").write_text("OPENAI_API_KEY=sk-test123\n")
    raw_diff = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", "--"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    result = gateway.execute(
        "task-1",
        repository,
        ToolRequest(operation_id="diff-secret", name="git_diff", arguments={}),
    )

    assert isinstance(result.output, dict)
    assert "sk-test123" not in str(result.output["diff"])
    assert result.output["diff_sha256"] == hashlib.sha256(raw_diff.encode()).hexdigest()
    assert store.get_operation("task-1", "diff-secret") == result


def test_git_diff_includes_staged_and_untracked_files(repository: Path) -> None:
    """The final evidence view must include every worktree change class."""
    (repository / "value.txt").write_text("staged\n")
    git("add", "value.txt", cwd=repository)
    (repository / "new file.txt").write_text("untracked\n")

    result = GitDiffTool().execute(
        repository,
        ToolRequest(operation_id="complete-diff", name="git_diff", arguments={}),
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert isinstance(result.output, dict)
    assert set(result.output["changed_paths"]) == {"new file.txt", "value.txt"}
    assert "diff --git a/value.txt b/value.txt" in str(result.output["diff"])
    assert "diff --git a/new file.txt b/new file.txt" in str(result.output["diff"])


def test_inline_python_never_reaches_the_process_runner(
    gateway: SafeToolGateway,
    repository: Path,
) -> None:
    """Allowing inline code would bypass the exact verification-command contract."""
    result = gateway.execute(
        "task-1",
        repository,
        ToolRequest(
            operation_id="test-1",
            name="run_command",
            arguments={"argv": ["python", "-c", "print('should be policy denied')"]},
        ),
    )
    assert result.status is ToolStatus.DENIED
