"""Fail-closed policy decisions for every model-requested tool."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from fnmatch import fnmatchcase
from pathlib import Path
from typing import cast

from repopilot.domain.models import PolicyDecision, PolicyEffect, ToolRequest


class PathPolicyError(ValueError):
    """Raised when a requested path crosses the worktree boundary."""


def resolve_contained(worktree: Path, requested: str) -> Path:
    """Resolve a relative path and prove it remains under the worktree."""
    if "\x00" in requested:
        raise PathPolicyError("path contains a NUL byte")
    candidate = Path(requested)
    if candidate.is_absolute():
        raise PathPolicyError("absolute paths are outside the worktree boundary")
    root = worktree.resolve(strict=True)
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise PathPolicyError("resolved path is outside the worktree boundary") from error
    return resolved


def extract_diff_paths(patch: str) -> tuple[str, ...]:
    """Return normalized paths named by unified-diff file headers."""
    paths: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw_path = line[4:].split("\t", maxsplit=1)[0]
        if raw_path == "/dev/null":
            continue
        if raw_path.startswith(("a/", "b/")):
            raw_path = raw_path[2:]
        if raw_path:
            paths.add(raw_path)
    return tuple(sorted(paths))


def change_scope_violation(
    changed_paths: tuple[str, ...],
    *,
    allowed_paths: tuple[str, ...] = (),
    max_changed_files: int | None = None,
) -> str | None:
    """Explain the first structured change-scope violation, if any."""
    if max_changed_files is not None and len(changed_paths) > max_changed_files:
        return f"patch exceeds the host-enforced changed-file limit of {max_changed_files}"
    if allowed_paths:
        outside = tuple(
            path
            for path in changed_paths
            if not any(fnmatchcase(path, pattern) for pattern in allowed_paths)
        )
        if outside:
            return "patch targets a path outside the host-enforced allowed path patterns"
    return None


class PolicyEngine:
    """Classify tool effects without trusting the model's description."""

    _READ_ONLY = frozenset(
        {
            "git_diff",
            "repo_map",
            "skill_read",
            "skill_list",
            "rag_search",
            "context_read",
            "memory_search",
        }
    )
    _PATH_TOOLS = frozenset({"read_file", "search"})
    _DENIED_EXECUTABLES = frozenset(
        {
            "bash",
            "curl",
            "fish",
            "ftp",
            "nc",
            "netcat",
            "rm",
            "scp",
            "sh",
            "ssh",
            "sudo",
            "wget",
            "zsh",
        }
    )
    _SHELL_MARKERS = ("&&", "||", "$(", "`", ";", "|", ">", "<")

    def __init__(
        self,
        worktree: Path,
        *,
        allowed_commands: Iterable[tuple[str, ...]] = (),
        allowed_mcp_tools: Iterable[str] = (),
        allowed_delegation_tools: Iterable[str] = (),
        allowed_paths: Iterable[str] = (),
        max_changed_files: int | None = None,
        skill_write_preview: Callable[[ToolRequest], str] | None = None,
    ) -> None:
        self.worktree = worktree.resolve(strict=True)
        self.allowed_commands = frozenset(tuple(command) for command in allowed_commands)
        self.allowed_mcp_tools = frozenset(allowed_mcp_tools)
        self.allowed_delegation_tools = frozenset(allowed_delegation_tools)
        self.allowed_paths = tuple(allowed_paths)
        self.max_changed_files = max_changed_files
        self.skill_write_preview = skill_write_preview

    def decide(self, request: ToolRequest) -> PolicyDecision:
        if request.name == "skill_write":
            if self.skill_write_preview is None:
                return self._decision(PolicyEffect.DENY, "skill writing is disabled", request)
            try:
                preview = self.skill_write_preview(request)
            except (OSError, ValueError):
                return self._decision(
                    PolicyEffect.DENY, "invalid or changed skill write target", request
                )
            return self._decision(PolicyEffect.ASK, preview, request)
        if request.name in self.allowed_delegation_tools:
            return self._decision(PolicyEffect.ALLOW, "host-scoped read-only delegation", request)
        if request.name in self._READ_ONLY:
            return self._decision(PolicyEffect.ALLOW, "registered read-only evidence tool", request)
        if request.name in self._PATH_TOOLS:
            return self._path_decision(request)
        if request.name == "apply_patch":
            patch = request.arguments.get("patch")
            if not isinstance(patch, str) or not patch.strip():
                return self._decision(PolicyEffect.DENY, "patch must be non-empty text", request)
            changed_paths = extract_diff_paths(patch)
            if not changed_paths:
                return self._decision(
                    PolicyEffect.DENY, "patch has no recognized changed paths", request
                )
            try:
                for path in changed_paths:
                    resolve_contained(self.worktree, path)
            except (PathPolicyError, OSError) as error:
                return self._decision(PolicyEffect.DENY, str(error), request)
            violation = change_scope_violation(
                changed_paths,
                allowed_paths=self.allowed_paths,
                max_changed_files=self.max_changed_files,
            )
            if violation is not None:
                return self._decision(PolicyEffect.DENY, violation, request)
            return self._decision(
                PolicyEffect.ALLOW, "patch is validated again before apply", request
            )
        if request.name == "run_command":
            return self._command_decision(request)
        if request.name.startswith("mcp__"):
            if request.name in self.allowed_mcp_tools:
                return self._decision(
                    PolicyEffect.ALLOW,
                    "MCP tool exactly matches the operator allowlist",
                    request,
                )
            return self._decision(
                PolicyEffect.DENY,
                "MCP tool is not present in the operator allowlist",
                request,
            )
        return self._decision(PolicyEffect.DENY, "tool is not registered in local policy", request)

    def _path_decision(self, request: ToolRequest) -> PolicyDecision:
        raw_path = request.arguments.get("path", ".")
        if not isinstance(raw_path, str):
            return self._decision(PolicyEffect.DENY, "path must be text", request)
        try:
            resolve_contained(self.worktree, raw_path)
        except (PathPolicyError, OSError) as error:
            return self._decision(PolicyEffect.DENY, str(error), request)
        return self._decision(PolicyEffect.ALLOW, "path is contained by the worktree", request)

    def _command_decision(self, request: ToolRequest) -> PolicyDecision:
        raw_argv = request.arguments.get("argv")
        if (
            not isinstance(raw_argv, list)
            or not raw_argv
            or any(not isinstance(part, str) or not part for part in raw_argv)
        ):
            return self._decision(
                PolicyEffect.DENY,
                "command must be a non-empty JSON argument array",
                request,
            )
        argv = tuple(cast(str, part) for part in raw_argv)
        executable = Path(argv[0]).name.lower()
        if executable in self._DENIED_EXECUTABLES:
            return self._decision(
                PolicyEffect.DENY, "destructive, shell, or network executable is denied", request
            )
        if (
            executable == "git"
            and len(argv) > 1
            and argv[1]
            in {
                "clean",
                "push",
                "reset",
                "restore",
            }
        ):
            return self._decision(
                PolicyEffect.DENY, "destructive Git subcommand is denied", request
            )
        if executable.startswith("python") and len(argv) > 1 and argv[1] == "-c":
            return self._decision(PolicyEffect.DENY, "inline Python execution is denied", request)
        if any(marker in part for part in argv for marker in self._SHELL_MARKERS):
            return self._decision(PolicyEffect.DENY, "shell-like command syntax is denied", request)
        if argv in self.allowed_commands:
            return self._decision(
                PolicyEffect.ALLOW, "command exactly matches the allowlist", request
            )
        if executable in {"cat", "head", "tail", "sed", "grep", "rg"}:
            return self._decision(
                PolicyEffect.DENY,
                "Unlisted text command was not executed. Use read_file for files or search "
                "for text; native tools retain path, size and time limits. Do not retry "
                "this command with another executable path.",
                request,
            )
        return self._decision(PolicyEffect.ASK, "command requires explicit human approval", request)

    @staticmethod
    def _decision(
        effect: PolicyEffect,
        reason: str,
        request: ToolRequest,
    ) -> PolicyDecision:
        return PolicyDecision(
            effect=effect,
            reason=reason,
            request_digest=request.arguments_digest,
        )
