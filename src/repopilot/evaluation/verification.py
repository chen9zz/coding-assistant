"""Require named hidden test completion, not merely a zero process exit."""

from __future__ import annotations

import ast
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from repopilot.infrastructure.process import ProcessRunner

from .cases import FixtureCase


def verify_hidden(case: FixtureCase, worktree: Path, report: Path) -> bool:
    expected = {
        (p.stem, node.name)
        for p in sorted((case.root / "hidden").glob("*.py"))
        for node in ast.parse(p.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }
    if not expected or report.exists() or report.is_symlink():
        return False
    try:
        installed = case.install_hidden_tests(worktree)
    except OSError:
        return False
    result = ProcessRunner().run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *(p.name for p in installed),
            f"--junitxml={report}",
        ],
        cwd=worktree,
        timeout_seconds=120,
        max_output_bytes=128_000,
    )
    if result.exit_code != 0 or not report.is_file() or report.is_symlink():
        return False
    if report.stat().st_size > 1_000_000:
        return False
    try:
        document = ET.fromstring(report.read_bytes())
    except ET.ParseError:
        return False
    tests = list(document.iter("testcase"))
    actual = {(test.get("classname"), test.get("name")) for test in tests}
    return (
        actual == expected and len(tests) == len(expected) and all(not list(test) for test in tests)
    )
