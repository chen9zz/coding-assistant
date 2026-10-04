from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from repopilot.agent.actions import ActionKind, AgentContext
from repopilot.agent.openai_runtime import OpenAIAgentsRuntime
from repopilot.domain.models import TaskSpec, ToolRequest, ToolStatus
from repopilot.infrastructure.mcp import MCPToolAdapter
from repopilot.infrastructure.optional import OptionalDependencyError
from repopilot.infrastructure.sqlite import SQLiteEventStore
from repopilot.infrastructure.telemetry import JsonlTelemetry, OpenTelemetryAdapter
from repopilot.tools.builtin import ToolRegistry
from repopilot.tools.gateway import SafeToolGateway
from repopilot.tools.policy import PolicyEngine
from repopilot.tools.redaction import Redactor


class FakeSDKRunner:
    def __init__(self, decision: object) -> None:
        self.decision = decision
        self.contexts: list[AgentContext] = []

    def run(self, context: AgentContext) -> object:
        self.contexts.append(context)
        return self.decision


class FakeMCPSession:
    def __init__(self) -> None:
        self.call_count = 0

    async def list_tools(self) -> object:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="get_issue",
                    description="Read an issue",
                    inputSchema={"type": "object"},
                )
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.call_count += 1
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text=f"{name}:{arguments['number']}")],
        )


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def __enter__(self) -> FakeSpan:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[tuple[str, FakeSpan]] = []

    def start_as_current_span(self, name: str) -> FakeSpan:
        span = FakeSpan()
        self.spans.append((name, span))
        return span


def test_openai_adapter_converts_sdk_tool_call_without_executing_it() -> None:
    runner = FakeSDKRunner(
        SimpleNamespace(tool_name="read_file", arguments={"path": "app.py"})
    )
    runtime = OpenAIAgentsRuntime(runner=runner)

    action = runtime.next_action(AgentContext.empty("inspect app.py"))

    assert action.kind is ActionKind.TOOL
    assert action.tool_name == "read_file"
    assert action.arguments == {"path": "app.py"}
    assert action.operation_id is not None
    assert len(runner.contexts) == 1


def test_openai_adapter_builds_stable_operation_identity() -> None:
    runner = FakeSDKRunner({"tool_name": "repo_map", "arguments": {"issue": "fix bug"}})
    runtime = OpenAIAgentsRuntime(runner=runner)
    context = AgentContext.empty("fix bug")

    first = runtime.next_action(context)
    second = runtime.next_action(context)

    assert first.operation_id == second.operation_id


def test_openai_adapter_converts_final_output() -> None:
    runtime = OpenAIAgentsRuntime(
        runner=FakeSDKRunner(SimpleNamespace(final_output={"final_text": "Verified."}))
    )

    assert runtime.next_action(AgentContext.empty()).final_text == "Verified."


def test_missing_online_extra_has_actionable_install_command() -> None:
    def missing(_name: str) -> Any:
        raise ModuleNotFoundError("agents")

    runtime = OpenAIAgentsRuntime(importer=missing)

    with pytest.raises(OptionalDependencyError, match=r"repopilot-r\[online\]"):
        runtime.next_action(AgentContext.empty("fix bug"))


def test_mcp_discovery_namespaces_remote_tools() -> None:
    session = FakeMCPSession()

    adapters = asyncio.run(MCPToolAdapter.discover(session, "github"))

    assert [adapter.name for adapter in adapters] == ["mcp__github__get_issue"]
    assert adapters[0].description == "Read an issue"


def test_mcp_tool_remains_subject_to_local_gateway(tmp_path: Path) -> None:
    repository, store = repository_and_store(tmp_path)
    session = FakeMCPSession()
    adapter = MCPToolAdapter(session, server_name="github", remote_name="get_issue")
    registry = ToolRegistry()
    registry.register(adapter)
    gateway = SafeToolGateway(
        store=store,
        registry=registry,
        policy=PolicyEngine(repository),
        redactor=Redactor(),
    )

    result = gateway.execute(
        "task-1",
        repository,
        ToolRequest(
            operation_id="mcp-denied",
            name="mcp__github__get_issue",
            arguments={"number": 7},
        ),
    )

    assert result.status is ToolStatus.DENIED
    assert session.call_count == 0


def test_explicitly_allowed_mcp_tool_executes_through_gateway(tmp_path: Path) -> None:
    repository, store = repository_and_store(tmp_path)
    session = FakeMCPSession()
    adapter = MCPToolAdapter(session, server_name="github", remote_name="get_issue")
    registry = ToolRegistry()
    registry.register(adapter)
    gateway = SafeToolGateway(
        store=store,
        registry=registry,
        policy=PolicyEngine(repository, allowed_mcp_tools={adapter.name}),
        redactor=Redactor(),
    )

    result = gateway.execute(
        "task-1",
        repository,
        ToolRequest(
            operation_id="mcp-allowed",
            name=adapter.name,
            arguments={"number": 7},
        ),
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert session.call_count == 1
    assert result.output == {"content": [{"type": "text", "text": "get_issue:7"}]}


def test_mcp_tool_returns_a_bounded_failure_when_transport_never_returns() -> None:
    """An optional remote adapter must not ignore the task's remaining deadline."""

    class NeverReturningSession:
        def call_tool(self, name: str, *, arguments: dict[str, object]) -> object:
            threading.Event().wait()
            raise AssertionError((name, arguments))

    adapter = MCPToolAdapter(
        NeverReturningSession(), server_name="slow", remote_name="forever"
    )
    started = time.monotonic()
    result = adapter.execute(
        object(),
        ToolRequest(
            operation_id="mcp-timeout",
            name=adapter.name,
            arguments={},
        ),
        timeout_seconds=0.05,
    )

    assert time.monotonic() - started < 0.5
    assert result.status is ToolStatus.FAILED
    assert "deadline" in (result.error or "").lower()


def test_jsonl_telemetry_redacts_content_and_writes_schema(tmp_path: Path) -> None:
    output = tmp_path / "trace.jsonl"
    sink = JsonlTelemetry(output, Redactor({"secret-value"}))

    sink.emit(
        "tool.completed",
        {"output": "secret-value", "duration_ms": 4, "task_id": "task-1"},
    )

    raw = output.read_text()
    record = json.loads(raw)
    assert "secret-value" not in raw
    assert record["schema_version"] == 1
    assert record["event_type"] == "tool.completed"
    assert record["payload"]["output"] == "[REDACTED]"


def test_otel_adapter_excludes_arbitrary_content_by_default() -> None:
    tracer = FakeTracer()
    sink = OpenTelemetryAdapter(tracer=tracer)

    sink.emit(
        "tool.completed",
        {
            "task_id": "task-1",
            "correlation_id": "corr-1",
            "duration_ms": 4,
            "output": "must-not-be-exported",
            "success": True,
        },
    )

    name, span = tracer.spans[0]
    assert name == "repopilot.tool.completed"
    assert span.attributes == {
        "repopilot.task_id": "task-1",
        "repopilot.correlation_id": "corr-1",
        "repopilot.duration_ms": 4,
        "repopilot.success": True,
    }


def repository_and_store(tmp_path: Path) -> tuple[Path, SQLiteEventStore]:
    repository = tmp_path / "repo"
    repository.mkdir()
    store = SQLiteEventStore(tmp_path / "state.db")
    store.create_task(
        "task-1",
        TaskSpec(
            repository=repository,
            issue="Read issue",
            test_command=("python", "-m", "pytest"),
        ),
    )
    return repository, store
