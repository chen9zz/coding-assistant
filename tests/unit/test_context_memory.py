import hashlib

from repopilot.agent.actions import AgentAction, AgentContext, ToolExchange
from repopilot.domain.models import ToolResult, ToolStatus


def exchange(number, status=ToolStatus.SUCCEEDED):
    request = AgentAction.tool(
        "read_file", {"path": "a.py"}, operation_id=f"op-{number}"
    ).to_tool_request()
    result = ToolResult(
        operation_id=request.operation_id,
        tool_name=request.name,
        status=status,
        output={"text": "evidence " * 1000},
    )
    return ToolExchange(request=request, result=result)


def test_repeated_compaction_keeps_original_digest():
    original = exchange(1)
    first = AgentContext._compacted_exchange(original, include_preview=True)
    second = AgentContext._compacted_exchange(first, include_preview=False)
    assert (
        second.result.output["original_sha256"]
        == hashlib.sha256(original.result.model_dump_json().encode()).hexdigest()
    )
    assert second.result.operation_id == "op-1"


def test_checkpoint_rebuilds_from_original_or_compacted_history():
    context = AgentContext(
        issue="fix",
        constraints=("no approval bypass",),
        exchanges=(exchange(1, ToolStatus.FAILED), exchange(2)),
    )
    assert callable(getattr(context, "checkpoint", None))
    checkpoint = context.checkpoint()
    assert checkpoint["failed_operations"] == ["op-1"]
    compacted = context.model_copy(
        update={
            "exchanges": tuple(
                context._compacted_exchange(e, include_preview=False) for e in context.exchanges
            )
        }
    )
    assert compacted.checkpoint() == checkpoint


def test_full_payload_budget_counts_system_and_tools():
    import json

    import pytest

    from repopilot.agent.actions import ContextBudgetExceeded
    from repopilot.agent.providers import HTTPRuntime, ProviderConfig

    config = ProviderConfig(protocol="openai", base_url="http://127.0.0.1:9", model="test")
    context = AgentContext(issue="test", exchanges=tuple(exchange(i) for i in range(5)))
    runtime = HTTPRuntime(config, tools=[], instructions="host rules " * 50)
    compacted = runtime.prepare_context(context, 30000)
    assert len(json.dumps(runtime._payload(compacted), ensure_ascii=False)) <= 30000
    runtime = HTTPRuntime(config, tools=[], instructions="rules" * 10000)
    with pytest.raises(ContextBudgetExceeded):
        runtime.prepare_context(AgentContext(issue="test"), 24000)
