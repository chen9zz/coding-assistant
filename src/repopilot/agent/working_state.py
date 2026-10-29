"""Bounded historical evidence, never executable instructions or current truth."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from repopilot.domain.models import ToolStatus

if TYPE_CHECKING:
    from repopilot.agent.actions import ToolExchange

# Unknown tools may have effects. A failed write may also have partially executed.
_READ_ONLY = frozenset(
    {
        "read_file",
        "search",
        "repo_map",
        "git_diff",
        "rag_search",
        "memory_search",
        "context_read",
        "skill_read",
        "skill_list",
        "agent_status",
        "agent_result",
    }
)


def facts(exchange: ToolExchange) -> dict[str, Any]:
    output = exchange.result.output
    if not isinstance(output, dict):
        return {}
    if exchange.compacted:
        stored = output.get("facts")
        return dict(stored) if isinstance(stored, dict) else {}
    name = exchange.request.name
    source_hits = output.get("hits")
    if name in {"rag_search", "memory_search"} and isinstance(source_hits, list):
        hits = []
        for hit in source_hits[:2]:
            if not isinstance(hit, dict):
                continue
            selected = {
                key: value[:256]
                for key in (
                    "path",
                    "citation",
                    "sha256",
                    "id",
                    "source",
                    "source_sha256",
                    "effective_state",
                )
                if isinstance(value := hit.get(key), str)
            }
            text = hit.get("text")
            if isinstance(text, str):
                selected["excerpt"] = text[:240]
            hits.append(selected)
        return {"hits": hits, "total_hits": len(source_hits)}
    content = output.get("content")
    if name == "read_file" and isinstance(content, str):
        path = exchange.request.arguments.get("path")
        return {
            "path": path[:256] if isinstance(path, str) else "",
            "offset": output.get("offset", 0),
            "next_offset": output.get("next_offset"),
            "size_bytes": output.get("size_bytes"),
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        }
    if name == "run_command":
        return {
            key: output[key]
            for key in ("exit_code", "timed_out")
            if type(output.get(key)) in {int, bool}
        }
    patch_sha = output.get("patch_sha256")
    if name == "apply_patch" and isinstance(patch_sha, str):
        return {"patch_sha256": patch_sha[:64]}
    return {}


def working_state(exchanges: tuple[ToolExchange, ...]) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    receipts: dict[str, dict[str, Any]] = {}
    boundary = 0
    failed: list[str] = []
    for index, exchange in enumerate(exchanges):
        name = exchange.request.name
        if name not in _READ_ONLY:
            boundary = index + 1
            receipts.clear()
        if exchange.result.status in {ToolStatus.FAILED, ToolStatus.DENIED}:
            failed.append(exchange.request.operation_id)
        extracted = facts(exchange)
        if extracted:
            evidence.append(
                {
                    "operation_id": exchange.request.operation_id,
                    "tool": name,
                    "status": exchange.result.status.value,
                    "facts": extracted,
                    "historical_only": True,
                    "sequence": index + 1,
                }
            )
        if name == "read_file" and exchange.result.status is ToolStatus.SUCCEEDED and extracted:
            key = exchange.request.arguments_digest
            previous = receipts.pop(key, None)
            receipts[key] = {
                **extracted,
                "operation_id": exchange.request.operation_id,
                "arguments_digest": key,
                "read_count": (previous["read_count"] if previous else 0) + 1,
                "historical_only": True,
            }
    anchors = [e for e in evidence if e["tool"] != "read_file"][-8:]
    reads = [e for e in evidence if e["tool"] == "read_file"][-4:]
    return {
        "untrusted_evidence": True,
        "phase": "working_after_possible_effect" if boundary else "investigating",
        "last_possible_effect_sequence": boundary,
        "evidence": sorted(anchors + reads, key=lambda e: e["sequence"]),
        "read_receipts": list(receipts.values())[-8:],
        "unresolved_operations": failed[-8:],
        "guidance": "Receipts are historical, not freshness guarantees. Reuse available evidence "
        "for reasoning; use context_read for compacted evidence. Read current files "
        "again before editing or when external changes are possible. Failures remain "
        "unresolved until explicitly checked; command exit codes are not host acceptance.",
    }
