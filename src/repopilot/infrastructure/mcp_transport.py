"""Explicit official-SDK MCP connections owned by one persistent async task.

Each connection owns a background event loop. Its transport and session context
managers enter and exit in the same task, including cancellation and shutdown.
Only explicit allow-listed tools can be called through the synchronous bridge.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import threading
import time
from concurrent.futures import Future
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repopilot.domain.models import JsonValue
from repopilot.infrastructure.mcp import MCPToolAdapter
from repopilot.infrastructure.optional import OptionalDependencyError

if TYPE_CHECKING:
    from mcp import ClientSession
    from mcp.types import Tool

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHUTDOWN_SECONDS = 10.0


class MCPServerConfig(BaseModel):
    """Trusted launch settings; env maps child names to host variable names."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    name: str = Field(min_length=1, max_length=100)
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    bearer_token_env: str | None = None
    allow_tools: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=20, gt=0, le=300, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_connection(self) -> Self:
        MCPToolAdapter._segment(self.name)
        if self.transport == "stdio":
            if (
                not self.command
                or "\x00" in self.command
                or self.url is not None
                or self.bearer_token_env is not None
            ):
                raise ValueError("stdio requires a command and no URL")
            if any("\x00" in arg for arg in self.args):
                raise ValueError("stdio arguments cannot contain NUL characters")
        else:
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.url or ""):
                raise ValueError("http URL cannot contain whitespace or control characters")
            endpoint = urlsplit(self.url or "")
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.fragment
                or endpoint.query
                or self.command is not None
                or self.args
                or self.env
            ):
                raise ValueError(
                    "http requires an HTTP(S) URL without credentials or stdio settings"
                )
            try:
                if endpoint.port == 0:
                    raise ValueError("port must be positive")
            except ValueError as error:
                raise ValueError("http URL has an invalid port") from error
            if endpoint.scheme == "http" and endpoint.hostname != "localhost":
                try:
                    loopback = ipaddress.ip_address(endpoint.hostname).is_loopback
                except ValueError:
                    loopback = False
                if not loopback:
                    raise ValueError("MCP endpoints require HTTPS except on loopback hosts")
        if self.bearer_token_env is not None and not _ENV_NAME.fullmatch(self.bearer_token_env):
            raise ValueError("bearer_token_env must name a host environment variable")
        if any(not _ENV_NAME.fullmatch(item) for pair in self.env.items() for item in pair):
            raise ValueError("env must map valid child variable names to host variable names")
        if any(not name or len(name) > 512 for name in self.allow_tools):
            raise ValueError("allow_tools must contain nonempty remote tool names")
        if len(set(self.allow_tools)) != len(self.allow_tools):
            raise ValueError("allow_tools contains duplicate names")
        return self


@dataclass
class _Request:
    operation: Literal["list", "call"]
    name: str
    arguments: dict[str, JsonValue]
    deadline: float
    future: Future[object]


