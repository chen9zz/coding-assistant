"""Bounded Git source snapshots and line-preserving chunks for repository RAG."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from repopilot.infrastructure.process import ProcessRunner
from repopilot.tools.redaction import Redactor
from repopilot.tools.repository import _IGNORED_DIRECTORIES, RepositoryMapper
from repopilot.tools.skill_files import open_directory

_EXCLUDED = _IGNORED_DIRECTORIES | {
    "logs",
    "artifacts",
    "workspace",
    "workspaces",
    "worktrees",
    "backups",
    "skills",
    "references",
    "mysql-data",
    "vendor",
    "venv",
    "evaluation-results",
}
_SENSITIVE = re.compile(
    r"(?i)(secret|password|credential|token|api.?key|private.?key|local.?config)"
)
MAX_FILE_BYTES = 256_000


@dataclass(frozen=True)
class Source:
    path: str
    text: str
    sha256: str


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise ValueError("RAG deadline exceeded")
    return value


def read_source(root: Path, relative: str, redactor: Redactor) -> Source | None:
    parts = relative.split("/")
    if (
        any(
            not part
            or part in {".", ".."}
            or part.startswith(".")
            or "\x00" in part
            or part.lower() in _EXCLUDED
            or _SENSITIVE.search(part)
            for part in parts
        )
        or Path(relative).suffix.lower() not in {".py", ".md"}
        or Path(relative).name.upper() == "SKILL.MD"
    ):
        return None
    directory = open_directory(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > MAX_FILE_BYTES
            ):
                return None
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("source changed during read; retry with a new operation")
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    if len(data) > MAX_FILE_BYTES or b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Keep raw secrets out of SQLite (including its journal), not just tool output.
    if redactor.redact(text) != text:
        return None
    return Source(relative, text, hashlib.sha256(data).hexdigest())


def snapshot(root: Path, redactor: Redactor, deadline: float) -> tuple[dict[str, Source], str, int]:
    runner = ProcessRunner()

    def run(args: list[str], input_text: str | None = None, codes: tuple[int, ...] = (0,)) -> str:
        result = runner.run(
            ["git", *args],
            cwd=root,
            input_text=input_text,
            timeout_seconds=remaining(deadline),
            max_output_bytes=1_000_000,
        )
        if result.exit_code not in codes or result.truncated or result.timed_out:
            raise ValueError("RAG Git discovery failed or exceeded limits")
        return result.stdout

    if Path(run(["rev-parse", "--show-toplevel"]).strip()).resolve() != root:
        raise ValueError("RAG requires the authorized repository root")
    commit = run(["rev-parse", "HEAD"]).strip()
    candidates = set(
        filter(
            None,
            run(["ls-files", "--cached", "--others", "--exclude-standard", "-z"]).split("\x00"),
        )
    )
    if len(candidates) > 10_000:
        raise ValueError("RAG discovery exceeds 10000 paths")
    ignored = set(
        filter(
            None,
            run(
                ["check-ignore", "--no-index", "-z", "--stdin"],
                "\x00".join(sorted(candidates)) + "\x00",
                (0, 1),
            ).split("\x00"),
        )
    )
    sources: dict[str, Source] = {}
    skipped = 0
    total = 0
    for path in sorted(candidates - ignored):
        remaining(deadline)
        try:
            source = read_source(root, path, redactor)
        except OSError:
            source = None
        if source is None:
            skipped += 1
            continue
        sources[path] = source
        total += len(source.text.encode("utf-8"))
        if len(sources) > 500 or total > 10_000_000:
            raise ValueError("RAG source budget exceeded (500 files / 10 MB)")
    return sources, commit, skipped + len(ignored)


def chunks(source: Source, mapper: RepositoryMapper) -> list[tuple[int, int, str, str]]:
    lines = source.text.splitlines(keepends=True)
    if not lines:
        return []
    boundaries: dict[int, str] = {1: ""}
    if source.path.endswith(".py"):
        for symbol in mapper._python_symbols(source.path, source.text.encode("utf-8")):
            boundaries[symbol.start_line] = symbol.name[:160]
            boundaries.setdefault(symbol.end_line + 1, "")
    else:
        for number, line in enumerate(lines, 1):
            if re.match(r"^#{1,6}\s", line):
                boundaries[number] = line.strip()[:160]
    starts = [*sorted(n for n in boundaries if n <= len(lines)), len(lines) + 1]
    result = []
    for start, stop in pairwise(starts):
        for first in range(start, stop, 100):
            last = min(first + 99, stop - 1)
            text = "".join(lines[first - 1 : last])
            if text.strip():
                result.append((first, last, boundaries[start], text))
    return result
