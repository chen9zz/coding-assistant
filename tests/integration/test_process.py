import os
import sys
import time
from pathlib import Path

from repopilot.domain.models import ToolRequest, ToolResult, ToolStatus
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


def _read(tmp_path: Path, **arguments: object) -> ToolResult:
    return ReadFileTool().execute(
        tmp_path,
        ToolRequest(
            operation_id="paged-read",
            name="read_file",
            arguments={"path": "data.txt", **arguments},
        ),
    )


def _content(result: ToolResult) -> str:
    assert isinstance(result.output, dict)
    value = result.output["content"]
    assert isinstance(value, str)
    return value


def test_read_file_pages_by_byte_offset_and_limit(tmp_path: Path) -> None:
    """Silently ignoring paging arguments would return the whole file body."""
    (tmp_path / "data.txt").write_text("abcdefghij", encoding="utf-8")

    middle = _read(tmp_path, offset=4, limit=3)
    assert middle.status is ToolStatus.SUCCEEDED
    assert _content(middle) == "efg"
    assert isinstance(middle.output, dict)
    assert middle.output["offset"] == 4
    assert middle.output["next_offset"] == 7
    assert middle.output["size_bytes"] == 10
    assert middle.truncated is True

    tail = _read(tmp_path, offset=6, limit=4)
    assert _content(tail) == "ghij"
    assert tail.truncated is False
    assert isinstance(tail.output, dict) and tail.output["next_offset"] is None


def test_read_file_offset_at_or_beyond_eof_returns_empty_content(tmp_path: Path) -> None:
    """Reading past EOF must be an empty success, not an error or a rewind."""
    (tmp_path / "data.txt").write_text("abcdefghij", encoding="utf-8")

    for offset in (10, 11, 1_000):
        result = _read(tmp_path, offset=offset, limit=1)
        assert result.status is ToolStatus.SUCCEEDED
        assert _content(result) == ""
        assert result.truncated is False
        assert isinstance(result.output, dict)
        assert result.output["size_bytes"] == 10


def test_read_file_omitted_paging_keeps_the_previous_default(tmp_path: Path) -> None:
    """Dropping the default would change the legacy call behaviour."""
    (tmp_path / "data.txt").write_bytes(b"x" * 65_000)

    result = _read(tmp_path)
    assert result.status is ToolStatus.SUCCEEDED
    assert len(_content(result).encode()) == 64_000
    assert result.truncated is True
    assert isinstance(result.output, dict)
    assert result.output["offset"] == 0
    assert result.output["next_offset"] == 64_000

    full = _read(tmp_path, offset=0, limit=64_000)
    assert full.truncated is True


def test_read_file_rejects_invalid_paging_arguments(tmp_path: Path) -> None:
    """Invalid paging values must fail loudly instead of being ignored."""
    (tmp_path / "data.txt").write_text("abcdefghij", encoding="utf-8")
    invalid = (
        {"offset": -1},
        {"offset": None},
        {"offset": True},
        {"offset": 1.0},
        {"offset": "1"},
        {"limit": None},
        {"limit": 0},
        {"limit": -5},
        {"limit": 64_001},
        {"limit": False},
        {"limit": 2.5},
        {"limit": "5"},
    )
    for arguments in invalid:
        result = _read(tmp_path, **arguments)
        assert result.status is ToolStatus.FAILED, arguments
        assert result.error, arguments


def test_read_file_explicit_null_paging_is_rejected_not_defaulted(tmp_path: Path) -> None:
    """An explicit JSON null is a concrete value and must not silently become a default."""
    (tmp_path / "data.txt").write_bytes(b"x" * 65_000)

    null_limit = _read(tmp_path, limit=None)
    assert null_limit.status is ToolStatus.FAILED
    assert "limit" in (null_limit.error or "")

    null_offset = _read(tmp_path, offset=None, limit=10)
    assert null_offset.status is ToolStatus.FAILED
    assert "offset" in (null_offset.error or "")

    both_null = _read(tmp_path, offset=None, limit=None)
    assert both_null.status is ToolStatus.FAILED


