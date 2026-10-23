"""Browser and terminal control API for an operator-configured local workspace."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from repopilot.application.jobs import JobManager
from repopilot.application.service import TaskApplicationService
from repopilot.configuration import load_config
from repopilot.domain.models import FrozenModel

from .app import create_app

_JOB_FIELDS = (
    "job_id",
    "task_id",
    "kind",
    "state",
    "parent_job_id",
    "issue",
    "summary",
    "error",
    "cancel_requested",
    "created_at",
    "updated_at",
)


class JobCreateRequest(FrozenModel):
    issue: Annotated[str, Field(min_length=1, max_length=16_000)]
    kind: Literal["task", "analysis"] = "task"


class ApprovalRequest(FrozenModel):
    operation_id: Annotated[str, Field(min_length=1, max_length=1024)]


def _same_origin(origin: str, request: Request) -> bool:
    try:
        parsed = urlsplit(origin)
        expected = urlsplit(str(request.url))
        return (
            parsed.scheme in {"http", "https"}
            and parsed.scheme == expected.scheme
            and parsed.hostname == expected.hostname
            and (parsed.port or (443 if parsed.scheme == "https" else 80))
            == (expected.port or (443 if expected.scheme == "https" else 80))
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def create_control_app(manager: JobManager) -> FastAPI:
    """Expose issue-only commands; repository, credentials and commands stay server-side."""
    resources = ExitStack()
    container = resources.enter_context(manager.container())
    service: TaskApplicationService = container.service
    app = create_app(service)
    app.title = "ForgePilot local control"
    app.description = "Local single-user task control for an operator-configured repository."
    session_token = token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            resources.close()

    app.router.lifespan_context = lifespan
    # The generic task endpoints accept arbitrary repositories/actions. They are intentionally
    # absent from this fixed-workspace interface, including its generated OpenAPI document.
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            isinstance(route, APIRoute)
            and route.path.startswith("/v1/tasks")
            and "POST" in (route.methods or set())
        )
    ]
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def guard(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        origins = request.headers.getlist("origin")
        if (
            request.headers.get("sec-fetch-site", "").lower() == "cross-site"
            or len(origins) > 1
            or (origins and not _same_origin(origins[0], request))
        ):
            return JSONResponse(status_code=403, content={"detail": "Same-origin access required"})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            tokens = request.headers.getlist("x-forgepilot-token")
            if len(tokens) != 1 or not compare_digest(tokens[0].encode(), session_token.encode()):
                return JSONResponse(
                    status_code=403, content={"detail": "Valid session token required"}
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError) -> JSONResponse:
        # Pydantic's default response echoes rejected input, including accidentally pasted keys.
        return JSONResponse(status_code=422, content={"detail": "Invalid control request"})

    @app.exception_handler(KeyError)
    async def unknown_job(_request: Request, _error: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Job not found"})

    @app.exception_handler(ValueError)
    async def rejected(_request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": manager._redactor().redact(str(error))}
        )

    def public_job(job: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], manager._redactor().redact({key: job.get(key) for key in _JOB_FIELDS})
        )

    @app.get("/v1/control")
    def control() -> dict[str, Any]:
        config = load_config(manager.config) if manager.config else None
        public = {
            "name": "ForgePilot",
            "mode": "demo" if manager.demo else "online",
            "repository": str(manager.repository)
            if manager.repository
            else "Built-in demo fixture",
            "test_command": list(manager.test_command),
            "model": config.provider.model if config else "scripted-offline",
            "storage": config.storage.backend if config else "sqlite",
        }
        safe = cast(dict[str, Any], manager._redactor().redact(public))
        safe["csrf_token"] = session_token
        return safe

    @app.get("/v1/jobs")
    def jobs() -> list[dict[str, Any]]:
        return [public_job(job) for job in manager.list()]

    @app.post("/v1/jobs", status_code=202)
    def submit(request: JobCreateRequest) -> dict[str, Any]:
        return public_job(manager.submit(request.issue, kind=request.kind))

    @app.get("/v1/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        return public_job(manager.get(job_id))

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict[str, Any]:
        return public_job(manager.cancel(job_id))

    @app.post("/v1/jobs/{job_id}/resume")
    def resume(job_id: str) -> dict[str, Any]:
        return public_job(manager.resume(job_id))

    @app.post("/v1/jobs/{job_id}/approve")
    def approve(job_id: str, request: ApprovalRequest) -> dict[str, Any]:
        return public_job(manager.approve(job_id, request.operation_id))

    @app.get("/v1/tasks/{task_id}/artifacts/{kind}/content")
    def artifact_content(
        task_id: str,
        kind: Literal["patch", "test_report", "trajectory", "pr_draft"],
    ) -> dict[str, Any]:
        bundle = service.artifacts(task_id)
        artifacts = {
            "patch": bundle.patch,
            "test_report": bundle.test_report,
            "trajectory": bundle.trajectory,
            "pr_draft": bundle.pr_draft,
        }
        path = artifacts[kind].path.resolve()
        if not path.is_relative_to(bundle.root.resolve()):
            return {"content": "Artifact unavailable", "truncated": False}
        content = cast(str, manager._redactor().redact(path.read_text(encoding="utf-8")))
        return {"content": content[:64_000], "truncated": len(content) > 64_000}

    web_dist = Path(__file__).resolve().parents[3] / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app
