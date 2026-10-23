"""SQLAlchemy contract tests plus explicitly opted-in real MySQL verification.

SQLite exercises persistence logic, never MySQL locking or dialect correctness.
"""

import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

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
    StoreError,
    TaskAlreadyExistsError,
    TaskNotFoundError,
)


def adapter():
    assert importlib.util.find_spec("repopilot.infrastructure.mysql"), "MySQL adapter is missing"
    return importlib.import_module("repopilot.infrastructure.mysql")


def spec():
    return TaskSpec(repository=Path("/tmp/repo"), issue="Fix bug", test_command=("pytest",))


def event(task_id, sequence=1, operation_id=None, **payload):
    return TaskEvent(
        event_id=str(uuid4()),
        task_id=task_id,
        sequence=sequence,
        event_type="tool.completed" if operation_id else "task.progress",
        from_state=TaskState.CREATED,
        to_state=TaskState.CREATED,
        operation_id=operation_id,
        payload=payload,
    )


def request(path="app.py"):
    return ToolRequest(operation_id="op-1", name="read_file", arguments={"path": path})


def result():
    return ToolResult(
        operation_id="op-1",
        tool_name="read_file",
        status=ToolStatus.SUCCEEDED,
        output={"content": "original"},
        duration_ms=1,
    )


@pytest.fixture(params=["sqlite-sqlalchemy", "mysql"])
def stores(request, monkeypatch, tmp_path):
    backend = request.param
    if backend == "mysql" and os.getenv("FORGEPILOT_TEST_MYSQL_ENABLE") != "1":
        pytest.skip("real MySQL not explicitly enabled")
    sqlalchemy = pytest.importorskip("sqlalchemy")
    module = adapter()
    if backend == "sqlite-sqlalchemy":
        engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'contract.db'}")
        monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: engine)
        monkeypatch.setenv("FORGEPILOT_MYSQL_TEST_FAKE_PASSWORD", "local-contract-only")
        config = module.MySQLConfig(
            host="127.0.0.1",
            port=3306,
            database="contract",
            username="contract",
            password_env="FORGEPILOT_MYSQL_TEST_FAKE_PASSWORD",
        )
    else:
        required = ["HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD"]
        missing = [key for key in required if not os.getenv(f"FORGEPILOT_TEST_MYSQL_{key}")]
        if missing:
            pytest.fail(f"explicit MySQL test configuration missing: {', '.join(missing)}")
        ca = os.getenv("FORGEPILOT_TEST_MYSQL_TLS_CA")
        config = module.MySQLConfig(
            host=os.environ["FORGEPILOT_TEST_MYSQL_HOST"],
            port=int(os.environ["FORGEPILOT_TEST_MYSQL_PORT"]),
            database=os.environ["FORGEPILOT_TEST_MYSQL_DATABASE"],
            username=os.environ["FORGEPILOT_TEST_MYSQL_USERNAME"],
            password_env="FORGEPILOT_TEST_MYSQL_PASSWORD",
            tls_ca=Path(ca) if ca else None,
        )
    created = []

    def create():
        store = module.MySQLStore(config, tmp_path / "approval.key")
        created.append(store)
        return store

    yield create, backend
    for store in created:
        store.close()


def test_config_requires_remote_tls_and_rejects_embedded_passwords():
    module = adapter()
    with pytest.raises(ValueError):
        module.MySQLConfig(host="db.example.org", port=3306, database="app", username="app")
    with pytest.raises(ValueError):
        module.MySQLConfig(host="127.0.0.1", port=0, database="app", username="app")
    with pytest.raises(ValueError):
        module.MySQLConfig(
            host="127.0.0.1", port=3306, database="app", username="app", password="secret"
        )
    with pytest.raises(ValueError):
        module.MySQLConfig(
            host="127.0.0.1", port=3306, database="app", username="app", password_env="bad name"
        )


def test_missing_password_fails_before_connection(tmp_path, monkeypatch):
    module = adapter()
    monkeypatch.delenv("FORGEPILOT_MYSQL_UNSET_TEST", raising=False)
    config = module.MySQLConfig(
        host="127.0.0.1",
        port=3306,
        database="app",
        username="app",
        password_env="FORGEPILOT_MYSQL_UNSET_TEST",
    )
    with pytest.raises(StoreError, match="password environment variable"):
        module.MySQLStore(config, tmp_path / "key")
    assert not (tmp_path / "key").exists()


def test_old_driver_is_rejected_before_connection(tmp_path, monkeypatch):
    module = adapter()
    import importlib.metadata

    sqlalchemy = pytest.importorskip("sqlalchemy")

    def no_engine(*args, **kwargs):
        pytest.fail("an unsupported driver must fail before creating a connection engine")

    monkeypatch.setattr(sqlalchemy, "create_engine", no_engine)

    original_version = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: "1.1.2" if name == "PyMySQL" else original_version(name),
    )
    monkeypatch.setenv("FORGEPILOT_MYSQL_TEST_FAKE_PASSWORD", "local-contract-only")
    config = module.MySQLConfig(
        host="127.0.0.1",
        database="app",
        username="app",
        password_env="FORGEPILOT_MYSQL_TEST_FAKE_PASSWORD",
    )
    with pytest.raises(StoreError, match=r"PyMySQL 1\.2"):
        module.MySQLStore(config, tmp_path / "key")
    assert not (tmp_path / "key").exists()


