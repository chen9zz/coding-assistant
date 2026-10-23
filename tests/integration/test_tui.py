"""Exercise the terminal UI through real widgets and the HTTP boundary."""

import asyncio
import json
from typing import Any

import httpx2 as httpx
import pytest

pytest.importorskip("textual")
from textual.widgets import Button, Checkbox, DataTable, Input, Static

from repopilot.tui import ForgePilotApp


class LocalControl:
    def __init__(self) -> None:
        self.job: dict[str, Any] = {
            "job_id": "job-one",
            "task_id": "task-one",
            "kind": "task",
            "state": "WAITING_APPROVAL",
            "parent_job_id": None,
            "issue": "Fix parsing",
            "summary": "Inspected parser",
            "error": None,
            "created_at": "2026-10-22T00:00:00Z",
            "updated_at": "2026-10-22T00:00:00Z",
        }
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            assert request.headers["X-ForgePilot-Token"] == "session-token"
            body = json.loads(request.content or b"{}")
            self.writes.append((path, body))
            if path == "/v1/jobs":
                self.job.update(body, state="QUEUED")
            elif path.endswith("/approve"):
                assert body == {"operation_id": "op-exact-42"}
                self.job["state"] = "RUNNING"
            elif path.endswith("/cancel"):
                self.job["state"] = "CANCELLED"
            elif path.endswith("/resume"):
                self.job["state"] = "RUNNING"
            return httpx.Response(200, json=self.job)
        if path == "/v1/control":
            return httpx.Response(
                200,
                json={
                    "name": "ForgePilot",
                    "mode": "demo",
                    "repository": "/repo",
                    "test_command": ["pytest"],
                    "model": "demo",
                    "storage": "sqlite",
                    "csrf_token": "session-token",
                },
            )
        if path == "/v1/jobs":
            return httpx.Response(200, json=[self.job])
        if path.endswith("/events"):
            event = {
                "event_id": "event-one",
                "task_id": "task-one",
                "sequence": 1,
                "event_type": "task.approval_requested",
                "from_state": "EXECUTING",
                "to_state": "WAITING_APPROVAL",
                "timestamp": "2026-10-22T00:00:00Z",
                "payload": {"operation_id": "op-exact-42", "tool": "shell.run"},
                "operation_id": None,
                "causation_id": None,
                "schema_version": 1,
            }
            return httpx.Response(200, text=f"id: 1\ndata: {json.dumps(event)}\n\n")
        return httpx.Response(200, json=self.job)


def make_app(server: LocalControl) -> ForgePilotApp:
    return ForgePilotApp(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(server.respond),
            base_url="http://127.0.0.1:8765",
        )
    )


@pytest.mark.parametrize("analysis", [False, True])
def test_submit_issue_uses_selected_kind_and_csrf(analysis: bool) -> None:
    async def scenario() -> None:
        server = LocalControl()
        app = make_app(server)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            app.query_one("#issue", Input).value = "Explain the parser"
            if analysis:
                await pilot.click("#analysis")
            assert app.query_one("#analysis", Checkbox).value is analysis
            await pilot.click("#submit")
            await pilot.pause()
            assert server.writes == [
                (
                    "/v1/jobs",
                    {
                        "issue": "Explain the parser",
                        "kind": "analysis" if analysis else "task",
                    },
                )
            ]
            assert app.query_one("#issue", Input).value == ""
            assert "QUEUED" in str(app.query_one("#detail", Static).content)
            assert app.query_one("#jobs", DataTable).get_row("job-one")[0] == "QUEUED"
        assert len(server.writes) == 1  # Closing the UI must not cancel the job.

    asyncio.run(scenario())


def test_approve_uses_event_operation_then_cancel_disables_resume() -> None:
    async def scenario() -> None:
        server = LocalControl()
        app = make_app(server)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            assert "Inspected parser" in str(app.query_one("#detail", Static).content)
            assert "op-exact-42" in str(app.query_one("#approval", Static).content)
            assert not app.query_one("#approve", Button).disabled
            await pilot.click("#approve")
            await pilot.pause()
            await pilot.click("#cancel")
            await pilot.pause()
            assert app.query_one("#resume", Button).disabled
            assert server.writes == [
                ("/v1/jobs/job-one/approve", {"operation_id": "op-exact-42"}),
                ("/v1/jobs/job-one/cancel", {}),
            ]
            assert "CANCELLED" in str(app.query_one("#detail", Static).content)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["task", "analysis"])
