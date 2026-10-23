"""Real-repository regressions: native parser lifetime and complete map budgets."""

import os
import subprocess
import sys
from pathlib import Path

from repopilot.tools.repository import RepositoryMapper


def test_native_parser_survives_large_symbol_walk(tmp_path):
    # A native memory fault must fail this subprocess, not kill the pytest runner.
    (tmp_path / "large.py").write_text(
        "".join(f"def function_{i}():\n    return {i}\n\n" for i in range(2000))
    )
    script = """
import gc, sys
from pathlib import Path
from repopilot.tools.repository import RepositoryMapper
mapper = RepositoryMapper()
for _ in range(3):
    result = mapper.build(Path(sys.argv[1]), '', max_chars=1_000_000)
    assert result.capabilities['tree_sitter']
    assert len(result.symbols) == 2000
    assert result.symbols[1999].name == 'function_1999'
    assert result.symbols[1999].start_line == 5998
    gc.collect()
print('native-walk-ok')
"""
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TMPDIR") if key in os.environ}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native-walk-ok"


def test_import_heavy_map_fits_budget_without_discarding_all_files(tmp_path):
    for i in range(80):
        (tmp_path / f"test_module_{i:03}.py").write_text(
            "".join(f"from package_{j} import imported_symbol_{j}\n" for j in range(20))
        )
    result = RepositoryMapper(enable_tree_sitter=False).build(tmp_path, "", max_chars=2000)
    assert len(result.model_dump_json()) <= 2000
    assert result.truncated
    assert result.files
    assert set(result.imports) <= set(result.files)
    assert set(result.test_files) <= set(result.files)
