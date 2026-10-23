import { useEffect, useState } from "react";
import { subscribeTaskEvents } from "./api";
import { HttpError, request, writeJob } from "./control";
import type { Control, Job } from "./control";
import type { TaskEvent } from "./types";

const artifactNames = { patch: "Patch", test_report: "Test report", trajectory: "Trajectory", pr_draft: "PR draft" };
type ArtifactKind = keyof typeof artifactNames;

export default function JobDetail({ job, control, onUpdate }: { job: Job; control: Control; onUpdate: (job: Job) => void }) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [artifact, setArtifact] = useState<ArtifactKind | null>(null);
  const [content, setContent] = useState<{ content: string; truncated: boolean } | null>(null);
  const [artifactError, setArtifactError] = useState<string | null>(null);
  const [artifactRefresh, setArtifactRefresh] = useState(0);
  const state = job.state.toUpperCase();

  useEffect(() => {
    setEvents([]);
    if (!job.task_id) return;
    return subscribeTaskEvents(job.task_id, (event) => setEvents((current) => [...current.filter((item) => item.sequence !== event.sequence), event].sort((a, b) => a.sequence - b.sequence)), () => {});
  }, [job.task_id]);

  useEffect(() => {
    if (!artifact || !job.task_id) return;
    const controller = new AbortController();
    setContent(null);
    setArtifactError(null);
    void request<{ content: string; truncated: boolean }>(`/v1/tasks/${encodeURIComponent(job.task_id)}/artifacts/${artifact}/content`, { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setContent(result); })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setArtifactError(reason instanceof HttpError && reason.status === 404 ? "Artifact is not available yet. Select it again after the task completes." : errorMessage(reason));
      });
    return () => controller.abort();
  }, [artifact, job.task_id, job.updated_at, artifactRefresh]);

  const outstanding = new Map<string, TaskEvent>();
  for (const event of events) {
    const operationId = event.operation_id ?? (typeof event.payload.operation_id === "string" ? event.payload.operation_id : null);
    if (!operationId) continue;
    if (event.event_type.endsWith("approval_requested")) outstanding.set(operationId, { ...event, payload: { ...outstanding.get(operationId)?.payload, ...event.payload } });
    else if (["task.approved", "tool.completed", "tool.failed", "tool.denied", "tool.replayed"].includes(event.event_type)) outstanding.delete(operationId);
  }

  async function actOnJob(action: "cancel" | "resume" | "approve", operationId?: string) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try { onUpdate(await writeJob(`/v1/jobs/${encodeURIComponent(job.job_id)}/${action}`, control.csrf_token, operationId ? { operation_id: operationId } : {})); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  return <section className="job-detail" aria-label="Selected job">
    <div className="job-detail-heading"><div><p className="eyebrow">{job.kind} · {job.job_id}</p><h3>{job.issue}</h3></div><span className={`job-state ${state.toLowerCase()}`}>{state.toLowerCase().replaceAll("_", " ")}</span></div>
    <div className="job-meta"><span>Updated {new Date(job.updated_at).toLocaleString()}</span>{job.parent_job_id && <span>Parent: {job.parent_job_id}</span>}{job.task_id && <span>Task: {job.task_id}</span>}</div>
    {job.summary ? <pre className="result-content">{job.summary}</pre> : <p className="composer-hint">{state === "QUEUED" ? "Queued for a background worker." : state === "RUNNING" ? "Worker is active. You can leave this page and return to the result." : "No result summary is available yet."}</p>}
    {job.error && <p role="alert" className="notice error">{job.error}</p>}
    {job.cancel_requested && <p role="status" className="notice">Cancellation requested. The worker will stop at a safe boundary.</p>}
    <div className="job-actions">
      {["QUEUED", "RUNNING", "WAITING_APPROVAL"].includes(state) && <button type="button" className="secondary-button" disabled={busy || job.cancel_requested} onClick={() => void actOnJob("cancel")}>Cancel job</button>}
      {job.kind === "task" && state === "INTERRUPTED" && <button type="button" className="primary-button" disabled={busy} onClick={() => void actOnJob("resume")}>Resume job</button>}
      {busy && <span role="status">Sending request…</span>}
    </div>
    {state === "WAITING_APPROVAL" && <section className="approval-panel"><h4>Approval required</h4><p>Review the requested operation before allowing the worker to continue.</p>{outstanding.size === 0 ? <p>Waiting for the exact operation request from the event stream…</p> : [...outstanding].map(([id, event]) => <div className="operation" key={id}><code>{id}</code><pre>{JSON.stringify(event.payload, null, 2)}</pre><button className="primary-button" type="button" disabled={busy} onClick={() => void actOnJob("approve", id)}>Approve operation {id}</button></div>)}</section>}
    {error && <p className="notice error" role="alert">{error}</p>}
    {job.task_id && <section className="artifact-preview"><h4>Task artifacts</h4><div className="artifact-tabs">{(Object.keys(artifactNames) as ArtifactKind[]).map((key) => <button type="button" className={key === artifact ? "active" : ""} aria-pressed={key === artifact} key={key} onClick={() => { setArtifact(key); setArtifactRefresh((value) => value + 1); }}>{artifactNames[key]}</button>)}</div>
      {!artifact ? <p className="composer-hint">Select an artifact to inspect its redacted content.</p> : artifactError ? <p className="notice" role="status">{artifactError}</p> : !content ? <p role="status">Loading artifact…</p> : <><pre className="result-content">{content.content || "This artifact is empty."}</pre>{content.truncated && <p className="notice">Preview truncated to the server limit. The full artifact remains on the local server.</p>}</>}
    </section>}
  </section>;
}

function errorMessage(reason: unknown): string { return reason instanceof Error ? reason.message : "The request could not be completed."; }
