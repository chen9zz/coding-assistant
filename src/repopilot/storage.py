"""Select the operator-configured event store without importing optional drivers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import model_validator

from repopilot.domain.models import FrozenModel
from repopilot.domain.ports import EventStore
from repopilot.infrastructure.mysql import MySQLConfig, MySQLStore
from repopilot.infrastructure.sqlite import SQLiteEventStore


class StorageConfig(FrozenModel):
    backend: Literal["sqlite", "mysql"] = "sqlite"
    mysql: MySQLConfig | None = None

    @model_validator(mode="after")
    def consistent_backend(self) -> StorageConfig:
        if (self.backend == "mysql") != (self.mysql is not None):
            raise ValueError("mysql settings are required exactly when backend is mysql")
        return self


def configured_store(root: Path, config: StorageConfig) -> EventStore:
    root = root.resolve(strict=False)
    if config.backend == "mysql":
        assert config.mysql is not None  # Enforced by StorageConfig validation.
        return MySQLStore(config.mysql, root / "mysql-approval.key")
    return SQLiteEventStore(root / "state.db")
