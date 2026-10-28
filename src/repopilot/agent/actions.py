"""Typed actions and paired tool context for all agent runtimes."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from repopilot.domain.models import FrozenModel, JsonValue, ToolRequest, ToolResult, ToolStatus


class ActionKind(StrEnum):
    TOOL = "TOOL"
    FINAL = "FINAL"


class AgentAction(FrozenModel):
    kind: ActionKind
    operation_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    final_text: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> AgentAction:
        if self.kind is ActionKind.TOOL:
            if not self.operation_id or not self.tool_name or self.final_text is not None:
                raise ValueError("tool actions require operation/tool identity and no final text")
        elif not self.final_text or self.operation_id or self.tool_name or self.arguments:
            raise ValueError("final actions require only non-empty final text")
        return self

    @classmethod
    def tool(
        cls,
        name: str,
        arguments: dict[str, JsonValue],
        *,
        operation_id: str,
    ) -> AgentAction:
        return cls(
            kind=ActionKind.TOOL,
            operation_id=operation_id,
            tool_name=name,
            arguments=arguments,
        )

    @classmethod
    def final(cls, text: str) -> AgentAction:
        return cls(kind=ActionKind.FINAL, final_text=text)

    def to_tool_request(self) -> ToolRequest:
        if self.kind is not ActionKind.TOOL or self.operation_id is None or self.tool_name is None:
            raise ValueError("only a tool action can become a tool request")
        return ToolRequest(
            operation_id=self.operation_id,
            name=self.tool_name,
            arguments=self.arguments,
        )


class ToolExchange(FrozenModel):
    request: ToolRequest
    result: ToolResult
    compacted: bool = False

    @model_validator(mode="after")
    def identities_match(self) -> ToolExchange:
        if self.request.operation_id != self.result.operation_id:
            raise ValueError("tool request/result operation IDs must remain paired")
        if self.request.name != self.result.tool_name:
            raise ValueError("tool request/result names must remain paired")
        return self


class AgentContext(FrozenModel):
    issue: str = ""
    constraints: tuple[str, ...] = ()
    exchanges: tuple[ToolExchange, ...] = ()
    compaction_count: int = 0

    @classmethod
    def empty(
        cls,
        issue: str = "",
        constraints: tuple[str, ...] = (),
    ) -> AgentContext:
        return cls(issue=issue, constraints=constraints)

    def with_exchange(self, exchange: ToolExchange) -> AgentContext:
        return self.model_copy(update={"exchanges": (*self.exchanges, exchange)})

    def compact(self, max_chars: int, keep_recent: int = 2) -> AgentContext:
        if max_chars < 256:
            raise ValueError("context budget must be at least 256 characters")
        if len(self.model_dump_json()) <= max_chars:
            return self
        exchanges = list(self.exchanges)
        compact_until = max(0, len(exchanges) - keep_recent)
        candidate = self.model_copy(
            update={
                "exchanges": tuple(exchanges),
                "compaction_count": self.compaction_count + 1,
            }
        )
        for index in range(compact_until):
            exchanges[index] = self._compacted_exchange(exchanges[index], include_preview=True)
            candidate = candidate.model_copy(update={"exchanges": tuple(exchanges)})
            if len(candidate.model_dump_json()) <= max_chars:
                return candidate
        for index in range(compact_until):
            exchanges[index] = self._compacted_exchange(exchanges[index], include_preview=False)
            candidate = candidate.model_copy(update={"exchanges": tuple(exchanges)})
            if len(candidate.model_dump_json()) <= max_chars:
                return candidate
        raise ContextBudgetExceeded("paired context metadata exceeds the configured budget")

    def checkpoint(self) -> dict[str, Any]:
        """Bounded host facts reconstructed from durable history, never model assertions."""
        return {
            "untrusted_evidence": True,
            "historical_only": True,
            "operation_count": len(self.exchanges),
            "failed_operations": [
                e.request.operation_id
                for e in self.exchanges
                if e.result.status in {ToolStatus.FAILED, ToolStatus.DENIED}
            ][-8:],
            "approval_operations": [
                e.request.operation_id
                for e in self.exchanges
                if e.result.status is ToolStatus.APPROVAL_REQUIRED
            ][-8:],
            "successful_patch_operations": [
                e.request.operation_id
                for e in self.exchanges
                if e.request.name == "apply_patch" and e.result.status is ToolStatus.SUCCEEDED
            ][-8:],
            "recent_operations": [
                {
                    "id": e.request.operation_id,
                    "tool": e.request.name,
                    "status": e.result.status.value,
                }
                for e in self.exchanges[-4:]
            ],
        }

    @staticmethod
    def _compacted_exchange(exchange: ToolExchange, *, include_preview: bool) -> ToolExchange:
        serialized = exchange.result.model_dump_json()
        if exchange.compacted and isinstance(exchange.result.output, dict):
            previous = dict(exchange.result.output)
            if not include_preview:
                previous.pop("preview", None)
            return exchange.model_copy(
                update={"result": exchange.result.model_copy(update={"output": previous})}
            )
        summary: dict[str, JsonValue] = {
            "compacted": True,
            "original_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        }
        if include_preview:
            summary["preview"] = serialized[:120]
        result = exchange.result.model_copy(update={"output": summary, "truncated": True})
        return exchange.model_copy(update={"result": result, "compacted": True})


class FinalAnswer(FrozenModel):
    text: str
    steps: int
    context: AgentContext


class ContextBudgetExceeded(RuntimeError):
    """Raised when even compacted paired metadata cannot fit."""
