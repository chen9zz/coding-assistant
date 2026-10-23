"""Optional MySQL 8 event store with task-scoped transactional row locks."""

from __future__ import annotations

import importlib
import importlib.metadata
import ipaddress
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken
from pydantic import Field, model_validator

from repopilot.domain.models import (
    FrozenModel,
    TaskEvent,
    TaskSnapshot,
    TaskSpec,
    ToolRequest,
    ToolResult,
)
from repopilot.infrastructure.sqlite import (
    ConcurrencyError,
    IdempotencyConflict,
    SQLiteEventStore,
    StoreError,
    TaskAlreadyExistsError,
    TaskNotFoundError,
)

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement, Connection, Table


class MySQLConfig(FrozenModel):
    """Explicit connection settings; credentials are obtained only from the environment."""

    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.:-]+$")
    port: int = Field(default=3306, ge=1, le=65535, strict=True)
    database: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    username: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password_env: str = Field(
        default="FORGEPILOT_MYSQL_PASSWORD", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    tls_ca: Path | None = None

    @model_validator(mode="after")
    def remote_requires_tls(self) -> MySQLConfig:
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host.lower() == "localhost"
        if not loopback and self.tls_ca is None:
            raise ValueError(
                "remote MySQL requires tls_ca for certificate and hostname verification"
            )
        return self


class MySQLStore:
    """Implement EventStore using InnoDB transactions and encrypted approval requests.

    Every write for an existing task first locks its tasks row. This serializes
    sequence allocation, claims, and results across independent worker processes.
    Reads use separate short transactions and expose only committed records.
    """

    _operations: Table
    _operation_claims: Table
    _pending_approvals: Table

    def __init__(self, config: MySQLConfig, key_path: Path) -> None:
        password = os.environ.get(config.password_env)
        if not password:
            raise StoreError("MySQL password environment variable is missing or empty")
        try:
            importlib.import_module("pymysql")
            import sqlalchemy as sa
        except ImportError:
            raise StoreError("MySQL storage requires the mysql optional dependency group") from None
        version = importlib.metadata.version("PyMySQL").split(".")
        if tuple(int(part) for part in version[:2]) < (1, 2):
            raise StoreError("MySQL storage requires PyMySQL 1.2 or newer for enforced TLS")
        self._sa = sa
        connect_args: dict[str, object] = {
            "connect_timeout": 5,
            "read_timeout": 30,
            "write_timeout": 30,
            "local_infile": False,
            "init_command": "SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'",
        }
        if config.tls_ca is not None:
            if not config.tls_ca.is_file():
                raise StoreError("MySQL TLS CA file is missing")
            connect_args.update(
                ssl_ca=str(config.tls_ca),
                ssl_verify_cert=True,
                ssl_verify_identity=True,
            )
        url = sa.URL.create(
            "mysql+pymysql",
            username=config.username,
            password=password,
            host=config.host,
            port=config.port,
            database=config.database,
            query={"charset": "utf8mb4"},
        )
        self._engine = sa.create_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_recycle=1800,
            isolation_level="READ COMMITTED",
            hide_parameters=True,
            echo=False,
        )
        key_path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = SQLiteEventStore._load_cipher(key_path)
        self._metadata = sa.MetaData()
        self._define_tables()
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    def _define_tables(self) -> None:
        from sqlalchemy.dialects.mysql import LONGBLOB, LONGTEXT

        sa = self._sa
        text_type = sa.Text().with_variant(LONGTEXT(), "mysql")
        blob_type = sa.LargeBinary().with_variant(LONGBLOB(), "mysql")
        options = {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_0900_bin",
        }
        self._tasks = sa.Table(
            "tasks",
            self._metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.String(191), nullable=False, unique=True),
            sa.Column("snapshot_json", text_type, nullable=False),
            **options,
        )
        self._events = sa.Table(
            "events",
            self._metadata,
            sa.Column("task_id", sa.String(191), sa.ForeignKey("tasks.task_id"), primary_key=True),
            sa.Column("sequence", sa.Integer, primary_key=True),
            sa.Column("event_id", sa.String(191), nullable=False, unique=True),
            sa.Column("event_json", text_type, nullable=False),
            **options,
        )
        for name in ("operations", "operation_claims", "pending_approvals"):
            columns = [
                sa.Column(
                    "task_id", sa.String(191), sa.ForeignKey("tasks.task_id"), primary_key=True
                ),
                sa.Column("operation_id", sa.String(191), primary_key=True),
                sa.Column("request_digest", sa.String(64), nullable=False),
            ]
            if name == "operations":
                columns.append(sa.Column("result_json", text_type, nullable=False))
            if name == "pending_approvals":
                columns.append(sa.Column("request_token", blob_type, nullable=False))
            setattr(self, f"_{name}", sa.Table(name, self._metadata, *columns, **options))

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        try:
            with self._engine.begin() as connection:
                yield connection
        except self._sa.exc.SQLAlchemyError:
            # Driver messages can include SQL values or connection credentials.
            raise StoreError(
                "MySQL storage operation failed; check connectivity and schema"
            ) from None

    def initialize(self) -> None:
        with self._transaction() as connection:
            self._metadata.create_all(connection)

    def close(self) -> None:
        self._engine.dispose()

    def _load_task(
        self, connection: Connection, task_id: str, *, lock: bool = False
    ) -> TaskSnapshot:
        query = self._sa.select(self._tasks.c.snapshot_json).where(self._tasks.c.task_id == task_id)
        if lock:
            query = query.with_for_update()
        row = connection.execute(query).first()
        if row is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return TaskSnapshot.model_validate_json(row[0])

    def create_task(self, task_id: str, spec: TaskSpec) -> TaskSnapshot:
        snapshot = TaskSnapshot(task_id=task_id, spec=spec)
        with self._transaction() as connection:
            try:
                connection.execute(
                    self._tasks.insert().values(
                        task_id=task_id,
                        snapshot_json=snapshot.model_dump_json(),
                    )
                )
            except self._sa.exc.IntegrityError:
                raise TaskAlreadyExistsError(f"task already exists: {task_id}") from None
        return snapshot

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self._transaction() as connection:
            return self._load_task(connection, task_id)

    def list_tasks(self) -> list[TaskSnapshot]:
        with self._transaction() as connection:
            rows = connection.execute(
                self._sa.select(self._tasks.c.snapshot_json).order_by(self._tasks.c.id)
            ).all()
        return [TaskSnapshot.model_validate_json(row[0]) for row in rows]

    def _write_event(
        self, connection: Connection, snapshot: TaskSnapshot, event: TaskEvent
    ) -> None:
        try:
            connection.execute(
                self._events.insert().values(
                    task_id=event.task_id,
                    sequence=event.sequence,
                    event_id=event.event_id,
                    event_json=event.model_dump_json(),
                )
            )
        except self._sa.exc.IntegrityError:
            raise ConcurrencyError("event sequence or identity already exists") from None
        connection.execute(
            self._tasks.update()
            .where(self._tasks.c.task_id == snapshot.task_id)
            .values(snapshot_json=snapshot.model_dump_json())
        )

    def append(self, event: TaskEvent, expected_sequence: int) -> TaskSnapshot:
        with self._transaction() as connection:
            snapshot = self._load_task(connection, event.task_id, lock=True)
            updated = SQLiteEventStore._updated_snapshot(snapshot, event, expected_sequence)
            self._write_event(connection, updated, event)
        return updated

    def events(self, task_id: str, after: int = 0) -> list[TaskEvent]:
        with self._transaction() as connection:
            self._load_task(connection, task_id)
            rows = connection.execute(
                self._sa.select(self._events.c.event_json)
                .where(self._events.c.task_id == task_id, self._events.c.sequence > after)
                .order_by(self._events.c.sequence)
            ).all()
        return [TaskEvent.model_validate_json(row[0]) for row in rows]

    def _operation_filter(
        self, table: Table, task_id: str, operation_id: str
    ) -> ColumnElement[bool]:
        return self._sa.and_(table.c.task_id == task_id, table.c.operation_id == operation_id)

    def _digest(
        self, connection: Connection, table: Table, task_id: str, operation_id: str
    ) -> str | None:
        value = connection.execute(
            self._sa.select(table.c.request_digest).where(
                self._operation_filter(table, task_id, operation_id)
            )
        ).scalar_one_or_none()
        return str(value) if value is not None else None

    def get_operation(self, task_id: str, operation_id: str) -> ToolResult | None:
        with self._transaction() as connection:
            self._load_task(connection, task_id)
            row = connection.execute(
                self._sa.select(self._operations.c.result_json).where(
                    self._operation_filter(self._operations, task_id, operation_id)
                )
            ).first()
        return None if row is None else ToolResult.model_validate_json(row[0])

    def operation_history(self, task_id: str) -> list[tuple[ToolRequest, ToolResult]]:
        history = []
        seen: set[str] = set()
        for event in self.events(task_id):
            if (
                event.operation_id is None
                or event.operation_id in seen
                or event.event_type not in {"tool.completed", "tool.denied", "tool.failed"}
            ):
                continue
            name, arguments = event.payload.get("tool_name"), event.payload.get("arguments")
            result = self.get_operation(task_id, event.operation_id)
            if not isinstance(name, str) or not isinstance(arguments, dict) or result is None:
                continue
            history.append(
                (
                    ToolRequest(operation_id=event.operation_id, name=name, arguments=arguments),
                    result,
                )
            )
            seen.add(event.operation_id)
        return history

    def save_pending_approval(
        self, task_id: str, request: ToolRequest, event: TaskEvent, expected_sequence: int
    ) -> TaskSnapshot:
        token = self._cipher.encrypt(request.model_dump_json(exclude_computed_fields=True).encode())
        with self._transaction() as connection:
            snapshot = self._load_task(connection, task_id, lock=True)
            digest = self._digest(
                connection, self._pending_approvals, task_id, request.operation_id
            )
            if digest is not None and digest != request.arguments_digest:
                raise IdempotencyConflict("pending approval has different arguments")
            updated = SQLiteEventStore._updated_snapshot(snapshot, event, expected_sequence)
            values = {"request_digest": request.arguments_digest, "request_token": token}
            if digest is None:
                connection.execute(
                    self._pending_approvals.insert().values(
                        task_id=task_id, operation_id=request.operation_id, **values
                    )
                )
            else:
                connection.execute(
                    self._pending_approvals.update()
                    .where(
                        self._operation_filter(
                            self._pending_approvals, task_id, request.operation_id
                        )
                    )
                    .values(**values)
                )
            self._write_event(connection, updated, event)
        return updated

    def get_pending_approval(self, task_id: str, operation_id: str) -> ToolRequest | None:
        with self._transaction() as connection:
            self._load_task(connection, task_id)
            row = connection.execute(
                self._sa.select(self._pending_approvals).where(
                    self._operation_filter(self._pending_approvals, task_id, operation_id)
                )
            )
            pending = row.mappings().first()
        if pending is None:
            return None
        try:
            request = ToolRequest.model_validate_json(
                self._cipher.decrypt(pending["request_token"])
            )
        except (InvalidToken, ValueError):
            raise StoreError("pending approval request cannot be decrypted") from None
        if (
            request.operation_id != operation_id
            or request.arguments_digest != pending["request_digest"]
        ):
            raise StoreError("pending approval request failed its integrity check")
        return request

    def claim_operation(
        self, task_id: str, request: ToolRequest, event: TaskEvent, expected_sequence: int
    ) -> bool:
        with self._transaction() as connection:
            snapshot = self._load_task(connection, task_id, lock=True)
            for table in (self._operations, self._operation_claims):
                digest = self._digest(connection, table, task_id, request.operation_id)
                if digest is not None:
                    if digest != request.arguments_digest:
                        raise IdempotencyConflict(
                            "operation was already claimed with other arguments"
                        )
                    return False
            pending = self._digest(
                connection, self._pending_approvals, task_id, request.operation_id
            )
            if pending is not None and pending != request.arguments_digest:
                raise IdempotencyConflict("approved request has different arguments")
            updated = SQLiteEventStore._updated_snapshot(snapshot, event, expected_sequence)
            connection.execute(
                self._operation_claims.insert().values(
                    task_id=task_id,
                    operation_id=request.operation_id,
                    request_digest=request.arguments_digest,
                )
            )
            connection.execute(
                self._pending_approvals.delete().where(
                    self._operation_filter(self._pending_approvals, task_id, request.operation_id)
                )
            )
            self._write_event(connection, updated, event)
        return True

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
        with self._transaction() as connection:
            snapshot = self._load_task(connection, task_id, lock=True)
            digest = self._digest(connection, self._operations, task_id, request.operation_id)
            claim = self._digest(connection, self._operation_claims, task_id, request.operation_id)
            if any(
                value is not None and value != request.arguments_digest for value in (digest, claim)
            ):
                raise IdempotencyConflict("operation identity has different arguments")
            updated = SQLiteEventStore._updated_snapshot(snapshot, event, expected_sequence)
            if digest is None:
                connection.execute(
                    self._operations.insert().values(
                        task_id=task_id,
                        operation_id=request.operation_id,
                        request_digest=request.arguments_digest,
                        result_json=result.model_dump_json(),
                    )
                )
                connection.execute(
                    self._operation_claims.delete().where(
                        self._operation_filter(
                            self._operation_claims, task_id, request.operation_id
                        )
                    )
                )
            self._write_event(connection, updated, event)
        return updated