def test_read_file_large_file_offset_at_or_beyond_eof_returns_empty_content(
    tmp_path: Path,
) -> None:
    """After the header probe, an offset at/beyond EOF must not rewind or overflow."""
    (tmp_path / "data.txt").write_bytes(b"x" * 20_000)

    for offset in (20_000, 20_001, 10**40):
        result = _read(tmp_path, offset=offset, limit=64_000)
        assert result.status is ToolStatus.SUCCEEDED, offset
        assert _content(result) == "", offset
        assert result.truncated is False, offset
        assert isinstance(result.output, dict)
        assert result.output["size_bytes"] == 20_000
        assert result.output["next_offset"] is None
        assert result.output["offset"] == offset


def test_read_file_large_file_mid_offset_reads_from_the_requested_position(
    tmp_path: Path,
) -> None:
    """Seeking past the probe buffer must not replay the already probed header bytes."""
    body = bytes(ord("a") + index % 26 for index in range(20_000))
    (tmp_path / "data.txt").write_bytes(body)

    for offset in (8_192, 8_193, 19_999):
        result = _read(tmp_path, offset=offset, limit=1)
        assert result.status is ToolStatus.SUCCEEDED, offset
        assert _content(result) == chr(body[offset]), offset
        assert isinstance(result.output, dict)
        assert result.output["offset"] == offset
        assert result.output["size_bytes"] == 20_000
        assert result.truncated is (offset < 19_999), offset


def test_read_file_utf8_truncation_uses_replacement_decoding(tmp_path: Path) -> None:
    """A multi-byte character split by a page boundary must decode with replacement."""
    (tmp_path / "data.txt").write_text("a\u00e9b", encoding="utf-8")

    result = _read(tmp_path, offset=0, limit=2)
    assert result.status is ToolStatus.SUCCEEDED
    assert _content(result) == "a\ufffd"
    assert result.truncated is True


def test_read_file_binary_check_ignores_paging_arguments(tmp_path: Path) -> None:
    """A small page would otherwise bypass the NUL check on a binary file."""
    payload = b"a" * 20 + b"\x00" + b"b" * 100
    (tmp_path / "data.txt").write_bytes(payload)

    cases = (
        {"limit": 1},
        {"offset": 21, "limit": 10},
        {"offset": 200, "limit": 10},
        {"offset": 10_000, "limit": 1},
    )
    for arguments in cases:
        result = _read(tmp_path, **arguments)
        assert result.status is ToolStatus.FAILED, arguments
        assert "binary" in (result.error or ""), arguments


def test_read_file_rejects_nul_inside_the_requested_page(tmp_path: Path) -> None:
    """A NUL beyond the header probe must still be rejected when the page includes it."""
    (tmp_path / "data.txt").write_bytes(b"a" * 20_000 + b"\x00" + b"b" * 100)

    rejected = _read(tmp_path, offset=19_999, limit=2)
    assert rejected.status is ToolStatus.FAILED
    assert "binary" in (rejected.error or "")


def test_read_file_keeps_path_isolation_and_regular_file_rules(tmp_path: Path) -> None:
    """Paging must not relax containment or the regular-file restriction."""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    escape = ReadFileTool().execute(
        tmp_path,
        ToolRequest(
            operation_id="escape",
            name="read_file",
            arguments={"path": "../outside.txt", "offset": 0, "limit": 1},
        ),
    )
    assert escape.status is ToolStatus.FAILED

    if not hasattr(os, "mkfifo"):
        return
    os.mkfifo(tmp_path / "data.txt")
    special = ReadFileTool().execute(
        tmp_path,
        ToolRequest(
            operation_id="fifo",
            name="read_file",
            arguments={"path": "data.txt", "offset": 0, "limit": 1},
        ),
        timeout_seconds=1,
    )
    assert special.status is ToolStatus.FAILED


def test_read_file_catalog_declares_optional_byte_paging() -> None:
    """An undeclared option would be unusable for the model."""
    from repopilot.tools.builtin import build_builtin_registry
    from repopilot.tools.catalog import tool_catalog

    catalog = {entry["name"]: entry for entry in tool_catalog(build_builtin_registry())}
    read_schema = catalog["read_file"]["input_schema"]
    assert read_schema["required"] == ["path"]
    assert read_schema["additionalProperties"] is False
    assert read_schema["properties"]["offset"]["type"] == "integer"
    assert read_schema["properties"]["offset"]["minimum"] == 0
    assert read_schema["properties"]["limit"]["type"] == "integer"
    assert read_schema["properties"]["limit"]["minimum"] == 1
    assert read_schema["properties"]["limit"]["maximum"] == 64_000
