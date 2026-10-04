"""Safe built-in repository tools; each validates its own concrete arguments."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path
from typing import Protocol, cast

from repopilot.domain.models import JsonValue, ToolRequest, ToolResult, ToolStatus
from repopilot.infrastructure.process import ProcessResult, ProcessRunner
from repopilot.tools.policy import PathPolicyError, resolve_contained
from repopilot.tools.repository import RepositoryMapper


class Tool(Protocol):
    name: str

    def execute(
        self,
        worktree: Path,
        request: ToolRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult: ...


class ToolNotFoundError(LookupError):
    """Raised when an allowed request has no executable adapter."""


class ToolRegistry:
    """Map stable tool names to host-controlled implementations."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolNotFoundError(f"tool is not registered: {name}") from error

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))


class ReadFileTool:
    name = "read_file"

    def __init__(self, max_bytes: int = 64_000) -> None:
        self.max_bytes = max_bytes

    def execute(
        self, worktree: Path, request: ToolRequest, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        started = time.monotonic()
        raw_path = request.arguments.get("path")
        if not isinstance(raw_path, str):
            return _failure(request, "path must be text", started)
        if timeout_seconds is not None and timeout_seconds <= 0:
            return _failure(request, "task time budget is exhausted", started)
        descriptor: int | None = None
        try:
            path = resolve_contained(worktree, raw_path)
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("read_file only accepts regular files")
            chunks = bytearray()
            while len(chunks) <= self.max_bytes:
                if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                    raise OSError("read_file exceeded task deadline")
                chunk = os.read(descriptor, self.max_bytes + 1 - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            data = bytes(chunks)
        except (OSError, PathPolicyError) as error:
            return _failure(request, str(error), started)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if b"\x00" in data[:8192]:
            return _failure(request, "binary files are not returned to the model", started)
        bounded = data[: self.max_bytes]
        return _success(
            request,
            {
                "path": raw_path,
                "content": bounded.decode("utf-8", errors="replace"),
                "size_bytes": file_stat.st_size,
            },
            started,
            truncated=file_stat.st_size > len(bounded),
        )


class SearchTool:
    name = "search"

    def __init__(self, runner: ProcessRunner | None = None, max_output_bytes: int = 64_000) -> None:
        self.runner = runner or ProcessRunner()
        self.max_output_bytes = max_output_bytes

    def execute(
        self, worktree: Path, request: ToolRequest, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        query = request.arguments.get("query")
        raw_path = request.arguments.get("path", ".")
        if not isinstance(query, str) or not query or not isinstance(raw_path, str):
            return _failure(request, "query and path must be non-empty text")
        try:
            path = resolve_contained(worktree, raw_path)
        except (OSError, PathPolicyError) as error:
            return _failure(request, str(error))
        result = self.runner.run(
            ["rg", "--line-number", "--no-heading", "--color", "never", query, str(path)],
            cwd=worktree,
            timeout_seconds=_bounded_timeout(10, timeout_seconds),
            max_output_bytes=self.max_output_bytes,
        )
        status = ToolStatus.SUCCEEDED if result.exit_code in {0, 1} else ToolStatus.FAILED
        return ToolResult(
            operation_id=request.operation_id,
            tool_name=request.name,
            status=status,
            output={"matches": result.stdout, "exit_code": result.exit_code},
            error=result.stderr.strip() or None if status is ToolStatus.FAILED else None,
            duration_ms=result.duration_ms,
            truncated=result.truncated,
        )


class RepoMapTool:
    name = "repo_map"

    def __init__(self, mapper: RepositoryMapper | None = None) -> None:
        self.mapper = mapper or RepositoryMapper()

    def execute(
        self, worktree: Path, request: ToolRequest, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        started = time.monotonic()
        issue = request.arguments.get("issue", "repository change")
        if not isinstance(issue, str):
            return _failure(request, "issue must be text", started)
        try:
            repo_map = self.mapper.build(worktree, issue)
        except (OSError, ValueError) as error:
            return _failure(request, str(error), started)
        return _success(
            request,
            cast(dict[str, JsonValue], repo_map.model_dump(mode="json")),
            started,
        )


class ApplyPatchTool:
    name = "apply_patch"

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or ProcessRunner()

    def execute(
        self, worktree: Path, request: ToolRequest, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        started = time.monotonic()
        patch = request.arguments.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            return _failure(request, "patch must be non-empty text")
        try:
            self._validate_paths(worktree, patch)
        except PathPolicyError as error:
            return _failure(request, str(error))
        check = self.runner.run(
            ["git", "apply", "--check", "--whitespace=error", "-"],
            cwd=worktree,
            timeout_seconds=_bounded_timeout(15, timeout_seconds),
            max_output_bytes=64_000,
            input_text=patch,
        )
        if check.exit_code != 0:
            return ToolResult(
                operation_id=request.operation_id,
                tool_name=request.name,
                status=ToolStatus.FAILED,
                output={"check_exit_code": check.exit_code},
                error=check.stderr.strip() or "git apply --check rejected the patch",
                duration_ms=check.duration_ms,
                truncated=check.truncated,
            )
        apply_budget = (
            None
            if timeout_seconds is None
            else timeout_seconds - (time.monotonic() - started)
        )
        applied = self.runner.run(
            ["git", "apply", "--whitespace=error", "-"],
            cwd=worktree,
            timeout_seconds=_bounded_timeout(15, apply_budget),
            max_output_bytes=64_000,
            input_text=patch,
        )
        status = ToolStatus.SUCCEEDED if applied.exit_code == 0 else ToolStatus.FAILED
        return ToolResult(
            operation_id=request.operation_id,
            tool_name=request.name,
            status=status,
            output={"patch_sha256": hashlib.sha256(patch.encode()).hexdigest()},
            error=applied.stderr.strip() or None if status is ToolStatus.FAILED else None,
            duration_ms=check.duration_ms + applied.duration_ms,
            truncated=check.truncated or applied.truncated,
        )

    @staticmethod
    def _validate_paths(worktree: Path, patch: str) -> None:
        if "GIT binary patch" in patch or "Binary files " in patch:
            raise PathPolicyError("binary patches are denied")
        found_path = False
        for line in patch.splitlines():
            if not line.startswith(("--- ", "+++ ")):
                continue
            raw_path = line[4:].split("\t", maxsplit=1)[0]
            if raw_path == "/dev/null":
                continue
            if raw_path.startswith(("a/", "b/")):
                raw_path = raw_path[2:]
            resolve_contained(worktree, raw_path)
            found_path = True
        if not found_path:
            raise PathPolicyError("patch has no validated worktree path")


class RunCommandTool:
    name = "run_command"

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or ProcessRunner()

    def execute(
        self, worktree: Path, request: ToolRequest, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        raw_argv = request.arguments.get("argv")
        if not isinstance(raw_argv, list) or not all(isinstance(part, str) for part in raw_argv):
            return _failure(request, "argv must be a JSON array of strings")
        argv = [cast(str, part) for part in raw_argv]
        argv_json: list[JsonValue] = list(argv)
        result = self.runner.run(
            argv,
            cwd=worktree,
            timeout_seconds=_bounded_timeout(120, timeout_seconds),
            max_output_bytes=128_000,
        )
        status = ToolStatus.SUCCEEDED if result.exit_code == 0 else ToolStatus.FAILED
        return ToolResult(
            operation_id=request.operation_id,
            tool_name=request.name,
            status=status,
            output={
                "argv": argv_json,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
            },
            error="command did not complete successfully" if status is ToolStatus.FAILED else None,
            duration_ms=result.duration_ms,
            truncated=result.truncated,
        )


class GitDiffTool:
    name = "git_diff"

    _MAX_DIFF_BYTES = 256_000

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or ProcessRunner()

    def execute(
        self, worktree: Path, request: ToolRequest, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        started = time.monotonic()
        try:
            tracked = self._run(
                worktree,
                ["git", "diff", "HEAD", "--binary", "--no-ext-diff", "--"],
                started,
                timeout_seconds,
                self._MAX_DIFF_BYTES + 1,
            )
            self._require_complete(tracked, {0}, "tracked diff")
            tracked_names = self._run(
                worktree,
                ["git", "diff", "--name-only", "-z", "HEAD", "--"],
                started,
                timeout_seconds,
                self._MAX_DIFF_BYTES,
            )
            self._require_complete(tracked_names, {0}, "tracked path list")
            untracked_names = self._run(
                worktree,
                ["git", "ls-files", "--others", "--exclude-standard", "-z"],
                started,
                timeout_seconds,
                self._MAX_DIFF_BYTES,
            )
            self._require_complete(untracked_names, {0}, "untracked path list")

            changed_paths = set(self._nul_paths(tracked_names.stdout))
            untracked = self._nul_paths(untracked_names.stdout)
            changed_paths.update(untracked)
            pieces = [tracked.stdout]
            bytes_used = len(tracked.stdout.encode("utf-8"))
            if bytes_used > self._MAX_DIFF_BYTES:
                raise ValueError("complete worktree diff exceeds the output limit")
            for path in untracked:
                remaining = self._MAX_DIFF_BYTES - bytes_used
                if remaining <= 0:
                    raise ValueError("complete worktree diff exceeds the output limit")
                addition = self._run(
                    worktree,
                    [
                        "git",
                        "diff",
                        "--no-index",
                        "--binary",
                        "--no-ext-diff",
                        "--",
                        "/dev/null",
                        path,
                    ],
                    started,
                    timeout_seconds,
                    remaining + 1,
                )
                self._require_complete(addition, {0, 1}, f"untracked diff for {path}")
                encoded_size = len(addition.stdout.encode("utf-8"))
                if encoded_size > remaining:
                    raise ValueError("complete worktree diff exceeds the output limit")
                pieces.append(addition.stdout)
                bytes_used += encoded_size
            diff = "".join(pieces)
        except (OSError, ValueError) as error:
            return _failure(request, str(error), started)
        changed_paths_json: list[JsonValue] = [path for path in sorted(changed_paths)]
        output: dict[str, JsonValue] = {
            "diff": diff,
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "changed_paths": changed_paths_json,
        }
        return _success(
            request,
            output,
            started,
        )

    def _run(
        self,
        worktree: Path,
        argv: list[str],
        started: float,
        timeout_seconds: float | None,
        max_output_bytes: int,
    ) -> ProcessResult:
        return self.runner.run(
            argv,
            cwd=worktree,
            timeout_seconds=_remaining_timeout(15, timeout_seconds, started),
            max_output_bytes=max_output_bytes,
        )

    @staticmethod
    def _require_complete(result: ProcessResult, exit_codes: set[int], label: str) -> None:
        if result.timed_out:
            raise ValueError(f"{label} exceeded task deadline")
        if result.truncated:
            raise ValueError(f"{label} exceeded its output limit")
        if result.exit_code not in exit_codes:
            raise ValueError(result.stderr.strip() or f"{label} failed")

    @staticmethod
    def _nul_paths(raw: str) -> tuple[str, ...]:
        paths = tuple(path for path in raw.split("\x00") if path)
        if any("\x00" in path for path in paths):
            raise ValueError("git returned an invalid changed path")
        return paths


def build_builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        RepoMapTool(),
        ReadFileTool(),
        SearchTool(),
        ApplyPatchTool(),
        RunCommandTool(),
        GitDiffTool(),
    ):
        registry.register(tool)
    return registry


def _bounded_timeout(default: float, remaining: float | None) -> float:
    if remaining is None:
        return default
    if remaining <= 0:
        raise ValueError("task time budget is exhausted")
    return min(default, remaining)


def _remaining_timeout(
    default: float,
    remaining: float | None,
    started: float,
) -> float:
    total = default if remaining is None else min(default, remaining)
    budget = total - (time.monotonic() - started)
    if budget <= 0:
        raise ValueError("task time budget is exhausted")
    return budget


def _success(
    request: ToolRequest,
    output: JsonValue,
    started: float | None = None,
    *,
    truncated: bool = False,
) -> ToolResult:
    return ToolResult(
        operation_id=request.operation_id,
        tool_name=request.name,
        status=ToolStatus.SUCCEEDED,
        output=output,
        duration_ms=_duration(started),
        truncated=truncated,
    )


def _failure(
    request: ToolRequest,
    error: str,
    started: float | None = None,
) -> ToolResult:
    return ToolResult(
        operation_id=request.operation_id,
        tool_name=request.name,
        status=ToolStatus.FAILED,
        error=error,
        duration_ms=_duration(started),
    )


def _duration(started: float | None) -> int:
    if started is None:
        return 0
    return max(0, round((time.monotonic() - started) * 1000))
