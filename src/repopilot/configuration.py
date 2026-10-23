"""Operator-selected TOML configuration; never auto-executed from a target repo."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import Field, ValidationError, model_validator

from repopilot.agent.providers import ProviderConfig
from repopilot.domain.models import FrozenModel
from repopilot.infrastructure.mcp_transport import MCPServerConfig
from repopilot.storage import StorageConfig


class SkillsConfig(FrozenModel):
    roots: tuple[Path, ...] = ()


class AssistantConfig(FrozenModel):
    provider: ProviderConfig
    storage: StorageConfig = Field(default_factory=StorageConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    mcp_servers: tuple[MCPServerConfig, ...] = ()

    @model_validator(mode="after")
    def unique_servers(self) -> AssistantConfig:
        if len({server.name for server in self.mcp_servers}) != len(self.mcp_servers):
            raise ValueError("MCP server names must be unique")
        return self


def load_config(path: Path) -> AssistantConfig:
    path = path.resolve(strict=True)
    if path.stat().st_size > 65_536:
        raise ValueError("configuration exceeds 65536 bytes")
    try:
        config = AssistantConfig.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    except (ValidationError, tomllib.TOMLDecodeError):
        # ValidationError includes raw input values; a mistakenly pasted key must not be echoed.
        raise ValueError(
            "invalid configuration; check field names, endpoint and required values"
        ) from None
    roots = tuple((path.parent / root).resolve() for root in config.skills.roots)
    storage = config.storage
    if storage.mysql is not None and storage.mysql.tls_ca is not None:
        mysql = storage.mysql.model_copy(
            update={"tls_ca": (path.parent / storage.mysql.tls_ca).resolve()}
        )
        storage = storage.model_copy(update={"mysql": mysql})
    return config.model_copy(
        update={
            "skills": config.skills.model_copy(update={"roots": roots}),
            "storage": storage,
        }
    )
