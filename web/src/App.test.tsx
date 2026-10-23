import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
        if (url === "/v1/control" || url === "/v1/jobs") return new Response("{}", { status: 404 });
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

  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

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
    vi.mocked(fetch).mockImplementation(async (input) => String(input) === "/v1/tasks" ? Response.json([]) : new Response("{}", { status: 404 }));
    render(<App />);
    expect(await screen.findByText("No agent runs yet")).toBeInTheDocument();
  });

  it("keeps the composer available without audit runs and submits analysis with CSRF protection", async () => {
    const requests: Array<{ url: string; options?: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input);
      requests.push({ url, options });
      if (url === "/v1/control") return Response.json({ name: "ForgePilot", mode: "demo", repository: "/workspace/demo", test_command: ["python", "-m", "pytest"], model: "offline", storage: "sqlite", csrf_token: "session-token" });
      if (options?.method === "POST") return Response.json({ job_id: "job-new", task_id: null, kind: "analysis", state: "queued", parent_job_id: null, issue: "Explain validation", summary: null, error: null, created_at: "2026-10-22T00:00:00Z", updated_at: "2026-10-22T00:00:00Z" });
      return Response.json([]);
    }));
    render(<App />);
    const input = await screen.findByRole("textbox", { name: "Task prompt" });
    expect(await screen.findByText("Demo · offline")).toBeInTheDocument();
    expect(screen.getByText(/runs a fixed scripted example/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start task" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "  Explain validation  " } });
    fireEvent.click(screen.getByRole("radio", { name: "Read-only analysis" }));
    fireEvent.click(screen.getByRole("button", { name: "Start analysis" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Explain validation" })).toBeInTheDocument());
    const request = requests.find((entry) => entry.options?.method === "POST");
    expect(request?.url).toBe("/v1/jobs");
    expect(request?.options?.headers).toMatchObject({ "X-ForgePilot-Token": "session-token" });
    expect(JSON.parse(String(request?.options?.body))).toEqual({ issue: "Explain validation", kind: "analysis" });
    expect(input).toHaveValue("");
  });

  it("keeps the audit view usable when an older server has no control API", async () => {
    render(<App />);
    expect(await screen.findByText(/Task controls unavailable/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Fix configuration validation" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start task" })).toBeDisabled();
  });

  it("discovers audit tasks created after the initially empty page loads", async () => {
    vi.useFakeTimers();
    let available = false;
    vi.stubGlobal("fetch", vi.fn(async (input) => String(input) === "/v1/tasks" ? Response.json(available ? [task] : []) : new Response("{}", { status: 404 })));
    render(<App />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("No agent runs yet")).toBeInTheDocument();
    available = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(screen.getByRole("heading", { name: "Fix configuration validation" })).toBeInTheDocument();
  });

  it("does not claim a SQLite store when the configured backend is MySQL", async () => {
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, options?: RequestInit) => String(input) === "/v1/control" ? Promise.resolve(Response.json({ name: "ForgePilot", mode: "online", repository: "/workspace/demo", test_command: ["pytest"], model: "gpt", storage: "mysql", csrf_token: "token" })) : originalFetch(input, options)));
    render(<App />);
    await screen.findByText("gpt · mysql");
    expect(await screen.findByText("Durable event store")).toBeInTheDocument();
    expect(screen.queryByText("SQLite-backed")).not.toBeInTheDocument();
    expect(screen.queryByText(/runs a fixed scripted example/)).not.toBeInTheDocument();
  });
});
