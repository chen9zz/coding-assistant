import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import Workbench from "./Workbench";
import type { Job } from "./control";

class EventStream {
  static latest: EventStream;
  callbacks = new Map<string, (event: MessageEvent) => void>();
  constructor() { EventStream.latest = this; }
  addEventListener(type: string, callback: (event: MessageEvent) => void) { this.callbacks.set(type, callback); }
  close() {}
  emit(type: string, operationId: string, sequence: number, extra: Record<string, unknown> = {}) {
    const payload = { ...(type.startsWith("task.") ? { operation_id: operationId } : { operation_id: operationId, tool_name: "run_command", reason: "Verify tests" }), ...extra };
    this.callbacks.get(type)?.(new MessageEvent(type, { data: JSON.stringify({ event_id: `e${sequence}`, task_id: "task-1", event_type: type, operation_id: type.startsWith("task.") ? null : operationId, sequence, from_state: "EXECUTING", to_state: "WAITING_APPROVAL", payload, timestamp: "2026-10-22T00:00:00Z", schema_version: 1 }) }));
  }
}

let job: Job;
let writes: Array<{ path: string; body: object; token: string | null }>;
beforeEach(() => {
  job = { job_id: "job-1", task_id: "task-1", kind: "task", state: "RUNNING", parent_job_id: null, issue: "Fix validation", summary: null, error: null, created_at: "2026-10-22T00:00:00Z", updated_at: "2026-10-22T00:00:00Z" };
  writes = [];
  vi.stubGlobal("EventSource", EventStream);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const path = String(input);
    if (options?.method === "POST") {
      writes.push({ path, body: JSON.parse(String(options.body)), token: new Headers(options.headers).get("X-ForgePilot-Token") });
      if (path.endsWith("/cancel")) job = { ...job, cancel_requested: true };
      else job = { ...job, state: "RUNNING" };
      return Response.json(job);
    }
    if (path === "/v1/control") return Response.json({ name: "ForgePilot", mode: "online", repository: "/repo", test_command: ["pytest"], model: "gpt", storage: "sqlite", csrf_token: "csrf-exact" });
    if (path === "/v1/jobs") return Response.json([job]);
    if (path === "/v1/jobs/job-1") return Response.json(job);
    if (path.endsWith("/patch/content")) return Response.json({ content: "-old\n+new", truncated: true });
    return new Response("{}", { status: 404 });
  }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("requests cooperative cancellation and keeps the returned status visible", async () => {
  render(<Workbench />);
  fireEvent.click(await screen.findByRole("button", { name: "Cancel job" }));
  expect(await screen.findByText(/Cancellation requested/)).toBeInTheDocument();
  expect(writes).toEqual([{ path: "/v1/jobs/job-1/cancel", body: {}, token: "csrf-exact" }]);
  expect(screen.getByRole("button", { name: "Cancel job" })).toBeDisabled();
});

it("only approves the outstanding exact operation from task events", async () => {
  job.state = "WAITING_APPROVAL";
  render(<Workbench />);
  await screen.findByRole("heading", { name: "Fix validation" });
  await waitFor(() => expect(EventStream.latest).toBeDefined());
  act(() => {
    EventStream.latest.emit("tool.approval_requested", "old-operation", 1);
    EventStream.latest.emit("task.approved", "old-operation", 2);
    EventStream.latest.emit("tool.approval_requested", "verify-exact-2", 3);
    EventStream.latest.emit("task.approval_requested", "verify-exact-2", 4);
  });
  expect(screen.getByText(/Verify tests/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve operation old-operation" })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "Approve operation verify-exact-2" }));
  await waitFor(() => expect(writes).toEqual([{ path: "/v1/jobs/job-1/approve", body: { operation_id: "verify-exact-2" }, token: "csrf-exact" }]));
  expect(screen.queryByRole("button", { name: "Approve operation verify-exact-2" })).not.toBeInTheDocument();
});

it("shows a plain-text skill diff before approving its exact write", async () => {
  job.state = "WAITING_APPROVAL";
  render(<Workbench />);
  await screen.findByRole("heading", { name: "Fix validation" });
  const preview = "Approve skill write: /managed/repair/SKILL.md\n-old\n+<script>not executable</script>\n";
  act(() => {
    EventStream.latest.emit("tool.approval_requested", "skill-1", 1, { tool_name: "skill_write", approval_preview: preview });
    EventStream.latest.emit("task.approval_requested", "skill-1", 2);
  });
  expect(screen.getByLabelText("Approval preview").textContent).toBe(preview);
  expect(writes).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "Approve operation skill-1" }));
  await waitFor(() => expect(writes).toEqual([{ path: "/v1/jobs/job-1/approve", body: { operation_id: "skill-1" }, token: "csrf-exact" }]));
});

it("resumes an interrupted task and renders artifact content with its truncation notice", async () => {
  job.state = "INTERRUPTED";
  render(<Workbench />);
  fireEvent.click(await screen.findByRole("button", { name: "Resume job" }));
  await waitFor(() => expect(writes[0]?.path).toBe("/v1/jobs/job-1/resume"));
  fireEvent.click(screen.getByRole("button", { name: "Patch" }));
  expect(await screen.findByText("-old +new")).toBeInTheDocument();
  expect(screen.getByText(/Preview truncated/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Test report" }));
  expect(await screen.findByText(/Artifact is not available yet/)).toBeInTheDocument();
  const originalFetch = fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, options?: RequestInit) => String(input).endsWith("/test_report/content") ? Promise.resolve(Response.json({ content: "Tests passed", truncated: false })) : originalFetch(input, options)));
  fireEvent.click(screen.getByRole("button", { name: "Test report" }));
  expect(await screen.findByText("Tests passed")).toBeInTheDocument();
});

it("polls an analysis result without exposing task artifact or resume controls", async () => {
  vi.useFakeTimers();
  job.kind = "analysis";
  job.task_id = null;
  render(<Workbench />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  job = { ...job, state: "SUCCEEDED", summary: "Validation is missing at the input boundary." };
  await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
  expect(screen.getByText("Validation is missing at the input boundary.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Patch" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Resume job" })).not.toBeInTheDocument();
});

it("preserves the draft and shows a server rejection instead of pretending submission succeeded", async () => {
  const originalFetch = fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, options?: RequestInit) => options?.method === "POST" ? Promise.resolve(Response.json({ detail: "Worker budget exhausted" }, { status: 409 })) : originalFetch(input, options)));
  render(<Workbench />);
  await screen.findByText("Online · live model");
  fireEvent.change(screen.getByRole("textbox", { name: "Task prompt" }), { target: { value: "Repair input" } });
  fireEvent.click(screen.getByRole("button", { name: "Start task" }));
  expect(await screen.findByText("Worker budget exhausted")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Task prompt" })).toHaveValue("Repair input");
});
