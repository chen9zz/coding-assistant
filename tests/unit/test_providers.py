"""Exercise provider wire protocols against real local HTTP servers."""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from pydantic import ValidationError

from repopilot.agent.actions import AgentContext, ToolExchange
from repopilot.domain.models import ToolResult, ToolStatus


def provider_module() -> Any:
    assert importlib.util.find_spec("repopilot.agent.providers") is not None, (
        "real HTTP provider transport is missing"
    )
    return importlib.import_module("repopilot.agent.providers")


TOOLS = [
    {
        "name": "read_file",
        "description": "Read a repository file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
]


class LocalProvider:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []


@contextmanager
def serving(responses: list[dict[str, Any]]) -> Iterator[tuple[str, LocalProvider]]:
    provider = LocalProvider(responses)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            provider.requests.append(
                {
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            reply = provider.responses.pop(0)
            time.sleep(reply.get("delay", 0))
            self.send_response(reply.get("status", 200))
            for key, value in reply.get("headers", {}).items():
                self.send_header(key, value)
            self.end_headers()
            payload = reply.get("raw", json.dumps(reply.get("body", {})).encode())
            with suppress(BrokenPipeError, ConnectionResetError):
                if "first_chunk" in reply:
                    self.wfile.write(reply["first_chunk"])
                    self.wfile.flush()
                    time.sleep(reply["body_delay"])
                self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", provider
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def response(protocol: str, *, tool_name: str | None = None) -> dict[str, Any]:
    if protocol == "openai":
        message: dict[str, Any] = {"role": "assistant", "content": "Verified the fix."}
        if tool_name:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "upstream-id",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": '{"path":"main.py"}',
                        },
                    }
                ],
            }
        return {
            "choices": [
                {"message": message, "finish_reason": ("tool_calls" if tool_name else "stop")}
            ]
        }
    content: list[dict[str, Any]] = [{"type": "text", "text": "Verified the fix."}]
    if tool_name:
        content = [
            {
                "type": "tool_use",
                "id": "upstream-id",
                "name": tool_name,
                "input": {"path": "main.py"},
            }
        ]
    return {
        "role": "assistant",
        "content": content,
        "stop_reason": "tool_use" if tool_name else "end_turn",
    }


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_real_http_native_tool_roundtrip_and_restart(protocol: str, monkeypatch: Any) -> None:
    """Dropping schemas, auth, paired evidence, or argument parsing breaks this contract."""
    providers = provider_module()
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-secret-value")
    with serving(
        [{"body": response(protocol, tool_name="read_file")}, {"body": response(protocol)}]
    ) as (url, server):
        config = providers.ProviderConfig(
            protocol=protocol, base_url=url, model="chosen-model", api_key_env="TEST_PROVIDER_KEY"
        )
        runtime = providers.HTTPRuntime(config, TOOLS, instructions="Verify before finishing.")
        context = AgentContext.empty("Fix the arithmetic", ("Keep the API",))
        action = runtime.next_action(context)
        assert action.tool_name == "read_file"
        assert action.arguments == {"path": "main.py"}
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", action.operation_id)
        request = action.to_tool_request()
        context = context.with_exchange(
            ToolExchange(
                request=request,
                result=ToolResult(
                    operation_id=request.operation_id,
                    tool_name=request.name,
                    status=ToolStatus.SUCCEEDED,
                    output={"content": "return a - b"},
                ),
            )
        )
        final = providers.HTTPRuntime(config, TOOLS).next_action(context)
        assert final.final_text == "Verified the fix."
        first, second = server.requests
        assert first["body"]["model"] == "chosen-model"
        assert "Fix the arithmetic" in json.dumps(first["body"]["messages"])
        assert "Keep the API" in json.dumps(first["body"])
        assert "Verify before finishing" in json.dumps(first["body"])
        if protocol == "openai":
            assert first["path"] == "/v1/chat/completions"
            assert first["headers"]["authorization"] == "Bearer test-secret-value"
            assert first["body"]["tools"][0]["function"]["parameters"] == TOOLS[0]["input_schema"]
            assert first["body"]["parallel_tool_calls"] is False
            messages = second["body"]["messages"]
            assert messages[-2]["tool_calls"][0]["function"]["name"] == "read_file"
            assert messages[-2]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"]
            assert messages[-1]["role"] == "tool"
            evidence = json.loads(messages[-1]["content"])
        else:
            assert first["path"] == "/v1/messages"
            assert first["headers"]["x-api-key"] == "test-secret-value"
            assert first["headers"]["anthropic-version"] == "2023-06-01"
            assert first["body"]["tools"][0]["input_schema"] == TOOLS[0]["input_schema"]
            assert first["body"]["tool_choice"]["disable_parallel_tool_use"] is True
            assert first["body"]["max_tokens"] > 0
            messages = second["body"]["messages"]
            call, result = messages[-2]["content"][0], messages[-1]["content"][0]
            assert call["type"] == "tool_use" and call["name"] == "read_file"
            assert result["type"] == "tool_result" and result["tool_use_id"] == call["id"]
            evidence = json.loads(result["content"])
        assert evidence["status"] == "SUCCEEDED"
        assert evidence["output"] == {"content": "return a - b"}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://user:secret@example.com/v1",
        "https://x/v1?key=secret",
        "https://x/v1#fragment",
        "file:///tmp/model",
        "https://",
        "https://x/\nsecret",
    ],
)
def test_rejects_unsafe_endpoints(url: str) -> None:
    providers = provider_module()
    with pytest.raises(ValidationError):
        providers.ProviderConfig(protocol="openai", base_url=url, model="chosen")


