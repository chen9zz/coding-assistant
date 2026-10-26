"""Paired, bounded fixture runs with incremental reports and no auto-approval."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repopilot.agent.actions import AgentAction
from repopilot.agent.loop import Runtime
from repopilot.agent.providers import HTTPRuntime, ProviderConfig
from repopilot.agent.scripted import ScriptedRuntime
from repopilot.bootstrap import build_local_container
from repopilot.connected import _INSTRUCTIONS
from repopilot.domain.models import TaskState, ToolRequest, ToolStatus
from repopilot.infrastructure.process import ProcessRunner
from repopilot.tools.builtin import GitDiffTool, build_builtin_registry
from repopilot.tools.catalog import tool_catalog
from repopilot.tools.rag import RagSearchTool
from repopilot.tools.redaction import Redactor

from .cases import FixtureCase, load_builtin_cases
from .retrieval import retrieval_rows, scheduled_rows, valid_citation
from .verification import verify_hidden

__all__ = ["add_note", "run_comparison", "valid_citation"]


def _atomic_text(path: Path, text: str) -> None:
    if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
        raise ValueError("report cannot be a link")
    fd, temporary = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_report(root: Path, report: dict[str, Any]) -> None:
    for name in ("report.json", "report.md"):
        path = root / name
        if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
            raise ValueError("report cannot be a link")
    summary = {}
    for enabled, label in ((False, "off"), (True, "on")):
        attempts = [a for a in report["attempts"] if a["rag"] == enabled]
        retrieval = [r for r in report["retrieval"] if r["rag"] == enabled]
        resolved = sum(a["resolved"] for a in attempts)
        citations = sum(r["citations"] for r in retrieval)
        summary[label] = dict(
            scheduled=len(attempts),
            resolved=resolved,
            resolved_rate=resolved / len(attempts) if attempts else None,
            states={
                s: sum(a["state"] == s for a in attempts)
                for s in sorted({a["state"] for a in attempts})
            },
            approval_requests=sum(len(a["approval_requests"]) for a in attempts),
            rag_calls=sum(a.get("rag_calls", 0) for a in attempts),
            human_notes=sum(len(a["human_notes"]) for a in attempts),
            retrieval_scheduled=len({a["case_id"] for a in attempts}) * 8,
            retrieval_queries=len(retrieval),
            hit_at_5=sum(r["hit_at_5"] for r in retrieval) / len(retrieval) if retrieval else None,
            mrr=sum(r["reciprocal_rank"] for r in retrieval) / len(retrieval)
            if retrieval
            else None,
            citation_accuracy=(
                sum(r["valid_citations"] for r in retrieval) / citations if citations else None
            ),
            retrieval_failures=sum(r["failed"] for r in retrieval),
        )
    report["summary"] = summary
    # This directory is newly created and private; replace the report after each attempt.
    _atomic_text(root / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    rows = [
        "# ForgePilot RAG on/off evaluation",
        "",
        f"Mode: {report['mode']}",
        "",
        "Small fixed fixtures; scripted results do not measure model improvement.",
        "All scheduled attempts remain in the denominator; no automatic approvals/retries.",
        "",
        "| Attempt | RAG | State | Resolved | Approval requests | Human notes |",
        "|---|---|---|---|---|---|",
    ]
    rows += [
        f"| {a['id']} | {a['rag']} | {a['state']} | {a['resolved']} | "
        f"{len(a['approval_requests'])} | {len(a['human_notes'])} |"
        for a in report["attempts"]
    ]
    rows += ["", "```json", json.dumps(summary, ensure_ascii=False, indent=2), "```", ""]
    _atomic_text(root / "report.md", "\n".join(rows))


def _attempt(
    root: Path,
    source: Path,
    case: FixtureCase,
    record: dict[str, Any],
    provider: ProviderConfig | None,
    redactor: Redactor,
) -> None:
    directory = root / record["id"]
    directory.mkdir(mode=0o700)
    registry = build_builtin_registry()
    cache = directory / "rag"
    if record["rag"]:
        registry.register(RagSearchTool(cache, redactor))
    container = build_local_container(
        directory,
        registry=registry,
        secret_values={os.environ[provider.api_key_env]}
        if provider and provider.api_key_env and provider.api_key_env in os.environ
        else set(),
        rag_cache_root=cache.absolute() if record["rag"] else None,
    )
    spec = case.task_spec(source).model_copy(update={"max_steps": 24, "timeout_seconds": 300})
    task = container.service.create(spec)
    record["task_id"] = task.task_id
    runtime: Runtime
    if provider is not None:
        runtime = HTTPRuntime(provider, tools=tool_catalog(registry), instructions=_INSTRUCTIONS)
    else:
        query = case.definition.expected_changed_paths[0].removesuffix(".py")
        initial = AgentAction.tool(
            "rag_search" if record["rag"] else "search",
            {"query": query},
            operation_id="evaluation-retrieval",
        )
        runtime = ScriptedRuntime((initial, *case.actions))
    try:
        snapshot = container.service.run(task.task_id, runtime)
        record["state"] = snapshot.state.value
        if snapshot.worktree is not None and snapshot.state is not TaskState.WAITING_APPROVAL:
            patch = GitDiffTool().execute(
                snapshot.worktree,
                ToolRequest(name="git_diff", operation_id="evaluation-final-diff", arguments={}),
            )
            output = patch.output if isinstance(patch.output, dict) else {}
            record["patch_sha256"] = output.get("diff_sha256")
            raw_paths = output.get("changed_paths", [])
            paths = (
                [p for p in raw_paths if isinstance(p, str)] if isinstance(raw_paths, list) else []
            )
            record["scope_ok"] = patch.status is ToolStatus.SUCCEEDED and sorted(paths) == sorted(
                case.definition.expected_changed_paths
            )
            record["hidden_tests_passed"] = verify_hidden(
                case, snapshot.worktree, directory / "hidden-results.xml"
            )
            record["resolved"] = (
                snapshot.state is TaskState.SUCCEEDED
                and record["scope_ok"]
                and record["hidden_tests_passed"]
            )
    finally:
        events = container.service.events(task.task_id)
        # Store event identities, never raw tool arguments, model text or exception messages.
        record["events"] = [dict(type=e.event_type, sequence=e.sequence) for e in events]
        record["approval_requests"] = [
            e.payload.get("operation_id")
            for e in events
            if e.event_type == "task.approval_requested"
        ]
        record["approval_decisions"] = [
            e.payload.get("operation_id") for e in events if e.event_type == "task.approved"
        ]
        record["tool_calls"] = sum(e.event_type == "tool.started" for e in events)
        record["rag_calls"] = sum(
            e.event_type == "tool.started" and e.payload.get("tool_name") == "rag_search"
            for e in events
        )
        record["failure_types"] = [
            e.payload.get("error_type", e.event_type)
            for e in events
            if e.event_type in {"task.failed", "tool.failed", "tool.denied"}
        ]


def run_comparison(
    workspace: Path,
    *,
    cases: tuple[FixtureCase, ...] | None = None,
    provider: ProviderConfig | None = None,
    repetitions: int = 1,
) -> dict[str, Any]:
    if not 1 <= repetitions <= 3:
        raise ValueError("repetitions must be 1..3")
    cases = load_builtin_cases() if cases is None else cases
    if not cases or len({c.case_id for c in cases}) != len(cases):
        raise ValueError("cases must be nonempty and unique")
    root = workspace.absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    secrets = (
        {os.environ[provider.api_key_env]}
        if (provider and provider.api_key_env and provider.api_key_env in os.environ)
        else set()
    )
    redactor = Redactor(secrets)
    report: dict[str, Any] = dict(
        schema_version=1,
        mode="online-model" if provider else "scripted-contract",
        created_at=datetime.now(UTC).isoformat(),
        complete=False,
        model=redactor.redact(provider.model) if provider else None,
        provider_config_sha256=hashlib.sha256(provider.model_dump_json().encode()).hexdigest()
        if provider
        else None,
        repetitions=repetitions,
        retrieval=[
            row
            for c in cases
            for row in scheduled_rows(c.case_id, c.definition.expected_changed_paths[0])
        ],
        attempts=[],
    )
    for repeat in range(repetitions):
        for index, case in enumerate(cases):
            for enabled in (False, True) if (index + repeat) % 2 == 0 else (True, False):
                report["attempts"].append(
                    dict(
                        id=f"{case.case_id}-{repeat}-{'on' if enabled else 'off'}",
                        case_id=case.case_id,
                        repetition=repeat,
                        rag=enabled,
                        state="SCHEDULED",
                        resolved=False,
                        max_steps=24,
                        timeout_seconds=300,
                        max_context_chars=128_000,
                        approval_requests=[],
                        approval_decisions=[],
                        human_notes=[],
                    )
                )
    _write_report(root, report)
    sources: dict[str, Path] = {}
    for case in cases:
        try:
            source = case.prepare_repository(root / f"source-{case.case_id}")
            sources[case.case_id] = source
            head = ProcessRunner().run(
                ["git", "rev-parse", "HEAD"], cwd=source, timeout_seconds=10, max_output_bytes=1000
            )
            if head.exit_code:
                raise RuntimeError("baseline HEAD unavailable")
            for record in report["attempts"]:
                if record["case_id"] == case.case_id:
                    record["base_commit"] = head.stdout.strip()
                    record["issue_sha256"] = hashlib.sha256(
                        case.definition.issue.encode()
                    ).hexdigest()
                    record["fixture_sha256"] = hashlib.sha256(
                        b"".join(
                            str(p.relative_to(case.root)).encode() + b"\0" + p.read_bytes() + b"\0"
                            for p in sorted(case.root.rglob("*"))
                            if p.is_file()
                        )
                    ).hexdigest()
        except Exception as error:
            for record in report["attempts"]:
                if record["case_id"] == case.case_id:
                    record.update(state="SETUP_ERROR", error_type=type(error).__name__)
        if case.case_id in sources and all(
            a["state"] == "SCHEDULED" for a in report["attempts"] if a["case_id"] == case.case_id
        ):
            results = retrieval_rows(
                source,
                root / f"retrieval-{case.case_id}",
                case.case_id,
                case.definition.expected_changed_paths[0],
            )
            report["retrieval"] = [
                r for r in report["retrieval"] if r["case_id"] != case.case_id
            ] + results
        else:
            for row in report["retrieval"]:
                if row["case_id"] == case.case_id:
                    row["status"] = "SETUP_ERROR"
        _write_report(root, report)
    by_id = {c.case_id: c for c in cases}
    for record in report["attempts"]:
        if record["state"] != "SCHEDULED":
            continue
        record["state"] = "RUNNING"
        _write_report(root, report)
        started = time.monotonic()
        try:
            _attempt(
                root,
                sources[record["case_id"]],
                by_id[record["case_id"]],
                record,
                provider,
                redactor,
            )
        except Exception as error:
            record.update(state="ERROR", resolved=False, error_type=type(error).__name__)
        record["duration_ms"] = round((time.monotonic() - started) * 1000)
        _write_report(root, report)
    report["complete"] = True
    _write_report(root, report)
    return report


def _add_note(workspace: Path, attempt_id: str, kind: str, note: str) -> None:
    """Append operator observations; never approve, resume or rewrite an outcome."""
    if kind not in {"diagnosis", "approval", "cancellation"} or not 1 <= len(note) <= 2000:
        raise ValueError("invalid note kind or length")
    root = workspace.resolve(strict=True)
    path = root / "report.json"
    if path.is_symlink() or path.stat().st_nlink != 1:
        raise ValueError("report must be an ordinary unlinked file")
    if path.stat().st_size > 2_000_000:
        raise ValueError("report too large")
    report = json.loads(path.read_text())
    if not report["complete"]:
        raise ValueError("notes require a completed comparison")
    record = next((a for a in report["attempts"] if a["id"] == attempt_id), None)
    if record is None:
        raise ValueError("unknown attempt")
    redactor = Redactor(
        {
            value
            for name, value in os.environ.items()
            if any(word in name.upper() for word in ("KEY", "TOKEN", "PASSWORD"))
        }
    )
    record["human_notes"].append(
        dict(
            kind=kind,
            recorded_at=datetime.now(UTC).isoformat(),
            note=redactor.redact(note),
            note_sha256=hashlib.sha256(note.encode()).hexdigest(),
            source="operator_annotation_not_execution_evidence",
        )
    )
    _write_report(root, report)


def add_note(workspace: Path, attempt_id: str, kind: str, note: str) -> None:
    """Serialize operator annotations; never trigger execution or approval."""
    from repopilot.tools.skill_files import private_root

    root = workspace.absolute()
    directory, _ = private_root(root)
    try:
        fd = os.open(
            ".notes.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            import stat

            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("invalid notes lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _add_note(root, attempt_id, kind, note)
        finally:
            os.close(fd)
    finally:
        os.close(directory)
