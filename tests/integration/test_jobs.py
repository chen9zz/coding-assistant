import time
from pathlib import Path

import pytest


def wait_done(manager, job_id, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = manager.get(job_id)
        if job["state"] not in {"QUEUED", "RUNNING"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {manager.get(job_id)}")


def test_demo_is_a_real_background_process_with_verified_artifacts(tmp_path: Path) -> None:
    from repopilot.application.jobs import JobManager

    manager = JobManager(tmp_path, demo=True)
    job = manager.submit("Repair the demo fixture")
    assert job["state"] in {"QUEUED", "RUNNING"}
    done = wait_done(manager, job["job_id"])
    assert done["state"] == "SUCCEEDED", done
    assert (tmp_path / "artifacts" / done["task_id"] / "change.patch").exists()
    restarted = JobManager(tmp_path, demo=True)
    assert restarted.get(job["job_id"])["state"] == "SUCCEEDED"


def test_analysis_is_separate_background_result_without_patch(tmp_path: Path) -> None:
    from repopilot.application.jobs import JobManager

    manager = JobManager(tmp_path, demo=True)
    job = manager.submit("Explain the fixture", kind="analysis")
    done = wait_done(manager, job["job_id"])
    assert done["state"] == "SUCCEEDED", done
    assert done["task_id"] is None
    assert done["summary"]


def test_invalid_kind_unknown_job_and_terminal_resume_are_rejected(tmp_path: Path) -> None:
    from repopilot.application.jobs import JobManager

    manager = JobManager(tmp_path, demo=True)
    with pytest.raises(ValueError):
        manager.submit("hello", kind="shell")
    with pytest.raises(KeyError):
        manager.get("missing")
    done = wait_done(manager, manager.submit("repair")["job_id"])
    with pytest.raises(ValueError):
        manager.resume(done["job_id"])
