"""Explicit, bounded Agent Skills discovery and progressive resource loading."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, ClassVar

import yaml

from repopilot.domain.models import ToolRequest, ToolResult
from repopilot.tools.builtin import _failure, _success
from repopilot.tools.policy import resolve_contained

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MAX_BYTES = 65_536


def _read(root: Path, relative: str) -> str:
    path = resolve_contained(root, relative)
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("skill resources must be regular files")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise ValueError("skill resource is too large (limit 65536 bytes)")
        if b"\x00" in data:
            raise ValueError("skill resource must be text")
        return data.decode("utf-8")
    finally:
        os.close(descriptor)


class SkillCatalog:
    """Only explicitly trusted roots are scanned; instructions never grant privileges."""

    def __init__(self, roots: tuple[Path, ...]) -> None:
        self._skills: dict[str, tuple[Path, str]] = {}
        for root in roots:
            root = root.resolve(strict=True)
            for child in sorted(root.iterdir()):
                if not (child / "SKILL.md").exists():
                    continue
                if len(self._skills) >= 64:
                    raise ValueError("at most 64 skills can be configured")
                folder = resolve_contained(root, child.name)
                text = _read(folder, "SKILL.md")
                lines = text.splitlines()
                if not lines or lines[0] != "---" or "---" not in lines[1:]:
                    raise ValueError(f"skill {child.name} requires YAML frontmatter")
                end = lines.index("---", 1)
                try:
                    metadata = yaml.safe_load("\n".join(lines[1:end]))
                except yaml.YAMLError:
                    raise ValueError(f"skill {child.name} has invalid YAML") from None
                if not isinstance(metadata, dict):
                    raise ValueError("skill metadata must be a mapping")
                name, description = metadata.get("name"), metadata.get("description")
                if (
                    not isinstance(name, str)
                    or len(name) > 64
                    or not _NAME.fullmatch(name)
                    or name != child.name
                    or not isinstance(description, str)
                    or not 1 <= len(description.strip()) <= 1024
                ):
                    raise ValueError(f"invalid name or description for skill {child.name}")
                if name in self._skills:
                    raise ValueError(f"duplicate skill name: {name}")
                self._skills[name] = (folder, description.strip())

    def metadata(self) -> list[dict[str, str]]:
        return [
            {"name": name, "description": description}
            for name, (_, description) in sorted(self._skills.items())
        ]

    def read(self, name: str, resource: str = "SKILL.md") -> str:
        if name not in self._skills:
            raise ValueError("unknown skill name")
        return _read(self._skills[name][0], resource)


class SkillReadTool:
    name = "skill_read"
    description = (
        "Read a skill or relative reference in 8000-character pages. "
        "Continue at next_offset until null. Does not run scripts."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "resource": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def __init__(self, catalog: SkillCatalog) -> None:
        self.catalog = catalog

    def execute(
        self, worktree: Path, request: ToolRequest, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        if timeout_seconds is not None and timeout_seconds <= 0:
            return _failure(request, "task deadline exceeded")
        name = request.arguments.get("name")
        resource = request.arguments.get("resource", "SKILL.md")
        offset = request.arguments.get("offset", 0)
        if type(offset) is not int or offset < 0:
            return _failure(request, "offset must be a nonnegative integer")
        if not isinstance(name, str) or not isinstance(resource, str):
            return _failure(request, "name and resource must be text")
        try:
            content = self.catalog.read(name, resource)
        except (OSError, ValueError):
            return _failure(request, "skill resource missing, invalid, too large, or outside root")
        if offset > len(content):
            return _failure(request, "offset exceeds resource length")
        end = offset + 8000
        return _success(
            request,
            {
                "name": name,
                "resource": resource,
                "content": content[offset:end],
                "offset": offset,
                "next_offset": end if end < len(content) else None,
                "total_chars": len(content),
            },
            truncated=end < len(content),
        )