@pytest.mark.parametrize(
    "reply",
    [
        {"status": 401, "raw": b"test-secret-value"},
        {"raw": b"not-json-test-secret-value"},
        {"body": {"error": "test-secret-value"}},
        {"raw": b" " * (2 * 1024 * 1024 + 1)},
    ],
)
def test_http_failures_are_sanitized_and_bounded(reply: dict[str, Any], monkeypatch: Any) -> None:
    providers = provider_module()
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-secret-value")
    with serving([reply]) as (url, _):
        runtime = providers.HTTPRuntime(
            providers.ProviderConfig(
                protocol="openai",
                base_url=url,
                model="chosen",
                api_key_env="TEST_PROVIDER_KEY",
            ),
            TOOLS,
        )
        with pytest.raises(providers.ProviderError) as error:
            runtime.next_action(AgentContext.empty("fix"))
        assert "test-secret-value" not in str(error.value)
        assert url not in str(error.value)
        assert error.value.__suppress_context__


def test_redirects_never_forward_credentials(monkeypatch: Any) -> None:
    providers = provider_module()
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-secret-value")
    with (
        serving([]) as (target, target_server),
        serving(
            [
                {"status": 307, "headers": {"Location": target}},
            ]
        ) as (url, _),
    ):
        runtime = providers.HTTPRuntime(
            providers.ProviderConfig(
                protocol="openai",
                base_url=url,
                model="chosen",
                api_key_env="TEST_PROVIDER_KEY",
            ),
            TOOLS,
        )
        with pytest.raises(providers.ProviderError):
            runtime.next_action(AgentContext.empty("fix"))
        assert target_server.requests == []


def test_missing_credentials_prevent_requests(monkeypatch: Any) -> None:
    providers = provider_module()
    monkeypatch.delenv("TEST_PROVIDER_KEY", raising=False)
    with serving([]) as (url, server):
        runtime = providers.HTTPRuntime(
            providers.ProviderConfig(
                protocol="openai",
                base_url=url,
                model="chosen",
                api_key_env="TEST_PROVIDER_KEY",
            ),
            TOOLS,
        )
        with pytest.raises(providers.ProviderError, match="credential"):
            runtime.next_action(AgentContext.empty("fix"))
        assert server.requests == []


