import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from repopilot.bootstrap import build_local_container
from repopilot.cli import app
from repopilot.domain.models import TaskSpec, TaskState, ToolRequest


def test_doctor_reports_required_and_optional_capabilities() -> None:
    """Treating Docker as required would make the promised local quickstart unusable."""
    result = CliRunner().invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["python"]["ok"] is True
    assert payload["git"]["ok"] is True
    assert payload["ripgrep"]["ok"] is True
    assert payload["tree_sitter"]["ok"] is True
    assert payload["docker"]["required"] is False


def test_demo_runs_offline_benchmark_and_prints_report_paths(tmp_path: Path) -> None:
    """A demo that only prints canned prose would not prove the agent pipeline."""
    result = CliRunner().invoke(
        app,
        ["demo", "--workspace", str(tmp_path), "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["runtime"] == "scripted-offline"
    assert payload["resolved"] == 3
    assert payload["total"] == 3
    assert Path(payload["reports"]["json"]).exists()
    assert Path(payload["reports"]["markdown"]).exists()
    showcase = payload["showcase"]
    assert showcase["task_id"] == "config_validation"
    assert Path(showcase["workspace"]).joinpath("state.db").exists()
    assert set(showcase["artifacts"]) == {
        "patch",
        "test_report",
        "trajectory",
        "pr_draft",
    }
    assert all(Path(path).exists() for path in showcase["artifacts"].values())


def test_demo_refuses_to_overwrite_an_existing_run(tmp_path: Path) -> None:
    """Overwriting an earlier evidence bundle would destroy reproducibility."""
    first = CliRunner().invoke(app, ["demo", "--workspace", str(tmp_path), "--json"])
    second = CliRunner().invoke(app, ["demo", "--workspace", str(tmp_path), "--json"])

    assert first.exit_code == 0
    assert second.exit_code != 0
    assert "already exists" in second.stdout.lower()


def test_serve_builds_the_api_and_binds_to_loopback_by_default(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The portfolio demo needs one safe command, not an undocumented app-factory snippet."""
    observed: dict[str, object] = {}

    def fake_run(api: object, *, host: str, port: int) -> None:
        observed.update(api=api, host=host, port=port)

    monkeypatch.setattr("repopilot.cli.uvicorn.run", fake_run)

    result = CliRunner().invoke(app, ["serve", "--workspace", str(tmp_path)])

    assert result.exit_code == 0
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 8000
    assert observed["api"].title == "RepoPilot-R API"  # type: ignore[attr-defined]


def test_approve_command_authorizes_the_exact_persisted_request(tmp_path: Path) -> None:
    """Operators need a discoverable CLI path for the approval lifecycle promised by the API."""
    container = build_local_container(tmp_path, lambda: "task-approval")
    snapshot = container.service.create(
        TaskSpec(
            repository=tmp_path,
            issue="Run an operator-approved command",
            test_command=("python", "-m", "pytest"),
        )
    )
    for target, event_type in (
        (TaskState.INDEXING, "task.indexing"),
        (TaskState.PLANNING, "task.planning"),
        (TaskState.EXECUTING, "task.executing"),
    ):
        snapshot = container.service.orchestrator._transition(snapshot, target, event_type, {})
    request = ToolRequest(
        operation_id="operator-command",
        name="run_command",
        arguments={"argv": ["python", "safe_script.py"]},
    )
    container.service.orchestrator._transition(
        snapshot,
        TaskState.WAITING_APPROVAL,
        "task.approval_requested",
        {
            "operation_id": request.operation_id,
            "arguments_digest": request.arguments_digest,
        },
    )

    result = CliRunner().invoke(
        app,
        [
            "approve",
            "task-approval",
            "operator-command",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["state"] == "EXECUTING"
