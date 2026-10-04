"""Immutable value objects for task lifecycle, tools, policy, and artifacts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
NonEmptyText = Annotated[str, Field(min_length=1)]


def utc_now() -> datetime:
    """Return an aware UTC timestamp for durable records."""
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    """Base for domain values that must not change after validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskState(StrEnum):
    CREATED = "CREATED"
    INDEXING = "INDEXING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    RETRYING = "RETRYING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ToolStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


class PolicyEffect(StrEnum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


class TaskSpec(FrozenModel):
    """Validated input for one isolated repository task."""

    repository: Path
    issue: NonEmptyText
    test_command: tuple[NonEmptyText, ...]
    constraints: tuple[str, ...] = Field(
        default=(),
        description="Advisory natural-language instructions supplied to the model.",
    )
    allowed_paths: tuple[NonEmptyText, ...] = Field(
        default=(),
        description="Host-enforced relative glob patterns for changed files.",
    )
    max_changed_files: Annotated[int | None, Field(ge=1, le=1000)] = None
    max_steps: Annotated[int, Field(ge=1, le=100)] = 24
    timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 300

    @field_validator("test_command", mode="before")
    @classmethod
    def reject_shell_string(cls, value: object) -> object:
        if isinstance(value, (str, bytes)):
            raise ValueError("test_command must be an argument array, not a shell string")
        return value

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            normalized = pattern.replace("\\", "/")
            if "\x00" in pattern or normalized.startswith("/"):
                raise ValueError("allowed path patterns must be relative to the worktree")
            if ".." in normalized.split("/"):
                raise ValueError("allowed path patterns cannot traverse above the worktree")
        return value


class PlanStep(FrozenModel):
    step_id: NonEmptyText
    objective: NonEmptyText
    affected_paths: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()


class AgentPlan(FrozenModel):
    summary: NonEmptyText
    steps: tuple[PlanStep, ...]
    risks: tuple[str, ...] = ()


class TaskSnapshot(FrozenModel):
    task_id: NonEmptyText
    spec: TaskSpec
    state: TaskState = TaskState.CREATED
    sequence: Annotated[int, Field(ge=0)] = 0
    worktree: Path | None = None
    plan: AgentPlan | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TaskEvent(FrozenModel):
    event_id: NonEmptyText
    task_id: NonEmptyText
    sequence: Annotated[int, Field(ge=1)]
    event_type: NonEmptyText
    from_state: TaskState
    to_state: TaskState
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)
    schema_version: Annotated[int, Field(ge=1)] = 1
    operation_id: str | None = None
    causation_id: str | None = None


class ToolRequest(FrozenModel):
    operation_id: NonEmptyText
    name: NonEmptyText
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def arguments_digest(self) -> str:
        canonical = json.dumps(
            self.arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolResult(FrozenModel):
    operation_id: NonEmptyText
    tool_name: NonEmptyText
    status: ToolStatus
    output: JsonValue = None
    error: str | None = None
    duration_ms: Annotated[int, Field(ge=0)] = 0
    truncated: bool = False


class PolicyDecision(FrozenModel):
    effect: PolicyEffect
    reason: NonEmptyText
    request_digest: NonEmptyText


class Artifact(FrozenModel):
    kind: NonEmptyText
    path: Path
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactBundle(FrozenModel):
    """The four reviewable files emitted only after deterministic success."""

    root: Path
    patch: Artifact
    test_report: Artifact
    trajectory: Artifact
    pr_draft: Artifact
