"""Optional full-screen terminal client for the local ForgePilot control API."""

import ipaddress
import json
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import httpx2 as httpx
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, RichLog, Static


def _local_url(value: str) -> str:
    """Accept only an explicit local origin, without credentials or URL suffixes."""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if (
        not local
        or parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("The control URL must be a loopback HTTP origin without credentials.")
    # Validate the port now, before creating a client or displaying the supplied URL.
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid loopback control port.") from exc
    return value.rstrip("/")


class ForgePilotApp(App[None]):
    """Async HTTP view; leaving the terminal never cancels backend jobs."""

    TITLE = "ForgePilot"
    SUB_TITLE = "Local work console"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+n", "new_issue", "New issue", priority=True),
        Binding("ctrl+r", "refresh_jobs", "Refresh", priority=True),
        Binding("ctrl+s", "submit_issue", "Submit", priority=True),
        Binding("f6", "approve_job", "Approve operation"),
        Binding("f7", "cancel_job", "Cancel job"),
        Binding("f8", "resume_job", "Resume job"),
    ]
    CSS = """
    Screen { layout: vertical; }
    #control { height: auto; max-height: 3; padding: 0 1; color: $text-muted; }
    #status { height: 2; padding: 0 1; }
    #composer { height: 6; padding: 0 1; }
    #composer-actions { height: 3; }
    #analysis { width: 1fr; }
    #submit { width: 18; }
    #workspace { height: 1fr; padding: 0 1; }
    #jobs { width: 38%; height: 1fr; border: round $primary; }
    #right { width: 62%; height: 1fr; }
    #details { height: 1fr; border: round $primary; }
    #detail, #approval { height: auto; padding: 0 1; }
    #approval { color: $warning; margin-top: 1; }
    #events { height: 1fr; border: round $secondary; }
    #job-actions { height: 3; padding: 0 1; }
    #job-actions Button { width: 1fr; min-width: 10; }
    .compact #workspace { layout: vertical; }
    .compact #jobs { width: 100%; height: 6; }
    .compact #right { width: 100%; height: 1fr; }
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__()
        self.base_url = _local_url(base_url)
        if client is not None:
            _local_url(str(client.base_url))
        self.client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=8.0,
            trust_env=False,
            follow_redirects=False,
        )
        self.csrf_token: str | None = None
        self.jobs: dict[str, dict[str, Any]] = {}
        self._displayed_jobs: dict[str, dict[str, Any]] = {}
        self.selected_job: str | None = None
        self.pending_operation: str | None = None
        self.event_cursor: dict[str, int] = {}
        self.event_history: dict[str, list[dict[str, Any]]] = {}
        self._polling = False
        self._writing = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Connecting to local control service…", id="control", markup=False)
        yield Static("Starting…", id="status", markup=False)
        with Vertical(id="composer"):
            yield Input(placeholder="Describe an issue or analysis request", id="issue")
            with Horizontal(id="composer-actions"):
                yield Checkbox("Read-only analysis", id="analysis")
                yield Button("Submit", id="submit", variant="primary", disabled=True)
        with Horizontal(id="workspace"):
            yield DataTable(id="jobs", cursor_type="row")
            with Vertical(id="right"):
                with VerticalScroll(id="details"):
                    yield Static("No jobs yet.", id="detail", markup=False)
                    yield Static("", id="approval", markup=False)
                yield RichLog(id="events", markup=False, wrap=True, max_lines=300)
        with Horizontal(id="job-actions"):
            yield Button("Approve operation", id="approve", variant="warning", disabled=True)
            yield Button("Cancel", id="cancel", disabled=True)
            yield Button("Resume", id="resume", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#jobs", DataTable).add_columns("State", "Kind", "Issue")
        self.query_one("#jobs", DataTable).border_title = "Jobs"
        self.query_one("#events", RichLog).border_title = "Events"
        self.set_interval(2.0, self.action_refresh_jobs)
        self.action_refresh_jobs()

    async def on_unmount(self) -> None:
        await self.client.aclose()

    def on_resize(self, event: Resize) -> None:
        self.screen.set_class(event.size.width < 85, "compact")

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {"X-ForgePilot-Token": self.csrf_token} if self.csrf_token else {}
        response = await self.client.request(
            method,
            self.base_url + path,
            json=body,
            headers=headers,
            follow_redirects=False,
            timeout=8.0,
        )
        response.raise_for_status()
        return response

    def action_refresh_jobs(self) -> None:
        if not self._polling:
            self._refresh_jobs()

    @work(group="poll", exclusive=True)
    async def _refresh_jobs(self) -> None:
        self._polling = True
        try:
            if self.csrf_token is None:
                control = (await self._request("GET", "/v1/control")).json()
                if not isinstance(control, dict) or not isinstance(control.get("csrf_token"), str):
                    raise ValueError("Invalid control response")
                self.csrf_token = control["csrf_token"]
                self.query_one("#control", Static).update(
                    f"{control.get('mode', '?')} • {control.get('repository', '?')}\n"
                    f"Model: {control.get('model', '?')} • Storage: {control.get('storage', '?')}\n"
                    f"Tests: {control.get('test_command', '?')}"
                )
            data = (await self._request("GET", "/v1/jobs")).json()
            if not isinstance(data, list):
                raise ValueError("Invalid jobs response")
            jobs = {
                item["job_id"]: item
                for item in data
                if isinstance(item, dict) and isinstance(item.get("job_id"), str)
            }
            table = self.query_one("#jobs", DataTable)
            self.jobs = jobs
            if jobs != self._displayed_jobs:
                self._displayed_jobs = jobs.copy()
                with self.prevent(DataTable.RowHighlighted):
                    table.clear()
                    for job_id, job in jobs.items():
                        table.add_row(
                            str(job.get("state", "?")),
                            str(job.get("kind", "task")),
                            str(job.get("issue", "")),
                            key=job_id,
                        )
            if self.selected_job not in jobs:
                self.selected_job = next(iter(jobs), None)
            if self.selected_job:
                with self.prevent(DataTable.RowHighlighted):
                    table.move_cursor(row=list(jobs).index(self.selected_job), animate=False)
                await self._fetch_events(self.selected_job)
            self._render_selected()
            self._status(f"Connected • {len(jobs)} job(s) • Jobs continue after you quit")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self._failure(exc)
        finally:
            self._polling = False
            self._buttons()

    async def _fetch_events(self, job_id: str) -> None:
        job = self.jobs.get(job_id, {})
        task_id = job.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return
        cursor = self.event_cursor.get(task_id, 0)
        try:
            response = await self._request(
                "GET",
                f"/v1/tasks/{quote(task_id, safe='')}/events?after={cursor}",
            )
        except httpx.HTTPStatusError as error:
            # The queue allocates an ID before the worker creates its task. Startup failures
            # can leave only job metadata, which must remain visible and actionable.
            if error.response.status_code == 404:
                return
            raise
        history = self.event_history.setdefault(task_id, [])
        for block in response.text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(
                line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
            )
            if not data:
                continue
            event = json.loads(data)
            if not isinstance(event, dict):
                continue
            sequence = event.get("sequence")
            if isinstance(sequence, int) and sequence > self.event_cursor.get(task_id, 0):
                history.append(event)
                self.event_cursor[task_id] = sequence
        del history[:-300]

    @on(DataTable.RowHighlighted, "#jobs")
    def select_job(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        if key is not None and key in self.jobs and key != self.selected_job:
            self.selected_job = key
            self._render_selected()
            self.action_refresh_jobs()

    def _render_selected(self) -> None:
        job = self.jobs.get(self.selected_job or "")
        self.pending_operation = None
        log = self.query_one("#events", RichLog)
        log.clear()
        if job is None:
            self.query_one("#detail", Static).update("No jobs yet. Submit an issue to begin.")
            self.query_one("#approval", Static).update("")
            self._buttons()
            return
        self.query_one("#detail", Static).update(
            f"{job.get('state', '?')} • {job.get('kind', 'task')} • {self.selected_job}\n"
            f"{job.get('issue', '')}\n\nSummary: {job.get('summary') or 'Pending'}\n"
            f"Error: {job.get('error') or 'None'}"
            + (
                "\nCancellation requested; waiting for a safe boundary."
                if job.get("cancel_requested")
                else ""
            )
        )
        for event in self.event_history.get(str(job.get("task_id", "")), []):
            payload = event.get("payload", {})
            log.write(
                f"{event.get('sequence', '?')} {event.get('event_type', '?')} "
                f"{json.dumps(payload, ensure_ascii=False)}"
            )
            if event.get("event_type") == "task.approval_requested" and isinstance(payload, dict):
                operation = payload.get("operation_id")
                self.pending_operation = operation if isinstance(operation, str) else None
            elif event.get("event_type") == "task.approved":
                self.pending_operation = None
        if str(job.get("state", "")).upper() != "WAITING_APPROVAL":
            self.pending_operation = None
        self.query_one("#approval", Static).update(
            f"Pending operation: {self.pending_operation}\nReview its event above before approving."
            if self.pending_operation
            else "No operation awaiting approval."
        )
        self._buttons()

    def _buttons(self) -> None:
        connected = self.csrf_token is not None and not self._writing
        self.query_one("#submit", Button).disabled = not connected
        job = self.jobs.get(self.selected_job or "", {})
        state = str(job.get("state", "")).upper()
        self.query_one("#approve", Button).disabled = not (connected and self.pending_operation)
        self.query_one("#cancel", Button).disabled = not (
            connected and job and state not in {"SUCCEEDED", "FAILED", "CANCELLED"}
        )
        self.query_one("#resume", Button).disabled = not (
            connected and job.get("kind") == "task" and state == "INTERRUPTED"
        )

    def _failure(self, error: Exception) -> None:
        if isinstance(error, httpx.HTTPStatusError):
            message = f"Request failed (HTTP {error.response.status_code}); refresh and retry."
            if error.response.status_code == 403:
                self.csrf_token = None
        else:
            message = "Unavailable: start the local control service, then press Ctrl+R."
            self.csrf_token = None
        self._status(message)

    def action_new_issue(self) -> None:
        self.query_one("#issue", Input).focus()

    @on(Button.Pressed, "#submit")
    @on(Input.Submitted, "#issue")
    def action_submit_issue(self) -> None:
        issue = self.query_one("#issue", Input).value.strip()
        if not issue:
            self._status("Enter an issue before submitting.")
            return
        if self.csrf_token and not self._writing:
            kind = "analysis" if self.query_one("#analysis", Checkbox).value else "task"
            self._write("/v1/jobs", {"issue": issue, "kind": kind}, clear_issue=True)

    @on(Button.Pressed, "#approve")
    def action_approve_job(self) -> None:
        if self.pending_operation:
            self._job_action("approve", {"operation_id": self.pending_operation})

    @on(Button.Pressed, "#cancel")
    def action_cancel_job(self) -> None:
        if not self.query_one("#cancel", Button).disabled:
            self._job_action("cancel")

    @on(Button.Pressed, "#resume")
    def action_resume_job(self) -> None:
        if not self.query_one("#resume", Button).disabled:
            self._job_action("resume")

    def _job_action(self, action: str, body: dict[str, str] | None = None) -> None:
        if self.selected_job and self.csrf_token and not self._writing:
            self._write(f"/v1/jobs/{quote(self.selected_job, safe='')}/{action}", body or {})

    @work(group="write", exclusive=True)
    async def _write(
        self,
        path: str,
        body: dict[str, str],
        *,
        clear_issue: bool = False,
    ) -> None:
        self._writing = True
        self._buttons()
        try:
            job = (await self._request("POST", path, body)).json()
            if not isinstance(job, dict) or not isinstance(job.get("job_id"), str):
                raise ValueError("Invalid job response")
            self.selected_job = job["job_id"]
            self.jobs[self.selected_job] = job
            if clear_issue:
                self.query_one("#issue", Input).value = ""
            self._render_selected()
            self._status("Request accepted. Waiting for the next job update.")
            self.action_refresh_jobs()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self._failure(exc)
        finally:
            self._writing = False
            self._buttons()
