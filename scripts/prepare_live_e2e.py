#!/usr/bin/env python3
"""Prepare a local, fixed-commit live-model rehearsal without calling a model."""
# ruff: noqa: RUF001 -- Chinese punctuation in the user-facing task prompt is intentional.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

BASE_COMMIT = "be2a6e58e961c2b2945f564b85cdd52d1667cd7a"

TASK = """为 read_file 工具增加可选 offset / limit 字节分页，保持原有路径与安全检查。

约定：offset 是从 0 开始的字节偏移，默认 0；limit 是正整数字节数，默认
64,000，上限 64,000。负 offset、非整数（包括 bool）、零/负/超限 limit
应返回 FAILED，不得静默忽略。offset 到达或超过 EOF 返回空 content。
省略参数仍保留旧调用行为。truncated 表示此次读取结束后仍有剩余字节；
不改变 size_bytes 的整文件含义。UTF-8 截断沿用既有 replacement 解码策略。

更新模型可见的 tool_catalog，声明可选 integer offset / limit 及边界，
并补充仓库内回归测试。保持路径隔离、普通文件限制、二进制检查和时间预算。
二进制检查必须独立于分页：文件头前 8192 字节含 NUL 时，即使 limit 很小、
offset 跳过文件头或超过 EOF，也必须拒绝；目标页含 NUL 也应拒绝。
仅修改 src/repopilot/tools/builtin.py、src/repopilot/tools/catalog.py 及相关测试。
使用宿主给定的外部验收命令验证；不得修改、替换或绕过外部 acceptance.py。
完成后说明修改、实际测试结果及剩余限制，不要推送或创建远程 PR。
"""

ACCEPTANCE_SOURCE = r'''"""Host-owned acceptance: run from the candidate checkout/worktree root."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

source = Path.cwd().resolve() / "src"
sys.path.insert(0, str(source))
try:
    from repopilot.domain.models import ToolRequest, ToolStatus
    from repopilot.tools import builtin
    from repopilot.tools.catalog import tool_catalog
    if not Path(builtin.__file__).resolve().is_relative_to(source):
        raise RuntimeError("candidate source was not imported from current worktree")
except Exception as error:
    print(json.dumps({"status": "ENVIRONMENT_ERROR", "error": type(error).__name__}))
    raise SystemExit(2)

failures = []
passed = []
with tempfile.TemporaryDirectory(prefix="forgepilot-acceptance-") as temporary:
    root = Path(temporary)
    fixture = root / "fixture"
    fixture.mkdir()
    (fixture / "sample.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (fixture / "binary.bin").write_bytes(b"hello\x00world")
    (fixture / "late-header.bin").write_bytes(b"a" * 8191 + b"\x00" + b"tail")
    (fixture / "late-page.bin").write_bytes(b"a" * 9000 + b"\x00end")
    (fixture / "large.txt").write_bytes(b"a" * 20000)
    (fixture / "directory").mkdir()
    (root / "outside.txt").write_text("must remain outside", encoding="utf-8")
    (fixture / "escape").symlink_to(root / "outside.txt")
    tool = builtin.ReadFileTool()

    def run(arguments):
        return tool.execute(fixture, ToolRequest(
            operation_id="external-acceptance", name="read_file", arguments=arguments))

    def content(arguments, wanted, truncated=None):
        result = run(arguments)
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.output, dict)
        assert result.output["content"] == wanted
        assert result.output["size_bytes"] == 17
        if truncated is not None:
            assert result.truncated is truncated

    def reject(arguments):
        assert run(arguments).status is ToolStatus.FAILED

    def large_eof(offset):
        result = run({"path": "large.txt", "offset": offset, "limit": 10})
        assert result.status is ToolStatus.SUCCEEDED
        assert result.output["content"] == ""
        assert result.output["size_bytes"] == 20000
        assert result.truncated is False

    def schema():
        entry = next(item for item in tool_catalog(builtin.build_builtin_registry())
                     if item["name"] == "read_file")
        spec = entry["input_schema"]
        assert spec["properties"]["offset"]["type"] == "integer"
        assert spec["properties"]["offset"]["minimum"] == 0
        assert spec["properties"]["limit"]["type"] == "integer"
        assert spec["properties"]["limit"]["minimum"] == 1
        assert spec["properties"]["limit"]["maximum"] == 64000
        assert "offset" not in spec["required"] and "limit" not in spec["required"]

    checks = [
        ("legacy-call", lambda: content({"path": "sample.txt"}, "alpha\nbeta\ngamma\n", False)),
        ("offset-limit", lambda: content(
            {"path": "sample.txt", "offset": 6, "limit": 5}, "beta\n", True)),
        ("offset-default-limit", lambda: content(
            {"path": "sample.txt", "offset": 11}, "gamma\n", False)),
        ("limit-only", lambda: content({"path": "sample.txt", "limit": 1}, "a", True)),
        ("offset-eof", lambda: content(
            {"path": "sample.txt", "offset": 17, "limit": 5}, "", False)),
        ("offset-past-eof", lambda: content(
            {"path": "sample.txt", "offset": 99, "limit": 5}, "", False)),
        ("catalog-pagination", schema),
        ("path-boundary", lambda: reject({"path": "../outside.txt", "offset": 0, "limit": 5})),
        ("binary-rejected", lambda: reject({"path": "binary.bin"})),
        ("binary-small-limit", lambda: reject({"path": "binary.bin", "limit": 1})),
        ("binary-skipped-prefix", lambda: reject(
            {"path": "binary.bin", "offset": 6, "limit": 5})),
        ("binary-at-eof", lambda: reject({"path": "binary.bin", "offset": 11})),
        ("binary-far-offset", lambda: reject({"path": "binary.bin", "offset": 1000000})),
        ("binary-header-boundary", lambda: reject(
            {"path": "late-header.bin", "offset": 8192, "limit": 4})),
        ("binary-in-page", lambda: reject(
            {"path": "late-page.bin", "offset": 9000, "limit": 4})),
        ("directory-rejected", lambda: reject({"path": "directory", "offset": 1})),
        ("symlink-escape", lambda: reject({"path": "escape", "limit": 1})),
        ("large-file-eof", lambda: large_eof(20000)),
        ("large-file-past-eof", lambda: large_eof(20001)),
        ("large-file-huge-offset", lambda: large_eof(10**40)),
    ]
    for field, invalid in [("offset", -1), ("offset", True), ("offset", 1.5), ("offset", "2"),
                           ("limit", 0), ("limit", -1), ("limit", 64001), ("limit", True),
                           ("limit", 1.5), ("limit", "2"), ("offset", None), ("limit", None)]:
        checks.append((f"invalid-{field}-{invalid!r}",
                       lambda field=field, invalid=invalid: reject(
                           {"path": "sample.txt", field: invalid})))
    for name, check in checks:
        try:
            check()
        except (AssertionError, KeyError, TypeError, ValueError):
            failures.append(name)
        except Exception as error:
            print(json.dumps({"status": "ENVIRONMENT_ERROR", "check": name,
                              "error": type(error).__name__}))
            raise SystemExit(2)
        else:
            passed.append(name)
print(json.dumps({"status": "FAIL" if failures else "PASS", "failures": failures,
                  "passed": passed, "acceptance_version": 3}, ensure_ascii=False), flush=True)
if failures:
    raise SystemExit(1)
# The host-owned gate also runs candidate regression tests, not just feature assertions.
# No ambient model/database secrets or inherited coverage instrumentation reach tests.
environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
               if key in os.environ}
environment.update(PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
try:
    regression = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=source.parent, env=environment, timeout=100, check=False)
except subprocess.TimeoutExpired:
    print(json.dumps({"regression_status": "TIMEOUT"}))
    raise SystemExit(1)
print(json.dumps({"regression_status": "PASS" if regression.returncode == 0 else "FAIL"}))
raise SystemExit(0 if regression.returncode == 0 else 1)
'''


