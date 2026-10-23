from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repopilot.agent.scripted import ScriptedRuntime
from repopilot.api.control import create_control_app
from repopilot.application.jobs import JobManager
from repopilot.domain.models import TaskSpec, TaskState, ToolRequest
from repopilot.evaluation.cases import load_builtin_cases


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JobManager:
    result = JobManager(tmp_path / "runtime", demo=True)
    # Child process execution has its own integration suite; exercise real storage and HTTP here.
    monkeypatch.setattr(result, "_launch", lambda _job_id: None)
    return result


@pytest.fixture
def client(manager: JobManager) -> Iterator[TestClient]:
    with TestClient(create_control_app(manager)) as result:
        yield result


def token(client: TestClient) -> dict[str, str]:
    return {"X-ForgePilot-Token": client.get("/v1/control").json()["csrf_token"]}


def test_control_and_submission_use_fixed_configuration(client: TestClient) -> None:
    control = client.get("/v1/control")
    assert control.status_code == 200
    assert control.json()["name"] == "ForgePilot"
    assert control.json()["mode"] == "demo"
    assert {"repository", "test_command", "model", "storage"} <= control.json().keys()
    assert len(control.json()["csrf_token"]) >= 32
    created = client.post("/v1/jobs", headers=token(client), json={"issue": "Fix parser"})
    assert created.status_code == 202
    job = created.json()
    assert job["kind"] == "task"
    assert job["state"] == "QUEUED"
    assert client.get(f"/v1/jobs/{job['job_id']}").json() == job
    assert client.get("/v1/jobs").json() == [job]
    analysis = client.post(
        "/v1/jobs", headers=token(client), json={"issue": "Explain parser", "kind": "analysis"}
    )
    assert analysis.status_code == 202
    assert analysis.json()["task_id"] is None


@pytest.mark.parametrize(
    "path",
    ["/v1/jobs", "/v1/jobs/missing/cancel", "/v1/jobs/missing/resume", "/v1/jobs/missing/approve"],
)
def test_every_control_write_requires_token(client: TestClient, path: str) -> None:
    for headers in ({}, {"X-ForgePilot-Token": "wrong"}):
        assert client.post(path, json={"issue": "oops"}, headers=headers).status_code == 403
    assert client.get("/v1/jobs").json() == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.test"},
        {"Origin": "null"},
        {"Origin": "http://testserver:9999"},
        {"Origin": "http://testserver.evil.test"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Host": "evil.test"},
    ],
)
def test_untrusted_browser_context_cannot_read_token_or_write(
    client: TestClient,
    headers: dict[str, str],
) -> None:
    auth = token(client)
    assert client.get("/v1/control", headers=headers).status_code in {400, 403}
    assert client.post("/v1/jobs", headers=auth | headers, json={"issue": "oops"}).status_code in {
        400,
        403,
    }
    assert client.get("/v1/jobs").json() == []


