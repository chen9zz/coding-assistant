"""Command-line product surface for local runs, recovery, and evaluation."""

from __future__ import annotations

import importlib.util
import json
import platform
import shlex
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn

from repopilot.agent.scripted import ScriptedRuntime
from repopilot.api import create_app
from repopilot.bootstrap import build_local_container
from repopilot.domain.models import TaskSpec
from repopilot.evaluation.cases import load_action_json, load_builtin_cases
from repopilot.evaluation.runner import EvaluationRunner

app = typer.Typer(
    name="repopilot",
    help="Reliable issue-to-verified-patch software engineering agent.",
    no_args_is_help=True,
)


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
            "detail": "install the online extra for real-model runs",
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


@app.command("run")
def run_command(
    repository: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False)],
    issue: Annotated[str, typer.Option("--issue")],
    actions: Annotated[Path, typer.Option("--actions", exists=True, dir_okay=False)],
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
    timeout_seconds: Annotated[
        int, typer.Option("--timeout-seconds", min=1, max=3600)
    ] = 300,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a structured offline action file against an isolated repository."""
    argv = tuple(shlex.split(test_command))
    if not argv:
        raise typer.BadParameter("test command cannot be empty")
    container = build_local_container(workspace)
    snapshot = container.service.create(
        TaskSpec(
            repository=repository.resolve(),
            issue=issue,
            test_command=argv,
            allowed_paths=tuple(allowed_path or ()),
            max_changed_files=max_changed_files,
            timeout_seconds=timeout_seconds,
        )
    )
    result = container.service.run(snapshot.task_id, ScriptedRuntime(load_action_json(actions)))
    _emit(result.model_dump(mode="json"), json_output)


@app.command()
def resume(
    task_id: Annotated[str, typer.Argument()],
    actions: Annotated[Path, typer.Option("--actions", exists=True, dir_okay=False)],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path(".repopilot/task"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Resume a non-terminal task from SQLite checkpoints."""
    container = build_local_container(workspace)
    result = container.service.resume(task_id, ScriptedRuntime(load_action_json(actions)))
    _emit(result.model_dump(mode="json"), json_output)


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
    showcase_workspace = (
        run_root / "cases" / showcase_task if showcase_task is not None else None
    )
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
