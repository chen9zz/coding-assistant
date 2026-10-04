import subprocess
from pathlib import Path

import pytest

from repopilot.infrastructure.git import (
    DirtyWorktreeError,
    GitRepositoryError,
    GitWorktreeManager,
)


def git(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


@pytest.fixture
def initialized_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.email", "tests@example.com", cwd=repo)
    git("config", "user.name", "RepoPilot Tests", cwd=repo)
    (repo / "app.py").write_text("original\n")
    git("add", "app.py", cwd=repo)
    git("commit", "-m", "initial", cwd=repo)
    return repo


def test_worktree_change_does_not_modify_source(initialized_repo: Path, tmp_path: Path) -> None:
    """Editing the source checkout would make agent failures destructive."""
    manager = GitWorktreeManager(tmp_path / "worktrees")
    worktree = manager.create(initialized_repo, "task-1")

    (worktree.path / "app.py").write_text("changed\n")

    assert (initialized_repo / "app.py").read_text() == "original\n"
    assert "app.py" in manager.diff(worktree)


def test_remove_refuses_to_discard_dirty_worktree(initialized_repo: Path, tmp_path: Path) -> None:
    """Automatic cleanup must not erase a patch that needs human review."""
    manager = GitWorktreeManager(tmp_path / "worktrees")
    worktree = manager.create(initialized_repo, "task-2")
    (worktree.path / "app.py").write_text("valuable patch\n")

    with pytest.raises(DirtyWorktreeError):
        manager.remove(worktree)

    assert worktree.path.exists()
    assert (worktree.path / "app.py").read_text() == "valuable patch\n"


def test_create_adopts_the_same_registered_worktree_after_restart(
    initialized_repo: Path,
    tmp_path: Path,
) -> None:
    """A crash after `git worktree add` must not turn a CREATED task into failure."""
    first_manager = GitWorktreeManager(tmp_path / "worktrees")
    created = first_manager.create(initialized_repo, "task-restart")

    reopened = GitWorktreeManager(tmp_path / "worktrees").create(
        initialized_repo,
        "task-restart",
    )

    assert reopened == created
    assert reopened.path.exists()
    assert git("branch", "--show-current", cwd=reopened.path).strip() == reopened.branch


def test_non_repository_is_rejected(tmp_path: Path) -> None:
    """Guessing repository state could create a worktree from the wrong checkout."""
    source = tmp_path / "not-a-repo"
    source.mkdir()
    manager = GitWorktreeManager(tmp_path / "worktrees")

    with pytest.raises(GitRepositoryError):
        manager.create(source, "task-3")
