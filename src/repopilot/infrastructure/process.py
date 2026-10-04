"""Shell-free, environment-limited, time-bounded process execution."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    cwd: Path
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool


class ProcessRunner:
    """Execute an argv array without a shell or ambient secret environment."""

    _ENV_ALLOWLIST = frozenset({"LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TMPDIR"})

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        extra_env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> ProcessResult:
        if not argv or any(not isinstance(part, str) or not part for part in argv):
            raise ValueError("argv must contain non-empty strings")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_output_bytes < 0:
            raise ValueError("max_output_bytes cannot be negative")
        resolved_cwd = cwd.resolve(strict=True)
        environment = {
            key: value for key, value in os.environ.items() if key in self._ENV_ALLOWLIST
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if extra_env:
            disallowed = set(extra_env) - self._ENV_ALLOWLIST
            if disallowed:
                names = ", ".join(sorted(disallowed))
                raise ValueError(f"environment keys are not allowlisted: {names}")
            environment.update(extra_env)

        started = time.monotonic()
        timed_out = False
        exit_code: int | None
        stdout_bytes = bytearray()
        stderr_bytes = bytearray()
        output_lock = Lock()
        total_seen = 0
        truncated = False

        def drain(stream: BinaryIO | None, destination: bytearray) -> None:
            nonlocal total_seen, truncated
            if stream is None:
                return
            while chunk := stream.read(8192):
                with output_lock:
                    remaining = max(0, max_output_bytes - len(stdout_bytes) - len(stderr_bytes))
                    destination.extend(chunk[:remaining])
                    total_seen += len(chunk)
                    truncated = truncated or total_seen > max_output_bytes

        process = subprocess.Popen(
            list(argv),
            cwd=resolved_cwd,
            env=environment,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        readers = (
            Thread(target=drain, args=(process.stdout, stdout_bytes), daemon=True),
            Thread(target=drain, args=(process.stderr, stderr_bytes), daemon=True),
        )
        for reader in readers:
            reader.start()
        if process.stdin is not None:
            try:
                process.stdin.write((input_text or "").encode("utf-8"))
                process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            self._terminate_process_group(process)
        for reader in readers:
            reader.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        return ProcessResult(
            argv=tuple(argv),
            cwd=resolved_cwd,
            exit_code=exit_code,
            stdout=bytes(stdout_bytes).decode("utf-8", errors="replace"),
            stderr=bytes(stderr_bytes).decode("utf-8", errors="replace"),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            timed_out=timed_out,
            truncated=truncated,
        )

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.2)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return