def test_host_timeout_bounds_slow_provider() -> None:
    providers = provider_module()
    with serving([{"delay": 0.5, "body": response("openai")}]) as (url, _):
        runtime = providers.HTTPRuntime(
            providers.ProviderConfig(
                protocol="openai",
                base_url=url,
                model="chosen",
                timeout_seconds=10,
            ),
            TOOLS,
        )
        started = time.monotonic()
        with pytest.raises(providers.ProviderError, match="timed out"):
            runtime.next_action_with_timeout(AgentContext.empty("fix"), 0.05)
        assert time.monotonic() - started < 0.4


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_namespaced_tool_alias_roundtrip_is_stable(protocol: str) -> None:
    """Real MCP names may contain punctuation forbidden by provider function names."""
    providers = provider_module()
    tools = [{**TOOLS[0], "name": "mcp.files.read_file"}]
    with serving([{"body": response(protocol)}]) as (url, server):
        config = providers.ProviderConfig(protocol=protocol, base_url=url, model="chosen")
        providers.HTTPRuntime(config, tools).next_action(AgentContext.empty("fix"))
        native = server.requests[0]["body"]["tools"][0]
        alias = native["function"]["name"] if protocol == "openai" else native["name"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias)
        server.responses.extend(
            [
                {"body": response(protocol, tool_name=alias)},
                {"body": response(protocol, tool_name=alias)},
                {"body": response(protocol)},
            ]
        )
        context = AgentContext.empty("fix")
        first = providers.HTTPRuntime(config, tools).next_action(context)
        second = providers.HTTPRuntime(config, tools).next_action(context)
        assert first.operation_id == second.operation_id
        assert first.tool_name == "mcp.files.read_file"
        request = first.to_tool_request()
        resumed = context.with_exchange(
            ToolExchange(
                request=request,
                result=ToolResult(
                    operation_id=request.operation_id,
                    tool_name=request.name,
                    status=ToolStatus.DENIED,
                    error="permission required",
                ),
            )
        )
        providers.HTTPRuntime(config, tools).next_action(resumed)
        messages = server.requests[-1]["body"]["messages"]
        if protocol == "openai":
            assert messages[-2]["tool_calls"][0]["function"]["name"] == alias
            assert json.loads(messages[-1]["content"])["status"] == "DENIED"
        else:
            assert messages[-2]["content"][0]["name"] == alias
            assert messages[-1]["content"][0]["is_error"] is True


@pytest.mark.parametrize(
    ("protocol", "malformation"),
    [
        (p, m)
        for p in ("openai", "anthropic")
        for m in ("truncated", "multiple", "unknown", "arguments", "shape")
        if (p, m) != ("openai", "multiple")
    ],
)
def test_invalid_native_decisions_never_become_actions(protocol: str, malformation: str) -> None:
    providers = provider_module()
    body = response(protocol, tool_name="read_file")
    if protocol == "openai":
        choice = body["choices"][0]
        call = choice["message"]["tool_calls"][0]
        if malformation == "truncated":
            choice["finish_reason"] = "length"
        elif malformation == "multiple":
            choice["message"]["tool_calls"].append(call)
        elif malformation == "unknown":
            call["function"]["name"] = "unregistered_tool"
        elif malformation == "arguments":
            call["function"]["arguments"] = "[]"
        else:
            choice["message"] = []
    else:
        call = body["content"][0]
        if malformation == "truncated":
            body["stop_reason"] = "max_tokens"
        elif malformation == "multiple":
            body["content"].append(call)
        elif malformation == "unknown":
            call["name"] = "unregistered_tool"
        elif malformation == "arguments":
            call["input"] = []
        else:
            body["content"] = ["invalid"]
    with serving([{"body": body}]) as (url, _):
        runtime = providers.HTTPRuntime(
            providers.ProviderConfig(
                protocol=protocol,
                base_url=url,
                model="chosen",
            ),
            TOOLS,
        )
        with pytest.raises(providers.ProviderError):
            runtime.next_action(AgentContext.empty("fix"))


@pytest.mark.parametrize("reason", ["unknown_tool", "incomplete_decision"])
def test_decision_diagnostics_identify_failure_without_echoing_response(reason: str) -> None:
    providers = provider_module()
    body = response("openai", tool_name="read_file")
    choice = body["choices"][0]
    choice["message"]["content"] = "private-provider-response"
    if reason == "unknown_tool":
        choice["message"]["tool_calls"][0]["function"]["name"] = "private-provider-response"
    else:
        choice["finish_reason"] = "length"
    with serving([{"body": body}]) as (url, _):
        runtime = providers.HTTPRuntime(
            providers.ProviderConfig(protocol="openai", base_url=url, model="chosen"), TOOLS
        )
        with pytest.raises(providers.ProviderError) as failure:
            runtime.next_action(AgentContext.empty("fix"))
        assert reason in str(failure.value)
        assert "private-provider-response" not in str(failure.value)


