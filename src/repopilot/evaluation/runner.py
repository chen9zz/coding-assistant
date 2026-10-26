"""Execute fixture cases through public services and grade real artifacts."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from repopilot.application.orchestrator import (
    CrashAfterOperation,
    DurableTaskOrchestrator,
    InjectedCrash,
)
from repopilot.application.service import TaskApplicationService
from repopilot.domain.models import TaskSpec, TaskState, ToolRequest, ToolStatus
from repopilot.infrastructure.artifacts import ArtifactBundleWriter
from repopilot.infrastructure.git import GitWorktreeManager
from repopilot.infrastructure.process import ProcessRunner
from repopilot.infrastructure.sqlite import SQLiteEventStore
from repopilot.tools.builtin import GitDiffTool, build_builtin_registry
from repopilot.tools.gateway import SafeToolGateway
from repopilot.tools.policy import PolicyEngine
from repopilot.tools.redaction import Redactor
from repopilot.tools.repository import RepositoryMapper

from .cases import FixtureCase
from .graders import CaseResult, EvaluationReport, write_report
from .verification import verify_hidden


class EvaluationRunner:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.runner = ProcessRunner()

    def run(self, cases: tuple[FixtureCase, ...]) -> EvaluationReport:
        started = time.monotonic()
        self.workspace.mkdir(parents=True, exist_ok=False)
        results = tuple(self._run_case(case) for case in cases)
        report = EvaluationReport(
            cases=results,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )
        write_report(report, self.workspace / "evaluation-results")
        return report

    def _run_case(self, case: FixtureCase) -> CaseResult:
        started = time.monotonic()
        case_workspace = self.workspace / "cases" / case.case_id
        case_workspace.mkdir(parents=True)
        source = case.prepare_repository(case_workspace / "source")
        database = case_workspace / "state.db"
        worktrees = case_workspace / "worktrees"
        service, store = self._build_service(database, worktrees, case.case_id)
        task = service.create(case.task_spec(source))
        recovery_attempts = 0
        if case.definition.inject_crash:
            tool_action = next(action for action in case.actions if action.operation_id)
            try:
                service.run(
                    task.task_id,
                    case.runtime(),
                    fault=CrashAfterOperation(tool_action.operation_id or ""),
                )
            except InjectedCrash:
                recovery_attempts = 1
            service, store = self._build_service(database, worktrees, case.case_id)
            snapshot = service.resume(task.task_id, case.runtime())
        else:
            snapshot = service.run(task.task_id, case.runtime())

        if snapshot.state is TaskState.SUCCEEDED:
            service.artifacts(task.task_id)

        assert snapshot.worktree is not None
        patch_result = GitDiffTool(self.runner).execute(
            snapshot.worktree,
            ToolRequest(
                operation_id=f"evaluation-diff-{task.task_id}",
                name="git_diff",
                arguments={},
            ),
        )
        hidden_passed = verify_hidden(
            case, snapshot.worktree, case_workspace / "hidden-results.xml"
        )
        patch_output = patch_result.output if isinstance(patch_result.output, dict) else {}
        raw_paths = patch_output.get("changed_paths")
        changed_paths = (
            tuple(sorted(path for path in raw_paths if isinstance(path, str)))
            if isinstance(raw_paths, list)
            else ()
        )
        scope_ok = changed_paths == tuple(sorted(case.definition.expected_changed_paths))
        events = store.events(task.task_id)
        denied = sum(event.event_type == "tool.denied" for event in events)
        policy_violations = denied
        tests_passed = hidden_passed
        resolved = (
            snapshot.state is TaskState.SUCCEEDED
            and patch_result.status is ToolStatus.SUCCEEDED
            and tests_passed
            and scope_ok
        )
        return CaseResult(
            case_id=case.case_id,
            state=snapshot.state,
            resolved=resolved,
            tests_passed=tests_passed,
            scope_ok=scope_ok,
            changed_paths=changed_paths,
            patch_sha256=str(patch_output.get("diff_sha256", hashlib.sha256(b"").hexdigest())),
            policy_violations=policy_violations,
            denied_operations=denied,
            tool_steps=sum(event.event_type.startswith("tool.") for event in events),
            recovery_attempts=recovery_attempts,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )

    @staticmethod
    def _build_service(
        database: Path,
        worktrees: Path,
        task_id: str,
    ) -> tuple[TaskApplicationService, SQLiteEventStore]:
        store = SQLiteEventStore(database)

        redactor = Redactor()

        def gateway_factory(worktree: Path, spec: TaskSpec) -> SafeToolGateway:
            return SafeToolGateway(
                store=store,
                registry=build_builtin_registry(),
                policy=PolicyEngine(
                    worktree,
                    allowed_commands={spec.test_command},
                    allowed_paths=spec.allowed_paths,
                    max_changed_files=spec.max_changed_files,
                ),
                redactor=redactor,
            )

        orchestrator = DurableTaskOrchestrator(
            store=store,
            worktrees=GitWorktreeManager(worktrees),
            mapper=RepositoryMapper(),
            gateway_factory=gateway_factory,
            redactor=redactor,
        )
        return TaskApplicationService(
            store,
            orchestrator,
            lambda: task_id,
            artifact_writer=ArtifactBundleWriter(database.parent / "artifacts", store, redactor),
            redactor=redactor,
        ), store
