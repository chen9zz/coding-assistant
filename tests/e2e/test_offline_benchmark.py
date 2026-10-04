from pathlib import Path

from repopilot.domain.models import ToolRequest, ToolStatus
from repopilot.evaluation.cases import load_builtin_cases
from repopilot.evaluation.graders import EvaluationReport
from repopilot.evaluation.runner import EvaluationRunner
from repopilot.tools.builtin import ApplyPatchTool


def outcome_signature(report: EvaluationReport) -> tuple[tuple[object, ...], ...]:
    cases = report.cases
    return tuple(
        (
            case.case_id,
            case.resolved,
            case.tests_passed,
            case.scope_ok,
            case.patch_sha256,
            case.policy_violations,
            case.tool_steps,
            case.recovery_attempts,
        )
        for case in cases
    )


def test_offline_benchmark_reports_real_metrics(tmp_path: Path) -> None:
    """A hand-written success claim would not prove the delivered system works."""
    report = EvaluationRunner(tmp_path / "run").run(load_builtin_cases())

    assert report.total == 3
    assert report.resolved == 3
    assert report.resolved_rate == 1.0
    assert report.policy_violations == 0
    assert report.recovery_attempts == 1
    assert report.environment.runtime == "scripted-offline"
    assert all(case.patch_sha256 for case in report.cases)
    assert (tmp_path / "run" / "evaluation-results" / "report.json").exists()
    assert (tmp_path / "run" / "evaluation-results" / "report.md").exists()


def test_offline_benchmark_report_does_not_leak_machine_specific_paths(
    tmp_path: Path,
) -> None:
    """A checked-in evidence report must be portable and safe to publish."""
    EvaluationRunner(tmp_path / "run").run(load_builtin_cases())
    report = (tmp_path / "run" / "evaluation-results" / "report.json").read_text()

    assert '"executable"' not in report
    assert str(Path.home()) not in report


def test_offline_benchmark_outcomes_are_reproducible(tmp_path: Path) -> None:
    """Changing outcomes between identical runs would make resume/eval evidence unreliable."""
    cases = load_builtin_cases()
    first = EvaluationRunner(tmp_path / "first").run(cases)
    second = EvaluationRunner(tmp_path / "second").run(cases)

    assert outcome_signature(first) == outcome_signature(second)


def test_every_builtin_fixture_patch_applies_to_its_seed_repository(tmp_path: Path) -> None:
    """A malformed benchmark patch would measure fixture syntax rather than agent reliability."""
    for case in load_builtin_cases():
        repository = case.prepare_repository(tmp_path / case.case_id)
        action = next(action for action in case.actions if action.operation_id)
        result = ApplyPatchTool().execute(
            repository,
            ToolRequest(
                operation_id=action.operation_id or "missing",
                name=action.tool_name or "apply_patch",
                arguments=action.arguments,
            ),
        )
        assert result.status is ToolStatus.SUCCEEDED, (case.case_id, result.error)