def test_openai_multi_call_response_selects_one_then_replans_without_queued_actions() -> None:
    providers = provider_module()
    body = response("openai", tool_name="read_file")
    body["choices"][0]["message"]["tool_calls"].append(
        {
            "id": "never-execute",
            "type": "function",
            "function": {"name": "run_command", "arguments": '{"argv":["dangerous"]}'},
        }
    )
    with serving([{"body": body}, {"body": response("openai")}]) as (url, server):
        runtime = providers.HTTPRuntime(
            providers.ProviderConfig(protocol="openai", base_url=url, model="chosen"), TOOLS
        )
        context = AgentContext.empty("fix")
        first = runtime.next_action(context)
        assert first.tool_name == "read_file"
        request = first.to_tool_request()
        context = context.with_exchange(
            ToolExchange(
                request=request,
                result=ToolResult(
                    operation_id=request.operation_id,
                    tool_name=request.name,
                    status=ToolStatus.DENIED,
                    output={"error": "denied by host"},
                ),
            )
        )
        second = runtime.next_action(context)
        assert second.final_text == "Verified the fix."
        assert len(server.requests) == 2
        history = server.requests[1]["body"]["messages"]
        assert len(history[-2]["tool_calls"]) == 1
        assert history[-2]["tool_calls"][0]["function"]["name"] == "read_file"
        assert json.loads(history[-1]["content"])["status"] == "DENIED"
        assert "never-execute" not in json.dumps(history)
        assert "dangerous" not in json.dumps(history)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_no_tools_configuration_can_complete_without_credentials(protocol: str) -> None:
    providers = provider_module()
    with serving([{"body": response(protocol)}]) as (url, server):
        runtime = providers.HTTPRuntime(
            providers.ProviderConfig(
                protocol=protocol,
                base_url=url,
                model="chosen",
            ),
            [],
        )
        assert runtime.next_action(AgentContext.empty("explain this")).final_text
        assert "tools" not in server.requests[0]["body"]
        assert "authorization" not in server.requests[0]["headers"]
        assert "x-api-key" not in server.requests[0]["headers"]


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_request_options_support_nonthinking_and_output_budget(protocol: str) -> None:
    providers = provider_module()
    with serving([{"body": response(protocol, tool_name="read_file")}]) as (url, server):
        config = providers.ProviderConfig(
            protocol=protocol,
            base_url=url,
            model="chosen",
            request_options={"thinking": {"type": "disabled"}, "max_tokens": 1234},
        )
        action = providers.HTTPRuntime(config, TOOLS).next_action(AgentContext.empty("fix"))
        assert action.tool_name == "read_file"
        assert server.requests[0]["body"]["thinking"] == {"type": "disabled"}
        assert server.requests[0]["body"]["max_tokens"] == 1234


@pytest.mark.parametrize(
    "options",
    [
        {"messages": []},
        {"tools": []},
        {"model": "override"},
        {"stream": True},
        {"system": "override"},
        {"tool_choice": "none"},
        {"parallel_tool_calls": True},
        {"api_key": "secret"},
        {"thinking": {"type": "enabled"}},
        {"max_tokens": -1},
        {"max_tokens": True},
        {"temperature": float("nan")},
    ],
)
def test_options_cannot_override_host_protocol_or_enable_unreplayable_reasoning(
    options: dict[str, Any],
) -> None:
    providers = provider_module()
    with pytest.raises(ValidationError):
        providers.ProviderConfig(
            protocol="openai",
            base_url="https://provider.invalid/v1",
            model="chosen",
            request_options=options,
        )


def test_configuration_error_does_not_echo_embedded_secret() -> None:
    providers = provider_module()
    with pytest.raises(ValidationError) as error:
        providers.ProviderConfig(
            protocol="openai", model="chosen", base_url="https://user:private-secret@example.com/v1"
        )
    assert "private-secret" not in str(error.value)


def test_each_body_read_uses_remaining_time_budget() -> None:
    providers = provider_module()
    with serving(
        [{"delay": 0.15, "first_chunk": b" ", "body_delay": 0.3, "body": response("openai")}]
    ) as (url, _):
        runtime = providers.HTTPRuntime(
            providers.ProviderConfig(
                protocol="openai",
                base_url=url,
                model="chosen",
                timeout_seconds=10,
            ),
            TOOLS,
        )
        started = time.monotonic()
        with pytest.raises(providers.ProviderError, match="timed out"):
            runtime.next_action_with_timeout(AgentContext.empty("fix"), 0.25)
        assert time.monotonic() - started < 0.35
