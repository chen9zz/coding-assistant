import os
import sys
import time
from pathlib import Path

from repopilot.domain.models import ToolRequest, ToolStatus
from repopilot.infrastructure.process import ProcessRunner
from repopilot.tools.builtin import ReadFileTool, RunCommandTool


def test_process_runner_truncates_output_by_bytes(tmp_path: Path) -> None:
    """Removing the byte budget would let one tool exhaust the agent context."""
    result = ProcessRunner().run(
        [sys.executable, "-c", "print('x' * 1000)"],
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_bytes=64,
    )
    assert result.exit_code == 0
    assert result.truncated is True
    assert len(result.stdout.encode("utf-8")) <= 64


def test_process_runner_times_out_without_hanging(tmp_path: Path) -> None:
    """Ignoring timeout would allow a test command to block the worker forever."""
    result = ProcessRunner().run(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        cwd=tmp_path,
        timeout_seconds=0.05,
        max_output_bytes=128,
    )
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.duration_ms < 1000


def test_timeout_terminates_descendants_in_the_same_process_group(tmp_path: Path) -> None:
    """Killing only the direct test process would leave repository subprocesses running."""
    marker = tmp_path / "orphan-ran"
    child_code = (
        "import time; from pathlib import Path; "
        f"time.sleep(.3); Path({str(marker)!r}).touch()"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10)"
    )

    result = ProcessRunner().run(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        timeout_seconds=0.05,
        max_output_bytes=128,
    )
    time.sleep(0.5)

    assert result.timed_out is True
    assert marker.exists() is False


def test_timeout_kills_descendant_even_when_parent_exits_and_child_ignores_term(
    tmp_path: Path,
) -> None:
    """A parent exiting on SIGTERM must not hide a surviving child from cleanup."""
    marker = tmp_path / "term-ignoring-orphan"
    child_code = (
        "import signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"time.sleep(.3); Path({str(marker)!r}).touch()"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10)"
    )

    result = ProcessRunner().run(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        timeout_seconds=0.1,
        max_output_bytes=128,
    )
    time.sleep(0.5)

    assert result.timed_out is True
    assert marker.exists() is False


def test_process_runner_does_not_inherit_unapproved_environment(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Inheriting a model API key would expose it to repository test code."""
    os.environ["REPOPILOT_SECRET_TEST"] = "must-not-leak"
    try:
        result = ProcessRunner().run(
            [
                sys.executable,
                "-c",
                "import os; print(os.getenv('REPOPILOT_SECRET_TEST', 'missing'))",
            ],
            cwd=tmp_path,
            timeout_seconds=2,
            max_output_bytes=128,
        )
    finally:
        os.environ.pop("REPOPILOT_SECRET_TEST", None)
    assert result.stdout.strip() == "missing"


def test_process_runner_treats_shell_metacharacters_as_plain_arguments(tmp_path: Path) -> None:
    """Switching to shell execution would create a command-injection primitive."""
    marker = tmp_path / "should-not-exist"
    result = ProcessRunner().run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", f";touch {marker}"],
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_bytes=256,
    )
    assert result.stdout.strip() == f";touch {marker}"
    assert marker.exists() is False


def test_run_command_clamps_its_process_to_the_task_time_remaining(tmp_path: Path) -> None:
    """The task deadline must override the command tool's longer default timeout."""
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(2)\n")
    result = RunCommandTool().execute(
        tmp_path,
        ToolRequest(
            operation_id="slow-command",
            name="run_command",
            arguments={"argv": [sys.executable, str(script)]},
        ),
        timeout_seconds=0.05,
    )
    assert result.status is ToolStatus.FAILED
    assert isinstance(result.output, dict) and result.output["timed_out"] is True
    assert result.duration_ms < 1000


def test_read_file_bounds_a_large_sparse_regular_file_before_loading_it(
    tmp_path: Path,
) -> None:
    """Output truncation must also be an input-memory bound and must reject special files."""
    large = tmp_path / "large.txt"
    with large.open("wb") as handle:
        handle.write(b"x" * 65_537)
        handle.seek(1024 * 1024 * 1024 - 1)
        handle.write(b"\n")

    started = time.monotonic()
    result = ReadFileTool(max_bytes=64_000).execute(
        tmp_path,
        ToolRequest(
            operation_id="bounded-read",
            name="read_file",
            arguments={"path": "large.txt"},
        ),
        timeout_seconds=0.5,
    )

    assert time.monotonic() - started < 0.5
    assert result.status is ToolStatus.SUCCEEDED
    assert result.truncated is True
    assert isinstance(result.output, dict)
    assert len(str(result.output["content"]).encode()) <= 64_000
    assert result.output["size_bytes"] == 1024 * 1024 * 1024
