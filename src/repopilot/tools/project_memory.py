"""Bounded repository memories. Only the host may propose/confirm/retire/delete."""

from __future__ import annotations

import builtins
import fcntl
import hashlib
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter

from repopilot.domain.models import FrozenModel
from repopilot.tools.rag_sources import Source, read_source, snapshot
from repopilot.tools.redaction import Redactor
from repopilot.tools.skill_files import private_root


class MemoryRecord(FrozenModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=2000)
    source: str = Field(min_length=1, max_length=255)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["candidate", "approved", "retired"] = "candidate"
    created_at: str
    updated_at: str


_RECORDS = TypeAdapter(list[MemoryRecord])
_LIMIT = 262144


class ProjectMemory:
    def __init__(self, root: Path, redactor: Redactor) -> None:
        self.root = root.absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, self.identity = private_root(self.root)
        os.close(fd)
        self.redactor = redactor

    def _scope(self, repository: Path) -> str:
        root = repository.resolve(strict=True)
        self._boundary(root)
        info = root.stat()
        return hashlib.sha256(f"{root}:{info.st_dev}:{info.st_ino}".encode()).hexdigest()

    def _boundary(self, root: Path) -> None:
        if self.root.is_relative_to(root) or root.is_relative_to(self.root):
            raise ValueError("memory and source roots must not overlap")

    def _sources(self, repository: Path) -> dict[str, Source]:
        root = repository.resolve(strict=True)
        self._boundary(root)
        return snapshot(root, self.redactor, time.monotonic() + 5)[0]

    @contextmanager
    def _locked(self) -> Iterator[int]:
        directory, identity = private_root(self.root)
        try:
            if identity != self.identity:
                raise ValueError("memory root replaced")
            fd = os.open(
                ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                self._ordinary(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield directory
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    @staticmethod
    def _ordinary(fd: int) -> None:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("memory files must be private ordinary files")

    def _read(self, directory: int, name: str) -> list[MemoryRecord]:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return []
        try:
            self._ordinary(fd)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(_LIMIT + 1)
            if len(data) > _LIMIT:
                raise ValueError("memory size limit")
            rows = _RECORDS.validate_json(data)
            if len(rows) > 64 or len({r.id for r in rows}) != len(rows):
                raise ValueError("invalid memory record count")
            return rows
        finally:
            os.close(fd)

    def _write(self, directory: int, name: str, rows: list[MemoryRecord]) -> None:
        data = _RECORDS.dump_json(rows)
        if len(data) > _LIMIT or len(rows) > 64:
            raise ValueError("memory size limit")
        temp = ".pending-" + uuid4().hex
        fd = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=directory)

    @staticmethod
    def _view(record: MemoryRecord, sources: dict[str, Source] | None) -> dict[str, Any]:
        source = sources.get(record.source) if sources is not None else None
        valid = source is not None and source.sha256 == record.source_sha256
        return {
            **record.model_dump(),
            "record_sha256": hashlib.sha256(record.model_dump_json().encode()).hexdigest(),
            "effective_state": (
                "unverified" if sources is None else record.state if valid else "stale"
            ),
            "untrusted": True,
        }

    def propose(self, repository: Path, title: str, text: str, source: str) -> dict[str, Any]:
        name = self._scope(repository) + ".json"
        sources = self._sources(repository)
        if source not in sources or self.redactor.redact(text) != text:
            raise ValueError("memory requires a current permitted source and nonsecret text")
        if self.redactor.redact(title) != title or "\x00" in text or "\x00" in title:
            raise ValueError("invalid memory text")
        now = datetime.now(UTC).isoformat()
        record = MemoryRecord(
            id=uuid4().hex,
            title=title,
            text=text,
            source=source,
            source_sha256=sources[source].sha256,
            created_at=now,
            updated_at=now,
        )
        with self._locked() as directory:
            rows = self._read(directory, name)
            self._write(directory, name, [*rows, record])
        return self._view(record, sources)

    def list(self, repository: Path) -> builtins.list[dict[str, Any]]:
        name = self._scope(repository) + ".json"
        try:
            sources = self._sources(repository)
        except (OSError, ValueError, sqlite3.Error):
            sources = None
        with self._locked() as directory:
            rows = self._read(directory, name)
        return [self._view(row, sources) for row in rows]

    def transition(self, repository: Path, identity: str, state: str, expected_sha256: str) -> None:
        if state not in {"approved", "retired", "deleted"}:
            raise ValueError("invalid memory transition")
        name = self._scope(repository) + ".json"
        sources = self._sources(repository) if state == "approved" else {}
        with self._locked() as directory:
            rows = self._read(directory, name)
            record = next((r for r in rows if r.id == identity), None)
            if record is None or self._view(record, sources)["record_sha256"] != expected_sha256:
                raise ValueError("memory missing or changed; inspect before confirming")
            if state == "approved":
                if (
                    record.state != "candidate"
                    or self._view(record, sources)["effective_state"] == "stale"
                ):
                    raise ValueError("only a current candidate can be approved")
                if any(
                    r.state == "approved" and r.title.casefold() == record.title.casefold()
                    for r in rows
                ):
                    raise ValueError(
                        "conflicting approved title; explicitly retire old record first"
                    )
            updated = [r for r in rows if r.id != identity]
            if state != "deleted":
                updated.append(
                    MemoryRecord.model_validate(
                        {
                            **record.model_dump(),
                            "state": state,
                            "updated_at": datetime.now(UTC).isoformat(),
                        }
                    )
                )
            self._write(directory, name, updated)

    def search(self, repository: Path, worktree: Path, query: str) -> builtins.list[dict[str, Any]]:
        if not 1 <= len(query) <= 256 or not query.strip():
            raise ValueError("query must contain 1..256 characters")
        name = self._scope(repository) + ".json"
        sources = self._sources(worktree)
        with self._locked() as directory:
            rows = self._read(directory, name)
        hits = []
        for record in rows:
            view = self._view(record, sources)
            if (
                view["effective_state"] == "approved"
                and query.casefold() in (record.title + " " + record.text).casefold()
                and self.redactor.redact(record.model_dump()) == record.model_dump()
            ):
                hits.append(view)
        verified = []
        for hit in hits[:3]:
            try:
                current = read_source(worktree.resolve(strict=True), hit["source"], self.redactor)
            except (OSError, ValueError):
                current = None
            if current is not None and current.sha256 == hit["source_sha256"]:
                verified.append(hit)
        return verified
