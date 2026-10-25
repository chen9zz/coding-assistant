"""Explicit, bounded Agent Skills discovery and progressive resource loading."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, ClassVar

import yaml

from repopilot.domain.models import JsonValue, ToolRequest, ToolResult
from repopilot.tools.builtin import _failure, _success
from repopilot.tools.policy import resolve_contained
from repopilot.tools.skill_files import markdown_resource, private_root, read_relative

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MAX_BYTES = 65_536


def _read(root: Path, relative: str) -> str:
    resolve_contained(root, relative)
    return read_relative(root, relative)


def validate_skill(name: str, text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("skill requires YAML frontmatter")
    try:
        metadata = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))
    except yaml.YAMLError:
        raise ValueError("skill has invalid YAML") from None
    if not isinstance(metadata, dict):
        raise ValueError("skill metadata must be a mapping")
    description = metadata.get("description")
    if (
        len(name) > 64
        or not _NAME.fullmatch(name)
        or metadata.get("name") != name
        or not isinstance(description, str)
        or not 1 <= len(description.strip()) <= 1024
    ):
        raise ValueError("invalid skill name or description")
    return description.strip()


class SkillCatalog:
    """Only explicitly trusted roots are scanned; instructions never grant privileges."""

    def __init__(self, roots: tuple[Path, ...], *, write_root: Path | None = None) -> None:
        self.roots = roots
        self.write_root = write_root
        self.root_id: str | None = None
        if write_root is not None:
            fd, self.root_id = private_root(write_root)
            os.close(fd)
            if any(
                write_root.is_relative_to(root.resolve())
                or root.resolve().is_relative_to(write_root)
                for root in roots
            ):
                raise ValueError("writable and read-only skill roots must not overlap")
        self.refresh()

    def refresh(self) -> None:
        if self.write_root is not None:
            fd, identity = private_root(self.write_root)
            os.close(fd)
            if identity != self.root_id:
                raise ValueError("writable skill root identity changed")
        self._skills: dict[str, tuple[Path, str]] = {}
        roots = self.roots + ((self.write_root,) if self.write_root else ())
        for root in roots:
            root = root.absolute()
            for child in sorted(root.iterdir()):
                if not (child / "SKILL.md").exists():
                    continue
                if len(self._skills) >= 64:
                    raise ValueError("at most 64 skills can be configured")
                resolve_contained(root, child.name)
                folder = root / child.name
                text = _read(folder, "SKILL.md")
                name = child.name
                description = validate_skill(name, text)
                if name in self._skills:
                    raise ValueError(f"duplicate skill name: {name}")
                self._skills[name] = (folder, description.strip())

    def metadata(self) -> list[dict[str, str]]:
        self.refresh()
        return [
            {"name": name, "description": description}
            for name, (_, description) in sorted(self._skills.items())
        ]

    def read(self, name: str, resource: str = "SKILL.md") -> str:
        self.refresh()
        if name not in self._skills:
            raise ValueError("unknown skill name")
        if self._skills[name][0].parent == self.write_root and not markdown_resource(resource):
            raise ValueError("managed skills support Markdown resources only")
        return _read(self._skills[name][0], resource)

    def writable(self, name: str) -> bool:
        self.refresh()
        return self.write_root is not None and (
            name not in self._skills or self._skills[name][0].parent == self.write_root
        )


class SkillListTool:
    name = "skill_list"
    description = "Refresh skill metadata. Returns write root identity; never writes files."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, catalog: SkillCatalog) -> None:
        self.catalog = catalog

    def execute(
        self, worktree: Path, request: ToolRequest, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        if request.arguments or (timeout_seconds is not None and timeout_seconds <= 0):
            return _failure(request, "invalid arguments or task deadline exceeded")
        try:
            entries: list[JsonValue] = [
                {**item, "writable": self.catalog.writable(item["name"])}
                for item in self.catalog.metadata()
            ]
            return _success(request, {"skills": entries, "root_id": self.catalog.root_id})
        except (OSError, ValueError):
            return _failure(request, "skill directory is invalid or changed")


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
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "writable": self.catalog.writable(name),
                "root_id": self.catalog.root_id,
            },
            truncated=end < len(content),
        )
