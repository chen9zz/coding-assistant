"""Real subprocess + HTTP transport; model replies are local contract fixtures, not an LLM."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from repopilot.application.jobs import JobManager
from repopilot.evaluation.cases import load_builtin_cases


def wait_job(manager, job_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["state"] not in {"QUEUED", "RUNNING"}:
            return job
        time.sleep(0.05)
    raise AssertionError(manager.get(job_id))


@pytest.mark.parametrize("mode", ["delegate", "approve", "cancel"])
def test_http_worker_delegation_and_approval(tmp_path: Path, mode: str) -> None:
    case = load_builtin_cases()[0]
    source = case.prepare_repository(tmp_path / "source")
    counters = {"main": 0, "child": 0}
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(payload)
            names = {t["function"]["name"] for t in payload["tools"]}
            kind = "main" if "agent_spawn" in names else "child"
            index = counters[kind]
            counters[kind] += 1
            if kind == "child":
                assert not names & {"apply_patch", "run_command", "agent_spawn"}
                replies = [("read_file", {"path": "config.py"}), (None, {})]
            else:
                first = (
                    ("agent_spawn", {"issue": "Explain parse_port"})
                    if mode == "delegate"
                    else (
                        "run_command",
                        {"argv": [sys.executable, "-m", "compileall", "-q", "config.py"]},
                    )
                )
                replies = [first, ("apply_patch", case.actions[0].arguments), (None, {})]
            name, args = replies[index]
            message = {"role": "assistant", "content": "Verified analysis"}
            if name:
                message.update(
                    content=None,
                    tool_calls=[
                        {
                            "id": f"{kind}-{index}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                )
            body = json.dumps(
                {
                    "choices": [
                        {"message": message, "finish_reason": "tool_calls" if name else "stop"}
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[provider]\nprotocol="openai"\nmodel="fixture"\n'
        f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
    )
    manager = JobManager(
        tmp_path / "state",
        repository=source,
        config=config,
        test_command=case.task_spec(source).test_command,
    )
    try:
        job = manager.submit("Fix parse_port; delegate read-only analysis when available")
        done = wait_job(manager, job["job_id"])
        if mode in {"approve", "cancel"}:
            assert done["state"] == "WAITING_APPROVAL", done
            if mode == "cancel":
                assert manager.cancel(job["job_id"])["state"] == "CANCELLED"
                with manager.container() as container:
                    assert container.service.get(job["task_id"]).state.value == "CANCELLED"
                return
            with pytest.raises(ValueError):
                manager.resume(job["job_id"])
            with manager.container() as container:
                pending = container.service.events(job["task_id"])[-1]
            manager.approve(job["job_id"], str(pending.payload["operation_id"]))
            done = wait_job(manager, job["job_id"])
        assert done["state"] == "SUCCEEDED", done
        assert done["summary"] == "Verified analysis"
        assert (manager.root / "artifacts" / job["task_id"] / "change.patch").exists()
        if mode == "delegate":
            children = [j for j in manager.list() if j["parent_job_id"] == job["job_id"]]
            assert len(children) == 1
            child = wait_job(manager, children[0]["job_id"])
            assert child["state"] == "SUCCEEDED", child
            assert child["task_id"] is None
            assert counters["child"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
