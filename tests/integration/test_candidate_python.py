"""Python verification must exercise the candidate, never the installed host checkout."""

import sys
from pathlib import Path

from repopilot.infrastructure.process import ProcessRunner


def test_pytest_uses_contained_candidate_src_without_inheriting_secrets(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "candidate_only_module.py").write_text("VALUE = 17\n")
    (tmp_path / "test_candidate.py").write_text(
        "import os\nfrom candidate_only_module import VALUE\n"
        "def test_candidate():\n    assert VALUE == 17\n"
        "    assert 'DEEPSEEK_API_KEY' not in os.environ\n"
        "    assert 'untrusted-parent-path' not in os.environ.get('PYTHONPATH', '')\n"
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-only")
    monkeypatch.setenv("PYTHONPATH", "untrusted-parent-path")
    result = ProcessRunner().run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_candidate.py"],
        cwd=tmp_path,
        timeout_seconds=10,
        max_output_bytes=5000,
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_pytest_does_not_add_symlinked_external_src(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").symlink_to(outside, target_is_directory=True)
    (root / "test_environment.py").write_text(
        "import os\ndef test_environment():\n    assert 'PYTHONPATH' not in os.environ\n"
    )
    result = ProcessRunner().run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_environment.py"],
        cwd=root,
        timeout_seconds=10,
        max_output_bytes=5000,
    )
    assert result.exit_code == 0, result.stdout + result.stderr
