"""Storage selection and connected-session wiring without external services."""

from pathlib import Path
from typing import ClassVar

import pytest

from repopilot.bootstrap import build_local_container
from repopilot.configuration import load_config
from repopilot.domain.models import TaskSpec, ToolRequest, ToolResult, ToolStatus
from repopilot.infrastructure.sqlite import SQLiteEventStore

PROVIDER = '[provider]\nprotocol="openai"\nmodel="contract"\nbase_url="http://127.0.0.1:1/v1"\n'
MYSQL = (
    '[storage]\nbackend="mysql"\n[storage.mysql]\nhost="127.0.0.1"\n'
    'database="contract"\nusername="contract"\npassword_env="MYSQL_CONFIG_TEST_SECRET"\n'
)


def task_spec(issue="repair"):
    return TaskSpec(repository=Path("/tmp/repo"), issue=issue, test_command=("pytest",))


def test_legacy_configuration_defaults_to_sqlite(tmp_path):
    config_file = tmp_path / "agent.toml"
    config_file.write_text(PROVIDER)
    config = load_config(config_file)
    assert hasattr(config, "storage"), "configuration must explicitly model storage selection"
    from repopilot.storage import configured_store

    store = configured_store(tmp_path / "runtime", config.storage)
    store.create_task("legacy", task_spec())
    assert SQLiteEventStore(tmp_path / "runtime" / "state.db").get_task("legacy").sequence == 0


def test_mysql_configuration_requires_explicit_complete_settings(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(PROVIDER + '[storage]\nbackend="mysql"\n')
    with pytest.raises(ValueError, match="invalid configuration"):
        load_config(path)
    path.write_text(PROVIDER + MYSQL)
    config = load_config(path)
    assert config.storage.backend == "mysql"
    assert config.storage.mysql.password_env == "MYSQL_CONFIG_TEST_SECRET"
    path.write_text(PROVIDER + MYSQL.replace('backend="mysql"', 'backend="sqlite"'))
    with pytest.raises(ValueError):
        load_config(path)


def test_mysql_relative_ca_is_resolved_next_to_operator_config(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(
        PROVIDER
        + MYSQL.replace('host="127.0.0.1"', 'host="db.example.org"')
        + 'tls_ca="certificates/ca.pem"\n'
    )
    config = load_config(path)
    assert config.storage.mysql.tls_ca == tmp_path / "certificates" / "ca.pem"


def test_injected_store_is_used_without_creating_default_database(tmp_path):
    selected = SQLiteEventStore(tmp_path / "selected.db")
    root = tmp_path / "runtime"
    container = build_local_container(root, task_id_factory=lambda: "injected", store=selected)
    container.service.create(task_spec())
    assert selected.get_task("injected").spec.issue == "repair"
    assert not (root / "state.db").exists()


def test_connected_mysql_and_extra_tools_redact_database_secret(tmp_path, monkeypatch):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from repopilot.connected import connected_session

    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'mysql-contract.db'}")
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: engine)
    secret = "opaque-database-credential-for-contract"
    monkeypatch.setenv("MYSQL_CONFIG_TEST_SECRET", secret)
    config_file = tmp_path / "agent.toml"
    config_file.write_text(PROVIDER + MYSQL)

    class ExtraTool:
        name = "delegate_analysis"
        description = "Return independent analysis " + secret
        input_schema: ClassVar = {"type": "object", "properties": {}}

        def execute(self, worktree, request, *, timeout_seconds=None):
            return ToolResult(
                operation_id=request.operation_id,
                tool_name=self.name,
                status=ToolStatus.SUCCEEDED,
                output={"analysis": secret},
                duration_ms=0,
            )

    with connected_session(
        tmp_path / "runtime",
        config_file,
        task_id_factory=lambda: "connected-mysql",
        extra_tools=(ExtraTool(),),
    ) as (container, runtime):
        task = container.service.create(task_spec("repair " + secret))
        assert task.task_id == "connected-mysql"
        assert task.spec.issue == "repair [REDACTED]"
        assert not (tmp_path / "runtime" / "state.db").exists()
        assert any(tool["name"] == "delegate_analysis" for tool in runtime.tools)
        assert secret not in str(runtime.tools)
        gateway = container.service.orchestrator.gateway_factory(tmp_path, task.spec)
        result = gateway.execute(
            task.task_id,
            tmp_path,
            ToolRequest(operation_id="delegate-1", name="delegate_analysis", arguments={}),
        )
        assert result.status == ToolStatus.SUCCEEDED
        assert result.output == {"analysis": "[REDACTED]"}
        assert secret not in str(container.store.events(task.task_id))
