"""Transactional SQLite event, snapshot, and idempotency storage."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from repopilot.domain.models import (
    AgentPlan,
    TaskEvent,
    TaskSnapshot,
    TaskSpec,
    ToolRequest,
    ToolResult,
)
from repopilot.domain.state_machine import transition_allowed


class StoreError(RuntimeError):
    """Base class for persistence contract failures."""


class TaskNotFoundError(StoreError):
    """Raised when a requested task has never been created."""


class TaskAlreadyExistsError(StoreError):
    """Raised when task identity would overwrite an existing task."""


class ConcurrencyError(StoreError):
    """Raised when an append uses a stale expected sequence."""


class InvalidEventError(StoreError):
    """Raised when an event contradicts its task snapshot."""


class IdempotencyConflict(StoreError):
    """Raised when an operation ID is reused with different arguments."""


class SQLiteEventStore:
    """Keep task snapshots, ordered events, and tool results in one database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = self._load_cipher(path.with_name(f"{path.name}.key"))
        self.initialize()

    @staticmethod
    def _load_cipher(key_path: Path) -> Fernet:
        try:
            descriptor = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            try:
                key = Fernet.generate_key()
                os.write(descriptor, key)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        mode = stat.S_IMODE(key_path.stat().st_mode)
        if mode & 0o077:
            raise StoreError("approval encryption key must not be accessible by other users")
        try:
            return Fernet(key_path.read_bytes())
        except (OSError, ValueError) as error:
            raise StoreError("approval encryption key is unreadable or invalid") from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _managed_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._managed_connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    snapshot_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (task_id, sequence),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS operations (
                    task_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (task_id, operation_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS operation_claims (
                    task_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    PRIMARY KEY (task_id, operation_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS pending_approvals (
                    task_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    request_token BLOB NOT NULL,
                    PRIMARY KEY (task_id, operation_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );
                """
            )

    def create_task(self, task_id: str, spec: TaskSpec) -> TaskSnapshot:
        snapshot = TaskSnapshot(task_id=task_id, spec=spec)
        try:
            with self._managed_connection() as connection:
                connection.execute(
                    "INSERT INTO tasks(task_id, snapshot_json) VALUES (?, ?)",
                    (task_id, snapshot.model_dump_json()),
                )
        except sqlite3.IntegrityError as error:
            raise TaskAlreadyExistsError(f"task already exists: {task_id}") from error
        return snapshot

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self._managed_connection() as connection:
            return self._load_task(connection, task_id)

    def list_tasks(self) -> list[TaskSnapshot]:
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT snapshot_json FROM tasks ORDER BY rowid ASC"
            ).fetchall()
        return [TaskSnapshot.model_validate_json(row["snapshot_json"]) for row in rows]

    def append(self, event: TaskEvent, expected_sequence: int) -> TaskSnapshot:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._load_task(connection, event.task_id)
            updated = self._updated_snapshot(snapshot, event, expected_sequence)
            self._insert_event(connection, event)
            self._update_task(connection, updated)
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def events(self, task_id: str, after: int = 0) -> list[TaskEvent]:
        with self._managed_connection() as connection:
            self._load_task(connection, task_id)
            rows = connection.execute(
                """
                SELECT event_json
                FROM events
                WHERE task_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (task_id, after),
            ).fetchall()
        return [TaskEvent.model_validate_json(row["event_json"]) for row in rows]

    def get_operation(self, task_id: str, operation_id: str) -> ToolResult | None:
        with self._managed_connection() as connection:
            self._load_task(connection, task_id)
            row = connection.execute(
                """
                SELECT result_json
                FROM operations
                WHERE task_id = ? AND operation_id = ?
                """,
                (task_id, operation_id),
            ).fetchone()
        if row is None:
            return None
        return ToolResult.model_validate_json(row["result_json"])

    def operation_history(self, task_id: str) -> list[tuple[ToolRequest, ToolResult]]:
        history: list[tuple[ToolRequest, ToolResult]] = []
        seen: set[str] = set()
        for event in self.events(task_id):
            if (
                event.operation_id is None
                or event.operation_id in seen
                or event.event_type not in {"tool.completed", "tool.denied", "tool.failed"}
            ):
                continue
            tool_name = event.payload.get("tool_name")
            arguments = event.payload.get("arguments")
            result = self.get_operation(task_id, event.operation_id)
            if not isinstance(tool_name, str) or not isinstance(arguments, dict) or result is None:
                continue
            request = ToolRequest(
                operation_id=event.operation_id,
                name=tool_name,
                arguments=arguments,
            )
            history.append((request, result))
            seen.add(event.operation_id)
        return history

    def save_pending_approval(
        self,
        task_id: str,
        request: ToolRequest,
        event: TaskEvent,
        expected_sequence: int,
    ) -> TaskSnapshot:
        serialized = request.model_dump_json(exclude_computed_fields=True)
        token = self._cipher.encrypt(serialized.encode("utf-8"))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._load_task(connection, task_id)
            existing = connection.execute(
                """
                SELECT request_digest FROM pending_approvals
                WHERE task_id = ? AND operation_id = ?
                """,
                (task_id, request.operation_id),
            ).fetchone()
            if existing is not None and existing["request_digest"] != request.arguments_digest:
                raise IdempotencyConflict(
                    f"operation {request.operation_id} has a pending approval with other arguments"
                )
            updated = self._updated_snapshot(snapshot, event, expected_sequence)
            connection.execute(
                """
                INSERT INTO pending_approvals(
                    task_id, operation_id, request_digest, request_token
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id, operation_id) DO UPDATE SET
                    request_digest = excluded.request_digest,
                    request_token = excluded.request_token
                """,
                (task_id, request.operation_id, request.arguments_digest, token),
            )
            self._insert_event(connection, event)
            self._update_task(connection, updated)
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_pending_approval(
        self, task_id: str, operation_id: str
    ) -> ToolRequest | None:
        with self._managed_connection() as connection:
            self._load_task(connection, task_id)
            row = connection.execute(
                """
                SELECT request_digest, request_token FROM pending_approvals
                WHERE task_id = ? AND operation_id = ?
                """,
                (task_id, operation_id),
            ).fetchone()
        if row is None:
            return None
        try:
            request = ToolRequest.model_validate_json(self._cipher.decrypt(row["request_token"]))
        except (InvalidToken, ValueError) as error:
            raise StoreError("pending approval request cannot be decrypted") from error
        if (
            request.operation_id != operation_id
            or request.arguments_digest != row["request_digest"]
        ):
            raise StoreError("pending approval request failed its integrity check")
        return request

    def claim_operation(
        self,
        task_id: str,
        request: ToolRequest,
        event: TaskEvent,
        expected_sequence: int,
    ) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._load_task(connection, task_id)
            existing = connection.execute(
                """
                SELECT request_digest FROM operations
                WHERE task_id = ? AND operation_id = ?
                UNION ALL
                SELECT request_digest FROM operation_claims
                WHERE task_id = ? AND operation_id = ?
                LIMIT 1
                """,
                (task_id, request.operation_id, task_id, request.operation_id),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request.arguments_digest:
                    raise IdempotencyConflict(
                        f"operation {request.operation_id} was already claimed with other arguments"
                    )
                connection.commit()
                return False
            pending = connection.execute(
                """
                SELECT request_digest FROM pending_approvals
                WHERE task_id = ? AND operation_id = ?
                """,
                (task_id, request.operation_id),
            ).fetchone()
            if pending is not None and pending["request_digest"] != request.arguments_digest:
                raise IdempotencyConflict(
                    f"operation {request.operation_id} has an approved request with other arguments"
                )
            updated = self._updated_snapshot(snapshot, event, expected_sequence)
            connection.execute(
                """
                INSERT INTO operation_claims(task_id, operation_id, request_digest)
                VALUES (?, ?, ?)
                """,
                (task_id, request.operation_id, request.arguments_digest),
            )
            connection.execute(
                "DELETE FROM pending_approvals WHERE task_id = ? AND operation_id = ?",
                (task_id, request.operation_id),
            )
            self._insert_event(connection, event)
            self._update_task(connection, updated)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_operation(
        self,
        task_id: str,
        request: ToolRequest,
        result: ToolResult,
        event: TaskEvent,
        expected_sequence: int,
    ) -> TaskSnapshot:
        if result.operation_id != request.operation_id or result.tool_name != request.name:
            raise IdempotencyConflict("tool result identity does not match its request")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._load_task(connection, task_id)
            existing = connection.execute(
                """
                SELECT request_digest
                FROM operations
                WHERE task_id = ? AND operation_id = ?
                """,
                (task_id, request.operation_id),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request.arguments_digest:
                    raise IdempotencyConflict(
                        f"operation {request.operation_id} was already used with other arguments"
                    )
                updated = self._updated_snapshot(snapshot, event, expected_sequence)
                self._insert_event(connection, event)
                self._update_task(connection, updated)
                connection.commit()
                return updated

            claim = connection.execute(
                """
                SELECT request_digest
                FROM operation_claims
                WHERE task_id = ? AND operation_id = ?
                """,
                (task_id, request.operation_id),
            ).fetchone()
            if claim is not None and claim["request_digest"] != request.arguments_digest:
                raise IdempotencyConflict(
                    f"operation {request.operation_id} claim has different arguments"
                )
            updated = self._updated_snapshot(snapshot, event, expected_sequence)
            connection.execute(
                """
                INSERT INTO operations(task_id, operation_id, request_digest, result_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    task_id,
                    request.operation_id,
                    request.arguments_digest,
                    result.model_dump_json(),
                ),
            )
            connection.execute(
                "DELETE FROM operation_claims WHERE task_id = ? AND operation_id = ?",
                (task_id, request.operation_id),
            )
            self._insert_event(connection, event)
            self._update_task(connection, updated)
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _load_task(connection: sqlite3.Connection, task_id: str) -> TaskSnapshot:
        row = connection.execute(
            "SELECT snapshot_json FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return TaskSnapshot.model_validate_json(row["snapshot_json"])

    @staticmethod
    def _updated_snapshot(
        snapshot: TaskSnapshot,
        event: TaskEvent,
        expected_sequence: int,
    ) -> TaskSnapshot:
        if snapshot.sequence != expected_sequence:
            raise ConcurrencyError(
                f"expected sequence {expected_sequence}, found {snapshot.sequence}"
            )
        if event.task_id != snapshot.task_id:
            raise InvalidEventError("event task identity does not match the snapshot")
        if event.sequence != expected_sequence + 1:
            raise InvalidEventError("event sequence must immediately follow the snapshot")
        if event.from_state is not snapshot.state:
            raise InvalidEventError("event source state does not match the snapshot")
        if event.to_state is not event.from_state and not transition_allowed(
            event.from_state, event.to_state
        ):
            raise InvalidEventError(
                f"illegal transition: {event.from_state.value} -> {event.to_state.value}"
            )
        updates: dict[str, object] = {
            "state": event.to_state,
            "sequence": event.sequence,
            "updated_at": event.timestamp,
        }
        worktree = event.payload.get("worktree")
        if isinstance(worktree, str):
            updates["worktree"] = Path(worktree)
        plan = event.payload.get("plan")
        if isinstance(plan, dict):
            updates["plan"] = AgentPlan.model_validate(plan)
        return snapshot.model_copy(update=updates)

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: TaskEvent) -> None:
        try:
            connection.execute(
                """
                INSERT INTO events(task_id, sequence, event_id, event_json)
                VALUES (?, ?, ?, ?)
                """,
                (event.task_id, event.sequence, event.event_id, event.model_dump_json()),
            )
        except sqlite3.IntegrityError as error:
            raise ConcurrencyError("event sequence or identity already exists") from error

    @staticmethod
    def _update_task(connection: sqlite3.Connection, snapshot: TaskSnapshot) -> None:
        connection.execute(
            "UPDATE tasks SET snapshot_json = ? WHERE task_id = ?",
            (snapshot.model_dump_json(), snapshot.task_id),
        )
