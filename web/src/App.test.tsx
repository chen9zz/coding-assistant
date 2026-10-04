import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly listeners = new Map<string, Array<(event: MessageEvent) => void>>();
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, callback: (event: MessageEvent) => void): void {
    const callbacks = this.listeners.get(type) ?? [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }

  close(): void {}

  emit(type: string, data: object): void {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const callback of this.listeners.get(type) ?? []) callback(event);
  }
}

const task = {
  task_id: "task-recovery",
  spec: {
    repository: "/workspace/demo",
    issue: "Fix configuration validation",
    test_command: ["python", "-m", "pytest"],
    constraints: [],
    allowed_paths: ["config.py"],
    max_changed_files: 1,
    max_steps: 24,
    timeout_seconds: 300,
  },
  state: "SUCCEEDED",
  sequence: 12,
  worktree: "/workspace/worktrees/task-recovery",
  plan: null,
  created_at: "2026-08-24T00:00:00Z",
  updated_at: "2026-08-24T00:01:00Z",
};

describe("RepoPilot dashboard", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const body = url.endsWith("/artifacts")
          ? {
              task_id: task.task_id,
              state: "SUCCEEDED",
              event_count: 12,
              worktree: task.worktree,
              patch: {
                kind: "patch",
                path: "/workspace/artifacts/change.patch",
                sha256: "a1b2c3d4e5f60000000000000000000000000000000000000000000000000000",
                metadata: {
                  changed_paths: ["config.py"],
                  sensitivity: "access-controlled-exact",
                },
              },
              test_report: {
                kind: "test_report",
                path: "/workspace/artifacts/test-report.json",
                sha256: "b".repeat(64),
                metadata: { status: "SUCCEEDED" },
              },
              trajectory: {
                kind: "trajectory",
                path: "/workspace/artifacts/trajectory.jsonl",
                sha256: "c".repeat(64),
                metadata: { event_count: 12 },
              },
              pr_draft: {
                kind: "pr_draft",
                path: "/workspace/artifacts/pull-request.md",
                sha256: "d".repeat(64),
                metadata: {},
              },
            }
          : [task];
        return new Response(JSON.stringify(body), { status: 200 });
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  it("renders a failed tool event and recovery evidence", async () => {
    render(<App />);
    expect(
      await screen.findByRole("heading", { name: "Fix configuration validation" }),
    ).toBeInTheDocument();

    const stream = MockEventSource.instances[0];
    act(() => {
      stream.emit("tool.failed", {
        event_id: "evt-1",
        task_id: task.task_id,
        sequence: 8,
        event_type: "tool.failed",
        from_state: "EXECUTING",
        to_state: "EXECUTING",
        operation_id: "verify-1",
        payload: { tool_name: "run_command", status: "FAILED", duration_ms: 17 },
        timestamp: "2026-08-24T00:00:30Z",
        schema_version: 1,
      });
      stream.emit("tool.replayed", {
        event_id: "evt-2",
        task_id: task.task_id,
        sequence: 9,
        event_type: "tool.replayed",
        from_state: "EXECUTING",
        to_state: "EXECUTING",
        operation_id: "verify-1",
        payload: { tool_name: "run_command", status: "SUCCEEDED", duration_ms: 0 },
        timestamp: "2026-08-24T00:00:40Z",
        schema_version: 1,
      });
    });

    expect(screen.getByText("Recovery verified")).toBeInTheDocument();
    expect(screen.getByText("run_command · failed")).toBeInTheDocument();
    expect(screen.getByText("1 recovery")).toBeInTheDocument();
    expect(await screen.findByText("a1b2c3d4e5f6")).toBeInTheDocument();
    expect(screen.getByText("config.py")).toBeInTheDocument();
  });

  it("shows a useful empty state", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify([]), { status: 200 }),
    );
    render(<App />);
    expect(await screen.findByText("No agent runs yet")).toBeInTheDocument();
  });
});
