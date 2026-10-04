"""Reproducible offline evaluation for RepoPilot-R workflows."""

from repopilot.evaluation.cases import FixtureCase, load_builtin_cases
from repopilot.evaluation.graders import EvaluationReport
from repopilot.evaluation.runner import EvaluationRunner

__all__ = ["EvaluationReport", "EvaluationRunner", "FixtureCase", "load_builtin_cases"]

