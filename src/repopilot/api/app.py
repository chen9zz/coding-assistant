"""FastAPI application factory backed by the public application service."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field, field_validator

from repopilot.agent.actions import AgentAction
from repopilot.agent.scripted import ScriptedRuntime
from repopilot.application.service import ApprovalRequestError, TaskApplicationService
from repopilot.domain.models import FrozenModel, TaskSnapshot, TaskSpec
from repopilot.infrastructure.sqlite import (
    ConcurrencyError,
    IdempotencyConflict,
    TaskAlreadyExistsError,
    TaskNotFoundError,
)
from repopilot.tools.gateway import OperationInProgress

from .sse import event_stream


class TaskCreateRequest(FrozenModel):
    repository: Path
    issue: Annotated[str, Field(min_length=1)]
    test_command: tuple[Annotated[str, Field(min_length=1)], ...]
    constraints: tuple[str, ...] = Field(
        default=(), description="Advisory natural-language model instructions."
    )
    allowed_paths: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    max_changed_files: Annotated[int | None, Field(ge=1, le=1000)] = None
    max_steps: Annotated[int, Field(ge=1, le=100)] = 24
    timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 300

    @field_validator("test_command", mode="before")
    @classmethod
    def reject_shell_string(cls, value: object) -> object:
        if isinstance(value, (str, bytes)):
            raise ValueError("test_command must be a JSON argument array")
        return value

    @field_validator("allowed_paths")
    @classmethod
    def reject_escaping_scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            normalized = pattern.replace("\\", "/")
            if "\x00" in pattern or normalized.startswith("/") or ".." in normalized.split("/"):
                raise ValueError("allowed path patterns must remain inside the worktree")
        return value

    def to_spec(self) -> TaskSpec:
        return TaskSpec(
            repository=self.repository,
            issue=self.issue,
            test_command=self.test_command,
            constraints=self.constraints,
            allowed_paths=self.allowed_paths,
            max_changed_files=self.max_changed_files,
            max_steps=self.max_steps,
            timeout_seconds=self.timeout_seconds,
        )


class TaskRunRequest(FrozenModel):
    actions: tuple[AgentAction, ...] = Field(min_length=1)


def create_app(service: TaskApplicationService) -> FastAPI:
    app = FastAPI(
        title="RepoPilot-R API",
        version="1.0.0",
        description="Reliable issue-to-verified-patch task API.",
    )

    @app.exception_handler(TaskNotFoundError)
    async def task_not_found(_request: Request, error: TaskNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(TaskAlreadyExistsError)
    async def task_exists(_request: Request, error: TaskAlreadyExistsError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(ConcurrencyError)
    @app.exception_handler(IdempotencyConflict)
    @app.exception_handler(ApprovalRequestError)
    @app.exception_handler(OperationInProgress)
    async def conflict(_request: Request, error: RuntimeError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        service.list_tasks()
        return {"status": "ready"}

    @app.post(
        "/v1/tasks",
        response_model=TaskSnapshot,
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(request: TaskCreateRequest) -> TaskSnapshot:
        return service.create(request.to_spec())

    @app.get("/v1/tasks", response_model=list[TaskSnapshot])
    def list_tasks() -> list[TaskSnapshot]:
        return service.list_tasks()

    @app.get("/v1/tasks/{task_id}", response_model=TaskSnapshot)
    def get_task(task_id: str) -> TaskSnapshot:
        return service.get(task_id)

    @app.post("/v1/tasks/{task_id}/resume", response_model=TaskSnapshot)
    def resume_task(task_id: str, request: TaskRunRequest) -> TaskSnapshot:
        return service.resume(task_id, ScriptedRuntime(request.actions))

    @app.post("/v1/tasks/{task_id}/approvals/{request_id}", response_model=TaskSnapshot)
    def approve_task(task_id: str, request_id: str) -> TaskSnapshot:
        return service.approve(task_id, request_id)

    @app.get("/v1/tasks/{task_id}/events")
    def task_events(
        task_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None,
    ) -> StreamingResponse:
        cursor = max(after, last_event_id or 0)
        events = service.events(task_id, after=cursor)
        return StreamingResponse(
            event_stream(events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/tasks/{task_id}/artifacts")
    def task_artifacts(task_id: str) -> dict[str, object]:
        task = service.get(task_id)
        bundle = service.artifacts(task_id)
        return {
            "task_id": task.task_id,
            "state": task.state.value,
            "worktree": str(task.worktree) if task.worktree else None,
            "event_count": len(service.events(task_id)),
            **bundle.model_dump(mode="json"),
        }

    return app
