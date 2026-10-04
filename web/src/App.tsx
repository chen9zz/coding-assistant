import { useEffect, useMemo, useState } from "react";

import { fetchArtifacts, fetchTasks, subscribeTaskEvents } from "./api";
import type {
  ArtifactSummary,
  StreamStatus,
  TaskEvent,
  TaskSnapshot,
  TaskState,
} from "./types";
import "./styles.css";

const stages: TaskState[] = [
  "CREATED",
  "INDEXING",
  "PLANNING",
  "EXECUTING",
  "TESTING",
  "REVIEWING",
  "SUCCEEDED",
];

function App() {
  const [tasks, setTasks] = useState<TaskSnapshot[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactSummary | null>(null);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("closed");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchTasks(controller.signal)
      .then((loaded) => {
        setTasks(loaded);
        setSelectedId((current) => current ?? loaded[0]?.task_id ?? null);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Unable to load task evidence");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  const selected = tasks.find((task) => task.task_id === selectedId) ?? null;

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setEvents([]);
    setArtifacts(null);
    void fetchArtifacts(selectedId, controller.signal)
      .then(setArtifacts)
      .catch(() => undefined);
    const unsubscribe = subscribeTaskEvents(
      selectedId,
      (event) => {
        setEvents((current) =>
          [...current.filter((item) => item.sequence !== event.sequence), event].sort(
            (left, right) => left.sequence - right.sequence,
          ),
        );
        if (event.event_type.startsWith("task.")) {
          void fetchTasks().then(setTasks).catch(() => undefined);
        }
        if (event.event_type === "task.succeeded") {
          void fetchArtifacts(selectedId).then(setArtifacts).catch(() => undefined);
        }
      },
      setStreamStatus,
    );
    return () => {
      controller.abort();
      unsubscribe();
    };
  }, [selectedId]);

  const recoveries = useMemo(
    () => events.filter((event) => event.event_type === "tool.replayed").length,
    [events],
  );
  const denials = useMemo(
    () => events.filter((event) => event.event_type === "tool.denied").length,
    [events],
  );

  if (loading) return <CenteredState title="Loading durable runs…" detail="Reading local evidence" />;
  if (error) return <CenteredState title="Dashboard unavailable" detail={error} tone="error" />;
  if (tasks.length === 0) {
    return (
      <CenteredState
        title="No agent runs yet"
        detail="Run the offline demo, then open this page to inspect its evidence trail."
      />
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">R</span>
          <div>
            <p className="eyebrow">Reliable software agent</p>
            <h1>RepoPilot-R</h1>
          </div>
        </div>
        <div className={`stream-status stream-${streamStatus}`} role="status">
          <span aria-hidden="true" /> {streamLabel(streamStatus)}
        </div>
      </header>

      <div className="dashboard-grid">
        <aside className="run-panel" aria-label="Agent runs">
          <div className="panel-heading">
            <p className="eyebrow">Runs</p>
            <strong>{tasks.length.toString().padStart(2, "0")}</strong>
          </div>
          <nav>
            {tasks.map((task) => (
              <button
                className={`run-card ${task.task_id === selectedId ? "selected" : ""}`}
                key={task.task_id}
                onClick={() => setSelectedId(task.task_id)}
                type="button"
              >
                <span className={`state-dot state-${task.state.toLowerCase()}`} />
                <span>
                  <b>{task.spec.issue}</b>
                  <small>{shortRepository(task.spec.repository)}</small>
                </span>
                <time>{formatTime(task.updated_at)}</time>
              </button>
            ))}
          </nav>
          <div className="privacy-note">
            <span aria-hidden="true">◈</span>
            <p><b>Safe by default</b>No prompt, secret, or raw file content is rendered.</p>
          </div>
        </aside>

        {selected && (
          <main>
            <section className="hero-card">
              <div>
                <p className="eyebrow">Task {selected.task_id.slice(0, 12)}</p>
                <h2>{selected.spec.issue}</h2>
                <p className="repository-path">{selected.spec.repository}</p>
              </div>
              <StateBadge state={selected.state} />
              <Progress state={selected.state} />
            </section>

            <section className="metric-grid" aria-label="Reliability metrics">
              <Metric label="Durable events" value={artifacts?.event_count ?? selected.sequence} note="SQLite-backed" />
              <Metric label="Recovery" value={recoveries} note={recoveries ? "Idempotent replay" : "No replay needed"} />
              <Metric label="Policy denials" value={denials} note="Fail-closed gateway" />
              <Metric label="Verification" value={selected.state === "SUCCEEDED" ? "PASS" : "—"} note={selected.spec.test_command.join(" ")} />
            </section>

            {selected.state === "SUCCEEDED" && recoveries > 0 && (
              <section className="recovery-banner">
                <span className="recovery-icon" aria-hidden="true">✓</span>
                <div><h3>Recovery verified</h3><p>A committed operation was replayed without duplicating its side effect.</p></div>
                <strong>{recoveries} {recoveries === 1 ? "recovery" : "recoveries"}</strong>
              </section>
            )}

            <section className="evidence-grid">
              <article className="timeline-card">
                <div className="section-heading">
                  <div><p className="eyebrow">Trajectory</p><h3>Event timeline</h3></div>
                  <span>{events.length} streamed</span>
                </div>
                {events.length === 0 ? (
                  <p className="quiet-state">Waiting for replayable task events…</p>
                ) : (
                  <ol className="timeline">
                    {[...events].reverse().map((event) => (
                      <li key={event.event_id}>
                        <span className={`timeline-marker event-${eventTone(event)}`} />
                        <div><b>{eventLabel(event)}</b><small>{event.event_type} · seq {event.sequence}</small></div>
                        <time>{formatTime(event.timestamp)}</time>
                      </li>
                    ))}
                  </ol>
                )}
              </article>

              <aside className="evidence-card">
                <div className="section-heading">
                  <div><p className="eyebrow">Evidence bundle</p><h3>Release gate</h3></div>
                </div>
                <EvidenceRow label="Patch review" state={selected.state === "SUCCEEDED" ? "Verified" : "Pending"} />
                <EvidenceRow
                  label="Patch SHA-256"
                  state={artifacts ? artifacts.patch.sha256.slice(0, 12) : "Pending"}
                />
                <EvidenceRow
                  label="Test evidence"
                  state={artifactStatus(artifacts, selected.state)}
                />
                <EvidenceRow label="Secret redaction" state="Enforced" />
                <EvidenceRow label="Worktree isolation" state={selected.worktree ? "Active" : "Pending"} />
                {changedPaths(artifacts).length > 0 && (
                  <div className="changed-paths">
                    <span>Changed paths</span>
                    {changedPaths(artifacts).map((path) => <code key={path}>{path}</code>)}
                  </div>
                )}
                <div className="command-block"><span>$</span><code>{selected.spec.test_command.join(" ")}</code></div>
              </aside>
            </section>
          </main>
        )}
      </div>
    </div>
  );
}

function CenteredState({ title, detail, tone = "normal" }: { title: string; detail: string; tone?: "normal" | "error" }) {
  return <main className={`centered-state ${tone}`}><span className="brand-mark">R</span><h1>{title}</h1><p>{detail}</p></main>;
}

function StateBadge({ state }: { state: TaskState }) {
  return <span className={`state-badge state-${state.toLowerCase()}`}><i />{state.toLowerCase().replace("_", " ")}</span>;
}

function Progress({ state }: { state: TaskState }) {
  const current = state === "FAILED" || state === "CANCELLED" ? stages.indexOf("EXECUTING") : stages.indexOf(state);
  return <ol className="stage-progress">{stages.map((stage, index) => <li className={index <= current ? "complete" : ""} key={stage}><span>{index < current ? "✓" : index + 1}</span><small>{stage.toLowerCase()}</small></li>)}</ol>;
}

function Metric({ label, value, note }: { label: string; value: string | number; note: string }) {
  return <article className="metric-card"><p>{label}</p><strong>{value}</strong><small>{note}</small></article>;
}

function EvidenceRow({ label, state }: { label: string; state: string }) {
  return <div className="evidence-row"><span>{label}</span><b><i />{state}</b></div>;
}

function artifactStatus(
  artifacts: ArtifactSummary | null,
  state: TaskState,
): string {
  const status = artifacts?.test_report.metadata.status;
  return typeof status === "string" ? status : state === "SUCCEEDED" ? "Passed" : "Pending";
}

function changedPaths(artifacts: ArtifactSummary | null): string[] {
  const paths = artifacts?.patch.metadata.changed_paths;
  return Array.isArray(paths) ? paths.filter((path): path is string => typeof path === "string") : [];
}

function eventLabel(event: TaskEvent): string {
  const toolName = event.payload.tool_name;
  const status = event.payload.status;
  if (typeof toolName === "string") {
    const result = typeof status === "string" ? status.toLowerCase() : event.event_type.split(".")[1];
    return `${toolName} · ${result}`;
  }
  return event.event_type.replace("task.", "").replaceAll("_", " ");
}

function eventTone(event: TaskEvent): string {
  if (event.event_type.includes("failed") || event.event_type === "tool.denied") return "danger";
  if (event.event_type === "tool.replayed") return "recovery";
  return "success";
}

function streamLabel(status: StreamStatus): string {
  if (status === "live") return "Evidence stream live";
  if (status === "reconnecting") return "Replaying from cursor";
  if (status === "connecting") return "Connecting evidence";
  return "Evidence stream closed";
}

function shortRepository(path: string): string {
  return path.split("/").filter(Boolean).slice(-2).join("/");
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

export default App;
