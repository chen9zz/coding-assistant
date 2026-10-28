from pathlib import Path

import pytest

from repopilot.evaluation.cases import load_builtin_cases
from repopilot.tools import project_memory
from repopilot.tools.redaction import Redactor


def test_memory_requires_confirmation_and_invalidates_changed_source(tmp_path: Path):
    assert hasattr(project_memory, "ProjectMemory")
    repo = load_builtin_cases()[1].prepare_repository(tmp_path / "repo")
    store = project_memory.ProjectMemory(tmp_path / "memory", Redactor())
    record = store.propose(repo, "division", "Inspect denominator before division", "calculator.py")
    assert store.search(repo, repo, "denominator") == []
    with pytest.raises(ValueError):
        store.transition(repo, record["id"], "approved", "0" * 64)
    store.transition(repo, record["id"], "approved", record["record_sha256"])
    assert len(store.search(repo, repo, "denominator")) == 1
    (repo / "calculator.py").write_text("changed\n")
    assert store.search(repo, repo, "denominator") == []
    assert store.list(repo)[0]["effective_state"] == "stale"


def test_memory_rejects_unsafe_sources_and_private_store_links(tmp_path: Path):
    assert hasattr(project_memory, "ProjectMemory")
    repo = load_builtin_cases()[1].prepare_repository(tmp_path / "repo")
    store = project_memory.ProjectMemory(tmp_path / "memory", Redactor())
    with pytest.raises(ValueError):
        store.propose(repo, "secret", "example", "../outside.md")
    (repo / "linked.md").symlink_to(repo / "calculator.py")
    with pytest.raises(ValueError):
        store.propose(repo, "link", "example", "linked.md")


def test_memory_conflicts_retirement_deletion_and_repository_isolation(tmp_path: Path):
    repo = load_builtin_cases()[1].prepare_repository(tmp_path / "repo")
    other = load_builtin_cases()[1].prepare_repository(tmp_path / "other")
    store = project_memory.ProjectMemory(tmp_path / "memory", Redactor())
    first = store.propose(repo, "division", "denominator", "calculator.py")
    store.transition(repo, first["id"], "approved", first["record_sha256"])
    assert store.search(other, other, "denominator") == []
    second = store.propose(repo, "division", "new denominator rule", "calculator.py")
    with pytest.raises(ValueError):
        store.transition(repo, second["id"], "approved", second["record_sha256"])
    approved = next(row for row in store.list(repo) if row["id"] == first["id"])
    store.transition(repo, first["id"], "retired", approved["record_sha256"])
    store.transition(repo, second["id"], "approved", second["record_sha256"])
    assert [row["id"] for row in store.search(repo, repo, "denominator")] == [second["id"]]
    current = next(row for row in store.list(repo) if row["id"] == second["id"])
    store.transition(repo, second["id"], "deleted", current["record_sha256"])
    assert store.search(repo, repo, "denominator") == []
    assert all(row["id"] != second["id"] for row in store.list(repo))


def test_secret_exclusion_and_linked_database(tmp_path: Path):
    repo = load_builtin_cases()[1].prepare_repository(tmp_path / "repo")
    secret = "hidden-memory-value-123"
    store = project_memory.ProjectMemory(tmp_path / "memory", Redactor({secret}))
    with pytest.raises(ValueError):
        store.propose(repo, "division", secret, "calculator.py")
    (repo / "private.md").write_text(secret)
    with pytest.raises(ValueError):
        store.propose(repo, "division", "safe text", "private.md")
    record = store.propose(repo, "division", "safe text", "calculator.py")
    data = next((tmp_path / "memory").glob("*.json"))
    outside = tmp_path / "outside.json"
    data.rename(outside)
    data.symlink_to(outside)
    with pytest.raises(OSError):
        store.transition(repo, record["id"], "approved", record["record_sha256"])
    assert "candidate" in outside.read_text()


