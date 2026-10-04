"""Recursive secret redaction before persistence, APIs, and telemetry."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_CREDENTIAL = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:api[_-]?key|token|secret|password)[a-z0-9_.-]*)"
    r"\s*([:=])(\s*)([^\s,}\]]+)"
)
_OPENAI_STYLE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")
_REDACTED = "[REDACTED]"


class Redactor:
    """Remove configured values and common credential shapes recursively."""

    def __init__(self, secrets: set[str] | frozenset[str] | None = None) -> None:
        self._secrets = tuple(
            sorted((value for value in secrets or set() if value), key=len, reverse=True)
        )

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, Mapping):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        return value

    def _redact_text(self, text: str) -> str:
        text = _CREDENTIAL.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}{_REDACTED}",
            text,
        )
        text = _OPENAI_STYLE_KEY.sub(_REDACTED, text)
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        return text
