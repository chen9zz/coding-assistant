"""Load and prepare versioned benchmark fixture cases."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from pydantic import Field, TypeAdapter

from repopilot.agent.actions import AgentAction
from repopilot.agent.scripted import ScriptedRuntime
from repopilot.domain.models import FrozenModel, TaskSpec
from repopilot.infrastructure.process import ProcessRunner

_ACTIONS: TypeAdapter[list[AgentAction]] = TypeAdapter(list[AgentAction])


class FixtureDefinition(FrozenModel):
    case_id: str
    issue: str
    expected_changed_paths: tuple[str, ...]
    inject_crash: bool = False


class FixtureCase(FrozenModel):
    definition: FixtureDefinition
    root: Path
    actions: tuple[AgentAction, ...] = Field(min_length=2)

    @property
    def case_id(self) -> str:
        return self.definition.case_id

    def runtime(self) -> ScriptedRuntime:
        return ScriptedRuntime(self.actions)

    def prepare_repository(self, destination: Path) -> Path:
        source = self.root / "source"
        shutil.copytree(source, destination)
        runner = ProcessRunner()
        for argv in (
            ["git", "init", "-b", "main"],
            ["git", "config", "user.email", "evaluation@repopilot.local"],
            ["git", "config", "user.name", "RepoPilot Evaluation"],
            ["git", "add", "."],
            ["git", "commit", "-m", "seed benchmark bug"],
        ):
            result = runner.run(
                argv,
                cwd=destination,
                timeout_seconds=15,
                max_output_bytes=64_000,
            )
            if result.exit_code != 0:
                raise RuntimeError(result.stderr or f"failed to prepare fixture: {argv}")
        return destination

    def install_hidden_tests(self, worktree: Path) -> tuple[Path, ...]:
        installed: list[Path] = []
        for source in sorted((self.root / "hidden").glob("*.py")):
            target = worktree / source.name
            shutil.copy2(source, target)
            installed.append(target)
        return tuple(installed)

    def task_spec(self, repository: Path) -> TaskSpec:
        return TaskSpec(
            repository=repository,
            issue=self.definition.issue,
            test_command=(sys.executable, "-m", "pytest", "-q"),
            allowed_paths=self.definition.expected_changed_paths,
            max_changed_files=len(self.definition.expected_changed_paths),
            max_steps=max(4, len(self.actions) + 1),
        )


def load_builtin_cases(root: Path | None = None) -> tuple[FixtureCase, ...]:
    fixture_root = root or Path(__file__).resolve().parents[3] / "benchmarks" / "fixtures"
    cases: list[FixtureCase] = []
    for case_root in sorted(path for path in fixture_root.iterdir() if path.is_dir()):
        definition = FixtureDefinition.model_validate_json(
            (case_root / "case.json").read_text()
        )
        actions = tuple(_ACTIONS.validate_json((case_root / "actions.json").read_text()))
        if definition.case_id != case_root.name:
            raise ValueError(f"fixture directory/name mismatch: {case_root}")
        cases.append(FixtureCase(definition=definition, root=case_root, actions=actions))
    if not cases:
        raise ValueError(f"no benchmark fixtures found under {fixture_root}")
    identifiers = [case.case_id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("benchmark fixture identifiers must be unique")
    return tuple(cases)


def load_action_json(path: Path) -> tuple[AgentAction, ...]:
    """Load external action fixtures through the same strict schema."""
    return tuple(_ACTIONS.validate_python(json.loads(path.read_text())))
