from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from repopilot.cli import app
from repopilot.evaluation.cases import load_builtin_cases


@pytest.mark.parametrize("mode", ["basic", "mcp", "approval", "large_read"])
def test_online_cli_uses_skill_native_tools_and_produces_verified_patch(
    tmp_path: Path, mode: str
) -> None:
    """Real HTTP responses must drive the real gateway, worktree, tests and evidence."""
    case = load_builtin_cases()[0]
    source = case.prepare_repository(tmp_path / "source")
    if mode == "large_read":
        code = source / "large.py"
        code.write_text("# " + "x" * 30_000 + "\n")
        subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "large source"], cwd=source, check=True, capture_output=True
        )
    skill = tmp_path / "skills" / "repair"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: repair\ndescription: Fix regressions\n---\nInspect before patching."
    )
    replies = [
        ("skill_read", {"name": "repair"}),
        ("read_file", {"path": "large.py" if mode == "large_read" else "config.py"}),
        ("apply_patch", case.actions[0].arguments),
        (None, {}),
    ]
    if mode == "mcp":
        replies.insert(2, ("$mcp", {"value": "coding context"}))
    if mode == "approval":
        replies.insert(
            2, ("run_command", {"argv": [sys.executable, "-m", "compileall", "-q", "config.py"]})
        )
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            name, args = replies[len(requests) - 1]
            if name == "$mcp":
                name = next(
                    t["function"]["name"]
                    for t in payload["tools"]
                    if t["function"]["name"].startswith("mcp__")
                )
            message: dict[str, Any] = {"role": "assistant", "content": "Ready for verification."}
            if name:
                message.update(
                    content=None,
                    tool_calls=[
                        {
                            "id": f"call_{len(requests)}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                )
            body = json.dumps(
                {
                    "choices": [
                        {"message": message, "finish_reason": "tool_calls" if name else "stop"}
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "agent.toml"
    config.write_text(
        f'[provider]\nprotocol="openai"\nmodel="local-contract-test"\n'
        f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
        '[skills]\nroots=["skills"]\n'
    )
    if mode == "mcp":
        fixture = Path(__file__).parents[1] / "fixtures" / "mcp_server.py"
        config.write_text(
            config.read_text() + '\n[[mcp_servers]]\nname="fixture"\n'
            'transport="stdio"\n' + f"command={json.dumps(sys.executable)}\n"
            f'args=[{json.dumps(str(fixture))}]\nallow_tools=["Echo.Value"]\n'
        )
    try:
        result = CliRunner().invoke(
            app,
            [
                "run",
                "--repo",
                str(source),
                "--issue",
                "Fix parse_port using repair skill",
                "--config",
                str(config),
                "--workspace",
                str(tmp_path / "runtime"),
                "--test-command",
                shlex.join(case.task_spec(source).test_command),
                "--json",
            ],
        )
        if mode == "approval":
            paused = json.loads(result.stdout)
            assert paused["state"] == "WAITING_APPROVAL"
            from repopilot.bootstrap import build_local_container

            container = build_local_container(tmp_path / "runtime")
            pending = container.service.events(paused["task_id"])[-1]
            approved = CliRunner().invoke(
                app,
                [
                    "approve",
                    paused["task_id"],
                    str(pending.payload["operation_id"]),
                    "--workspace",
                    str(tmp_path / "runtime"),
                ],
            )
            assert approved.exit_code == 0, approved.output
            result = CliRunner().invoke(
                app,
                [
                    "resume",
                    paused["task_id"],
                    "--config",
                    str(config),
                    "--workspace",
                    str(tmp_path / "runtime"),
                    "--json",
                ],
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.exit_code == 0, f"{result.stdout}\n{result.exception}"
    task = json.loads(result.stdout)
    assert task["state"] == "SUCCEEDED"
    bundle = tmp_path / "runtime" / "artifacts" / task["task_id"]
    assert (bundle / "change.patch").exists()
    assert "parse_port" in (bundle / "change.patch").read_text()
    assert json.loads((bundle / "test-report.json").read_text())["status"] == "SUCCEEDED"
    assert (Path(task["worktree"]) / "config.py").read_text() != (source / "config.py").read_text()
    catalog = {t["function"]["name"]: t["function"] for t in requests[0]["tools"]}
    assert catalog["apply_patch"]["parameters"]["required"] == ["patch"]
    assert "skill_read" in catalog
    assert "Inspect before patching" not in json.dumps(requests[0])
    assert "Inspect before patching" in json.dumps(requests[1])
    assert "test_command" in json.dumps(requests[0])
    artifacts = CliRunner().invoke(
        app,
        [
            "inspect",
            task["task_id"],
            "--workspace",
            str(tmp_path / "runtime"),
            "--json",
        ],
    )
    assert "skill_read" in artifacts.stdout
    if mode == "mcp":
        assert "coding context" in json.dumps(requests[-1])
        mcp_tools = [t for t in catalog if t.startswith("mcp__")]
        assert len(mcp_tools) == 1
        assert "mcp__" in artifacts.stdout


def test_check_config_discovers_without_contacting_model(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    config.write_text(
        '[provider]\nprotocol="openai"\nmodel="no-request"\nbase_url="http://127.0.0.1:1/v1"\n'
    )
    result = CliRunner().invoke(
        app, ["check-config", "--config", str(config), "--workspace", str(tmp_path / "runtime")]
    )
    assert result.exit_code == 0, result.output
    assert "model-not-contacted" in result.output
    assert "apply_patch" in json.loads(result.stdout)["tools"]


def test_run_requires_exactly_one_runtime_source(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["run", "--repo", str(tmp_path), "--issue", "test"])
    assert result.exit_code != 0
    assert "exactly one" in result.output.lower()


def test_configuration_relative_roots_and_unknown_settings(tmp_path: Path) -> None:
    from repopilot.configuration import load_config

    path = tmp_path / "agent.toml"
    path.write_text(
        '[provider]\nprotocol="openai"\nmodel="example"\nbase_url="https://example.com/v1"\n'
        '[skills]\nroots=["skills"]\n'
    )
    config = load_config(path)
    assert config.skills.roots == (tmp_path / "skills",)
    path.write_text(path.read_text() + "allow_everything=true\n")
    with pytest.raises(ValueError):
        load_config(path)
