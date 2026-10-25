"""POSIX descriptor-relative skill I/O; never follow path-component symlinks."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

MAX_BYTES = 65_536


def markdown_resource(resource: str) -> bool:
    return resource == "SKILL.md" or bool(
        re.fullmatch(r"references/[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\.md", resource)
    )


def open_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("skill root must be an absolute directory")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def private_root(path: Path) -> tuple[int, str]:
    home = Path.home()
    forbidden = (home / ".codex/skills", home / ".claude/skills", home / ".agents/skills")
    if path in {Path("/"), home, Path("/tmp"), Path("/private/tmp")} or any(
        path.is_relative_to(item) or item.is_relative_to(path) for item in forbidden
    ):
        raise ValueError("use a dedicated ForgePilot skill directory, not a global skill root")
    fd = open_directory(path)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("writable skill root must be operator-owned with mode 0700")
    identity = hashlib.sha256(f"{path}:{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    return fd, identity


def read_at(directory: int, name: str) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("skill resources must be regular files without hard links")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("skill resource is too large (limit 65536 bytes)")
        if b"\x00" in data:
            raise ValueError("skill resource must be text")
        return data.decode("utf-8")
    finally:
        os.close(fd)


def read_relative(root: Path, relative: str) -> str:
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        raise ValueError("skill resource must be a contained relative path")
    fd = open_directory(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return read_at(fd, parts[-1])
    finally:
        os.close(fd)
