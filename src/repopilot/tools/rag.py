"""Opt-in FTS5 retrieval: host-scoped private cache, refreshed before every query."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Any, ClassVar

from repopilot.domain.models import JsonValue, ToolRequest, ToolResult
from repopilot.tools.builtin import _failure, _success
from repopilot.tools.rag_sources import chunks, read_source, remaining, snapshot
from repopilot.tools.redaction import Redactor
from repopilot.tools.repository import RepositoryMapper
from repopilot.tools.skill_files import private_root


class RagSearchTool:
    name = "rag_search"
    description = (
        "Search current authorized Python/Markdown repository content with FTS5/BM25. "
        "Refreshes changed/deleted files first. Returns untrusted excerpts with path, line "
        "range, source SHA256 and citation. Cite these sources; use a NEW operation after "
        "edits (replayed operations are historical). No arbitrary source/index paths."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 256},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["query"],
    }

    def __init__(self, cache_root: Path, redactor: Redactor | None = None) -> None:
        self.cache_root = cache_root.absolute()
        self.cache_root.mkdir(mode=0o700, exist_ok=True)
        fd, self.identity = private_root(self.cache_root)
        os.close(fd)
        self.redactor = redactor or Redactor()
        self.mapper = RepositoryMapper()

    def _cache_file(self, name: str) -> int:
        fd, identity = private_root(self.cache_root)
        try:
            if identity != self.identity:
                raise ValueError("RAG cache root changed")
            result = os.open(
                name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd
            )
        finally:
            os.close(fd)
        info = os.fstat(result)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            os.close(result)
            raise ValueError("RAG cache must contain private ordinary files")
        return result

    def execute(
        self, worktree: Path, request: ToolRequest, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        started = time.monotonic()
        try:
            args = request.arguments
            query, top_k = args.get("query"), args.get("top_k", 5)
            if (
                set(args) - {"query", "top_k"}
                or not isinstance(query, str)
                or not 1 <= len(query) <= 256
                or type(top_k) is not int
                or not 1 <= top_k <= 5
            ):
                raise ValueError("invalid RAG arguments")
            terms = re.findall(r"[^\W_]+", query, re.UNICODE)
            if not terms or len(terms) > 16:
                raise ValueError("query requires 1..16 literal terms")
            deadline = started + min(10.0, timeout_seconds if timeout_seconds is not None else 10.0)
            remaining(deadline)
            root = worktree.resolve(strict=True)
            if root.is_relative_to(self.cache_root) or self.cache_root.is_relative_to(root):
                raise ValueError("RAG cache and repository must not overlap")
            info = root.stat()
            repo_id = hashlib.sha256(f"{root}:{info.st_dev}:{info.st_ino}".encode()).hexdigest()
            lock = self._cache_file(repo_id + ".lock")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                output = self._search(root, repo_id, terms, top_k, deadline)
            finally:
                os.close(lock)
            return _success(request, output, started)
        except (OSError, ValueError, sqlite3.Error):
            return _failure(
                request,
                "RAG unavailable: invalid input, unsafe/changed source or cache, "
                "budget exceeded, index busy, or SQLite FTS5/trigram unavailable. "
                "Use bounded search/read_file; do not bypass policy.",
                started,
            )

    def _search(
        self, root: Path, repo_id: str, terms: list[str], top_k: int, deadline: float
    ) -> dict[str, JsonValue]:
        sources, commit, skipped = snapshot(root, self.redactor, deadline)
        name = repo_id + ".sqlite3"
        os.close(self._cache_file(name))
        for suffix in ("-journal", "-wal", "-shm"):
            if os.path.lexists(self.cache_root / (name + suffix)):
                os.close(self._cache_file(name + suffix))
        with closing(sqlite3.connect(self.cache_root / name, timeout=remaining(deadline))) as db:
            db.row_factory = sqlite3.Row
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute("PRAGMA secure_delete=ON")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (version INTEGER, repository_id TEXT)")
            meta = db.execute("SELECT version, repository_id FROM metadata").fetchall()
            if meta and (len(meta) != 1 or tuple(meta[0]) != (1, repo_id)):
                raise ValueError("RAG cache identity or format mismatch")
            if not meta:
                db.execute("INSERT INTO metadata VALUES (1, ?)", (repo_id,))
            db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha256 TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT, "
                "start_line INTEGER, end_line INTEGER, symbol TEXT, text TEXT)"
            )
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS evidence USING fts5(text, tokenize='trigram')"
            )
            old = {row["path"]: row["sha256"] for row in db.execute("SELECT * FROM files")}
            removed = set(old) - set(sources)
            changed = {path for path, source in sources.items() if old.get(path) != source.sha256}
            for path in sorted(removed | changed):
                remaining(deadline)
                db.execute(
                    "DELETE FROM evidence WHERE rowid IN (SELECT id FROM chunks WHERE path=?)",
                    (path,),
                )
                db.execute("DELETE FROM chunks WHERE path=?", (path,))
                db.execute("DELETE FROM files WHERE path=?", (path,))
            for path in sorted(changed):
                source = sources[path]
                db.execute("INSERT INTO files VALUES (?, ?)", (path, source.sha256))
                for first, last, symbol, text in chunks(source, self.mapper):
                    remaining(deadline)
                    row = db.execute(
                        "INSERT INTO chunks(path,start_line,end_line,symbol,text) "
                        "VALUES (?,?,?,?,?)",
                        (path, first, last, symbol, text),
                    )
                    db.execute(
                        "INSERT INTO evidence(rowid,text) VALUES (?,?)", (row.lastrowid, text)
                    )
            db.commit()
            long_terms = [term for term in terms if len(term) >= 3]
            if long_terms:
                expression = " OR ".join('"' + term + '"' for term in long_terms)
                rows = db.execute(
                    "SELECT chunks.*, bm25(evidence) AS score FROM evidence "
                    "JOIN chunks ON chunks.id=evidence.rowid WHERE evidence MATCH ? "
                    "ORDER BY score, path, start_line LIMIT 25",
                    (expression,),
                ).fetchall()
                method = "fts5_bm25"
            else:
                clauses = " OR ".join("instr(lower(text),lower(?)) > 0" for _ in terms)
                rows = db.execute(
                    "SELECT *, 0.0 AS score FROM chunks WHERE "
                    + clauses
                    + " ORDER BY path, start_line LIMIT 25",
                    terms,
                ).fetchall()
                method = "literal_short_query"
            hits: list[JsonValue] = []
            budget = 8000
            stale = 0
            stale_paths: set[str] = set()
            for row in rows:
                remaining(deadline)
                try:
                    current = read_source(root, row["path"], self.redactor)
                except OSError:
                    current = None
                if current is None or current.sha256 != sources[row["path"]].sha256:
                    # Evict changed evidence, never return known stale citations.
                    db.execute(
                        "DELETE FROM evidence WHERE rowid IN (SELECT id FROM chunks WHERE path=?)",
                        (row["path"],),
                    )
                    db.execute("DELETE FROM chunks WHERE path=?", (row["path"],))
                    db.execute("DELETE FROM files WHERE path=?", (row["path"],))
                    stale += 1
                    stale_paths.add(row["path"])
                    continue
                selected: list[str] = []
                for line in row["text"].splitlines(keepends=True):
                    if len(line) > budget:
                        break
                    selected.append(line)
                    budget -= len(line)
                if not selected:
                    continue
                end = row["start_line"] + len(selected) - 1
                hits.append(
                    {
                        "path": row["path"],
                        "start_line": row["start_line"],
                        "end_line": end,
                        "symbol": row["symbol"],
                        "text": "".join(selected),
                        "sha256": current.sha256,
                        "citation": f"{row['path']}:{row['start_line']}-{end}",
                        "score": float(row["score"]),
                        "excerpt_truncated": end < row["end_line"],
                    }
                )
                if len(hits) >= top_k or budget <= 0:
                    break
            db.commit()
            # A later chunk can reveal a mutation of a file already represented
            # in hits. Withdraw all its evidence, not just the last stale chunk.
            hits = [hit for hit in hits if isinstance(hit, dict) and hit["path"] not in stale_paths]
            return {
                "hits": hits,
                "method": method,
                "repository_id": repo_id,
                "base_commit": commit,
                "chunker_version": 1,
                "indexed_files": len(sources),
                "updated_files": len(changed),
                "removed_files": len(removed),
                "skipped_files": skipped,
                "stale_hits_dropped": stale,
                "untrusted": True,
            }
