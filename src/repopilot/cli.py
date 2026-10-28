"""Command-line product surface for local runs, recovery, and evaluation."""

from __future__ import annotations

import importlib.util
import json
import platform
import shlex
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn

from repopilot.agent.loop import Runtime
from repopilot.agent.scripted import ScriptedRuntime
from repopilot.api import create_app
from repopilot.bootstrap import LocalContainer, build_local_container
from repopilot.domain.models import TaskSpec, TaskState
from repopilot.evaluation.cases import load_action_json, load_builtin_cases
from repopilot.evaluation.runner import EvaluationRunner

app = typer.Typer(
    name="forgepilot",
    help="Reliable issue-to-verified-patch software engineering agent.",
    no_args_is_help=True,
)


@app.command("ui")
def ui_command(
    demo: Annotated[
        bool, typer.Option("--demo", help="Offline scripted demo, no model calls.")
    ] = False,
    repository: Annotated[Path | None, typer.Option("--repo", exists=True, file_okay=False)] = None,
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path(".forgepilot/server"),
    test_command: Annotated[str, typer.Option("--test-command")] = "python -m pytest -q",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
) -> None:
    """Open the loopback-only task workbench (web + shared TUI backend)."""
    from repopilot.api.control import create_control_app
    from repopilot.application.jobs import JobManager

    if demo and (repository or config):
        raise typer.BadParameter("--demo cannot be combined with --repo or --config")
    try:
        manager = JobManager(
            workspace,
            repository=repository,
            config=config,
            test_command=tuple(shlex.split(test_command)),
            demo=demo,
        )
        control = create_control_app(manager)
    except (ValueError, OSError, RuntimeError) as error:
        raise typer.BadParameter(str(error)) from None
    typer.echo(f"ForgePilot {'OFFLINE DEMO' if demo else 'ONLINE'}: http://127.0.0.1:{port}")
    typer.echo("Keep this service running for UI access; existing workers survive UI disconnects.")
    uvicorn.run(control, host="127.0.0.1", port=port)


@app.command("tui")
def tui_command(
    url: Annotated[str, typer.Option("--url")] = "http://127.0.0.1:8765",
) -> None:
    """Connect the interactive terminal to a running ForgePilot ui service."""
    from urllib.parse import urlsplit

    target = urlsplit(url)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("TUI must connect to a local loopback HTTP service")
    try:
        from repopilot.tui import ForgePilotApp
    except ImportError:
        raise typer.BadParameter(
            "install ForgePilot's tui extra first: pip install -e '.[tui]'"
        ) from None
    ForgePilotApp(base_url=url).run()


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Emit stable JSON.")] = False,
) -> None:
    """Check required local and optional production capabilities."""
    checks: dict[str, dict[str, Any]] = {
        "python": {
            "ok": sys.version_info >= (3, 12),
            "required": True,
            "version": platform.python_version(),
            "path": sys.executable,
        },
        "git": _binary_check("git", required=True),
        "ripgrep": _binary_check("rg", required=True),
        "tree_sitter": {
            "ok": importlib.util.find_spec("tree_sitter") is not None
            and importlib.util.find_spec("tree_sitter_python") is not None,
            "required": True,
            "detail": "Python parser packages",
        },
        "docker": _binary_check("docker", required=False),
        "openai_agents": {
            "ok": importlib.util.find_spec("agents") is not None,
            "required": False,
            "detail": "legacy SDK adapter; native HTTP providers do not require this extra",
        },
        "mcp": {
            "ok": importlib.util.find_spec("mcp") is not None,
            "required": False,
            "detail": "install the mcp extra to connect MCP servers",
        },
    }
    if json_output:
        typer.echo(json.dumps(checks, ensure_ascii=False, sort_keys=True))
    else:
        for name, check in checks.items():
            label = "ready" if check["ok"] else ("missing" if check["required"] else "optional")
            typer.echo(f"{name:16} {label}")
    if any(not check["ok"] and check["required"] for check in checks.values()):
        raise typer.Exit(1)


@app.command()
def serve(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Directory for API state, worktrees, and artifacts."),
    ] = Path(".repopilot/server"),
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
) -> None:
    """Serve the versioned API and evidence endpoints locally."""
    container = build_local_container(workspace)
    uvicorn.run(create_app(container.service), host=host, port=port)


@app.command("demo")
def demo_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Parent directory for a new demo evidence bundle."),
    ] = Path(".repopilot"),
    json_output: Annotated[bool, typer.Option("--json", help="Emit stable JSON.")] = False,
) -> None:
    """Run all built-in cases with no API key or Docker."""
    run_root = workspace.resolve(strict=False) / "demo-run"
    try:
        report = EvaluationRunner(run_root).run(load_builtin_cases())
    except FileExistsError:
        typer.echo(f"demo evidence directory already exists: {run_root}")
        raise typer.Exit(2) from None
    payload = _report_payload(report, run_root)
    _emit(payload, json_output)


