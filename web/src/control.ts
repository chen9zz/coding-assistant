export interface Control {
  name: string;
  mode: "demo" | "online";
  repository: string;
  test_command: string[];
  model: string;
  storage: string;
  csrf_token: string;
}

export interface Job {
  job_id: string;
  task_id: string | null;
  kind: "task" | "analysis";
  state: string;
  parent_job_id: string | null;
  issue: string;
  summary: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  cancel_requested?: boolean;
}

export class HttpError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: unknown };
    throw new HttpError(response.status, typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function writeJob(path: string, token: string, body: object = {}): Promise<Job> {
  return request<Job>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-ForgePilot-Token": token },
    body: JSON.stringify(body),
  });
}
