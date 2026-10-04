"""Redacted local JSONL telemetry and content-minimal OpenTelemetry export."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from repopilot.infrastructure.optional import OptionalDependencyError
from repopilot.tools.redaction import Redactor


class Span(Protocol):
    def __enter__(self) -> Span: ...

    def __exit__(self, *args: object) -> object: ...

    def set_attribute(self, key: str, value: object) -> None: ...


class Tracer(Protocol):
    def start_as_current_span(self, name: str) -> Span: ...


class JsonlTelemetry:
    """Append one redacted, versioned JSON object per line."""

    def __init__(self, path: Path, redactor: Redactor) -> None:
        self.path = path
        self.redactor = redactor
        self._lock = Lock()

    def emit(self, event_type: str, payload: dict[str, object]) -> None:
        record = {
            "schema_version": 1,
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "payload": self.redactor.redact(payload),
        }
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{serialized}\n")


class OpenTelemetryAdapter:
    """Export IDs and numeric metrics while excluding prompts and tool content."""

    _IDENTIFIERS = frozenset({"task_id", "correlation_id", "operation_id"})

    def __init__(
        self,
        *,
        tracer: Tracer | None = None,
        importer: Callable[[str], Any] = import_module,
    ) -> None:
        if tracer is None:
            try:
                trace = importer("opentelemetry.trace")
            except (ImportError, ModuleNotFoundError) as error:
                raise OptionalDependencyError(
                    extra="otel", dependency="opentelemetry-sdk"
                ) from error
            tracer = trace.get_tracer("repopilot-r")
        self.tracer = tracer

    def emit(self, event_type: str, payload: dict[str, object]) -> None:
        with self.tracer.start_as_current_span(f"repopilot.{event_type}") as span:
            for key, value in self._safe_attributes(payload).items():
                span.set_attribute(f"repopilot.{key}", value)

    @classmethod
    def _safe_attributes(cls, payload: Mapping[str, object]) -> dict[str, object]:
        attributes: dict[str, object] = {}
        for key, value in payload.items():
            if (key in cls._IDENTIFIERS and isinstance(value, str)) or isinstance(
                value, (bool, int, float)
            ):
                attributes[key] = value
        return attributes