@app.command("eval")
def evaluation_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="New directory for evaluation state and reports."),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Emit stable JSON.")] = False,
) -> None:
    """Run the deterministic benchmark suite."""
    try:
        report = EvaluationRunner(workspace.resolve(strict=False)).run(load_builtin_cases())
    except FileExistsError:
        typer.echo(f"evaluation directory already exists: {workspace}")
        raise typer.Exit(2) from None
    _emit(_report_payload(report, workspace.resolve()), json_output)


@app.command("memory")
def memory_command(
    action: Annotated[str, typer.Argument(help="list, propose, approve, retire or forget")],
    workspace: Annotated[Path, typer.Option("--workspace")],
    repository: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False)],
    title: Annotated[str, typer.Option("--title")] = "",
    text: Annotated[str, typer.Option("--text")] = "",
    source: Annotated[str, typer.Option("--source")] = "",
    identity: Annotated[str, typer.Option("--id")] = "",
    expected: Annotated[str, typer.Option("--expected-sha256")] = "",
) -> None:
    """Explicit host-only project memory management; never grants tool execution permission."""
    import os

    from repopilot.tools.project_memory import ProjectMemory
    from repopilot.tools.redaction import Redactor

    redactor = Redactor(
        {
            v
            for k, v in os.environ.items()
            if any(word in k.upper() for word in ("KEY", "TOKEN", "PASSWORD"))
        }
    )
    try:
        if action not in {"list", "propose", "approve", "retire", "forget"}:
            raise ValueError("unknown action")
        memory = ProjectMemory(workspace.absolute() / "memory", redactor)
        if action == "propose":
            result = memory.propose(repository, title, text, source)
        elif action == "list":
            result = {"memories": memory.list(repository)}
        else:
            state = {"approve": "approved", "retire": "retired", "forget": "deleted"}[action]
            memory.transition(repository, identity, state, expected)
            result = {"status": state, "id": identity}
    except (ValueError, OSError):
        typer.echo("Memory rejected: inspect private directory, source, state and exact digest.")
        raise typer.Exit(2) from None
    typer.echo(json.dumps(redactor.redact(result), ensure_ascii=False, indent=2))


@app.command("eval-compare")
def compare_command(
    workspace: Annotated[Path, typer.Option("--workspace")],
    config: Annotated[Path | None, typer.Option("--config", exists=True)] = None,
    repetitions: Annotated[int, typer.Option("--repetitions", min=1, max=3)] = 1,
) -> None:
    """Compare RAG on/off on built-in trusted fixtures; --config makes paid model calls."""
    from repopilot.configuration import load_config
    from repopilot.evaluation.paired import run_comparison

    try:
        provider = load_config(config).provider if config else None
        report = run_comparison(workspace, provider=provider, repetitions=repetitions)
    except (ValueError, OSError):
        typer.echo("Cannot start evaluation: check config and use a new workspace directory.")
        raise typer.Exit(2) from None
    typer.echo(json.dumps({"mode": report["mode"], "summary": report["summary"]}, indent=2))
    typer.echo(f"Reports: {workspace.absolute() / 'report.json'}")


@app.command("eval-note")
def evaluation_note_command(
    workspace: Annotated[Path, typer.Option("--workspace", exists=True)],
    attempt: Annotated[str, typer.Option("--attempt")],
    kind: Annotated[str, typer.Option("--kind")],
    note: Annotated[str, typer.Option("--note")],
) -> None:
    """Record an operator observation, without approving or resuming a task."""
    from repopilot.evaluation.paired import add_note

    if kind not in {"diagnosis", "approval", "cancellation"}:
        raise typer.BadParameter("kind must be diagnosis, approval or cancellation")
    try:
        add_note(workspace, attempt, kind, note)
    except (ValueError, OSError):
        typer.echo("Cannot append note: check completed report, attempt and note length.")
        raise typer.Exit(2) from None
    typer.echo("Operator observation recorded; original outcome unchanged.")


