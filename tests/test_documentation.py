from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "README.md",
    "docs/PROJECT_GUIDE.md",
    "docs/CODE_WALKTHROUGH.md",
    "docs/PROJECT_BRIEF_AND_RESUME.md",
    "docs/BUSINESS_INTEGRATION.md",
    "docs/EVALUATION.md",
    "docs/SECURITY.md",
    "docs/DEMO.md",
    "docs/SOURCES.md",
    "docs/VERIFICATION_REPORT.md",
)
UNFINISHED = ("TB" + "D", "TO" + "DO", "FIX" + "ME", "PLACE" + "HOLDER")
LOCAL_LINK = re.compile(r"\[[^]]+\]\((?!https?://|#)([^)#]+)(?:#[^)]+)?\)")
SOURCE_RANGE = re.compile(r"`(src/repopilot/[^`]+\.py):(\d+)-(\d+)`")


@pytest.mark.parametrize("relative_path", REQUIRED)
def test_required_document_is_substantive_and_finished(relative_path: str) -> None:
    path = ROOT / relative_path
    text = path.read_text(encoding="utf-8")
    assert len(text) > 800
    assert not any(marker in text for marker in UNFINISHED)


def test_local_markdown_links_resolve() -> None:
    for relative_path in REQUIRED:
        document = ROOT / relative_path
        for raw_target in LOCAL_LINK.findall(document.read_text(encoding="utf-8")):
            target = (document.parent / raw_target).resolve()
            assert target.exists(), f"broken link in {relative_path}: {raw_target}"


def test_code_walkthrough_ranges_exist_in_final_source() -> None:
    walkthrough = (ROOT / "docs/CODE_WALKTHROUGH.md").read_text(encoding="utf-8")
    ranges = SOURCE_RANGE.findall(walkthrough)
    assert len(ranges) >= 12
    for relative_path, start, end in ranges:
        line_count = len((ROOT / relative_path).read_text(encoding="utf-8").splitlines())
        assert 1 <= int(start) <= int(end) <= line_count


def test_resume_metrics_are_backed_by_generated_evaluation() -> None:
    report = json.loads((ROOT / "docs/generated/evaluation-report.json").read_text())
    resume = (ROOT / "docs/PROJECT_BRIEF_AND_RESUME.md").read_text()
    evaluation = (ROOT / "docs/EVALUATION.md").read_text()
    assert report["resolved"] == report["total"] == 3
    assert report["policy_violations"] == 0
    assert report["recovery_attempts"] == 1
    expected_coverage = f'{report["branch_coverage_percent"]:.2f}%'
    for claim in ("3/3", expected_coverage, "0 策略违规"):
        assert claim in resume
    assert report["report_sha256"] in evaluation


def test_checked_in_evaluation_hash_is_exact_and_portable() -> None:
    summary = json.loads((ROOT / "docs/generated/evaluation-report.json").read_text())
    raw = (ROOT / "docs/generated/evaluation-report.raw.json").read_bytes()

    assert hashlib.sha256(raw).hexdigest() == summary["report_sha256"]
    assert str(Path.home()).encode() not in raw
    assert b'"executable"' not in raw
