"""Optional OpenAI Agents SDK decision adapter with no direct tool executor."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any, Protocol, cast

from pydantic import Field, TypeAdapter, model_validator

from repopilot.agent.actions import AgentAction, AgentContext
from repopilot.domain.models import FrozenModel, JsonValue
from repopilot.infrastructure.optional import OptionalDependencyError

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_DEFAULT_TOOLS = (
    "repo_map",
    "read_file",
    "search",
    "apply_patch",
    "run_command",
    "git_diff",
)


class DecisionRunner(Protocol):
    def run(self, context: AgentContext) -> object: ...


class _SDKDecision(FrozenModel):
    """Structured output returned by the SDK; execution remains host-owned."""

    tool_name: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    final_text: str | None = None

    @model_validator(mode="after")
    def exactly_one_decision(self) -> _SDKDecision:
        if bool(self.tool_name) == bool(self.final_text):
            raise ValueError("return exactly one of tool_name or final_text")
        if self.final_text and self.arguments:
            raise ValueError("final decisions cannot contain tool arguments")
        return self


class OpenAIAgentsRuntime:
    """Translate model decisions into domain actions without executing side effects."""

    def __init__(
        self,
        *,
        runner: DecisionRunner | None = None,
        model: str = "gpt-5.6",
        tool_names: tuple[str, ...] = _DEFAULT_TOOLS,
        importer: Callable[[str], Any] = import_module,
    ) -> None:
        self._runner = runner
        self.model = model
        self.tool_names = tool_names
        self._importer = importer

    def next_action(self, context: AgentContext) -> AgentAction:
        if self._runner is None:
            self._runner = _AgentsSDKDecisionRunner(
                model=self.model,
                tool_names=self.tool_names,
                importer=self._importer,
            )
        raw = self._runner.run(context)
        decision = self._decision(raw)
        if decision.final_text is not None:
            return AgentAction.final(decision.final_text)
        assert decision.tool_name is not None
        operation_id = self._operation_id(context, decision.tool_name, decision.arguments)
        return AgentAction.tool(
            decision.tool_name,
            decision.arguments,
            operation_id=operation_id,
        )

    @classmethod
    def _decision(cls, raw: object) -> _SDKDecision:
        final_output = cls._get(raw, "final_output")
        if final_output is not None:
            raw = final_output
        if isinstance(raw, str):
            return _SDKDecision(final_text=raw)
        return _SDKDecision(
            tool_name=cast(str | None, cls._get(raw, "tool_name")),
            arguments=_JSON_OBJECT.validate_python(cls._get(raw, "arguments") or {}),
            final_text=cast(str | None, cls._get(raw, "final_text")),
        )

    @staticmethod
    def _get(value: object, field: str) -> object | None:
        if isinstance(value, Mapping):
            return value.get(field)
        return getattr(value, field, None)

    @staticmethod
    def _operation_id(
        context: AgentContext,
        tool_name: str,
        arguments: dict[str, JsonValue],
    ) -> str:
        canonical = json.dumps(
            {
                "issue": context.issue,
                "constraints": context.constraints,
                "exchange_count": len(context.exchanges),
                "tool_name": tool_name,
                "arguments": arguments,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        return f"sdk-{len(context.exchanges) + 1}-{digest}"


class _AgentsSDKDecisionRunner:
    """Small SDK wrapper loaded only when online execution is selected."""

    def __init__(
        self,
        *,
        model: str,
        tool_names: tuple[str, ...],
        importer: Callable[[str], Any],
    ) -> None:
        try:
            agents = importer("agents")
        except (ImportError, ModuleNotFoundError) as error:
            raise OptionalDependencyError(extra="online", dependency="openai-agents") from error
        agent_type = agents.Agent
        self._runner = agents.Runner
        catalog = ", ".join(tool_names)
        self._agent = agent_type(
            name="ForgePilot decision agent",
            model=model,
            instructions=(
                "Choose the next bounded repository action. Return structured output only. "
                f"Available host-controlled tools: {catalog}. Never claim a tool ran; the host "
                "will authorize, execute, redact, persist, and return its result. When the issue "
                "is verified, return final_text instead of a tool."
            ),
            output_type=_SDKDecision,
        )

    def run(self, context: AgentContext) -> object:
        prompt = (
            "Decide the next action from this redacted, paired task context:\n"
            f"{context.model_dump_json()}"
        )
        result = self._runner.run_sync(self._agent, prompt)
        return cast(object, result.final_output)
