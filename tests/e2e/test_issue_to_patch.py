from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from repopilot.agent.actions import AgentAction, AgentContext
from repopilot.agent.scripted import ScriptedRuntime
from repopilot.api.app import create_app
from repopilot.bootstrap import build_local_container
from repopilot.domain.models import TaskState
from repopilot.evaluation.cases import load_builtin_cases


def test_issue_to_verified_patch_public_journey(tmp_path: Path) -> None:
    """A successful public run must leave a self-contained, machine-readable evidence bundle."""
    case = load_builtin_cases()[0]
    source = case.prepare_repository(tmp_path / "source")
    container = build_local_container(
        tmp_path / "runtime",
        task_id_factory=lambda: "public-journey",
    )
    task = container.service.create(case.task_spec(source))

    result = container.service.run(task.task_id, case.runtime())
    artifacts = container.service.artifacts(task.task_id)

    assert result.state is TaskState.SUCCEEDED
    patch_text = artifacts.patch.path.read_text()
    raw_diff = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", "--"],
        cwd=result.worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert patch_text.startswith("diff --git")
    assert patch_text == raw_diff
    assert artifacts.patch.metadata["sensitivity"] == "access-controlled-exact"
    assert artifacts.patch.sha256 == hashlib.sha256(raw_diff.encode()).hexdigest()
    test_report = json.loads(artifacts.test_report.path.read_text())
    assert test_report["status"] == "SUCCEEDED"
    assert test_report["output"]["exit_code"] == 0
    trajectory = artifacts.trajectory.path.read_text().splitlines()
    assert len(trajectory) == len(container.service.events(task.task_id))
    assert all(json.loads(line)["task_id"] == task.task_id for line in trajectory)
    assert result.spec.issue in artifacts.pr_draft.path.read_text()

    with TestClient(create_app(container.service)) as client:
        response = client.get(f"/v1/tasks/{task.task_id}/artifacts")
    assert response.status_code == 200
    assert response.json()["patch"]["sha256"] == artifacts.patch.sha256


def test_exact_patch_is_not_corrupted_by_audit_log_redaction(tmp_path: Path) -> None:
    """A redacted patch can be invalid, so the local bundle verifies then preserves exact bytes."""
    case = load_builtin_cases()[0]
    source = case.prepare_repository(tmp_path / "source")
    container = build_local_container(tmp_path / "runtime", lambda: "exact-patch")
    first = case.actions[0]
    raw_patch = str(first.arguments["patch"]).replace(
        "+def parse_port",
        "+# OPENAI_API_KEY=sk-test123\n+def parse_port",
    ).replace("@@ -1,3 +1,7 @@", "@@ -1,3 +1,8 @@")
    runtime = ScriptedRuntime(
        (
            first.model_copy(update={"arguments": {"patch": raw_patch}}),
            *case.actions[1:],
        )
    )

    task = container.service.create(case.task_spec(source))
    result = container.service.run(task.task_id, runtime)
    artifacts = container.service.artifacts(task.task_id)
    persisted = container.store.get_operation(task.task_id, f"diff-{task.task_id}")

    assert result.state is TaskState.SUCCEEDED
    assert "sk-test123" in artifacts.patch.path.read_text()
    assert persisted is not None
    assert "sk-test123" not in persisted.model_dump_json()


def test_artifact_generation_refuses_unverified_task(tmp_path: Path) -> None:
    case = load_builtin_cases()[0]
    source = case.prepare_repository(tmp_path / "source")
    container = build_local_container(tmp_path / "runtime", lambda: "not-run")
    task = container.service.create(case.task_spec(source))

    try:
        container.service.artifacts(task.task_id)
    except ValueError as error:
        assert "SUCCEEDED" in str(error)
    else:
        raise AssertionError("unverified tasks must not produce release artifacts")


def test_task_input_and_final_text_are_redacted_before_storage_or_api(tmp_path: Path) -> None:
    """Gateway-only redaction would still leak model text and issue credentials through SSE."""
    secret = "sk-redaction-secret"
    case = load_builtin_cases()[0]
    source = case.prepare_repository(tmp_path / "source")
    container = build_local_container(tmp_path / "runtime", lambda: "redacted-task")
    spec = case.task_spec(source).model_copy(
        update={"issue": f"Fix config with OPENAI_API_KEY={secret}"}
    )
    actions = (*case.actions[:-1], AgentAction.final(f"completed with token={secret}"))

    created = container.service.create(spec)
    result = container.service.run(created.task_id, ScriptedRuntime(actions))

    serialized_events = "\n".join(
        event.model_dump_json() for event in container.service.events(created.task_id)
    )
    with TestClient(create_app(container.service)) as client:
        api_payload = client.get(f"/v1/tasks/{created.task_id}").text
        sse_payload = client.get(f"/v1/tasks/{created.task_id}/events").text

    assert secret not in created.model_dump_json()
    assert secret not in result.model_dump_json()
    assert secret not in serialized_events
    assert secret not in api_payload
    assert secret not in sse_payload


def test_exact_artifact_includes_staged_and_untracked_new_files(tmp_path: Path) -> None:
    """A successful bundle must describe the complete reviewed worktree, not one diff class."""
    case = load_builtin_cases()[0]
    source = case.prepare_repository(tmp_path / "source")
    container = build_local_container(tmp_path / "runtime", lambda: "complete-artifact")
    spec = case.task_spec(source).model_copy(
        update={
            "allowed_paths": ("config.py", "staged-note.txt", "untracked-note.txt"),
            "max_changed_files": 3,
        }
    )
    task = container.service.create(spec)
    executing = container.service.orchestrator._prepare(task)
    assert executing.worktree is not None

    class AddsNewFilesRuntime:
        def next_action(self, context: AgentContext) -> AgentAction:
            if not context.exchanges:
                return case.actions[0]
            (executing.worktree / "staged-note.txt").write_text("staged evidence\n")
            subprocess.run(
                ["git", "add", "staged-note.txt"],
                cwd=executing.worktree,
                check=True,
                capture_output=True,
            )
            (executing.worktree / "untracked-note.txt").write_text("untracked evidence\n")
            return AgentAction.final("all files ready")

    result = container.service.resume(task.task_id, AddsNewFilesRuntime())
    artifacts = container.service.artifacts(task.task_id)
    patch = artifacts.patch.path.read_text()

    assert result.state is TaskState.SUCCEEDED
    assert artifacts.patch.metadata["changed_paths"] == (
        "config.py",
        "staged-note.txt",
        "untracked-note.txt",
    )
    assert "diff --git a/staged-note.txt b/staged-note.txt" in patch
    assert "diff --git a/untracked-note.txt b/untracked-note.txt" in patch
