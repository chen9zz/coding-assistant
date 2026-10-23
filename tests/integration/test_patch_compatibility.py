"""Real git checks for model diff formatting, without weakening content/path checks."""

from pathlib import Path

from repopilot.domain.models import ToolRequest, ToolStatus
from repopilot.tools.builtin import ApplyPatchTool


def request(patch: str) -> ToolRequest:
    return ToolRequest(operation_id="format-repair", name="apply_patch", arguments={"patch": patch})


def test_patch_recounts_hunks_and_removes_only_final_envelope_marker(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 1\n")
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1,99 +1,99 @@\n-answer = 1\n+answer = 2\n*** End Patch"
    result = ApplyPatchTool().execute(tmp_path, request(patch))
    assert result.status is ToolStatus.SUCCEEDED, result.error
    assert (tmp_path / "app.py").read_text() == "answer = 2\n"


def test_recount_keeps_context_validation_and_atomic_rejection(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 1\n")
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1,99 +1,99 @@\n-answer = 3\n+answer = 2\n*** End Patch"
    result = ApplyPatchTool().execute(tmp_path, request(patch))
    assert result.status is ToolStatus.FAILED
    assert (tmp_path / "app.py").read_text() == "answer = 1\n"


def test_envelope_normalization_does_not_permit_path_escape(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("answer = 1\n")
    patch = (
        "--- a/../outside.py\n+++ b/../outside.py\n"
        "@@ -1 +1 @@\n-answer = 1\n+answer = 2\n*** End Patch"
    )
    result = ApplyPatchTool().execute(repository, request(patch))
    assert result.status is ToolStatus.FAILED
    assert outside.read_text() == "answer = 1\n"
