from pathlib import Path

import pytest

from repopilot.evaluation import paired
from repopilot.evaluation.cases import load_builtin_cases


def test_paired_runner_exists() -> None:
    assert callable(getattr(paired, "run_comparison", None))


def test_paired_fixture_identity_and_reports(tmp_path: Path) -> None:
    report = paired.run_comparison(tmp_path / "run", cases=load_builtin_cases()[1:2])
    assert report["mode"] == "scripted-contract"
    attempts = report["attempts"]
    assert len(attempts) == 2
    assert {a["rag"] for a in attempts} == {False, True}
    assert len({a["base_commit"] for a in attempts}) == 1
    assert all(a["resolved"] for a in attempts)
    assert all(a["max_steps"] == 24 for a in attempts)
    assert report["summary"]["off"]["scheduled"] == 1
    assert report["summary"]["on"]["rag_calls"] == 1
    assert report["summary"]["off"]["rag_calls"] == 0
    assert (tmp_path / "run/report.md").exists()
    with pytest.raises(FileExistsError):
        paired.run_comparison(tmp_path / "run")


def test_citation_checks_content_and_hash(tmp_path: Path) -> None:
    import hashlib

    source = tmp_path / "example.py"
    source.write_text("one\ntwo\n")
    hit = dict(
        path="example.py",
        start_line=1,
        end_line=1,
        text="one\n",
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    assert paired.valid_citation(tmp_path, hit)
    assert not paired.valid_citation(tmp_path, {**hit, "text": "invented\n"})
    assert not paired.valid_citation(tmp_path, {**hit, "path": "../example.py"})
    source.write_text("new\ntwo\n")
    assert not paired.valid_citation(tmp_path, hit)


def test_errors_preserved_and_notes_do_not_change_score(tmp_path: Path, monkeypatch) -> None:
    import json

    def fail(*args, **kwargs):
        raise RuntimeError("secret-value-never-export")

    monkeypatch.setattr(paired, "_attempt", fail)
    root = tmp_path / "run"
    report = paired.run_comparison(root, cases=load_builtin_cases()[1:2])
    assert all(a["state"] == "ERROR" for a in report["attempts"])
    assert report["summary"]["on"]["resolved_rate"] == 0
    assert "secret-value-never-export" not in (root / "report.json").read_text()
    monkeypatch.setenv("TEST_API_KEY", "secret-for-note-redaction")
    paired.add_note(
        root, report["attempts"][0]["id"], "diagnosis", "checked failure secret-for-note-redaction"
    )
    saved = json.loads((root / "report.json").read_text())
    assert saved["attempts"][0]["state"] == "ERROR"
    assert saved["attempts"][0]["human_notes"][0]["kind"] == "diagnosis"
    assert "secret-for-note-redaction" not in (root / "report.json").read_text()
    with pytest.raises(ValueError):
        paired.add_note(root, "unknown", "diagnosis", "reason")
    with pytest.raises(ValueError):
        paired.add_note(root, report["attempts"][0]["id"], "other", "reason")


def test_approval_is_retained_without_auto_approval(tmp_path: Path, monkeypatch) -> None:
    from repopilot.agent.actions import AgentAction
    from repopilot.agent.scripted import ScriptedRuntime

    monkeypatch.setattr(
        paired,
        "ScriptedRuntime",
        lambda actions: ScriptedRuntime(
            [
                AgentAction.tool("run_command", {"argv": ["pwd"]}, operation_id="needs-approval"),
                AgentAction.final("must not run"),
            ]
        ),
    )
    report = paired.run_comparison(tmp_path / "run", cases=load_builtin_cases()[1:2])
    for attempt in report["attempts"]:
        assert attempt["state"] == "WAITING_APPROVAL"
        assert attempt["approval_requests"] == ["needs-approval"]
        assert attempt["approval_decisions"] == []
        assert not attempt["resolved"]


def test_retrieval_has_24_questions_and_failures_in_denominator(tmp_path: Path) -> None:
    report = paired.run_comparison(tmp_path / "run")
    assert len(report["retrieval"]) == 48
    assert len(report["attempts"]) == 6
    for arm in ("on", "off"):
        summary = report["summary"][arm]
        assert summary["retrieval_queries"] == 24
        assert summary["scheduled"] == 3
        assert summary["citation_accuracy"] == 1
        assert 0 < summary["hit_at_5"] < 1  # paraphrases/Chinese do not magically match
    assert all(a["resolved"] for a in report["attempts"])


def test_interruption_keeps_scheduled_attempts(tmp_path: Path, monkeypatch) -> None:
    import json

    def stop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(paired, "_attempt", stop)
    with pytest.raises(KeyboardInterrupt):
        paired.run_comparison(tmp_path / "run", cases=load_builtin_cases()[1:2])
    report = json.loads((tmp_path / "run/report.json").read_text())
    assert not report["complete"]
    assert [a["state"] for a in report["attempts"]] == ["RUNNING", "SCHEDULED"]
    with pytest.raises(ValueError):
        paired.add_note(tmp_path / "run", report["attempts"][0]["id"], "diagnosis", "reason")


@pytest.mark.parametrize("repetitions", [0, 4])
def test_reject_excess_budget(tmp_path: Path, repetitions: int) -> None:
    with pytest.raises(ValueError):
        paired.run_comparison(tmp_path / "run", repetitions=repetitions)
    assert not (tmp_path / "run").exists()


def test_retrieval_error_does_not_drop_queries_or_agent_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    from repopilot.evaluation import retrieval

    def fail(*args, **kwargs):
        raise RuntimeError("private detail")

    monkeypatch.setattr(retrieval.SearchTool, "execute", fail)
    report = paired.run_comparison(tmp_path / "run", cases=load_builtin_cases()[1:2])
    assert len(report["retrieval"]) == 16
    assert report["summary"]["off"]["hit_at_5"] == 0
    assert report["summary"]["off"]["retrieval_failures"] == 8
    assert all(a["state"] != "SETUP_ERROR" for a in report["attempts"])


def test_hidden_tests_require_completed_assertions(tmp_path: Path) -> None:
    from repopilot.agent.actions import AgentAction

    case = load_builtin_cases()[1]
    patch = (
        "diff --git a/calculator.py b/calculator.py\n"
        "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,4 @@\n"
        "+import os\n+os._exit(0)\n"
        " def safe_divide(numerator: float, denominator: float) -> float:\n"
        "     return numerator / denominator\n"
    )
    case = case.model_copy(
        update={
            "actions": (
                AgentAction.tool("apply_patch", {"patch": patch}, operation_id="exit-early"),
                AgentAction.final("done"),
            )
        }
    )
    report = paired.run_comparison(tmp_path / "run", cases=(case,))
    assert all(not a["resolved"] for a in report["attempts"])
    assert all(not a["hidden_tests_passed"] for a in report["attempts"])


def test_hidden_install_does_not_follow_candidate_links(tmp_path: Path) -> None:
    case = load_builtin_cases()[1]
    target = tmp_path / "candidate"
    target.mkdir()
    victim = tmp_path / "outside.py"
    victim.write_text("unchanged")
    name = next((case.root / "hidden").glob("*.py")).name
    (target / name).symlink_to(victim)
    with pytest.raises(FileExistsError):
        case.install_hidden_tests(target)
    assert victim.read_text() == "unchanged"


def test_online_pair_uses_real_http_and_distinct_tool_catalogs(tmp_path: Path) -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from repopilot.agent.providers import ProviderConfig

    case = load_builtin_cases()[1]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            patched = any(m["role"] == "tool" for m in payload["messages"])
            message = {"role": "assistant", "content": "done"}
            if not patched:
                message["tool_calls"] = [
                    {
                        "id": "patch",
                        "type": "function",
                        "function": {
                            "name": "apply_patch",
                            "arguments": json.dumps(case.actions[0].arguments),
                        },
                    }
                ]
            body = json.dumps(
                {
                    "choices": [
                        {"message": message, "finish_reason": "stop" if patched else "tool_calls"}
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = ProviderConfig(
            protocol="openai",
            model="local-contract-not-deepseek",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
        )
        report = paired.run_comparison(tmp_path / "run", cases=(case,), provider=provider)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert report["mode"] == "online-model"
    assert all(a["resolved"] for a in report["attempts"])
    assert all(a["rag_calls"] == 0 for a in report["attempts"])
    assert len(requests) == 4
    catalogs = [{t["function"]["name"] for t in r["tools"]} for r in requests]
    assert "rag_search" not in catalogs[0]
    assert catalogs[2] == catalogs[0] | {"rag_search"}
    assert requests[0]["model"] == requests[2]["model"]
    assert requests[0]["messages"][0] == requests[2]["messages"][0]


def test_notes_refuse_linked_report(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError

    monkeypatch.setattr(paired, "_attempt", fail)
    root = tmp_path / "run"
    report = paired.run_comparison(root, cases=load_builtin_cases()[1:2])
    target = tmp_path / "outside.md"
    target.write_text("unchanged")
    (root / "report.md").unlink()
    (root / "report.md").symlink_to(target)
    with pytest.raises(ValueError):
        paired.add_note(root, report["attempts"][0]["id"], "diagnosis", "checked")
    assert target.read_text() == "unchanged"


def test_cli_reports_and_notes(tmp_path: Path, monkeypatch) -> None:
    import json

    from typer.testing import CliRunner

    from repopilot.cli import app

    original = load_builtin_cases()[1:2]
    monkeypatch.setattr(paired, "load_builtin_cases", lambda: original)
    runner = CliRunner()
    root = tmp_path / "run"
    result = runner.invoke(app, ["eval-compare", "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    assert "scripted-contract" in result.output
    duplicate = runner.invoke(app, ["eval-compare", "--workspace", str(root)])
    assert duplicate.exit_code == 2
    note = runner.invoke(
        app,
        [
            "eval-note",
            "--workspace",
            str(root),
            "--attempt",
            "divide_zero-0-off",
            "--kind",
            "diagnosis",
            "--note",
            "reviewed",
        ],
    )
    assert note.exit_code == 0, note.output
    report = json.loads((root / "report.json").read_text())
    assert report["attempts"][0]["human_notes"][0]["note"] == "reviewed"
    bad = runner.invoke(
        app,
        [
            "eval-note",
            "--workspace",
            str(root),
            "--attempt",
            "unknown",
            "--kind",
            "diagnosis",
            "--note",
            "reviewed",
        ],
    )
    assert bad.exit_code == 2
