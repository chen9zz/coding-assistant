# ForgePilot terminal console

Install the optional terminal interface with `pip install -e '.[tui]'`. Start the
local control service in another terminal, then connect with:

```sh
forgepilot tui --url http://127.0.0.1:8765
```

The console shows the service mode, trusted repository, test command, model and
storage backend. The server determines these settings. Enter an issue, optionally
select **Read-only analysis**, then press Enter or choose Submit. Analysis jobs
produce an independent read-only report; task jobs use the server's normal
worktree, approval and verification workflow.

Select a job with the arrow keys or mouse to read its status, summary, errors and
recent events. The console refreshes every two seconds. A narrow terminal stacks
the jobs above the details; enlarge the terminal for more event history.

| Key | Action |
| --- | --- |
| Ctrl+N | Focus the issue input |
| Ctrl+S / Enter in the input | Submit the issue |
| Ctrl+R | Refresh or reconnect |
| F6 | Approve the selected pending operation |
| F7 | Request cancellation of the selected active job |
| F8 | Resume an interrupted task |
| Ctrl+Q | Exit the console |
| Tab / Shift+Tab | Move between controls |

Approval sends the exact operation ID from the persisted approval event. Review
that operation's event before approving; the server validates the ID and resumes
the task. Failed and cancelled jobs are terminal and cannot be resumed. An
interrupted analysis must be submitted again. Cancellation is cooperative: a
running command or model request may finish or time out before cancellation
reaches a safe boundary.

Exiting the console only disconnects the UI. It does not stop the local service,
cancel jobs or terminate their background processes. Reopen it to see their
latest persisted status.

Only loopback HTTP(S) origins are accepted. Remote hosts, embedded credentials,
paths, queries and fragments are rejected. The default HTTP client ignores proxy
environment variables and does not follow redirects. The console retrieves its
session CSRF token from the local service and attaches it to write requests.
There is no model API key input; configure credentials on the server side.

If the service is unavailable, the input and exit controls continue to work.
Start the service at the configured address and press Ctrl+R. Failed writes keep
the issue text, allowing a deliberate retry. The UI never automatically retries
job submissions.

## Development verification

The widget tests use Textual's headless Pilot and an HTTP boundary transport to
exercise submission, analysis selection, precise approval, terminal-state
controls, reconnect failures, URL restrictions and typing during slow network
requests. They do not make model calls or start background jobs.

```sh
pytest tests/integration/test_tui.py
```

References: [Textual testing](https://textual.textualize.io/guide/testing/) and
[asynchronous workers](https://textual.textualize.io/guide/workers/).