def test_same_origin_browser_and_cli_are_accepted(client: TestClient) -> None:
    headers = token(client) | {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    assert client.post("/v1/jobs", headers=headers, json={"issue": "one"}).status_code == 202
    assert client.post("/v1/jobs", headers=token(client), json={"issue": "two"}).status_code == 202


def test_tokens_are_session_scoped_and_duplicate_headers_are_rejected(
    client: TestClient,
    manager: JobManager,
) -> None:
    headers = token(client)
    assert client.get("/v1/control").headers["cache-control"] == "no-store"
    with TestClient(create_control_app(manager)) as second:
        assert (
            second.post("/v1/jobs", headers=headers, json={"issue": "wrong session"}).status_code
            == 403
        )
    duplicate = [
        ("X-ForgePilot-Token", headers["X-ForgePilot-Token"]),
        ("X-ForgePilot-Token", headers["X-ForgePilot-Token"]),
    ]
    assert (
        client.post("/v1/jobs", headers=duplicate, json={"issue": "duplicate"}).status_code == 403
    )
    assert (
        client.post(
            "/v1/jobs", headers={"X-ForgePilot-Token": b"\xff"}, json={"issue": "invalid token"}
        ).status_code
        == 403
    )


def test_legacy_task_write_routes_cannot_bypass_fixed_command(client: TestClient) -> None:
    for path in ("/v1/tasks", "/v1/tasks/one/resume", "/v1/tasks/one/approvals/operation"):
        response = client.post(
            path,
            headers=token(client),
            json={
                "repository": "/tmp/untrusted",
                "issue": "oops",
                "test_command": ["sh", "-c", "bad"],
            },
        )
        assert response.status_code in {404, 405}
    assert client.get("/v1/tasks").json() == []


def test_jobs_reject_arbitrary_configuration_without_echoing_secrets(client: TestClient) -> None:
    for extra in (
        {"repository": "/tmp/elsewhere"},
        {"test_command": ["echo", "bad"]},
        {"api_key": "sk-sensitive123"},
        {"kind": "shell"},
    ):
        response = client.post("/v1/jobs", headers=token(client), json={"issue": "fix", **extra})
        assert response.status_code == 422
        assert "sk-sensitive123" not in response.text
    assert client.get("/v1/jobs").json() == []


def test_cancel_unknown_and_terminal_resume(client: TestClient, manager: JobManager) -> None:
    headers = token(client)
    created = client.post("/v1/jobs", headers=headers, json={"issue": "one"}).json()
    path = f"/v1/jobs/{created['job_id']}"
    assert client.post(path + "/cancel", headers=headers).json()["cancel_requested"] is True
    manager.store.update(created["job_id"], state="CANCELLED")
    assert client.post(path + "/resume", headers=headers).status_code == 409
    assert client.get("/v1/jobs/missing").status_code == 404
    assert client.post("/v1/jobs/missing/cancel", headers=headers).status_code == 404


def test_exact_approval_reuses_persisted_events_and_resumes(
    client: TestClient,
    manager: JobManager,
    tmp_path: Path,
) -> None:
    job = manager.submit("Needs approval")
    with manager.container() as container:
        container.service.task_id_factory = lambda: job["task_id"]
        task = container.service.create(
            TaskSpec(repository=tmp_path, issue="Needs approval", test_command=("pytest",))
        )
        for target in (TaskState.INDEXING, TaskState.PLANNING, TaskState.EXECUTING):
            task = container.service.orchestrator._transition(task, target, "task.progress", {})
        request = ToolRequest(
            operation_id="operation-exact", name="run_command", arguments={"argv": ["pytest"]}
        )
        container.service.orchestrator._transition(
            task,
            TaskState.WAITING_APPROVAL,
            "task.approval_requested",
            {
                "operation_id": request.operation_id,
                "arguments_digest": request.arguments_digest,
            },
        )
    manager.store.update(job["job_id"], state="WAITING_APPROVAL")
    path = f"/v1/jobs/{job['job_id']}/approve"
    assert (
        client.post(path, headers=token(client), json={"operation_id": "wrong"}).status_code == 409
    )
    assert manager.get(job["job_id"])["state"] == "WAITING_APPROVAL"
    events = client.get(f"/v1/tasks/{job['task_id']}/events")
    assert "operation-exact" in events.text
    approved = client.post(path, headers=token(client), json={"operation_id": "operation-exact"})
    assert approved.status_code == 200
    assert approved.json()["state"] == "QUEUED"


def test_public_jobs_redact_credentials(
    client: TestClient,
    manager: JobManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-env-credential-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    job = manager.submit("Analyze code")
    manager.store.update(job["job_id"], summary=f"Result {secret}", error=f"password={secret}")
    for path in ("/v1/control", "/v1/jobs", f"/v1/jobs/{job['job_id']}"):
        response = client.get(path)
        assert secret not in response.text
    assert "[REDACTED]" in client.get("/v1/jobs").text


def test_artifact_content_is_allowlisted_redacted_and_capped(
    client: TestClient,
    manager: JobManager,
    tmp_path: Path,
) -> None:
    case = load_builtin_cases()[0]
    source = case.prepare_repository(tmp_path / "source")
    first = case.actions[0]
    patch = (
        str(first.arguments["patch"])
        .replace(
            "+def parse_port",
            "+# OPENAI_API_KEY=sk-test123\n+def parse_port",
        )
        .replace("@@ -1,3 +1,7 @@", "@@ -1,3 +1,8 @@")
    )
    runtime = ScriptedRuntime(
        (first.model_copy(update={"arguments": {"patch": patch}}), *case.actions[1:])
    )
    with manager.container() as container:
        spec = case.task_spec(source).model_copy(update={"issue": "x" * 65_000})
        task = container.service.create(spec)
        result = container.service.run(task.task_id, runtime)
        assert result.state is TaskState.SUCCEEDED
        assert "sk-test123" in container.service.artifacts(task.task_id).patch.path.read_text()
    base = f"/v1/tasks/{task.task_id}/artifacts"
    for kind in ("patch", "test_report", "trajectory", "pr_draft"):
        response = client.get(f"{base}/{kind}/content")
        assert response.status_code == 200
        assert "sk-test123" not in response.text
        assert len(response.json()["content"]) <= 64_000
    assert client.get(f"{base}/pr_draft/content").json()["truncated"] is True
    assert client.get(f"{base}/state.db/content").status_code == 422
