import type { ArtifactSummary, StreamStatus, TaskEvent, TaskSnapshot } from "./types";

const eventTypes = [
  "task.indexing",
  "task.planning",
  "task.executing",
  "task.testing",
  "task.reviewing",
  "task.succeeded",
  "task.failed",
  "task.verification_failed",
  "task.review_failed",
  "task.approval_requested",
  "task.approved",
  "tool.completed",
  "tool.failed",
  "tool.denied",
  "tool.approval_requested",
  "tool.replayed",
] as const;

async function readJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal });
  if (!response.ok) throw new Error(`Request failed with status ${response.status}`);
  return (await response.json()) as T;
}

export function fetchTasks(signal?: AbortSignal): Promise<TaskSnapshot[]> {
  return readJson<TaskSnapshot[]>("/v1/tasks", signal);
}

export function fetchArtifacts(taskId: string, signal?: AbortSignal): Promise<ArtifactSummary> {
  return readJson<ArtifactSummary>(`/v1/tasks/${encodeURIComponent(taskId)}/artifacts`, signal);
}

export function subscribeTaskEvents(
  taskId: string,
  onEvent: (event: TaskEvent) => void,
  onStatus: (status: StreamStatus) => void,
  initialSequence = 0,
): () => void {
  let cursor = initialSequence;
  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;

  const scheduleReconnect = () => {
    if (stopped || reconnectTimer !== null) return;
    onStatus("reconnecting");
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, 1_500);
  };

  const receive = (raw: Event) => {
    const event = JSON.parse((raw as MessageEvent<string>).data) as TaskEvent;
    if (event.sequence <= cursor) return;
    cursor = event.sequence;
    onEvent(event);
  };

  const connect = () => {
    if (stopped) return;
    onStatus(cursor === 0 ? "connecting" : "reconnecting");
    source = new EventSource(
      `/v1/tasks/${encodeURIComponent(taskId)}/events?after=${cursor}`,
    );
    source.onopen = () => onStatus("live");
    source.onerror = () => {
      source?.close();
      scheduleReconnect();
    };
    for (const eventType of eventTypes) source.addEventListener(eventType, receive);
    source.addEventListener("stream.end", () => {
      source?.close();
      scheduleReconnect();
    });
  };

  connect();
  return () => {
    stopped = true;
    source?.close();
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    onStatus("closed");
  };
}
