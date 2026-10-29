"""Deterministic evidence-availability comparison; not a model quality benchmark."""

from __future__ import annotations

import json
from typing import Any

from repopilot.agent.actions import AgentAction, AgentContext, ToolExchange
from repopilot.domain.models import JsonValue, ToolResult, ToolStatus


def compare_context() -> dict[str, Any]:
    rows = []
    for case in range(6):
        expected = ["a" * 64, f"memory-{case}-" + "b" * 32, "read_receipts"]
        exchanges = []
        samples: list[tuple[str, dict[str, JsonValue], JsonValue]] = [
            (
                "rag_search",
                {"query": "boundary"},
                {
                    "hits": [
                        {
                            "path": "config.py",
                            "citation": "config.py:1-4",
                            "sha256": expected[0],
                            "text": "Validate input before conversion. " * 200,
                        }
                    ]
                },
            ),
            (
                "memory_search",
                {"query": "boundary"},
                {
                    "hits": [
                        {
                            "id": expected[1],
                            "source": "config.py",
                            "source_sha256": expected[0],
                            "text": "Preserve input validation.",
                            "effective_state": "approved",
                        }
                    ]
                },
            ),
            (
                "read_file",
                {"path": "config.py"},
                {"content": "def validate(): pass\n", "offset": 0, "size_bytes": 21},
            ),
        ]
        samples += [
            ("search", {"query": f"noise-{i}"}, {"matches": "noise " * (800 + case * 20)})
            for i in range(12)
        ]
        for i, (tool, arguments, output) in enumerate(samples):
            request = AgentAction.tool(tool, arguments, operation_id=f"op-{i}").to_tool_request()
            exchanges.append(
                ToolExchange(
                    request=request,
                    result=ToolResult(
                        operation_id=request.operation_id,
                        tool_name=tool,
                        status=ToolStatus.SUCCEEDED,
                        output=output,
                    ),
                )
            )
        context = AgentContext(issue="Fix input validation", exchanges=tuple(exchanges))
        enhanced = context.compact(30000)
        # Baseline uses the previous digest/preview representation with no retained facts.
        legacy = []
        for exchange in enhanced.exchanges:
            output = exchange.result.output
            if exchange.compacted and isinstance(output, dict):
                output = {key: value for key, value in output.items() if key != "facts"}
                exchange = exchange.model_copy(
                    update={"result": exchange.result.model_copy(update={"output": output})}
                )
            legacy.append(exchange)
        baseline_text = json.dumps([e.result.model_dump(mode="json") for e in legacy])
        enhanced_text = json.dumps(enhanced.working_state())
        before = sum(value in baseline_text for value in expected)
        after = sum(value in enhanced_text for value in expected)
        rows.append(
            {
                "case": case,
                "baseline_retained_fields": before,
                "enhanced_retained_fields": after,
                "required_fields": len(expected),
                "baseline_scripted_recovery_requests": len(expected) - before,
                "enhanced_scripted_recovery_requests": len(expected) - after,
                "enhanced_context_chars": len(enhanced.model_dump_json()),
            }
        )
    return {
        "kind": "deterministic representation ablation",
        "model_calls": 0,
        "budget_chars": 30000,
        "cases": rows,
        "limitations": "Recovery counts follow a fixed missing-field policy, not observed LLM "
        "behavior. Baseline removes retained facts from the same compacted "
        "history; this is not an exact replay of the prior HTTP harness.",
    }
