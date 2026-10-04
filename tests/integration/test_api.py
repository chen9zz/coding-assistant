from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repopilot.api.app import create_app
from repopilot.bootstrap import LocalContainer, build_local_container
from repopilot.domain.models import TaskEvent, TaskState, ToolRequest
from repopilot.infrastructure.sqlite import IdempotencyConflict
from repopilot.tools.gateway import OperationInProgress


@pytest.fixture
def container(tmp_path: Path) -> LocalContainer:
    identifiers = iter(["task-1", "task-2", "task-3"])
    return build_local_container(tmp_path / "runtime", lambda: next(identifiers))


@pytest.fixture
def api_client(container: LocalContainer) -> Iterator[TestClient]:
    with TestClient(create_app(container.service)) as client:
        yield client


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def task_payload(repository: Path) -> dict[str, object]:
    return {
        "repository": str(repository),
        "issue": "Fix the failing edge case",
        "test_command": ["python", "-m", "pytest", "-q"],
        "constraints": ["Do not change public API"],
        "allowed_paths": ["src/**", "tests/**"],
        "max_changed_files": 4,
        "max_steps": 8,
        "timeout_seconds": 45,
    }


def test_create_read_and_list_tasks(api_client: TestClient, repository: Path) -> None:
    """Divergent API/application models would make business integration unreliable."""
    created = api_client.post("/v1/tasks", json=task_payload(repository))
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    fetched = api_client.get(f"/v1/tasks/{task_id}")
    listed = api_client.get("/v1/tasks")

    assert fetched.status_code == 200
    assert fetched.json()["state"] == "CREATED"
    assert fetched.json()["spec"]["allowed_paths"] == ["src/**", "tests/**"]
    assert fetched.json()["spec"]["max_changed_files"] == 4
    assert fetched.json()["spec"]["timeout_seconds"] == 45
    assert [task["task_id"] for task in listed.json()] == [task_id]


def test_task_creation_rejects_a_shell_string(api_client: TestClient, repository: Path) -> None:
    """Coercing a shell string at the API boundary would bypass argv-only safety."""
    payload = task_payload(repository)
    payload["test_command"] = "python -m pytest"
    response = api_client.post("/v1/tasks", json=payload)
    assert response.status_code == 422


def test_task_creation_rejects_an_escaping_allowed_path(
    api_client: TestClient, repository: Path
) -> None:
    """Invalid host scope must fail at the HTTP schema boundary, not inside execution."""
    payload = task_payload(repository)
    payload["allowed_paths"] = ["../outside/**"]
    response = api_client.post("/v1/tasks", json=payload)
    assert response.status_code == 422


def test_sse_replays_only_events_after_query_cursor(
    api_client: TestClient,
    container: LocalContainer,
    repository: Path,
) -> None:
    """Replaying acknowledged events would duplicate dashboard and automation state."""
    task_id = api_client.post("/v1/tasks", json=task_payload(repository)).json()["task_id"]
    first = TaskEvent(
        event_id="event-1",
        task_id=task_id,
        sequence=1,
        event_type="task.indexing",
        from_state=TaskState.CREATED,
        to_state=TaskState.INDEXING,
        payload={"worktree": str(repository)},
    )
    container.store.append(first, expected_sequence=0)
    second = TaskEvent(
        event_id="event-2",
        task_id=task_id,
        sequence=2,
        event_type="task.planning",
        from_state=TaskState.INDEXING,
        to_state=TaskState.PLANNING,
    )
    container.store.append(second, expected_sequence=1)

    response = api_client.get(f"/v1/tasks/{task_id}/events?after=1")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 2" in response.text
    assert "event: task.planning" in response.text
    assert "id: 1" not in response.text


def test_sse_last_event_id_header_advances_cursor(
    api_client: TestClient,
    container: LocalContainer,
    repository: Path,
) -> None:
    """Ignoring EventSource reconnection identity would resend stale transitions."""
    task_id = api_client.post("/v1/tasks", json=task_payload(repository)).json()["task_id"]
    container.store.append(
        TaskEvent(
            event_id="event-1",
            task_id=task_id,
            sequence=1,
            event_type="task.indexing",
            from_state=TaskState.CREATED,
            to_state=TaskState.INDEXING,
            payload={"worktree": str(repository)},
        ),
        expected_sequence=0,
    )

    response = api_client.get(
        f"/v1/tasks/{task_id}/events",
        headers={"Last-Event-ID": "1"},
    )

    assert "id: 1" not in response.text
    assert "event: stream.end" in response.text


def test_unknown_task_maps_to_not_found(api_client: TestClient) -> None:
    """Leaking a storage exception would make the public API unstable."""
    response = api_client.get("/v1/tasks/missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "task not found: missing"}


@pytest.mark.parametrize(
    "conflict_error",
    (
        IdempotencyConflict("operation identity conflict"),
        OperationInProgress("operation outcome is indeterminate"),
    ),
)
def test_resume_operation_conflicts_map_to_http_409(
    api_client: TestClient,
    container: LocalContainer,
    monkeypatch: pytest.MonkeyPatch,
    conflict_error: RuntimeError,
) -> None:
    """Durable-operation conflicts are client-visible concurrency responses, not HTTP 500s."""

    def reject_resume(*_args: object, **_kwargs: object) -> object:
        raise conflict_error

    monkeypatch.setattr(container.service, "resume", reject_resume)
    response = api_client.post(
        "/v1/tasks/task-1/resume",
        json={"actions": [{"kind": "FINAL", "final_text": "done"}]},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": str(conflict_error)}


def test_approval_endpoint_rejects_wrong_identity_and_is_idempotent(
    api_client: TestClient,
    container: LocalContainer,
    repository: Path,
) -> None:
    """An approval URL must authorize its exact pending operation, not any waiting task."""
    task_id = api_client.post("/v1/tasks", json=task_payload(repository)).json()["task_id"]
    snapshot = container.service.get(task_id)
    for target, event_type in (
        (TaskState.INDEXING, "task.indexing"),
        (TaskState.PLANNING, "task.planning"),
        (TaskState.EXECUTING, "task.executing"),
    ):
        snapshot = container.service.orchestrator._transition(snapshot, target, event_type, {})
    request = ToolRequest(
        operation_id="approved-operation",
        name="run_command",
        arguments={"argv": ["python", "safe_script.py"]},
    )
    container.service.orchestrator._transition(
        snapshot,
        TaskState.WAITING_APPROVAL,
        "task.approval_requested",
        {
            "operation_id": request.operation_id,
            "arguments_digest": request.arguments_digest,
        },
    )

    wrong = api_client.post(f"/v1/tasks/{task_id}/approvals/wrong")
    approved = api_client.post(f"/v1/tasks/{task_id}/approvals/{request.operation_id}")
    repeated = api_client.post(f"/v1/tasks/{task_id}/approvals/{request.operation_id}")

    assert wrong.status_code == 409
    assert approved.status_code == 200
    assert approved.json()["state"] == "EXECUTING"
    assert repeated.status_code == 200
    assert repeated.json()["sequence"] == approved.json()["sequence"]


def test_health_and_readiness_are_distinct(api_client: TestClient) -> None:
    """A liveness-only endpoint cannot prove the persistent dependency is usable."""
    assert api_client.get("/health").json() == {"status": "alive"}
    assert api_client.get("/ready").json() == {"status": "ready"}