def test_resume_only_accepts_interrupted_task(kind: str) -> None:
    async def scenario() -> None:
        server = LocalControl()
        server.job.update(state="INTERRUPTED", kind=kind)
        app = make_app(server)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            assert app.query_one("#resume", Button).disabled is (kind == "analysis")
            await pilot.press("f8")
            await pilot.pause()
            assert server.writes == ([("/v1/jobs/job-one/resume", {})] if kind == "task" else [])

    asyncio.run(scenario())


def test_slow_network_does_not_block_typing_or_quitting() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return httpx.Response(503)

        app = ForgePilotApp(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(slow),
                base_url="http://127.0.0.1:8765",
            )
        )
        async with app.run_test(size=(70, 24)) as pilot:
            await asyncio.wait_for(started.wait(), timeout=2)
            await pilot.click("#issue")
            await pilot.press("n", "e", "w")
            assert app.query_one("#issue", Input).value == "new"
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_selecting_analysis_preserves_selection_and_clears_task_approval() -> None:
    async def scenario() -> None:
        server = LocalControl()
        analysis = {
            **server.job,
            "job_id": "job-two",
            "task_id": None,
            "kind": "analysis",
            "state": "FAILED",
            "summary": "Report incomplete",
            "error": "Model unavailable",
        }

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/jobs":
                return httpx.Response(200, json=[server.job, analysis])
            return server.respond(request)

        app = ForgePilotApp(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(respond),
                base_url="http://127.0.0.1:8765",
            )
        )
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            app.query_one("#jobs", DataTable).focus()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()
            detail = str(app.query_one("#detail", Static).content)
            assert "Report incomplete" in detail
            assert "Model unavailable" in detail
            assert app.query_one("#approve", Button).disabled
            assert "op-exact-42" not in str(app.query_one("#approval", Static).content)
            await pilot.press("f6")
            assert server.writes == []

    asyncio.run(scenario())


def test_rejected_submit_preserves_issue_and_refresh_does_not_resubmit() -> None:
    async def scenario() -> None:
        server = LocalControl()
        posts: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posts.append(request.url.path)
                return httpx.Response(503, json={"detail": "Service busy"})
            return server.respond(request)

        app = ForgePilotApp(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(respond),
                base_url="http://127.0.0.1:8765",
            )
        )
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            app.query_one("#issue", Input).value = "Keep this request"
            await pilot.click("#submit")
            await pilot.pause()
            assert "503" in str(app.query_one("#status", Static).content)
            assert app.query_one("#issue", Input).value == "Keep this request"
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert posts == ["/v1/jobs"]

    asyncio.run(scenario())


def test_missing_task_events_still_displays_job_startup_error() -> None:
    async def scenario() -> None:
        server = LocalControl()
        server.job.update(
            state="INTERRUPTED",
            summary="Worker did not initialize",
            error="worker exited before final status",
        )

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/events"):
                return httpx.Response(404, json={"detail": "task not found"})
            return server.respond(request)

        app = ForgePilotApp(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(respond),
                base_url="http://127.0.0.1:8765",
            )
        )
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            detail = str(app.query_one("#detail", Static).content)
            assert "INTERRUPTED" in detail
            assert "Worker did not initialize" in detail
            assert "worker exited before final status" in detail
            assert not app.query_one("#resume", Button).disabled
            assert "Connected" in str(app.query_one("#status", Static).content)

    asyncio.run(scenario())


def test_unavailable_server_keeps_input_and_quit_responsive() -> None:
    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario() -> None:
        app = ForgePilotApp(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(unavailable),
                base_url="http://127.0.0.1:8765",
            )
        )
        async with app.run_test(size=(70, 24)) as pilot:
            await pilot.pause()
            assert "Unavailable" in str(app.query_one("#status", Static).content)
            assert app.query_one("#submit", Button).disabled
            await pilot.click("#issue")
            await pilot.press("h", "i")
            assert app.query_one("#issue", Input).value == "hi"
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8765",
        "http://127.0.0.1.evil.test",
        "http://user:secret@localhost",
        "https://localhost/path",
        "http://0.0.0.0:8765",
        "file:///tmp/control",
    ],
)
def test_remote_or_credentialed_base_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        ForgePilotApp(base_url=url)
