"""MCP discovery and execution adapters kept behind the local safe gateway."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import queue
import re
import threading
import time
from collections.abc import Awaitable, Mapping, Sequence
from typing import Protocol, cast

from pydantic import TypeAdapter

from repopilot.domain.models import JsonValue, ToolRequest, ToolResult, ToolStatus

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


class MCPToolDefinition(Protocol):
    name: str
    description: str | None
    inputSchema: Mapping[str, object]


class MCPToolList(Protocol):
    tools: Sequence[MCPToolDefinition]


class MCPSession(Protocol):
    def list_tools(self) -> object: ...

    def call_tool(self, name: str, *, arguments: dict[str, JsonValue]) -> object: ...


class MCPToolAdapter:
    """Expose one namespaced remote MCP tool as a host-controlled local tool."""

    def __init__(
        self,
        session: MCPSession,
        *,
        server_name: str,
        remote_name: str,
        description: str = "",
        input_schema: Mapping[str, object] | None = None,
    ) -> None:
        self.session = session
        self.server_name = self._segment(server_name)
        self.remote_name = remote_name
        self.name = f"mcp__{self.server_name}__{self._segment(remote_name)}"
        self.description = description
        self.input_schema = dict(input_schema or {})

    @classmethod
    async def discover(cls, session: MCPSession, server_name: str) -> tuple[MCPToolAdapter, ...]:
        response = cast(MCPToolList, await cls._await(session.list_tools()))
        return tuple(
            cls(
                session,
                server_name=server_name,
                remote_name=tool.name,
                description=tool.description or "",
                input_schema=getattr(tool, "input_schema", getattr(tool, "inputSchema", {})),
            )
            for tool in response.tools
        )

    def execute(
        self,
        _worktree: object,
        request: ToolRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        if request.name != self.name:
            return self._result(
                request,
                ToolStatus.FAILED,
                started,
                error="MCP adapter name does not match the requested tool",
            )
        if timeout_seconds is not None and timeout_seconds <= 0:
            return self._result(
                request,
                ToolStatus.FAILED,
                started,
                error="MCP call exceeded task deadline",
            )
        try:
            response = self._call_with_deadline(request.arguments, timeout_seconds)
            is_error = bool(getattr(response, "is_error", getattr(response, "isError", False)))
            content = [self._content(item) for item in getattr(response, "content", ())]
            output: dict[str, JsonValue] = {"content": content}
            structured = getattr(
                response, "structured_content", getattr(response, "structuredContent", None)
            )
            if structured is not None:
                output["structured_content"] = _JSON.validate_python(structured)
            serialized = json.dumps(output, ensure_ascii=False)
            truncated = len(serialized) > 12_000
            if truncated:
                output = {
                    "preview": serialized[:8000],
                    "original_chars": len(serialized),
                    "notice": "MCP result truncated; request a narrower query or server-side page.",
                }
            return self._result(
                request,
                ToolStatus.FAILED if is_error else ToolStatus.SUCCEEDED,
                started,
                output=output,
                truncated=truncated,
                error="remote MCP tool returned an error" if is_error else None,
            )
        except TimeoutError:
            return self._result(
                request,
                ToolStatus.FAILED,
                started,
                error="MCP call exceeded task deadline",
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            return self._result(
                request,
                ToolStatus.FAILED,
                started,
                error=f"MCP tool call failed ({type(error).__name__})",
            )

    def _call_with_deadline(
        self,
        arguments: dict[str, JsonValue],
        timeout_seconds: float | None,
    ) -> object:
        bounded_call = getattr(self.session, "call_tool_with_deadline", None)
        if callable(bounded_call):
            return bounded_call(
                self.remote_name, arguments=arguments, timeout_seconds=timeout_seconds
            )
        if timeout_seconds is None:
            return self._run(self.session.call_tool(self.remote_name, arguments=arguments))
        completed: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                value = self.session.call_tool(self.remote_name, arguments=arguments)
                completed.put((True, self._run(value)))
            except Exception as error:
                completed.put((False, error))

        threading.Thread(target=invoke, daemon=True, name=f"{self.name}-deadline").start()
        try:
            succeeded, value = completed.get(timeout=timeout_seconds)
        except queue.Empty as error:
            raise TimeoutError("MCP call exceeded task deadline") from error
        if not succeeded:
            if isinstance(value, (OSError, RuntimeError, TypeError, ValueError)):
                raise value
            raise RuntimeError(f"remote MCP tool failed: {type(value).__name__}")
        return value

    @staticmethod
    def _segment(value: str) -> str:
        normalized = _SAFE_NAME.sub("_", value).strip("_").lower()
        if not normalized:
            raise ValueError("MCP server and tool names must have a safe non-empty segment")
        if normalized != value or "__" in normalized or len(normalized) > 25:
            prefix = re.sub(r"_+", "_", normalized)[:12].rstrip("_")
            digest = hashlib.sha256(value.encode()).hexdigest()[:12]
            return f"{prefix}_{digest}"
        return normalized

    @staticmethod
    async def _await(value: object) -> object:
        if inspect.isawaitable(value):
            return await cast(Awaitable[object], value)
        return value

    @staticmethod
    def _run(value: object) -> object:
        if not inspect.isawaitable(value):
            return value
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(MCPToolAdapter._await(value))
        if inspect.iscoroutine(value):
            value.close()
        raise RuntimeError("the synchronous gateway cannot await MCP inside a running event loop")

    @staticmethod
    def _content(item: object) -> JsonValue:
        if hasattr(item, "model_dump"):
            raw = item.model_dump(mode="json")
        elif isinstance(item, Mapping):
            raw = dict(item)
        else:
            raw = {key: value for key, value in vars(item).items() if not key.startswith("_")}
        return _JSON.validate_python(raw)

    @staticmethod
    def _result(
        request: ToolRequest,
        status: ToolStatus,
        started: float,
        *,
        output: JsonValue = None,
        error: str | None = None,
        truncated: bool = False,
    ) -> ToolResult:
        return ToolResult(
            operation_id=request.operation_id,
            tool_name=request.name,
            status=status,
            output=output,
            error=error,
            truncated=truncated,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )
