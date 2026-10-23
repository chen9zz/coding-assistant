"""Local persistent background jobs. Credentials stay in child process environments."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from repopilot.domain.models import utc_now
from repopilot.tools.redaction import Redactor

ACTIVE = {"QUEUED", "RUNNING"}


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "jobs.db"
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL, "
                "spec TEXT NOT NULL, pid INTEGER)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "generation" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
        self.path.chmod(0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, job_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("unknown job")
        return dict(json.loads(row[0]))

    def list(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM jobs ORDER BY rowid DESC")
            ]

    def spec(self, job_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT spec FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("unknown job")
        return dict(json.loads(row[0]))

    def insert(self, data: dict[str, Any], spec: dict[str, Any], *, limit: int = 3) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            jobs = [json.loads(row[0]) for row in db.execute("SELECT data FROM jobs")]
            if sum(j["state"] in ACTIVE for j in jobs) >= limit:
                raise ValueError("background concurrency limit reached; wait for a job to finish")
            parent = data["parent_job_id"]
            if parent:
                owner = next((j for j in jobs if j["job_id"] == parent), None)
                if (
                    not owner
                    or owner["kind"] != "task"
                    or owner["state"] != "RUNNING"
                    or owner["cancel_requested"]
                    or data["kind"] != "analysis"
                ):
                    raise ValueError("parent is not accepting analysis children")
            if parent and sum(j["parent_job_id"] == parent for j in jobs) >= 4:
                raise ValueError("parent subagent budget exhausted (4)")
            db.execute(
                "INSERT INTO jobs(id,data,spec) VALUES (?,?,?)",
                (data["job_id"], json.dumps(data), json.dumps(spec)),
            )

    def update(
        self,
        job_id: str,
        *,
        expected: set[str] | None = None,
        expected_generation: int | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data,generation FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("unknown job")
            data = json.loads(row[0])
            if expected_generation is not None and row[1] != expected_generation:
                raise ValueError("obsolete worker generation")
            if expected is not None and data["state"] not in expected:
                raise ValueError("job state does not permit this operation")
            if values.get("state") == "QUEUED" and data["state"] not in ACTIVE:
                active = sum(
                    json.loads(r[0])["state"] in ACTIVE for r in db.execute("SELECT data FROM jobs")
                )
                if active >= 3:
                    raise ValueError("background concurrency limit reached")
            data.update(values, updated_at=utc_now().isoformat())
            db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(data), job_id))
            if values.get("state") == "QUEUED":
                db.execute("UPDATE jobs SET pid=NULL,generation=generation+1 WHERE id=?", (job_id,))
            return dict(data)

    def launch_generation(self, job_id: str) -> int:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data,generation FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or json.loads(row[0])["state"] != "QUEUED":
                raise ValueError("job must be queued to launch")
            generation = int(row[1]) + 1
            db.execute("UPDATE jobs SET generation=?,pid=NULL WHERE id=?", (generation, job_id))
            return generation

    def claim(self, job_id: str, generation: int) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data,generation FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row[1] != generation:
                return False
            data = json.loads(row[0])
            if data["state"] != "QUEUED":
                return False
            data.update(state="RUNNING", updated_at=utc_now().isoformat())
            db.execute(
                "UPDATE jobs SET data=?,pid=? WHERE id=?", (json.dumps(data), os.getpid(), job_id)
            )
            return True


class JobManager:
    def __init__(
        self,
        root: Path,
        *,
        repository: Path | None = None,
        config: Path | None = None,
        test_command: tuple[str, ...] = (),
        demo: bool = False,
    ) -> None:
        self.store = JobStore(root)
        self.root = self.store.root
        self.repository = repository.resolve() if repository else None
        self.config = config.resolve() if config else None
        self.test_command = test_command
        self.demo = demo
        if not demo and (self.repository is None or self.config is None or not test_command):
            raise ValueError("online jobs require repository, model config and test command")

    def _secret_values(self) -> set[str]:
        names = {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "FORGEPILOT_MYSQL_PASSWORD",
        }
        if self.config:
            from repopilot.configuration import load_config

            config = load_config(self.config)
            if config.provider.api_key_env:
                names.add(config.provider.api_key_env)
            for server in config.mcp_servers:
                names.update(server.env.values())
                if server.bearer_token_env:
                    names.add(server.bearer_token_env)
            if config.storage.mysql:
                names.add(config.storage.mysql.password_env)
        return {os.environ[n] for n in names if os.environ.get(n)}

    def _redactor(self) -> Redactor:
        return Redactor(self._secret_values())

    def submit(
        self,
        issue: str,
        *,
        kind: str = "task",
        parent_job_id: str | None = None,
        analysis_repository: Path | None = None,
    ) -> dict[str, Any]:
        if kind not in {"task", "analysis"} or not issue.strip() or len(issue) > 16_000:
            raise ValueError("submit a nonempty issue (max 16000 characters) and valid job kind")
        if parent_job_id:
            parent = self.get(parent_job_id)
            if parent["kind"] != "task" or kind != "analysis" or parent["state"] != "RUNNING":
                raise ValueError("only a running task may delegate read-only analysis")
        job_id = str(uuid4())
        now = utc_now().isoformat()
        job = {
            "job_id": job_id,
            "task_id": str(uuid4()) if kind == "task" else None,
            "kind": kind,
            "state": "QUEUED",
            "parent_job_id": parent_job_id,
            "issue": self._redactor().redact(issue),
            "summary": "",
            "error": None,
            "cancel_requested": False,
            "created_at": now,
            "updated_at": now,
        }
        spec = {
            "repository": str(analysis_repository or self.repository)
            if (analysis_repository or self.repository)
            else None,
            "config": str(self.config) if self.config else None,
            "test_command": self.test_command,
            "demo": self.demo,
        }
        self.store.insert(job, spec)
        self._launch(job_id)
        return self.get(job_id)

    def _launch(self, job_id: str) -> None:
        generation = self.store.launch_generation(job_id)
        # Only explicit credential sources plus ordinary runtime environment reach workers.
        env = {
            n: os.environ[n]
            for n in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "HOME")
            if n in os.environ
        }
        if self.config:
            from repopilot.configuration import load_config

            config = load_config(self.config)
            names = {config.provider.api_key_env} if config.provider.api_key_env else set()
            for server in config.mcp_servers:
                names.update(server.env.values())
                if server.bearer_token_env:
                    names.add(server.bearer_token_env)
            if config.storage.mysql:
                names.add(config.storage.mysql.password_env)
            env.update({n: os.environ[n] for n in names if n in os.environ})
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "repopilot.worker",
                    "--workspace",
                    str(self.root),
                    "--job-id",
                    job_id,
                    "--generation",
                    str(generation),
                ],
                env=env,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            self.store.update(
                job_id,
                expected_generation=generation,
                state="FAILED",
                error="worker could not start",
            )
            raise RuntimeError("worker could not start") from None

        # Reap without tying the worker lifetime to UI lifetime; no PID-based signalling.
        def reap() -> None:
            process.wait()
            with suppress(ValueError):  # completed or superseded generation
                self.store.update(
                    job_id,
                    expected=ACTIVE,
                    expected_generation=generation,
                    state="INTERRUPTED",
                    error="worker exited before final status",
                )

        threading.Thread(target=reap, daemon=True).start()

    def list(self) -> list[dict[str, Any]]:
        return [self.get(j["job_id"]) for j in self.store.list()]

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job["state"] == "RUNNING":
            with self.store.connection() as db:
                row = db.execute("SELECT pid,generation FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row and row[0]:
                try:
                    os.kill(row[0], 0)  # existence probe only; never signal a reused PID
                except ProcessLookupError:
                    try:
                        return self.store.update(
                            job_id,
                            expected={"RUNNING"},
                            expected_generation=int(row[1]),
                            state="INTERRUPTED",
                            error="worker no longer exists",
                        )
                    except ValueError:
                        return self.store.get(job_id)
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job["state"] in ACTIVE or job["state"] == "WAITING_APPROVAL":
            self.store.update(job_id, cancel_requested=True)
            if job["state"] == "WAITING_APPROVAL":
                from repopilot.domain.models import TaskEvent, TaskState

                with self.container() as container:
                    task = container.service.get(job["task_id"])
                    if task.state is TaskState.WAITING_APPROVAL:
                        container.store.append(
                            TaskEvent(
                                event_id=str(uuid4()),
                                task_id=task.task_id,
                                sequence=task.sequence + 1,
                                event_type="task.cancelled",
                                from_state=task.state,
                                to_state=TaskState.CANCELLED,
                                payload={},
                            ),
                            task.sequence,
                        )
                self.store.update(job_id, state="CANCELLED")
            for child in self.list():
                if child["parent_job_id"] == job_id:
                    self.cancel(child["job_id"])
        return self.get(job_id)

    def resume(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["kind"] != "task" or job["cancel_requested"]:
            raise ValueError("this job cannot resume; submit a new task")
        if job["state"] not in {"WAITING_APPROVAL", "INTERRUPTED"}:
            raise ValueError("only approved or interrupted jobs can resume")
        with self.container() as container:
            from repopilot.domain.models import TaskState
            from repopilot.infrastructure.sqlite import TaskNotFoundError

            try:
                task = container.service.get(job["task_id"])
            except TaskNotFoundError:
                task = None  # process died before task creation; safe to create on resume
            if task and task.state is TaskState.WAITING_APPROVAL:
                raise ValueError("approve the exact pending operation before resuming")
            if task and task.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
                raise ValueError("terminal tasks cannot resume")
        self.store.update(
            job_id, expected={"WAITING_APPROVAL", "INTERRUPTED"}, state="QUEUED", error=None
        )
        self._launch(job_id)
        return self.get(job_id)

    @contextmanager
    def container(self) -> Iterator[Any]:
        from repopilot.bootstrap import build_local_container

        if self.config:
            from repopilot.configuration import load_config
            from repopilot.storage import configured_store

            store = configured_store(self.root, load_config(self.config).storage)
            try:
                yield build_local_container(
                    self.root, store=store, secret_values=self._secret_values()
                )
            finally:
                dispose = getattr(store, "close", None)
                if callable(dispose):
                    dispose()
        else:
            yield build_local_container(self.root)

    def approve(self, job_id: str, operation_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["state"] != "WAITING_APPROVAL" or job["cancel_requested"]:
            raise ValueError("job has no active approval request")
        with self.container() as container:
            container.service.approve(job["task_id"], operation_id)
        return self.resume(job_id)
