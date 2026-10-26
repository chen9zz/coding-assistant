"""Small lexical retrieval benchmark; never a model-quality leaderboard."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from repopilot.domain.models import ToolRequest, ToolStatus
from repopilot.tools.builtin import SearchTool
from repopilot.tools.rag import RagSearchTool
from repopilot.tools.rag_sources import read_source
from repopilot.tools.redaction import Redactor

# Eight probes per tiny fixture: identifiers, expressions, paraphrases and Chinese.
QUERIES = {
    "config_validation": (
        "parse_port",
        "int value",
        "port",
        "value str",
        "return int",
        "missing default port",
        "validate TCP range",
        "端口 默认值",
    ),
    "divide_zero": (
        "safe_divide",
        "denominator",
        "numerator",
        "float return",
        "numerator denominator",
        "division by zero",
        "safe division",
        "除零 分母",
    ),
    "slug_normalization": (
        "slugify",
        "strip",
        "lower",
        "replace",
        "value str",
        "normalize repeated whitespace",
        "punctuation hyphen",
        "连续 空格",
    ),
}


def valid_citation(root: Path, hit: dict[str, Any]) -> bool:
    try:
        path = hit["path"]
        if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
            return False
        source = read_source(root, path, Redactor())
        if source is None:
            return False
        start, end = hit["start_line"], hit["end_line"]
        lines = (root / path).read_text().splitlines(keepends=True)
        return (
            type(start) is int
            and type(end) is int
            and 1 <= start <= end <= len(lines)
            and source.sha256 == hit["sha256"]
            and "".join(lines[start - 1 : end]) == hit["text"]
        )
    except (OSError, KeyError, ValueError, TypeError):
        return False


def scheduled_rows(case_id: str, expected: str) -> list[dict[str, Any]]:
    return [
        dict(
            case_id=case_id,
            query=query,
            rag=enabled,
            expected_path=expected,
            status="NOT_RUN",
            hit_at_5=False,
            reciprocal_rank=0.0,
            citations=0,
            valid_citations=0,
            duration_ms=0,
            truncated=False,
            failed=True,
        )
        for query in QUERIES[case_id]
        for enabled in (False, True)
    ]


def retrieval_rows(root: Path, cache: Path, case_id: str, expected: str) -> list[dict[str, Any]]:
    rows = scheduled_rows(case_id, expected)
    for number, row in enumerate(rows):
        try:
            _retrieve(root, cache, number, row)
        except Exception as error:
            row.update(status="ERROR", error_type=type(error).__name__)
    return rows


def _retrieve(root: Path, cache: Path, number: int, row: dict[str, Any]) -> None:
    enabled, query = row["rag"], row["query"]
    terms = re.findall(r"[^\W_]+", query, re.UNICODE)
    request = ToolRequest(
        operation_id=f"retrieval-{number}",
        name="rag_search" if enabled else "search",
        arguments={"query": query if enabled else "|".join(map(re.escape, terms))},
    )
    tool = RagSearchTool(cache) if enabled else SearchTool()
    result = tool.execute(root, request)
    output = result.output if isinstance(result.output, dict) else {}
    hits: list[dict[str, Any]] = []
    if enabled:
        raw_hits = output.get("hits", [])
        hits = [h for h in raw_hits if isinstance(h, dict)] if isinstance(raw_hits, list) else []
    else:
        # Natural numeric line ordering; literal OR baseline is not relevance ranked.
        for match in str(output.get("matches", "")).splitlines():
            filename, line, text = match.split(":", 2)
            relative = str(Path(filename).relative_to(root))
            raw = (root / relative).read_bytes()
            hits.append(
                dict(
                    path=relative,
                    start_line=int(line),
                    end_line=int(line),
                    text=text + "\n",
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
            )
        hits = sorted(hits, key=lambda h: (h["path"], h["start_line"]))[:5]
    rank = next(
        (
            i
            for i, h in enumerate(hits, 1)
            if h["path"] == row["expected_path"] and valid_citation(root, h)
        ),
        None,
    )
    row.update(
        status=result.status.value,
        hit_at_5=rank is not None,
        reciprocal_rank=1 / rank if rank else 0.0,
        citations=len(hits),
        valid_citations=sum(valid_citation(root, h) for h in hits),
        duration_ms=result.duration_ms,
        truncated=result.truncated,
        failed=result.status is not ToolStatus.SUCCEEDED,
    )
