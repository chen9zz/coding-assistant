from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from repopilot.application.jobs import JobManager
from repopilot.cli import app
from repopilot.worker import run_worker


def test_ui_binds_only_loopback_and_prints_demo(tmp_path: Path, monkeypatch) -> None:
    server = Mock()
    monkeypatch.setattr("repopilot.cli.uvicorn.run", server)
    result = CliRunner().invoke(app, ["ui", "--demo", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "OFFLINE DEMO" in result.output
    assert server.call_args.kwargs == {"host": "127.0.0.1", "port": 8765}


@pytest.mark.parametrize("arguments", [[], ["--demo", "--repo", "."]])
def test_ui_rejects_missing_online_or_mixed_demo_settings(tmp_path: Path, arguments) -> None:
    result = CliRunner().invoke(app, ["ui", "--workspace", str(tmp_path), *arguments])
    assert result.exit_code != 0


def test_tui_connects_locally_only(monkeypatch) -> None:
    run = Mock()
    monkeypatch.setattr("repopilot.tui.ForgePilotApp.run", run)
    assert CliRunner().invoke(app, ["tui"]).exit_code == 0
    run.assert_called_once()
    assert CliRunner().invoke(app, ["tui", "--url", "http://evil.invalid"]).exit_code != 0


@pytest.mark.parametrize("kind,cancel", [("task", False), ("analysis", False), ("task", True)])
def test_worker_entrypoint_inline_observes_the_same_contract(
    tmp_path: Path, monkeypatch, kind, cancel
):
    manager = JobManager(tmp_path, demo=True)
    monkeypatch.setattr(manager, "_launch", lambda _: None)
    job = manager.submit("verify worker", kind=kind)
    generation = manager.store.launch_generation(job["job_id"])
    if cancel:
        manager.cancel(job["job_id"])
    run_worker(tmp_path, job["job_id"], generation)
    result = manager.get(job["job_id"])
    assert result["state"] == ("CANCELLED" if cancel else "SUCCEEDED"), result
    # A duplicate process cannot claim the same generation again.
    run_worker(tmp_path, job["job_id"], generation)
    assert manager.get(job["job_id"])["state"] == result["state"]
