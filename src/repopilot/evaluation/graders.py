"""Deterministic evaluation models, aggregation, and report writers."""

from __future__ import annotations

import platform
from pathlib import Path

from pydantic import Field, computed_field

from repopilot.domain.models import FrozenModel, TaskState


class EnvironmentInfo(FrozenModel):
    runtime: str = "scripted-offline"
    python: str = platform.python_version()
    platform: str = platform.platform()


class CaseResult(FrozenModel):
    case_id: str
    state: TaskState
    resolved: bool
    tests_passed: bool
    scope_ok: bool
    changed_paths: tuple[str, ...]
    patch_sha256: str
    policy_violations: int = Field(ge=0)
    denied_operations: int = Field(ge=0)
    tool_steps: int = Field(ge=0)
    recovery_attempts: int = Field(ge=0)
    duration_ms: int = Field(ge=0)


class EvaluationReport(FrozenModel):
    cases: tuple[CaseResult, ...]
    environment: EnvironmentInfo = Field(default_factory=EnvironmentInfo)
    duration_ms: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return len(self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved(self) -> int:
        return sum(case.resolved for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_rate(self) -> float:
        return self.resolved / self.total if self.total else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def policy_violations(self) -> int:
        return sum(case.policy_violations for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recovery_attempts(self) -> int:
        return sum(case.recovery_attempts for case in self.cases)


def write_report(report: EvaluationReport, output_directory: Path) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / "report.json"
    markdown_path = output_directory / "report.md"
    json_path.write_text(report.model_dump_json(indent=2) + "\n")
    rows = [
        "# ForgePilot Offline Evaluation",
        "",
        f"- Runtime: `{report.environment.runtime}`",
        f"- Python: `{report.environment.python}`",
        f"- Resolved: **{report.resolved}/{report.total} ({report.resolved_rate:.0%})**",
        f"- Policy violations: **{report.policy_violations}**",
        f"- Recovery attempts: **{report.recovery_attempts}**",
        f"- Duration: **{report.duration_ms} ms**",
        "",
        "| Case | State | Resolved | Hidden tests | Scope | Tool steps | Recovery "
        "| Patch SHA-256 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    rows.extend(
        (
            "| {case} | {state} | {resolved} | {tests} | {scope} | {steps} "
            "| {recovery} | `{sha}` |"
        ).format(
            case=case.case_id,
            state=case.state.value,
            resolved="yes" if case.resolved else "no",
            tests="pass" if case.tests_passed else "fail",
            scope="pass" if case.scope_ok else "fail",
            steps=case.tool_steps,
            recovery=case.recovery_attempts,
            sha=case.patch_sha256,
        )
        for case in report.cases
    )
    markdown_path.write_text("\n".join(rows) + "\n")
    return json_path, markdown_path