def local_environment() -> dict[str, str]:
    """The baseline has no need for provider keys, PYTHONPATH, or git credentials."""
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG")
        if key in os.environ
    }
    environment.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    return environment


def verify_baseline(repository: Path, environment: dict[str, str]) -> None:
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{BASE_COMMIT}^{{commit}}"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if resolved != BASE_COMMIT:
        raise ValueError("source repository does not contain the fixed baseline commit")


def prepare(source: Path, destination: Path) -> dict[str, object]:
    source = source.resolve(strict=True)
    # Check the lexical destination before resolve(), so a dangling symlink is also rejected.
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"destination already exists; nothing overwritten: {destination}")
    destination = destination.resolve(strict=False)
    if not destination.parent.is_dir():
        raise ValueError("destination parent must already exist")
    if destination.is_relative_to(source):
        raise ValueError("destination must be outside the source repository")
    environment = local_environment()
    bundle = source / "benchmarks" / "live-e2e-baseline.bundle"
    if bundle.is_file():
        # Public imports and shallow clones may have .git without our fixed history.
        # The bundled source fixture is checked against BASE_COMMIT after cloning.
        clone_source = bundle.resolve(strict=True)
    elif (source / ".git").exists():
        clone_source = source
        verify_baseline(source, environment)
    else:
        raise ValueError(
            "source has no .git metadata or benchmarks/live-e2e-baseline.bundle; "
            "use the complete offline source delivery or a local Git checkout"
        )
    destination.mkdir(mode=0o700)
    repository = destination / "repository"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", "--no-checkout", str(clone_source), str(repository)],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    verify_baseline(repository, environment)
    subprocess.run(
        ["git", "checkout", "--detach", BASE_COMMIT],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    acceptance = destination / "acceptance.py"
    acceptance.write_text(ACCEPTANCE_SOURCE, encoding="utf-8")
    acceptance.chmod(0o444)
    (destination / "task.md").write_text(TASK, encoding="utf-8")
    # Do not resolve this path: a venv's interpreter may be a symlink to a different environment.
    command = [str(Path(sys.executable).absolute()), str(acceptance)]
    baseline = subprocess.run(
        command,
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    try:
        result = json.loads(baseline.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(
            "baseline did not produce an acceptance report; check Python dependencies. "
            f"Prepared files remain at {destination}"
        ) from error
    (destination / "baseline.json").write_text(json.dumps(result, indent=2) + "\n")
    if (
        baseline.returncode != 1
        or result.get("status") != "FAIL"
        or not {"offset-limit", "catalog-pagination"}.issubset(result.get("failures", []))
    ):
        raise ValueError(
            "baseline must fail for the missing pagination contract, not environment errors; "
            f"inspect {destination / 'baseline.json'}"
        )
    manifest: dict[str, object] = {
        "acceptance_version": 3,
        "base_commit": BASE_COMMIT,
        "repository": str(repository),
        "acceptance": str(acceptance),
        "acceptance_sha256": hashlib.sha256(acceptance.read_bytes()).hexdigest(),
        "test_command": command,
        "test_command_shell": shlex.join(command),
        "task_file": str(destination / "task.md"),
        "workspace": str(destination / "workspace"),
        "model_calls": 0,
        "baseline": result,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination", required=True, type=Path, help="New directory outside repo"
    )
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    options = parser.parse_args()
    try:
        manifest = prepare(options.source, options.destination)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Preparation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
