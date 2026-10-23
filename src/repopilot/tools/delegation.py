"""Bounded background analysis tools; never grant children write or delegation tools."""

from pathlib import Path
from typing import Any

from repopilot.application.jobs import JobManager
from repopilot.domain.models import ToolRequest, ToolResult, ToolStatus


class DelegationTool:
    def __init__(self, name: str, manager: JobManager, parent_job_id: str) -> None:
        self.name = name
        self.manager = manager
        self.parent_job_id = parent_job_id
        spawn = name == "agent_spawn"
        self.description = (
            "Start a read-only analysis subagent with independent context. Maximum 4 children. "
            "It cannot edit files, run commands, use MCP or delegate."
            if spawn
            else "Inspect or cancel only this task's own analysis child. "
            "Results may still be running; continue other work before polling again."
        )
        field = "issue" if spawn else "job_id"
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {field: {"type": "string"}},
            "required": [field],
            "additionalProperties": False,
        }

    def execute(
        self,
        worktree: Path,
        request: ToolRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        try:
            if timeout_seconds is not None and timeout_seconds <= 0:
                raise ValueError("task deadline exhausted")
            if self.name == "agent_spawn":
                issue = request.arguments.get("issue")
                if not isinstance(issue, str):
                    raise ValueError("issue must be text")
                job = self.manager.submit(
                    issue,
                    kind="analysis",
                    parent_job_id=self.parent_job_id,
                    analysis_repository=worktree,
                )
            else:
                job_id = request.arguments.get("job_id")
                if not isinstance(job_id, str):
                    raise ValueError("job_id must be text")
                job = self.manager.get(job_id)
                if job["parent_job_id"] != self.parent_job_id:
                    raise ValueError("job does not belong to this parent")
                if self.name == "agent_cancel":
                    job = self.manager.cancel(job_id)
                elif self.name not in {"agent_status", "agent_result"}:
                    raise ValueError("unknown delegation tool")
            return ToolResult(
                operation_id=request.operation_id,
                tool_name=request.name,
                status=ToolStatus.SUCCEEDED,
                output=job,
            )
        except (ValueError, KeyError, RuntimeError) as error:
            return ToolResult(
                operation_id=request.operation_id,
                tool_name=request.name,
                status=ToolStatus.FAILED,
                output={"error": str(error)},
            )


def delegation_tools(manager: JobManager, parent_job_id: str) -> tuple[DelegationTool, ...]:
    return tuple(
        DelegationTool(name, manager, parent_job_id)
        for name in ("agent_spawn", "agent_status", "agent_result", "agent_cancel")
    )