@app.command("run")
def run_command(
    repository: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False)],
    issue: Annotated[str, typer.Option("--issue")],
    actions: Annotated[Path | None, typer.Option("--actions", exists=True, dir_okay=False)] = None,
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path(".repopilot/task"),
    test_command: Annotated[str, typer.Option("--test-command")] = "python -m pytest -q",
    allowed_path: Annotated[
        list[str] | None,
        typer.Option("--allowed-path", help="Repeatable host-enforced changed-path glob."),
    ] = None,
    max_changed_files: Annotated[
        int | None,
        typer.Option("--max-changed-files", min=1, max=1000),
    ] = None,
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds", min=1, max=3600)] = 300,
    max_steps: Annotated[int, typer.Option("--max-steps", min=1, max=100)] = 24,
    context_chars: Annotated[
        int, typer.Option("--context-chars", min=24_000, max=1_000_000)
    ] = 128_000,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run an online coding task (--config) or offline fixture (--actions) in a worktree."""
    argv = tuple(shlex.split(test_command))
    if not argv:
        raise typer.BadParameter("test command cannot be empty")
    spec = TaskSpec(
        repository=repository.resolve(),
        issue=issue,
        test_command=argv,
        allowed_paths=tuple(allowed_path or ()),
        max_changed_files=max_changed_files,
        timeout_seconds=timeout_seconds,
        max_steps=max_steps,
        max_context_chars=context_chars,
    )
    with _runtime_session(workspace, actions, config) as (container, runtime):
        snapshot = container.service.create(spec)
        result = container.service.run(snapshot.task_id, runtime)
        if result.state is TaskState.SUCCEEDED:
            container.service.artifacts(result.task_id)
    _emit(result.model_dump(mode="json"), json_output)
    if result.state is TaskState.FAILED:
        raise typer.Exit(1)


@app.command()
def resume(
    task_id: Annotated[str, typer.Argument()],
    actions: Annotated[Path | None, typer.Option("--actions", exists=True, dir_okay=False)] = None,
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path(".repopilot/task"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Resume a non-terminal task from SQLite checkpoints."""
    with _runtime_session(workspace, actions, config) as (container, runtime):
        result = container.service.resume(task_id, runtime)
        if result.state is TaskState.SUCCEEDED:
            container.service.artifacts(result.task_id)
    _emit(result.model_dump(mode="json"), json_output)
    if result.state is TaskState.FAILED:
        raise typer.Exit(1)


@app.command("check-config")
def check_config(
    config: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path(".repopilot/check"),
) -> None:
    """Connect trusted MCP servers and list tools; no model request or tool invocation."""
    from repopilot.configuration import load_config
    from repopilot.connected import connected_session
    from repopilot.tools.skills import SkillCatalog

    try:
        settings = load_config(config)
        with connected_session(workspace, config) as (_, runtime):
            # Discovery is done during session construction; no model credits are used.
            _emit(
                {
                    "protocol": settings.provider.protocol,
                    "model": settings.provider.model,
                    "mcp_servers": [s.name for s in settings.mcp_servers],
                    "tools": [tool["name"] for tool in runtime.tools],
                    "skills": SkillCatalog(settings.skills.roots).metadata(),
                    "status": "configuration-and-mcp-ready; model-not-contacted",
                },
                True,
            )
    except (ValueError, OSError, RuntimeError) as error:
        raise typer.BadParameter(str(error)) from None


@contextmanager
def _runtime_session(
    workspace: Path, actions: Path | None, config: Path | None
) -> Iterator[tuple[LocalContainer, Runtime]]:
    if (actions is None) == (config is None):
        raise typer.BadParameter("provide exactly one of --config or --actions")
    try:
        if config is not None:
            from repopilot.connected import connected_session

            with connected_session(workspace, config) as session:
                yield session
        else:
            assert actions is not None
            yield build_local_container(workspace), ScriptedRuntime(load_action_json(actions))
    except (ValueError, OSError, RuntimeError) as error:
        raise typer.BadParameter(str(error)) from None


@app.command()
def approve(
    task_id: Annotated[str, typer.Argument()],
    request_id: Annotated[str, typer.Argument()],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path(".repopilot/task"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Approve one persisted operation identity and argument digest."""
    container = build_local_container(workspace)
    result = container.service.approve(task_id, request_id)
    _emit(result.model_dump(mode="json"), json_output)


@app.command()
def inspect(
    task_id: Annotated[str, typer.Argument()],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path(".repopilot/task"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show persisted task state and audit events."""
    container = build_local_container(workspace)
    payload = {
        "task": container.service.get(task_id).model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in container.service.events(task_id)],
    }
    _emit(payload, json_output)


def _binary_check(name: str, *, required: bool) -> dict[str, Any]:
    path = shutil.which(name)
    return {"ok": path is not None, "required": required, "path": path}


def _report_payload(report: Any, run_root: Path) -> dict[str, Any]:
    showcase_task = report.cases[0].case_id if report.cases else None
    showcase_workspace = run_root / "cases" / showcase_task if showcase_task is not None else None
    artifact_root = (
        showcase_workspace / "artifacts" / showcase_task
        if showcase_workspace is not None and showcase_task is not None
        else None
    )
    return {
        "runtime": report.environment.runtime,
        "resolved": report.resolved,
        "total": report.total,
        "resolved_rate": report.resolved_rate,
        "policy_violations": report.policy_violations,
        "recovery_attempts": report.recovery_attempts,
        "reports": {
            "json": str(run_root / "evaluation-results" / "report.json"),
            "markdown": str(run_root / "evaluation-results" / "report.md"),
        },
        "showcase": {
            "task_id": showcase_task,
            "workspace": str(showcase_workspace) if showcase_workspace else None,
            "artifacts": {
                "patch": str(artifact_root / "change.patch"),
                "test_report": str(artifact_root / "test-report.json"),
                "trajectory": str(artifact_root / "trajectory.jsonl"),
                "pr_draft": str(artifact_root / "pull-request.md"),
            }
            if artifact_root is not None
            else {},
        },
    }


def _emit(payload: Any, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