def test_memory_and_context_tools_run_through_production_loop(tmp_path: Path):
    from repopilot.agent.actions import AgentAction
    from repopilot.agent.scripted import ScriptedRuntime
    from repopilot.bootstrap import build_local_container
    from repopilot.domain.models import TaskState, ToolRequest, ToolStatus
    from repopilot.tools.context_memory import ContextReadTool

    case = load_builtin_cases()[1]
    repo = case.prepare_repository(tmp_path / "repo")
    root = tmp_path / "runtime"
    memory = project_memory.ProjectMemory(root / "memory", Redactor())
    record = memory.propose(repo, "division", "denominator zero handling", "calculator.py")
    memory.transition(repo, record["id"], "approved", record["record_sha256"])
    container = build_local_container(root, memory_enabled=True)
    task = container.service.create(case.task_spec(repo).model_copy(update={"max_steps": 12}))
    actions = [
        AgentAction.tool("memory_search", {"query": "denominator"}, operation_id="before"),
        *case.actions[:-1],
        AgentAction.tool("memory_search", {"query": "denominator"}, operation_id="after"),
        AgentAction.tool("context_read", {"operation_id": "before"}, operation_id="recover"),
        case.actions[-1],
    ]
    done = container.service.run(task.task_id, ScriptedRuntime(actions))
    assert done.state is TaskState.SUCCEEDED
    assert len(container.store.get_operation(task.task_id, "before").output["hits"]) == 1
    assert container.store.get_operation(task.task_id, "after").output["hits"] == []
    recovered = container.store.get_operation(task.task_id, "recover")
    assert recovered.output["historical_only"]
    assert "denominator zero handling" in recovered.output["content"]
    other = container.service.create(case.task_spec(repo))
    other_done = container.service.run(other.task_id, case.runtime())
    result = ContextReadTool(container.store).execute(
        other_done.worktree,
        ToolRequest(
            name="context_read", operation_id="foreign", arguments={"operation_id": "before"}
        ),
    )
    assert result.status is ToolStatus.FAILED


def test_memory_cli_confirmation(tmp_path: Path):
    import json

    from typer.testing import CliRunner

    from repopilot.cli import app

    repo = load_builtin_cases()[1].prepare_repository(tmp_path / "repo")
    args = ["--workspace", str(tmp_path / "runtime"), "--repo", str(repo)]
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "memory",
            "propose",
            *args,
            "--title",
            "division",
            "--text",
            "denominator check",
            "--source",
            "calculator.py",
        ],
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    approved = runner.invoke(
        app,
        [
            "memory",
            "approve",
            *args,
            "--id",
            record["id"],
            "--expected-sha256",
            record["record_sha256"],
        ],
    )
    assert approved.exit_code == 0
    assert "approved" in runner.invoke(app, ["memory", "list", *args]).output
    assert runner.invoke(app, ["memory", "approve", *args, "--id", record["id"]]).exit_code == 2


def test_forget_available_when_source_indexing_fails(tmp_path: Path, monkeypatch):
    repo = load_builtin_cases()[1].prepare_repository(tmp_path / "repo")
    store = project_memory.ProjectMemory(tmp_path / "memory", Redactor())
    record = store.propose(repo, "division", "denominator check", "calculator.py")

    def unavailable(*args):
        raise ValueError("repository over limit")

    monkeypatch.setattr(store, "_sources", unavailable)
    assert store.list(repo)[0]["effective_state"] == "unverified"
    with pytest.raises(ValueError):
        store.transition(repo, record["id"], "approved", record["record_sha256"])
    store.transition(repo, record["id"], "deleted", record["record_sha256"])
    assert store.list(repo) == []


def test_search_rechecks_source_after_snapshot(tmp_path: Path, monkeypatch):
    repo = load_builtin_cases()[1].prepare_repository(tmp_path / "repo")
    store = project_memory.ProjectMemory(tmp_path / "memory", Redactor())
    record = store.propose(repo, "division", "denominator check", "calculator.py")
    store.transition(repo, record["id"], "approved", record["record_sha256"])
    original_sources = store._sources(repo)
    (repo / "calculator.py").write_text("changed after snapshot\n")
    monkeypatch.setattr(store, "_sources", lambda root: original_sources)
    assert store.search(repo, repo, "denominator") == []


