"""Local official-SDK server for transport integration tests (no external services)."""

import asyncio
import os
import sys

from mcp.server import MCPServer

server = MCPServer("transport-fixture", log_level="ERROR")
calls = 0
cancellations = 0


@server.tool(name="Echo.Value")
def echo(value: str) -> dict[str, object]:
    global calls
    calls += 1
    return {
        "value": value,
        "calls": calls,
        "pid": os.getpid(),
        "mapped": os.environ.get("FIXTURE_MAPPED"),
        "unmapped": os.environ.get("REPOTEST_PRIVATE"),
        "cancellations": cancellations,
    }


@server.tool()
async def slow() -> str:
    global cancellations
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        cancellations += 1
        raise
    return "too late"


@server.tool()
def hidden() -> str:
    return "must not run"


@server.tool()
def broken() -> str:
    raise ValueError("fixture failure")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "hang":
        from pathlib import Path

        Path(sys.argv[2]).write_text(str(os.getpid()))
        asyncio.run(asyncio.sleep(60))
    elif len(sys.argv) > 1:
        if len(sys.argv) > 2 and sys.argv[2] == "auth":
            import uvicorn
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.responses import JSONResponse

            class RequireBearer(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    if request.headers.get("authorization") != "Bearer fixture-local-token":
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                    return await call_next(request)

            app = server.streamable_http_app()
            app.add_middleware(RequireBearer)
            uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="error")
        else:
            server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
    else:
        server.run("stdio")
