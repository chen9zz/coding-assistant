import { useEffect, useState } from "react";
import { HttpError, request, writeJob } from "./control";
import type { Control, Job } from "./control";
import JobDetail from "./JobDetail";

export default function Workbench() {
  const [control, setControl] = useState<Control | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [issue, setIssue] = useState("");
  const [kind, setKind] = useState<"task" | "analysis">("task");
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  const selected = jobs.find((job) => job.job_id === selectedId) ?? null;

  useEffect(() => {
    const controller = new AbortController();
    setConnectionError(null);
    void request<Control>("/v1/control", { signal: controller.signal }).then(setControl).catch((reason: unknown) => {
      if (!controller.signal.aborted) setConnectionError(reason instanceof HttpError && reason.status === 404
        ? "Task controls unavailable on this server. Audit evidence remains available below. Start a ForgePilot control server to submit jobs."
        : message(reason));
    });
    return () => controller.abort();
  }, [retry]);

  useEffect(() => {
    if (!control) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const loaded = await request<Job[]>("/v1/jobs", { signal: controller.signal });
        if (controller.signal.aborted) return;
        setJobs(loaded);
        setSelectedId((current) => current ?? loaded[0]?.job_id ?? null);
        setConnectionError(null);
      } catch (reason) {
        if (!controller.signal.aborted) setConnectionError(message(reason));
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [control]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!control || busy || !issue.trim()) return;
    setBusy(true);
    setActionError(null);
    try {
      const job = await writeJob("/v1/jobs", control.csrf_token, { issue: issue.trim(), kind });
      setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)]);
      setSelectedId(job.job_id);
      setIssue("");
    } catch (reason) { setActionError(message(reason)); }
    finally { setBusy(false); }
  }

  return <section className="workbench" aria-label="Task workbench">
    <div className="workbench-heading"><div><p className="eyebrow">Local agent workspace</p><h2>What shall we work on?</h2></div>
      <span className={`mode-badge ${control?.mode ?? "disconnected"}`}>{control ? control.mode === "demo" ? "Demo · offline" : "Online · live model" : connectionError ? "Disconnected" : "Connecting…"}</span>
    </div>
    {control && <dl className="runtime-config"><div><dt>Repository</dt><dd>{control.repository}</dd></div><div><dt>Verification</dt><dd>{control.test_command.join(" ")}</dd></div><div><dt>Model / storage</dt><dd>{control.model} · {control.storage}</dd></div></dl>}
    {connectionError && <div className="notice error" role="alert">{connectionError} <button className="text-button" onClick={() => setRetry((value) => value + 1)}>Reconnect</button></div>}
    <div className="workbench-grid">
      <form onSubmit={submit} className="composer">
        <label htmlFor="task-prompt">Task prompt</label>
        {control?.mode === "demo" && <p className="notice">Offline fixture: runs a fixed scripted example. Your prompt does not drive a live model.</p>}
        <textarea id="task-prompt" value={issue} onChange={(event) => setIssue(event.target.value)} placeholder="Describe the change, expected behavior, and any constraints…" rows={5} maxLength={20000} />
        <fieldset className="kind-options"><legend className="sr-only">Job type</legend><label><input type="radio" name="job-kind" checked={kind === "task"} onChange={() => setKind("task")} />Implementation task</label><label><input type="radio" name="job-kind" checked={kind === "analysis"} onChange={() => setKind("analysis")} />Read-only analysis</label></fieldset>
        <p className="composer-hint">{kind === "analysis" ? "An independent worker reads the repository and returns findings." : "A background worker implements and verifies changes in an isolated worktree."}</p>
        <div className="composer-footer"><span>Model keys stay on the server.</span><button className="primary-button" type="submit" disabled={!control || !issue.trim() || busy}>{busy ? "Submitting…" : kind === "task" ? "Start task" : "Start analysis"}</button></div>
        {actionError && <p className="notice error" role="alert">{actionError}</p>}
      </form>
      <section className="jobs-panel" aria-label="Background jobs"><div className="panel-heading"><p className="eyebrow">Background jobs</p><span>{jobs.length}</span></div>
        {jobs.length === 0 ? <div className="jobs-empty"><span aria-hidden="true">↗</span><h3>Your next task starts here</h3><p>Submit a task or read-only analysis. Its progress and results will appear here.</p></div> : <nav aria-label="Select job">{jobs.map((job) => <button key={job.job_id} type="button" className={`job-card ${selectedId === job.job_id ? "selected" : ""}`} onClick={() => setSelectedId(job.job_id)}><b>{job.issue}</b><span>{job.kind === "analysis" ? "Analysis" : "Task"} · {job.state.toLowerCase().replaceAll("_", " ")}{job.parent_job_id ? " · sub-agent" : ""}</span></button>)}</nav>}
      </section>
    </div>
    {selected && control && <JobDetail key={selected.job_id} job={selected} control={control} onUpdate={(updated) => setJobs((current) => current.map((item) => item.job_id === updated.job_id ? updated : item))} />}
  </section>;
}

function message(reason: unknown): string { return reason instanceof Error ? reason.message : "Unable to contact the local server. Try reconnecting."; }
