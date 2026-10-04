from pathlib import Path

import pytest

from repopilot.domain.models import PolicyEffect, ToolRequest
from repopilot.tools.policy import PathPolicyError, PolicyEngine, resolve_contained


def request(name: str, **arguments: object) -> ToolRequest:
    return ToolRequest(operation_id=f"op-{name}", name=name, arguments=arguments)  # type: ignore[arg-type]


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("value = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private\n")
    (root / "link-outside").symlink_to(outside, target_is_directory=True)
    return root


@pytest.fixture
def policy(worktree: Path) -> PolicyEngine:
    return PolicyEngine(
        worktree,
        allowed_commands={("python", "-m", "pytest")},
    )


@pytest.mark.parametrize(
    "path", ["../outside/secret.txt", "/etc/passwd", "link-outside/secret.txt"]
)
def test_read_path_escape_is_denied(policy: PolicyEngine, path: str) -> None:
    """Removing resolved containment would expose host files to repository instructions."""
    decision = policy.decide(request("read_file", path=path))
    assert decision.effect is PolicyEffect.DENY
    assert "worktree" in decision.reason.lower()


def test_resolve_contained_accepts_an_internal_path(worktree: Path) -> None:
    """Overly broad denial would prevent the agent from reading its own repository."""
    assert resolve_contained(worktree, "src/app.py") == (worktree / "src/app.py").resolve()


def test_resolve_contained_rejects_nul_byte(worktree: Path) -> None:
    """Allowing a NUL path can cause validation/execution parsers to disagree."""
    with pytest.raises(PathPolicyError):
        resolve_contained(worktree, "src/app.py\x00.txt")


def test_exact_configured_test_command_is_allowed(policy: PolicyEngine) -> None:
    """Dropping exact allowlist matching would block the required verification step."""
    decision = policy.decide(
        request("run_command", argv=["python", "-m", "pytest"])
    )
    assert decision.effect is PolicyEffect.ALLOW


def test_unlisted_non_destructive_command_requires_approval(policy: PolicyEngine) -> None:
    """Auto-allowing an unreviewed command would expand model privileges silently."""
    decision = policy.decide(request("run_command", argv=["npm", "test"]))
    assert decision.effect is PolicyEffect.ASK


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "src"],
        ["git", "push", "origin", "main"],
        ["curl", "https://example.com"],
        ["python", "-c", "print(1); rm -rf src"],
    ],
)
def test_destructive_network_or_shell_like_command_is_denied(
    policy: PolicyEngine, argv: list[str]
) -> None:
    """Weak classification would permit destructive or network side effects."""
    decision = policy.decide(request("run_command", argv=argv))
    assert decision.effect is PolicyEffect.DENY


def test_unknown_tool_is_denied_by_default(policy: PolicyEngine) -> None:
    """Fail-open discovery would let MCP or model-invented tools bypass review."""
    decision = policy.decide(request("invented_admin_tool"))
    assert decision.effect is PolicyEffect.DENY


def test_diff_and_repository_map_are_read_only_allowed(policy: PolicyEngine) -> None:
    """Misclassifying read-only evidence tools would create needless approvals."""
    assert policy.decide(request("git_diff")).effect is PolicyEffect.ALLOW
    assert policy.decide(request("repo_map")).effect is PolicyEffect.ALLOW


def test_patch_outside_structured_allowed_paths_is_denied(worktree: Path) -> None:
    """Natural-language scope is advisory; host path scope must be machine-enforced."""
    policy = PolicyEngine(worktree, allowed_paths=("src/**",), max_changed_files=2)
    patch = """diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1 +1 @@
-old
+new
"""
    decision = policy.decide(request("apply_patch", patch=patch))
    assert decision.effect is PolicyEffect.DENY
    assert "allowed path" in decision.reason


def test_patch_exceeding_structured_file_limit_is_denied(worktree: Path) -> None:
    """A model cannot evade the maximum changed-file count through prompt interpretation."""
    policy = PolicyEngine(worktree, allowed_paths=("**",), max_changed_files=1)
    patch = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-old
+new
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""
    decision = policy.decide(request("apply_patch", patch=patch))
    assert decision.effect is PolicyEffect.DENY
    assert "file limit" in decision.reason
