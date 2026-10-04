import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from repopilot.domain.models import (
    TaskEvent,
    TaskSpec,
    TaskState,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from repopilot.infrastructure.sqlite import (
    ConcurrencyError,
    IdempotencyConflict,
    SQLiteEventStore,
    TaskNotFoundError,
)


def sample_spec() -> TaskSpec:
    return TaskSpec(
        repository=Path("/tmp/repo"),
        issue="Fix division by zero",
        test_command=("python", "-m", "pytest"),
    )


def indexing_event(task_id: str, event_id: str = "event-1") -> TaskEvent:
    return TaskEvent(
        event_id=event_id,
        task_id=task_id,
        sequence=1,
        event_type="task.indexing",
        from_state=TaskState.CREATED,
        to_state=TaskState.INDEXING,
        payload={"reason": "repository accepted"},
    )


def sample_request(path: str = "app.py") -> ToolRequest:
    return ToolRequest(
        operation_id="op-1",
        name="read_file",
        arguments={"path": path},
    )


def sample_result() -> ToolResult:
    return ToolResult(
        operation_id="op-1",
        tool_name="read_file",
        status=ToolStatus.SUCCEEDED,
        output={"content": "value = 1\n"},
        duration_ms=3,
    )


def intent_event(request: ToolRequest, sequence: int = 1) -> TaskEvent:
    return TaskEvent(
        event_id=f"intent-{sequence}",
        task_id="task-1",
        sequence=sequence,
        event_type="tool.started",
        from_state=TaskState.CREATED,
        to_state=TaskState.CREATED,
        operation_id=request.operation_id,
        payload={"arguments_digest": request.arguments_digest},
    )


def test_append_updates_event_and_snapshot_atomically(tmp_path: Path) -> None:
    """A partial commit would make replay disagree with the current task state."""
    store = SQLiteEventStore(tmp_path / "state.db")
    created = store.create_task("task-1", sample_spec())
    event = indexing_event(created.task_id)

    updated = store.append(event, expected_sequence=0)

    assert updated.state is TaskState.INDEXING
    assert updated.sequence == 1
    assert store.get_task("task-1") == updated
    assert store.events("task-1") == [event]


def test_stale_sequence_cannot_append_or_change_snapshot(tmp_path: Path) -> None:
    """Ignoring optimistic concurrency would silently overwrite another worker's event."""
    store = SQLiteEventStore(tmp_path / "state.db")
    store.create_task("task-1", sample_spec())
    store.append(indexing_event("task-1"), expected_sequence=0)

    with pytest.raises(ConcurrencyError):
        store.append(indexing_event("task-1", event_id="event-stale"), expected_sequence=0)

    assert store.get_task("task-1").sequence == 1
    assert len(store.events("task-1")) == 1


def test_operation_result_survives_store_reopen(tmp_path: Path) -> None:
    """Losing a committed result after restart would repeat a side effect."""
    database = tmp_path / "state.db"
    first = SQLiteEventStore(database)
    first.create_task("task-1", sample_spec())
    first.commit_operation(
        "task-1",
        sample_request(),
        sample_result(),
        indexing_event("task-1"),
        expected_sequence=0,
    )

    reopened = SQLiteEventStore(database)

    assert reopened.get_operation("task-1", "op-1") == sample_result()
    assert reopened.get_task("task-1").state is TaskState.INDEXING
    assert len(reopened.events("task-1")) == 1


def test_operation_history_reconstructs_request_result_pairs_in_event_order(
    tmp_path: Path,
) -> None:
    """A resumed online runtime needs durable paired context, not an empty conversation."""
    store = SQLiteEventStore(tmp_path / "state.db")
    store.create_task("task-1", sample_spec())
    request = sample_request()
    event = indexing_event("task-1")
    event = event.model_copy(
        update={
            "event_type": "tool.completed",
            "to_state": TaskState.CREATED,
            "operation_id": request.operation_id,
            "payload": {
                "tool_name": request.name,
                "arguments": request.arguments,
            },
        }
    )
    store.commit_operation("task-1", request, sample_result(), event, expected_sequence=0)

    assert store.operation_history("task-1") == [(request, sample_result())]


def test_operation_intent_is_claimed_once_across_store_instances(tmp_path: Path) -> None:
    """The unique claim must be durable before either worker can execute a side effect."""
    database = tmp_path / "state.db"
    first = SQLiteEventStore(database)
    second = SQLiteEventStore(database)
    first.create_task("task-1", sample_spec())
    request = sample_request()

    claimed = first.claim_operation(
        "task-1",
        request,
        intent_event(request),
        expected_sequence=0,
    )
    duplicate = second.claim_operation(
        "task-1",
        request,
        intent_event(request),
        expected_sequence=1,
    )

    assert claimed is True
    assert duplicate is False
    assert [event.event_type for event in first.events("task-1")] == ["tool.started"]
    with pytest.raises(IdempotencyConflict):
        second.claim_operation(
            "task-1",
            sample_request("different.py"),
            intent_event(sample_request("different.py"), sequence=2),
            expected_sequence=1,
        )


def test_same_operation_id_with_changed_arguments_is_rejected(tmp_path: Path) -> None:
    """Reusing approval/idempotency identity for new arguments would be privilege confusion."""
    store = SQLiteEventStore(tmp_path / "state.db")
    store.create_task("task-1", sample_spec())
    store.commit_operation(
        "task-1",
        sample_request(),
        sample_result(),
        indexing_event("task-1"),
        expected_sequence=0,
    )

    with pytest.raises(IdempotencyConflict):
        store.commit_operation(
            "task-1",
            sample_request("different.py"),
            sample_result(),
            TaskEvent(
                event_id="event-2",
                task_id="task-1",
                sequence=2,
                event_type="tool.completed",
                from_state=TaskState.INDEXING,
                to_state=TaskState.INDEXING,
                payload={"operation_id": "op-1"},
            ),
            expected_sequence=1,
        )

    assert len(store.events("task-1")) == 1


def test_events_support_a_strict_sequence_cursor(tmp_path: Path) -> None:
    """Returning an already acknowledged event would duplicate SSE client state."""
    store = SQLiteEventStore(tmp_path / "state.db")
    store.create_task("task-1", sample_spec())
    store.append(indexing_event("task-1"), expected_sequence=0)
    second = TaskEvent(
        event_id="event-2",
        task_id="task-1",
        sequence=2,
        event_type="task.planning",
        from_state=TaskState.INDEXING,
        to_state=TaskState.PLANNING,
    )
    store.append(second, expected_sequence=1)

    assert store.events("task-1", after=1) == [second]


def test_unknown_task_fails_explicitly(tmp_path: Path) -> None:
    """Returning an empty default would make callers operate on the wrong lifecycle."""
    store = SQLiteEventStore(tmp_path / "state.db")
    with pytest.raises(TaskNotFoundError):
        store.get_task("missing")


def test_every_sqlite_connection_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read helpers must close connections; sqlite's context manager only commits them."""

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    opened: list[TrackingConnection] = []
    original_connect = sqlite3.connect

    def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = TrackingConnection
        connection = cast(TrackingConnection, original_connect(*args, **kwargs))
        opened.append(connection)
        return connection

    monkeypatch.setattr(
        "repopilot.infrastructure.sqlite.sqlite3.connect",
        tracking_connect,
    )
    store = SQLiteEventStore(tmp_path / "state.db")
    store.create_task("task-1", sample_spec())
    store.get_task("task-1")
    store.list_tasks()
    store.events("task-1")
    store.get_operation("task-1", "missing")

    assert opened
    assert all(connection.closed for connection in opened)
