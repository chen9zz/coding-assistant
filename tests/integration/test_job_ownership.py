"""Regression checks for worker ownership and cancellation handoff boundaries."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from repopilot.application.jobs import JobManager
from repopilot.domain.models import TaskEvent, TaskState


@pytest.mark.parametrize("reap_before_launch", [False, True])
def test_old_reaper_cannot_interrupt_replacement_worker(
    tmp_path: Path, monkeypatch, reap_before_launch: bool
) -> None:
    reapers = []
    launch = Mock(return_value=Mock())
    monkeypatch.setattr("repopilot.application.jobs.subprocess.Popen", launch)

    def capture_thread(*, target, daemon):
        reapers.append(target)
        return Mock()

    monkeypatch.setattr("repopilot.application.jobs.threading.Thread", capture_thread)
    manager = JobManager(tmp_path, demo=True)
    job = manager.submit("approval then resume")
    job_id = job["job_id"]
    assert manager.store.claim(job_id, 1)
    manager.store.update(job_id, state="WAITING_APPROVAL")
    manager.store.update(job_id, state="QUEUED")
    if reap_before_launch:
        reapers[0]()
        assert manager.store.get(job_id)["state"] == "QUEUED"
    manager._launch(job_id)
    argv = launch.call_args.args[0]
    generation = int(argv[argv.index("--generation") + 1])
    assert manager.store.claim(job_id, generation)

    reapers[0]()

    assert manager.get(job_id)["state"] == "RUNNING"
    assert not manager.store.claim(job_id, 1)


def test_delayed_dead_pid_probe_cannot_interrupt_new_generation(
    tmp_path: Path, monkeypatch
) -> None:
    manager = JobManager(tmp_path, demo=True)
    monkeypatch.setattr(manager, "_launch", lambda _: None)
    job_id = manager.submit("stale existence probe")["job_id"]
    generation = manager.store.launch_generation(job_id)
    assert manager.store.claim(job_id, generation)

    def probe_old_worker(pid, signal):
        assert signal == 0
        # Another request resumes this job after this reader captured the old PID,
        # but before its failed existence probe is reconciled with the job store.
        manager.store.update(job_id, state="WAITING_APPROVAL")
        manager.store.update(job_id, state="QUEUED")
        replacement = manager.store.launch_generation(job_id)
        assert manager.store.claim(job_id, replacement)
        raise ProcessLookupError

    monkeypatch.setattr("repopilot.application.jobs.os.kill", probe_old_worker)
    observed = manager.get(job_id)

    assert observed["state"] == "RUNNING"
    assert manager.store.get(job_id)["state"] == "RUNNING"


def test_claim_commits_running_state_and_owner_pid_together(tmp_path: Path, monkeypatch) -> None:
    manager = JobManager(tmp_path, demo=True)
    monkeypatch.setattr(manager, "_launch", lambda _: None)
    job_id = manager.submit("atomic ownership")["job_id"]
    generation = manager.store.launch_generation(job_id)
    original_connection = manager.store.connection
    observations = []

    @contextmanager
    def observed_connection():
        with original_connection() as db:
            yield db
        with original_connection() as db:
            row = db.execute("SELECT data,pid FROM jobs WHERE id=?", (job_id,)).fetchone()
            observations.append((json.loads(row[0])["state"], row[1]))

    monkeypatch.setattr(manager.store, "connection", observed_connection)
    assert manager.store.claim(job_id, generation)
    assert observations
    assert all(pid == os.getpid() for state, pid in observations if state == "RUNNING")


def test_cancel_before_waiting_publication_finishes_job_and_task(
    tmp_path: Path, monkeypatch
) -> None:
    from repopilot import worker

    manager = JobManager(tmp_path, demo=True)
    monkeypatch.setattr(manager, "_launch", lambda _: None)
    job = manager.submit("cancel at approval handoff")
    generation = manager.store.launch_generation(job["job_id"])

    def reach_approval(container, runtime, task_id, spec):
        snapshot = container.service.create(spec)
        for target in (
            TaskState.INDEXING,
            TaskState.PLANNING,
            TaskState.EXECUTING,
            TaskState.WAITING_APPROVAL,
        ):
            snapshot = container.store.append(
                TaskEvent(
                    event_id=str(uuid4()),
                    task_id=task_id,
                    sequence=snapshot.sequence + 1,
                    event_type=f"task.{target.value.lower()}",
                    from_state=snapshot.state,
                    to_state=target,
                    payload={},
                ),
                snapshot.sequence,
            )
        # The domain task reached its approval boundary, but the worker has not yet
        # published WAITING_APPROVAL to the independent process-management store.
        manager.cancel(job["job_id"])
        return snapshot

    monkeypatch.setattr(worker, "_run_task", reach_approval)
    worker.run_worker(tmp_path, job["job_id"], generation)

    assert manager.get(job["job_id"])["state"] == "CANCELLED"
    with manager.container() as container:
        assert container.service.get(job["task_id"]).state is TaskState.CANCELLED


def test_cancelled_parent_cannot_spawn_child_after_cancellation_cascade(
    tmp_path: Path, monkeypatch
) -> None:
    manager = JobManager(tmp_path, demo=True)
    monkeypatch.setattr(manager, "_launch", lambda _: None)
    parent = manager.submit("parent")
    manager.store.update(parent["job_id"], state="RUNNING")
    manager.cancel(parent["job_id"])

    with pytest.raises(ValueError):
        manager.submit("late child", kind="analysis", parent_job_id=parent["job_id"])
