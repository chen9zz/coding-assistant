"""Git worktree isolation and evidence collection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from repopilot.infrastructure.process import ProcessResult, ProcessRunner


class GitRepositoryError(RuntimeError):
    """Raised when Git cannot prove the requested repository boundary."""


class WorktreeAlreadyExistsError(GitRepositoryError):
    """Raised when a task worktree or branch identity is already occupied."""


class DirtyWorktreeError(GitRepositoryError):
    """Raised before cleanup would discard uncommitted task evidence."""


@dataclass(frozen=True, slots=True)
class Worktree:
    task_id: str
    source: Path
    path: Path
    branch: str


class GitWorktreeManager:
    """Create one branch/worktree boundary per agent task."""

    def __init__(self, base_directory: Path, runner: ProcessRunner | None = None) -> None:
        self.base_directory = base_directory
        self.runner = runner or ProcessRunner()

    def create(self, repository: Path, task_id: str) -> Worktree:
        source = repository.resolve(strict=True)
        top_level = self._git(source, "rev-parse", "--show-toplevel").stdout.strip()
        if not top_level or Path(top_level).resolve() != source:
            raise GitRepositoryError("repository must be an existing Git top-level directory")
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-.")
        if not safe_id:
            raise GitRepositoryError("task identity has no safe branch characters")
        path = (self.base_directory / safe_id).resolve(strict=False)
        branch = f"repopilot/{safe_id}"
        if path.exists():
            return self._adopt_existing(source, path, branch, task_id)
        self.base_directory.mkdir(parents=True, exist_ok=True)
        result = self._git(
            source,
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            "HEAD",
            check=False,
        )
        if result.exit_code != 0:
            raise WorktreeAlreadyExistsError(result.stderr.strip() or "git worktree add failed")
        return Worktree(task_id=task_id, source=source, path=path, branch=branch)

    def _adopt_existing(
        self,
        source: Path,
        path: Path,
        branch: str,
        task_id: str,
    ) -> Worktree:
        top_level = self._git(path, "rev-parse", "--show-toplevel", check=False)
        active_branch = self._git(path, "branch", "--show-current", check=False)
        source_common = self._git(source, "rev-parse", "--git-common-dir", check=False)
        worktree_common = self._git(path, "rev-parse", "--git-common-dir", check=False)
        if any(
            result.exit_code != 0
            for result in (top_level, active_branch, source_common, worktree_common)
        ):
            raise WorktreeAlreadyExistsError(f"unrecognized worktree path already exists: {path}")
        source_git = self._resolved_git_path(source, source_common.stdout.strip())
        worktree_git = self._resolved_git_path(path, worktree_common.stdout.strip())
        if (
            Path(top_level.stdout.strip()).resolve() != path
            or active_branch.stdout.strip() != branch
            or source_git != worktree_git
        ):
            raise WorktreeAlreadyExistsError(f"worktree path belongs to another task: {path}")
        return Worktree(task_id=task_id, source=source, path=path, branch=branch)

    @staticmethod
    def _resolved_git_path(cwd: Path, raw: str) -> Path:
        candidate = Path(raw)
        return (candidate if candidate.is_absolute() else cwd / candidate).resolve()

    def diff(self, worktree: Worktree) -> str:
        return self._git(
            worktree.path,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--",
        ).stdout

    def remove(self, worktree: Worktree) -> None:
        status = self._git(worktree.path, "status", "--porcelain=v1").stdout
        if status.strip():
            raise DirtyWorktreeError(
                f"worktree contains uncommitted evidence and was preserved: {worktree.path}"
            )
        self._git(worktree.source, "worktree", "remove", str(worktree.path))

    def _git(
        self,
        cwd: Path,
        *arguments: str,
        check: bool = True,
    ) -> ProcessResult:
        result = self.runner.run(
            ["git", *arguments],
            cwd=cwd,
            timeout_seconds=15,
            max_output_bytes=1_000_000,
        )
        if check and result.exit_code != 0:
            raise GitRepositoryError(result.stderr.strip() or "Git command failed")
        return result