class MCPConnection:
    """Synchronous gateway bridge; use ``with MCPConnection(config) as session``."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._ready: Future[None] = Future()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_Request | None] | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._environment: dict[str, str] = {}
        self._headers: dict[str, str] = {}

    def __enter__(self) -> Self:
        if self._thread is not None or self._closed:
            raise RuntimeError("MCP connection cannot be reopened")
        for target, source in self.config.env.items():
            value = os.environ.get(source)
            if value is None:
                raise ValueError("MCP environment source variable is missing")
            self._environment[target] = value
        if self.config.bearer_token_env is not None:
            token = os.environ.get(self.config.bearer_token_env)
            if not token or any(character in token for character in ("\r", "\n", "\x00")):
                raise ValueError("MCP bearer token environment variable is missing or invalid")
            self._headers["Authorization"] = f"Bearer {token}"
        self._thread = threading.Thread(
            target=self._thread_main, name=f"mcp-{self.config.name}", daemon=True
        )
        self._thread.start()
        try:
            self._ready.result(timeout=self.config.timeout_seconds + _SHUTDOWN_SECONDS)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop, queue = self._loop, self._queue
        if loop is not None and queue is not None and not loop.is_closed():
            loop.call_soon_threadsafe(queue.put_nowait, None)
        if self._thread is not None:
            self._thread.join(timeout=self.config.timeout_seconds + _SHUTDOWN_SECONDS)
            if self._thread.is_alive():
                raise RuntimeError("MCP shutdown did not complete within its bounded deadline")
        self._environment.clear()
        self._headers.clear()

    def list_tools(self) -> object:
        return self._submit("list", "", {}, None)

    def tools(self) -> tuple[MCPToolAdapter, ...]:
        response = self.list_tools()
        adapters = tuple(
            MCPToolAdapter(
                self,
                server_name=self.config.name,
                remote_name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
            )
            for tool in getattr(response, "tools", ())
        )
        if len({adapter.name for adapter in adapters}) != len(adapters):
            raise ValueError("MCP discovered conflicting tool names")
        return adapters

    def call_tool(self, name: str, *, arguments: dict[str, JsonValue]) -> object:
        return self.call_tool_with_deadline(name, arguments=arguments, timeout_seconds=None)

    def call_tool_with_deadline(
        self,
        name: str,
        *,
        arguments: dict[str, JsonValue],
        timeout_seconds: float | None = None,
    ) -> object:
        if self._closed:
            raise RuntimeError("MCP connection is closed")
        if name not in self.config.allow_tools:
            raise ValueError("MCP tool is not in the explicit allow_tools list")
        return self._submit("call", name, arguments, timeout_seconds)

    def _submit(
        self,
        operation: Literal["list", "call"],
        name: str,
        arguments: dict[str, JsonValue],
        timeout_seconds: float | None,
    ) -> object:
        loop, queue = self._loop, self._queue
        if self._closed or loop is None or queue is None or loop.is_closed():
            raise RuntimeError("MCP connection is closed or has not been opened")
        duration = min(
            self.config.timeout_seconds,
            timeout_seconds if timeout_seconds is not None else self.config.timeout_seconds,
        )
        if duration <= 0:
            raise TimeoutError("MCP call exceeded task deadline")
        result: Future[object] = Future()
        request = _Request(operation, name, arguments, time.monotonic() + duration, result)
        loop.call_soon_threadsafe(queue.put_nowait, request)
        try:
            return result.result(timeout=duration + 0.5)
        except TimeoutError:
            result.cancel()
            raise TimeoutError("MCP call exceeded task deadline") from None

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as error:
            if not self._ready.done():
                self._ready.set_exception(
                    OptionalDependencyError(extra="mcp", dependency="mcp>=2,<3")
                    if isinstance(error, ModuleNotFoundError)
                    else RuntimeError(f"MCP connection failed ({type(error).__name__})")
                )
        finally:
            if self._queue is not None:
                while not self._queue.empty():
                    request = self._queue.get_nowait()
                    if request is not None and not request.future.done():
                        request.future.set_exception(RuntimeError("MCP connection is closed"))

    async def _serve(self) -> None:
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        with anyio.fail_after(self.config.timeout_seconds) as startup:
            async with AsyncExitStack() as stack:
                if self.config.transport == "stdio":
                    assert self.config.command is not None
                    params = StdioServerParameters(
                        command=self.config.command,
                        args=list(self.config.args),
                        env=self._environment,
                    )
                    # Server stderr is untrusted and can contain inherited secrets.
                    errlog = stack.enter_context(open(os.devnull, "w"))  # noqa: SIM115
                    streams = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                else:
                    assert self.config.url is not None
                    import httpx2

                    http = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            timeout=self.config.timeout_seconds,
                            trust_env=False,
                            follow_redirects=False,
                            headers=self._headers,
                        )
                    )
                    streams = await stack.enter_async_context(
                        streamable_http_client(self.config.url, http_client=http)
                    )
                session = await stack.enter_async_context(
                    ClientSession(streams[0], streams[1], self.config.timeout_seconds)
                )
                await session.initialize()
                startup.deadline = float("inf")
                self._ready.set_result(None)
                while (request := await self._queue.get()) is not None:
                    if request.future.cancelled():
                        continue
                    try:
                        with anyio.fail_after(max(0, request.deadline - time.monotonic())):
                            result = await self._perform(session, request)
                    except TimeoutError:
                        failure: Exception = TimeoutError("MCP call exceeded task deadline")
                        if not request.future.done():
                            request.future.set_exception(failure)
                    except Exception as error:
                        if not request.future.done():
                            request.future.set_exception(
                                RuntimeError(f"MCP request failed ({type(error).__name__})")
                            )
                    else:
                        if not request.future.done():
                            request.future.set_result(result)

    async def _perform(self, session: ClientSession, request: _Request) -> object:
        if request.operation == "call":
            return await session.call_tool(request.name, arguments=request.arguments)
        from mcp.types import PaginatedRequestParams

        tools: list[Tool] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(100):
            response = await session.list_tools(
                params=PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            tools.extend(tool for tool in response.tools if tool.name in self.config.allow_tools)
            cursor = response.next_cursor
            if not cursor:
                return SimpleNamespace(tools=tuple(tools))
            if cursor in seen:
                raise ValueError("MCP tool discovery returned a repeated cursor")
            seen.add(cursor)
        raise ValueError("MCP tool discovery exceeded page limit")