def test_context_read_paging_and_restore(tmp_path: Path):
    import hashlib

    from repopilot.agent.actions import AgentAction
    from repopilot.agent.scripted import ScriptedRuntime
    from repopilot.bootstrap import build_local_container
    from repopilot.domain.models import ToolRequest, ToolStatus
    from repopilot.tools.context_memory import ContextReadTool

    case = load_builtin_cases()[1]
    repo = case.prepare_repository(tmp_path / "repo")
    # Read a tracked file with a large working-tree body using the ordinary production reader.
    # Commit it so the isolated task starts with exactly this content.
    from repopilot.infrastructure.process import ProcessRunner

    (repo / "calculator.py").write_text(
        "# evidence\n" * 2000 + "def safe_divide(a,b): return a/b\n"
    )
    runner = ProcessRunner()
    assert (
        runner.run(
            ["git", "add", "."], cwd=repo, timeout_seconds=10, max_output_bytes=2000
        ).exit_code
        == 0
    )
    assert (
        runner.run(
            ["git", "commit", "-m", "large evidence"],
            cwd=repo,
            timeout_seconds=10,
            max_output_bytes=2000,
        ).exit_code
        == 0
    )
    root = tmp_path / "runtime"
    container = build_local_container(root)
    task = container.service.create(case.task_spec(repo))
    done = container.service.run(
        task.task_id,
        ScriptedRuntime(
            [
                AgentAction.tool(
                    "read_file", {"path": "calculator.py"}, operation_id="read-original"
                ),
                AgentAction.final("read only, no repair"),
            ]
        ),
    )
    # Re-open store/container just as a restarted task would.
    reopened = build_local_container(root)
    tool = ContextReadTool(reopened.store)
    chunks, offset = [], 0
    while offset is not None:
        result = tool.execute(
            done.worktree,
            ToolRequest(
                name="context_read",
                operation_id=f"p-{offset}",
                arguments={"operation_id": "read-original", "offset": offset},
            ),
        )
        assert result.status is ToolStatus.SUCCEEDED
        chunks.append(result.output["content"])
        offset = result.output["next_offset"]
    original = reopened.store.get_operation(task.task_id, "read-original").model_dump_json()
    assert "".join(chunks) == original
    assert result.output["sha256"] == hashlib.sha256(original.encode()).hexdigest()
    invalid = tool.execute(
        done.worktree,
        ToolRequest(
            name="context_read",
            operation_id="bad",
            arguments={"operation_id": "read-original", "task_id": "different-task"},
        ),
    )
    assert invalid.status is ToolStatus.FAILED


def test_memory_disabled_by_default_and_configuration_opt_in(tmp_path: Path):
    from repopilot.agent.actions import AgentAction
    from repopilot.agent.scripted import ScriptedRuntime
    from repopilot.bootstrap import build_local_container
    from repopilot.configuration import load_config
    from repopilot.domain.models import ToolStatus

    config = tmp_path / "agent.toml"
    config.write_text(
        '[provider]\nprotocol="openai"\nmodel="test"\nbase_url="http://127.0.0.1:9"\n'
    )
    assert not load_config(config).memory.enabled
    config.write_text(config.read_text() + "[memory]\nenabled=true\n")
    assert load_config(config).memory.enabled
    case = load_builtin_cases()[1]
    repo = case.prepare_repository(tmp_path / "repo")
    container = build_local_container(tmp_path / "runtime")
    task = container.service.create(case.task_spec(repo))
    container.service.run(
        task.task_id,
        ScriptedRuntime(
            [
                AgentAction.tool("memory_search", {"query": "division"}, operation_id="disabled"),
                AgentAction.final("done"),
            ]
        ),
    )
    assert container.store.get_operation(task.task_id, "disabled").status is ToolStatus.FAILED
    assert "not registered" in container.store.get_operation(task.task_id, "disabled").error
    assert not (tmp_path / "runtime/memory").exists()
