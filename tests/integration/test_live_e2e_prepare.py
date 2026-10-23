"""The online rehearsal must start from a real, known-failing independent checkout."""

import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "scripts" / "prepare_live_e2e.py"
BASE = "be2a6e58e961c2b2945f564b85cdd52d1667cd7a"


@pytest.mark.parametrize("regression_passes", [True, False])
def test_acceptance_requires_real_regression_after_feature_pass(
    tmp_path: Path, regression_passes: bool
) -> None:
    """Feature success cannot mask a failing candidate test suite."""
    source = runpy.run_path(str(PREPARE))["ACCEPTANCE_SOURCE"]
    shutil.copytree(ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sentinel.py").write_text(
        "import os\n"
        "def test_candidate_regression():\n"
        "    assert 'DEEPSEEK_API_KEY' not in os.environ\n"
        "    assert 'FORGEPILOT_MYSQL_PASSWORD' not in os.environ\n"
        f"    assert {regression_passes!r}\n"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE_", "COVERAGE_"))
    }
    environment.update(DEEPSEEK_API_KEY="fake-key", FORGEPILOT_MYSQL_PASSWORD="fake-password")
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert json.loads(result.stdout.splitlines()[0])["status"] == "PASS", result.stdout
    assert result.returncode == (0 if regression_passes else 1), result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["regression_status"] == (
        "PASS" if regression_passes else "FAIL"
    )


def prepare(destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PREPARE), "--destination", str(destination)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_prepare_clones_fixed_commit_and_proves_the_missing_feature(tmp_path: Path) -> None:
    destination = tmp_path / "live rehearsal"
    result = prepare(destination)
    assert result.returncode == 0, result.stderr + result.stdout
    manifest = json.loads((destination / "manifest.json").read_text())
    repository = destination / "repository"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True, capture_output=True, check=True
    ).stdout.strip()
    assert head == BASE == manifest["base_commit"]
    assert (repository / ".git").is_dir()
    assert manifest["baseline"]["status"] == "FAIL"
    assert "offset-limit" in manifest["baseline"]["failures"]
    assert "catalog-pagination" in manifest["baseline"]["failures"]
    assert manifest["acceptance_version"] == 3
    assert {
        "legacy-call",
        "path-boundary",
        "binary-rejected",
        "binary-small-limit",
        "binary-skipped-prefix",
        "binary-at-eof",
        "binary-far-offset",
        "binary-header-boundary",
        "directory-rejected",
        "symlink-escape",
    }.issubset(manifest["baseline"]["passed"])
    acceptance = destination / "acceptance.py"
    assert not acceptance.is_relative_to(repository)
    assert not acceptance.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert manifest["test_command"][1] == str(acceptance)
    assert Path(manifest["test_command"][0]).is_absolute()
    assert (destination / "task.md").is_file()
    # Re-running the host-owned command must import this checkout, not the installed package.
    # It validates an external candidate, not coverage of this project's current source tree.
    candidate_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE_", "COVERAGE_"))
    }
    baseline = subprocess.run(
        manifest["test_command"],
        cwd=repository,
        env=candidate_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert baseline.returncode == 1
    assert json.loads(baseline.stdout)["status"] == "FAIL"


def test_prepare_refuses_existing_destination_without_overwriting(tmp_path: Path) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("do not overwrite")
    result = prepare(destination)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert sentinel.read_text() == "do not overwrite"
    assert list(destination.iterdir()) == [sentinel]


def test_prepare_needs_no_model_call_and_never_persists_a_provider_key(tmp_path: Path) -> None:
    # The generated external acceptance command must also be usable without provider keys.
    destination = tmp_path / "without-key"
    original = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "e2e-secret-must-not-appear"
    try:
        result = prepare(destination)
    finally:
        if original is None:
            del os.environ["DEEPSEEK_API_KEY"]
        else:
            os.environ["DEEPSEEK_API_KEY"] = original
    assert result.returncode == 0, result.stderr
    assert "e2e-secret-must-not-appear" not in result.stdout + result.stderr
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["model_calls"] == 0
    assert manifest["baseline"]["status"] == "FAIL"


@pytest.mark.parametrize("unrelated_git_history", [False, True])
def test_prepare_exported_source_uses_bundle_despite_missing_baseline_history(
    tmp_path: Path, unrelated_git_history: bool
) -> None:
    exported = tmp_path / "exported source"
    (exported / "scripts").mkdir(parents=True)
    (exported / "benchmarks").mkdir()
    shutil.copy2(PREPARE, exported / "scripts" / PREPARE.name)
    bundle = exported / "benchmarks" / "live-e2e-baseline.bundle"
    builder = tmp_path / "bundle-builder"
    seed = ROOT / "benchmarks/live-e2e-baseline.bundle"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", str(seed), str(builder)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--allow-empty",
            "-m",
            "later source export commit",
        ],
        cwd=builder,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "bundle", "create", str(bundle), "HEAD"],
        cwd=builder,
        check=True,
        capture_output=True,
    )
    if unrelated_git_history:
        subprocess.run(["git", "init", str(exported)], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "--allow-empty",
                "-m",
                "independent public import",
            ],
            cwd=exported,
            check=True,
            capture_output=True,
        )
    destination = tmp_path / "bundle rehearsal"
    result = subprocess.run(
        [
            sys.executable,
            str(exported / "scripts" / PREPARE.name),
            "--destination",
            str(destination),
        ],
        cwd=exported,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((destination / "manifest.json").read_text())
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=destination / "repository",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == BASE == manifest["base_commit"]
    assert manifest["baseline"]["status"] == "FAIL"
    assert (exported / ".git").exists() is unrelated_git_history
    assert "offset-limit" in manifest["baseline"]["failures"]
