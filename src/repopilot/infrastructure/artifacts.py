"""Atomic release-evidence bundle generation from already persisted results."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from repopilot.domain.models import (
    Artifact,
    ArtifactBundle,
    TaskEvent,
    TaskSnapshot,
    TaskState,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from repopilot.domain.ports import EventStore
from repopilot.infrastructure.process import ProcessRunner
from repopilot.tools.builtin import GitDiffTool
from repopilot.tools.redaction import Redactor


class ArtifactBundleWriter:
    """Materialize the final patch, test evidence, trajectory, and PR narrative."""

    def __init__(
        self,
        root: Path,
        store: EventStore,
        redactor: Redactor,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.root = root
        self.store = store
        self.redactor = redactor
        self.runner = runner or ProcessRunner()

    def write(self, task: TaskSnapshot, events: Sequence[TaskEvent]) -> ArtifactBundle:
        if task.state is not TaskState.SUCCEEDED:
            raise ValueError("release artifacts require a SUCCEEDED task")
        verification = self._required_operation(task.task_id, f"verify-{task.task_id}")
        diff_result = self._required_operation(task.task_id, f"diff-{task.task_id}")
        if verification.status is not ToolStatus.SUCCEEDED:
            raise ValueError("release artifacts require successful verification evidence")
        if diff_result.status is not ToolStatus.SUCCEEDED:
            raise ValueError("release artifacts require successful diff evidence")

        destination = self.root / task.task_id
        destination.mkdir(parents=True, exist_ok=True)
        patch_text = self._patch_text(task, diff_result)
        report_payload = cast(dict[str, object], self.redactor.redact(
            verification.model_dump(mode="json")
        ))
        report_text = json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True)
        trajectory_text = "".join(self._event_line(event) for event in events)
        changed_paths = self._reviewed_paths(diff_result)
        pr_text = self._pr_draft(task, verification, changed_paths)

        patch_path = destination / "change.patch"
        report_path = destination / "test-report.json"
        trajectory_path = destination / "trajectory.jsonl"
        pr_path = destination / "pull-request.md"
        for path, content in (
            (patch_path, patch_text),
            (report_path, f"{report_text}\n"),
            (trajectory_path, trajectory_text),
            (pr_path, pr_text),
        ):
            self._atomic_write(path, content)

        return ArtifactBundle(
            root=destination,
            patch=self._artifact(
                "patch",
                patch_path,
                {
                    "media_type": "text/x-diff",
                    "changed_paths": changed_paths,
                    "sensitivity": "access-controlled-exact",
                },
            ),
            test_report=self._artifact(
                "test_report",
                report_path,
                {
                    "media_type": "application/json",
                    "status": verification.status.value,
                    "operation_id": verification.operation_id,
                },
            ),
            trajectory=self._artifact(
                "trajectory",
                trajectory_path,
                {"media_type": "application/x-ndjson", "event_count": len(events)},
            ),
            pr_draft=self._artifact(
                "pr_draft",
                pr_path,
                {"media_type": "text/markdown"},
            ),
        )

    def _required_operation(self, task_id: str, operation_id: str) -> ToolResult:
        result = self.store.get_operation(task_id, operation_id)
        if result is None:
            raise ValueError(f"missing persisted operation evidence: {operation_id}")
        return result

    def _patch_text(self, task: TaskSnapshot, result: ToolResult) -> str:
        if not isinstance(result.output, dict):
            raise ValueError("diff result output is not a JSON object")
        expected_digest = result.output.get("diff_sha256")
        if not isinstance(expected_digest, str):
            raise ValueError("diff result does not contain an integrity digest")
        if task.worktree is None:
            raise ValueError("task does not have a worktree")
        exact = GitDiffTool(self.runner).execute(
            task.worktree,
            ToolRequest(
                operation_id=f"artifact-diff-{task.task_id}",
                name="git_diff",
                arguments={},
            ),
        )
        if exact.status is not ToolStatus.SUCCEEDED or exact.truncated:
            raise ValueError("unable to reproduce the complete reviewed patch")
        if not isinstance(exact.output, dict):
            raise ValueError("reproduced diff output is incomplete")
        exact_diff = exact.output.get("diff")
        exact_paths = exact.output.get("changed_paths")
        if not isinstance(exact_diff, str) or not isinstance(exact_paths, list):
            raise ValueError("reproduced diff output is incomplete")
        actual_digest = hashlib.sha256(exact_diff.encode()).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError("worktree changed after review; exact patch digest mismatch")
        if tuple(exact_paths) != self._reviewed_paths(result):
            raise ValueError("worktree changed after review; changed paths mismatch")
        return exact_diff

    def _pr_draft(
        self,
        task: TaskSnapshot,
        verification: ToolResult,
        changed_paths: tuple[str, ...],
    ) -> str:
        issue = cast(str, self.redactor.redact(task.spec.issue))
        paths = "\n".join(f"- `{path}`" for path in changed_paths) or "- No paths changed"
        command = " ".join(task.spec.test_command)
        return (
            f"# {issue}\n\n"
            "## Summary\n\n"
            f"ForgePilot produced and reviewed a bounded patch for: {issue}\n\n"
            "## Changed paths\n\n"
            f"{paths}\n\n"
            "## Verification\n\n"
            f"- Command: `{command}`\n"
            f"- Persisted status: `{verification.status.value}`\n"
            f"- Operation: `{verification.operation_id}`\n\n"
            "## Reliability evidence\n\n"
            "- Tool effects passed through the local fail-closed policy gateway.\n"
            "- The patch and test result were persisted before this draft was generated.\n"
            "- No remote push or pull request creation was performed.\n"
        )

    @staticmethod
    def _reviewed_paths(result: ToolResult) -> tuple[str, ...]:
        if not isinstance(result.output, dict):
            raise ValueError("diff result output is not a JSON object")
        paths = result.output.get("changed_paths")
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise ValueError("diff result does not contain complete changed paths")
        return tuple(sorted(cast(str, path) for path in paths))

    def _event_line(self, event: TaskEvent) -> str:
        payload = self.redactor.redact(event.model_dump(mode="json"))
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return f"{serialized}\n"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        encoded = content.encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _artifact(kind: str, path: Path, metadata: dict[str, Any]) -> Artifact:
        data = path.read_bytes()
        return Artifact(
            kind=kind,
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            metadata=metadata,
        )
