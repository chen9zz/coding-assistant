from pathlib import Path

import pytest

from repopilot.application.jobs import JobManager
from repopilot.domain.models import ToolRequest, ToolStatus


def test_analysis_gateway_cannot_write_or_execute(tmp_path: Path) -> None:
    from repopilot.tools.builtin import build_builtin_registry
    from repopilot.tools.redaction import Redactor
    from repopilot.worker import AnalysisGateway

    gateway = AnalysisGateway(build_builtin_registry(), Redactor())
    for name in ("apply_patch", "run_command", "agent_spawn", "mcp__server__tool"):
        result = gateway.execute(
            "analysis", tmp_path, ToolRequest(operation_id=name, name=name, arguments={})
        )
        assert result.status is ToolStatus.DENIED


def test_search_query_cannot_become_a_ripgrep_preprocessor(tmp_path: Path) -> None:
    from repopilot.tools.builtin import SearchTool

    (tmp_path / "file.txt").write_text("--pre=/usr/bin/false\n")
    result = SearchTool().execute(
        tmp_path,
        ToolRequest(
            operation_id="search", name="search", arguments={"query": "--pre=/usr/bin/false"}
        ),
    )
    assert result.status is ToolStatus.SUCCEEDED
    assert "file.txt" in str(result.output)


def test_delegation_cannot_access_another_parents_child(tmp_path: Path, monkeypatch) -> None:
    from repopilot.tools.delegation import DelegationTool

    manager = JobManager(tmp_path, demo=True)
    monkeypatch.setattr(manager, "_launch", lambda _: None)
    parent = manager.submit("parent")
    manager.store.update(parent["job_id"], state="RUNNING")
    child = manager.submit("child", kind="analysis", parent_job_id=parent["job_id"])
    request = ToolRequest(
        operation_id="r", name="agent_result", arguments={"job_id": child["job_id"]}
    )
    denied = DelegationTool("agent_result", manager, "other-parent").execute(tmp_path, request)
    assert denied.status is ToolStatus.FAILED
    own = DelegationTool("agent_status", manager, parent["job_id"]).execute(tmp_path, request)
    assert own.status is ToolStatus.SUCCEEDED


def test_parent_cancellation_cascades_and_child_cannot_delegate(
    tmp_path: Path, monkeypatch
) -> None:
    manager = JobManager(tmp_path, demo=True)
    monkeypatch.setattr(manager, "_launch", lambda _: None)
    parent = manager.submit("parent")
    manager.store.update(parent["job_id"], state="RUNNING")
    child = manager.submit("child", kind="analysis", parent_job_id=parent["job_id"])
    manager.store.update(child["job_id"], state="RUNNING")
    with pytest.raises(ValueError):
        manager.submit("grandchild", kind="analysis", parent_job_id=child["job_id"])
    manager.cancel(parent["job_id"])
    assert manager.get(child["job_id"])["cancel_requested"] is True
    with pytest.raises(ValueError):
        manager.submit("late child", kind="analysis", parent_job_id=parent["job_id"])
