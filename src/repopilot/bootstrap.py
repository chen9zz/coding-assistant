"""Explicit dependency wiring shared by local interfaces."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from repopilot.application.orchestrator import DurableTaskOrchestrator
from repopilot.application.service import TaskApplicationService
from repopilot.domain.models import TaskSpec, ToolRequest
from repopilot.domain.ports import EventStore
from repopilot.infrastructure.artifacts import ArtifactBundleWriter
from repopilot.infrastructure.git import GitWorktreeManager
from repopilot.infrastructure.sqlite import SQLiteEventStore
from repopilot.tools.builtin import ToolRegistry, build_builtin_registry
from repopilot.tools.context_memory import ContextReadTool, MemorySearchTool
from repopilot.tools.gateway import SafeToolGateway
from repopilot.tools.policy import PolicyEngine
from repopilot.tools.project_memory import ProjectMemory
from repopilot.tools.redaction import Redactor
from repopilot.tools.repository import RepositoryMapper


@dataclass(frozen=True, slots=True)
class LocalContainer:
    root: Path
    store: EventStore
    service: TaskApplicationService


def build_local_container(
    root: Path,
    task_id_factory: Callable[[], str] | None = None,
    *,
    registry: ToolRegistry | None = None,
    allowed_mcp_tools: tuple[str, ...] = (),
    allowed_delegation_tools: tuple[str, ...] = (),
    secret_values: set[str] | None = None,
    store: EventStore | None = None,
    cancelled: Callable[[], bool] | None = None,
    skill_write_preview: Callable[[ToolRequest], str] | None = None,
    skill_write_root: Path | None = None,
    rag_cache_root: Path | None = None,
    memory_enabled: bool = False,
) -> LocalContainer:
    resolved_root = root.resolve(strict=False)
    resolved_root.mkdir(parents=True, exist_ok=True)
    store = store if store is not None else SQLiteEventStore(resolved_root / "state.db")
    secrets = {
        value
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN")
        if (value := os.environ.get(name))
    }
    secrets.update(secret_values or set())
    redactor = Redactor(secrets=secrets)
    actual_registry = registry or build_builtin_registry()
    actual_registry.register(ContextReadTool(store))
    if memory_enabled:
        actual_registry.register(
            MemorySearchTool(ProjectMemory(resolved_root / "memory", redactor), store)
        )

    def gateway_factory(worktree: Path, spec: TaskSpec) -> SafeToolGateway:
        if rag_cache_root is not None:
            for boundary in (worktree.resolve(), spec.repository.resolve()):
                if rag_cache_root.is_relative_to(boundary) or boundary.is_relative_to(
                    rag_cache_root
                ):
                    raise ValueError("RAG cache and repository/worktree must not overlap")
        if skill_write_root is not None:
            for boundary in (worktree.resolve(), spec.repository.resolve()):
                if skill_write_root.is_relative_to(boundary) or boundary.is_relative_to(
                    skill_write_root
                ):
                    raise ValueError("writable skills and repository/worktree must not overlap")
        return SafeToolGateway(
            store=store,
            registry=actual_registry,
            policy=PolicyEngine(
                worktree,
                allowed_commands={spec.test_command},
                allowed_mcp_tools=allowed_mcp_tools,
                allowed_delegation_tools=allowed_delegation_tools,
                allowed_paths=spec.allowed_paths,
                max_changed_files=spec.max_changed_files,
                skill_write_preview=skill_write_preview,
            ),
            redactor=redactor,
        )

    orchestrator = DurableTaskOrchestrator(
        store=store,
        worktrees=GitWorktreeManager(resolved_root / "worktrees"),
        mapper=RepositoryMapper(),
        gateway_factory=gateway_factory,
        redactor=redactor,
        cancelled=cancelled,
    )
    artifact_writer = ArtifactBundleWriter(resolved_root / "artifacts", store, redactor)
    service = TaskApplicationService(
        store,
        orchestrator,
        task_id_factory,
        artifact_writer=artifact_writer,
        redactor=redactor,
    )
    return LocalContainer(root=resolved_root, store=store, service=service)
