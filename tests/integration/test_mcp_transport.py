from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repopilot.domain.models import ToolRequest, ToolStatus
from repopilot.infrastructure import mcp as adapter_module
from repopilot.infrastructure.optional import OptionalDependencyError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mcp_server.py"


def connection(**overrides: object) -> object:
    from repopilot.infrastructure.mcp_transport import MCPConnection, MCPServerConfig

    values = {
        "name": "fixture",
        "transport": "stdio",
        "command": sys.executable,
        "args": (str(FIXTURE),),
        "allow_tools": ("Echo.Value", "slow", "broken"),
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return MCPConnection(MCPServerConfig.model_validate(values))


def test_real_stdio_reuses_session_preserves_names_and_filters_environment(monkeypatch):
    # Reopening sessions, normalizing the wire name, or inheriting all env breaks this.
    monkeypatch.setenv("REPOTEST_PRIVATE", "private-value")
    monkeypatch.setenv("REPOTEST_PUBLIC", "mapped-value")
    with connection(env={"FIXTURE_MAPPED": "REPOTEST_PUBLIC"}) as session:
        adapters = session.tools()
        assert {tool.remote_name for tool in adapters} == {"Echo.Value", "slow", "broken"}
        tool = next(tool for tool in adapters if tool.remote_name == "Echo.Value")
        assert tool.input_schema["properties"]["value"]["type"] == "string"
        for count in (1, 2):
            result = tool.execute(
                None,
                ToolRequest(
                    operation_id=f"echo-{count}", name=tool.name, arguments={"value": "hello"}
                ),
                timeout_seconds=2,
            )
            assert result.status is ToolStatus.SUCCEEDED, result.error
            payload = json.loads(result.output["content"][0]["text"])
            assert payload["value"] == "hello"
            assert payload["calls"] == count
            assert payload["mapped"] == "mapped-value"
            assert payload["unmapped"] is None
            pid = payload["pid"]
        with pytest.raises(ValueError, match="allow"):
            session.call_tool("hidden", arguments={})
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(RuntimeError, match="closed"):
        session.call_tool("Echo.Value", arguments={"value": "after close"})


def test_deadline_cancels_real_call_and_session_can_continue():
    with connection() as session:
        tool = next(tool for tool in session.tools() if tool.remote_name == "slow")
        started = time.monotonic()
        result = tool.execute(
            None,
            ToolRequest(operation_id="slow", name=tool.name, arguments={}),
            timeout_seconds=0.1,
        )
        assert time.monotonic() - started < 1
        assert result.status is ToolStatus.FAILED
        assert "deadline" in result.error
        response = session.call_tool("Echo.Value", arguments={"value": "still open"})
        assert "still open" in response.content[0].text
        assert json.loads(response.content[0].text)["cancellations"] == 1


def test_v2_remote_error_is_a_failed_tool_result():
    with connection() as session:
        tool = next(tool for tool in session.tools() if tool.remote_name == "broken")
        result = tool.execute(
            None, ToolRequest(operation_id="broken", name=tool.name, arguments={})
        )
        assert result.status is ToolStatus.FAILED


def test_namespacing_does_not_collapse_distinct_raw_names():
    tools = [
        adapter_module.MCPToolAdapter(object(), server_name="fixture", remote_name=name)
        for name in ("Echo.Value", "echo_value", "echo-value", "ECHO_VALUE")
    ]
    assert len({tool.name for tool in tools}) == 4
    assert [tool.remote_name for tool in tools] == [
        "Echo.Value",
        "echo_value",
        "echo-value",
        "ECHO_VALUE",
    ]


def test_missing_executable_error_does_not_expose_its_value():
    with (
        pytest.raises(RuntimeError) as failure,
        connection(command="secret-command-does-not-exist"),
    ):
        pass
    assert "secret-command" not in str(failure.value)
    assert len(str(failure.value)) < 200


def test_exception_exit_closes_the_real_stdio_process():
    with pytest.raises(LookupError), connection() as session:
        response = session.call_tool("Echo.Value", arguments={"value": "exit"})
        pid = json.loads(response.content[0].text)["pid"]
        raise LookupError("host cancelled task")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_missing_mapped_environment_is_rejected_without_echoing_values(monkeypatch):
    monkeypatch.delenv("MCP_SOURCE_NOT_SET", raising=False)
    with (
        pytest.raises(ValueError, match="missing"),
        connection(env={"CHILD_TOKEN": "MCP_SOURCE_NOT_SET"}),
    ):
        pass


def test_missing_optional_sdk_has_actionable_install_instruction(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp", None)
    with pytest.raises(OptionalDependencyError, match=r"\.\[mcp\]"), connection():
        pass


@pytest.mark.parametrize(
    "override",
    [
        {"transport": "http", "url": "file:///secret"},
        {"timeout_seconds": 0},
        {"name": ""},
        {"env": {"BAD=NAME": "TOKEN"}},
        {"unknown": "value"},
    ],
)
def test_invalid_config_is_rejected_before_starting_process(override):
    with pytest.raises(ValueError):
        connection(**override)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "http://192.168.1.2/mcp",
        "https://example.com/mcp?token=secret",
        "https://example.com:0/mcp",
        " https://example.com/mcp",
        "https://example.com/mcp\n",
        "https://example.com/mcp path",
        "https://example.com/mcp\x00",
    ],
)
def test_http_rejects_unsafe_endpoint_before_connecting(url):
    with pytest.raises(ValueError):
        connection(transport="http", command=None, args=(), url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/mcp",
        "http://localhost:8000/mcp",
        "http://[::1]:8000/mcp",
        "https://example.com/mcp",
    ],
)
def test_http_accepts_tls_and_loopback_urls(url):
    connection(transport="http", command=None, args=(), url=url)


def test_invalid_config_errors_do_not_echo_url_credentials():
    from repopilot.infrastructure.mcp_transport import MCPServerConfig

    with pytest.raises(ValueError) as error:
        MCPServerConfig.model_validate(
            {
                "url": "https://u:s3cr3t@example.com/mcp",
                "transport": "http",
                "name": "fixture",
            }
        )
    assert "s3cr3t" not in str(error.value)


def test_initialization_timeout_reaps_the_child_process(tmp_path):
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    with (
        pytest.raises(RuntimeError),
        connection(
            args=(str(FIXTURE), "hang", str(pid_file)),
            timeout_seconds=1,
        ),
    ):
        pass
    assert time.monotonic() - started < 6
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize("authenticated", [False, True])
def test_real_streamable_http_reuses_session_and_closes(authenticated, monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "fixture-local-token")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, str(FIXTURE), str(port)] + (["auth"] if authenticated else []),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={},
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                assert server.poll() is None, "local HTTP fixture exited"
                assert time.monotonic() < deadline, "local HTTP fixture did not start"
                time.sleep(0.025)
        with connection(
            transport="http",
            command=None,
            args=(),
            url=f"http://127.0.0.1:{port}/mcp",
            bearer_token_env="MCP_TEST_TOKEN" if authenticated else None,
        ) as session:
            assert len(session.tools()) == 3
            for count in (1, 2):
                response = session.call_tool("Echo.Value", arguments={"value": "http"})
                assert json.loads(response.content[0].text)["calls"] == count
        with pytest.raises(RuntimeError, match="closed"):
            session.list_tools()
    finally:
        server.terminate()
        server.wait(timeout=5)