def test_mysql_connection_disallows_silent_value_truncation(tmp_path, monkeypatch):
    """A server's permissive sql_mode must not silently alter operation identities."""
    sqlalchemy = pytest.importorskip("sqlalchemy")
    module = adapter()
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'contract.db'}")
    settings = {}

    def local_engine(url, **kwargs):
        settings.update(kwargs)
        return engine

    monkeypatch.setattr(sqlalchemy, "create_engine", local_engine)
    monkeypatch.setenv("FORGEPILOT_MYSQL_TEST_FAKE_PASSWORD", "local-contract-only")
    config = module.MySQLConfig(
        host="127.0.0.1",
        database="app",
        username="app",
        password_env="FORGEPILOT_MYSQL_TEST_FAKE_PASSWORD",
    )
    store = module.MySQLStore(config, tmp_path / "approval.key")
    try:
        assert settings["connect_args"].get("init_command") == (
            "SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'"
        )
        assert settings["isolation_level"] == "READ COMMITTED"
        assert settings["hide_parameters"] is True
        assert settings["connect_args"]["local_infile"] is False
    finally:
        store.close()


def test_event_snapshot_cursor_and_restart(stores):
    create, _ = stores
    store = create()
    task = f"mysql-test-{uuid4()}"
    store.create_task(task, spec())
    first, second = event(task), event(task, 2)
    store.append(first, 0)
    store.append(second, 1)
    reopened = create()
    assert reopened.get_task(task).sequence == 2
    assert reopened.events(task, after=1) == [second]
    assert task in [item.task_id for item in reopened.list_tasks()]
    with pytest.raises(TaskAlreadyExistsError):
        store.create_task(task, spec())
    with pytest.raises(TaskNotFoundError):
        store.events(f"missing-{uuid4()}")
    with pytest.raises(ConcurrencyError):
        store.append(event(task), 0)
    assert store.events(task) == [first, second]


def test_duplicate_event_rolls_back_new_operation(stores):
    create, _ = stores
    store = create()
    task = f"mysql-test-{uuid4()}"
    store.create_task(task, spec())
    first = event(task)
    store.append(first, 0)
    duplicate = event(task, 2).model_copy(update={"event_id": first.event_id})
    with pytest.raises(ConcurrencyError):
        store.commit_operation(task, request(), result(), duplicate, 1)
    assert store.get_operation(task, "op-1") is None
    assert store.get_task(task).sequence == 1


def test_approval_encryption_claim_and_committed_replay(stores):
    create, _ = stores
    store = create()
    task = f"mysql-test-{uuid4()}"
    store.create_task(task, spec())
    req = request("private-approved-input")
    store.save_pending_approval(task, req, event(task), 0)
    assert create().get_pending_approval(task, req.operation_id) == req
    from sqlalchemy import text

    with store._engine.connect() as connection:
        token = connection.execute(
            text("SELECT request_token FROM pending_approvals WHERE task_id=:task"), {"task": task}
        ).scalar_one()
    assert b"private-approved-input" not in token
    with pytest.raises(IdempotencyConflict):
        store.claim_operation(task, request("changed"), event(task, 2), 1)
    assert store.claim_operation(task, req, event(task, 2), 1) is True
    assert create().claim_operation(task, req, event(task, 3), 2) is False
    assert store.get_pending_approval(task, req.operation_id) is None
    store.commit_operation(
        task,
        req,
        result(),
        event(task, 3, req.operation_id, tool_name=req.name, arguments=req.arguments),
        2,
    )
    assert create().operation_history(task) == [(req, result())]
    changed_result = result().model_copy(update={"output": {"content": "replacement"}})
    store.commit_operation(task, req, changed_result, event(task, 4), 3)
    assert create().get_operation(task, req.operation_id) == result()
    assert store.get_task(task).sequence == 4
    with pytest.raises(IdempotencyConflict):
        store.commit_operation(task, request("changed"), result(), event(task, 5), 4)
    assert store.get_task(task).sequence == 4


def test_real_mysql_concurrent_append_and_claim(stores):
    create, backend = stores
    if backend != "mysql":
        pytest.skip("SQLite does not verify MySQL row locks")
    first, second = create(), create()
    task = f"mysql-test-{uuid4()}"
    first.create_task(task, spec())

    def append(store):
        try:
            store.append(event(task), 0)
            return "ok"
        except ConcurrencyError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(append, [first, second])) == ["ok", "stale"]
        claims = list(
            pool.map(
                lambda store: store.claim_operation(task, request(), event(task, 2), 1),
                [first, second],
            )
        )
    assert sorted(claims) == [False, True]
    assert first.get_task(task).sequence == 2
