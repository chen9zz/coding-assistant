export type TaskState =
  | "CREATED"
  | "INDEXING"
  | "PLANNING"
  | "EXECUTING"
  | "TESTING"
  | "REVIEWING"
  | "RETRYING"
  | "WAITING_APPROVAL"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface TaskSpec {
  repository: string;
  issue: string;
  test_command: string[];
  constraints: string[];
  allowed_paths: string[];
  max_changed_files: number | null;
  max_steps: number;
  timeout_seconds: number;
}

export interface TaskSnapshot {
  task_id: string;
  spec: TaskSpec;
  state: TaskState;
  sequence: number;
  worktree: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskEvent {
  event_id: string;
  task_id: string;
  sequence: number;
  event_type: string;
  from_state: TaskState;
  to_state: TaskState;
  operation_id: string | null;
  payload: Record<string, JsonValue>;
  timestamp: string;
  schema_version: number;
}

export interface ArtifactSummary {
  task_id: string;
  state: TaskState;
  worktree: string | null;
  event_count: number;
  root: string;
  patch: ArtifactDescriptor;
  test_report: ArtifactDescriptor;
  trajectory: ArtifactDescriptor;
  pr_draft: ArtifactDescriptor;
}

export interface ArtifactDescriptor {
  kind: string;
  path: string;
  sha256: string;
  metadata: Record<string, JsonValue>;
}

export type StreamStatus = "connecting" | "live" | "reconnecting" | "closed";
