# RepoPilot-R: Reliable Software Engineering Agent — Design Specification

- **Status:** Approved
- **Date:** 2026-08-23 (NZST)
- **Approval basis:** The user approved the recommended route and delegated the remaining design decisions to the recommended defaults without further confirmation.
- **Final delivery root:** `/Users/chen/Agent study/project`

## 1. Product definition

RepoPilot-R is an issue-to-verified-patch software engineering agent. Given a local Git repository and an issue description, it maps the repository, forms a structured plan, edits only an isolated Git worktree, runs bounded verification commands, reviews the resulting diff, and produces a patch, test evidence, an audit trajectory, metrics, and a pull-request draft.

The product demonstrates a distinction that matters in interviews and production systems: a model proposing code is not success. Success requires a controlled execution boundary, deterministic lifecycle, reproducible evidence, and honest evaluation.

### 1.1 Primary user journey

1. A developer submits a repository path, issue text, test command, and optional constraints through the CLI or REST API.
2. RepoPilot-R creates an immutable task record and an isolated Git worktree.
3. The repository mapper builds a compact symbol/file/test map.
4. The planner produces a validated, structured plan.
5. The runtime requests tools through the safe gateway; it never executes tools directly.
6. Every state transition and tool result is persisted before the next action.
7. RepoPilot-R runs tests, reviews the patch, and generates the final evidence bundle.
8. A human reviews the patch or PR draft. The first release never pushes or merges automatically.

### 1.2 Goals

- Deliver a complete, runnable portfolio product rather than a chat demo.
- Show understanding of the minimal agent loop and modern production SDK patterns.
- Make filesystem and command side effects safe, isolated, bounded, and auditable.
- Resume a task after an injected process failure without repeating committed tool effects.
- Provide deterministic offline demonstrations without an API key or Docker.
- Provide an optional real-model path using the OpenAI Agents SDK.
- Produce measurable quality, efficiency, safety, and recovery evidence.
- Expose business integration surfaces through FastAPI, SSE, CLI, and stable JSON schemas.

### 1.3 Non-goals

- No automatic Git push, pull-request creation, or merge in the default release.
- No unbounded arbitrary shell access.
- No multi-agent swarm or role-playing chat architecture.
- No invented SWE-bench score. The repository may include an adapter and instructions, but reports only results actually generated during verification.
- Docker, Temporal, PostgreSQL, Redis, and cloud deployment are not required for the local quickstart. They are documented production evolution points, not decorative runtime dependencies.
- The first benchmark focuses on small Python repositories so it can run quickly and deterministically. Repository mapping remains extensible to additional languages.

## 2. Success criteria and proof

| Requirement | Completion evidence |
|---|---|
| Offline run | One documented command completes an issue-to-patch fixture with no model key and no Docker |
| Safe isolation | Integration test proves the source checkout is unchanged while the task worktree contains the patch |
| Policy enforcement | Tests cover allow, ask, and deny decisions; path escape, shell metacharacters, and unapproved commands are rejected |
| Crash recovery | Failure-injection test stops after a committed tool call, resumes from SQLite, and proves the effect is not duplicated |
| Verification | A successful task includes test command, exit code, bounded output digest, patch digest, and review result |
| Observability | JSONL trajectory is always produced; optional OpenTelemetry spans share task and tool-call identifiers |
| Evaluation | Included fixture suite reports resolved rate, test-pass rate, policy violations, steps, retries, duration, and recovery success |
| Product interface | CLI and FastAPI use the same application service; SSE exposes persisted events |
| Frontend | React dashboard can list tasks and display state, events, diff, tests, and metrics from the API |
| Documentation | README, architecture, code walkthrough, evaluation, security, business integration, and resume guide are present and cross-linked |
| Reproducibility | Backend tests, static checks, frontend checks, and offline demo are run and their real outputs are recorded |

## 3. Architecture

RepoPilot-R uses a modular monolith with ports and adapters. The deterministic application core owns lifecycle and invariants. Models, persistence, telemetry, sandboxing, and interfaces are replaceable adapters.

```text
CLI / FastAPI / React
          |
          v
   TaskApplicationService
          |
          v
   DurableTaskOrchestrator ----> EventStore / CheckpointStore (SQLite)
      |       |       |
      |       |       +-------> TelemetryPort (JSONL + optional OTel)
      |       +---------------> AgentRuntimePort
      |                           |- Scripted offline runtime
      |                           |- Minimal tool-calling loop
      |                           `- OpenAI Agents SDK adapter
      +-----------------------> SafeToolGateway
                                  |- RepositoryMapper
                                  |- WorktreeManager
                                  |- Search / read / patch / test tools
                                  `- optional MCP adapter
```

### 3.1 Package boundaries

| Package | Responsibility | May depend on |
|---|---|---|
| `domain` | Immutable task, plan, event, policy, tool, and evaluation models; state transition rules | Python standard library, Pydantic |
| `application` | Use cases and durable orchestration | `domain` ports only |
| `agent` | Minimal loop, context budgeting, runtime adapters, structured prompts | `domain` and model SDK adapters |
| `tools` | Safe tool registry and implementations | `domain`, repository/sandbox adapters |
| `infrastructure` | SQLite, Git worktrees, subprocess runner, JSONL/OTel, MCP | ports defined by core packages |
| `api` | FastAPI routes and SSE serialization | `application` |
| `cli` | Local commands and human-readable output | `application` |
| `evaluation` | Fixture preparation, runs, graders, aggregate report | public application interfaces |
| `web` | React/TypeScript dashboard | REST/SSE contract only |

The dependency direction always points inward. Neither `domain` nor `application` imports FastAPI, OpenAI, SQLite, Git, or React.

## 4. State and data flow

### 4.1 Task states

```text
CREATED -> INDEXING -> PLANNING -> EXECUTING -> TESTING -> REVIEWING -> SUCCEEDED
                                |           |          |
                                +-----------+----------+-> RETRYING
                                +-----------------------> WAITING_APPROVAL
Any non-terminal state -------------------------------> FAILED / CANCELLED
```

State transitions are an explicit table. Illegal transitions raise a domain error before persistence. The model cannot directly set task state.

### 4.2 Event envelope

Every persisted event contains:

- monotonic sequence number per task;
- unique event and operation identifiers;
- task ID, state before, state after, and event type;
- timestamp and schema version;
- redacted structured payload;
- optional causation and correlation IDs;
- payload digest for audit and idempotency checks.

Events are appended in the same SQLite transaction that updates the task snapshot and idempotency record. SSE reads from persisted events, so reconnecting clients do not lose transitions.

### 4.3 Tool-call protocol

1. Runtime emits a structured `ToolRequest` with a unique operation ID.
2. Gateway validates schema and asks the policy engine for `allow`, `ask`, or `deny`.
3. `deny` becomes a recorded tool result; `ask` moves the task to `WAITING_APPROVAL`.
4. For `allow`, the gateway writes an intent event, executes the adapter, applies output budgets/redaction, then commits the result and checkpoint.
5. A repeated operation ID returns the stored result instead of executing the side effect again.
6. The runtime receives only the bounded, redacted result.

## 5. Agent runtimes

All runtimes implement the same `AgentRuntime` port and produce structured actions rather than executing effects.

### 5.1 Scripted offline runtime

The offline runtime replays fixture-specific structured decisions. Its purpose is deterministic verification of orchestration, isolation, policies, recovery, telemetry, API, and evaluation. Documentation labels it honestly; it is not presented as autonomous intelligence.

### 5.2 Minimal tool-calling loop

The educational baseline follows the `learn-claude-code` model:

- messages hold conversational state;
- the model emits a typed tool request;
- the host executes through the safe gateway;
- the tool result is paired with the request and returned to the model;
- the loop ends only on a structured final action or a bounded stop condition.

It adds production necessities: maximum steps, time/token budgets, context compaction, typed failures, bounded retries, cancellation, operation idempotency, and checkpoints.

### 5.3 OpenAI Agents SDK adapter

The online adapter uses the current Agents SDK for model execution and tracing integration while retaining RepoPilot-R's policy, worktree, durability, and evaluation boundaries. The adapter is an optional dependency and requires a user-supplied API key. Unit tests use a fake runner; no CI test spends model credits.

### 5.4 Planning and review

Planner, executor, and reviewer are controlled workflow stages, not independent conversational personalities:

- planner returns objectives, affected areas, ordered steps, verification commands, and risks;
- executor may only invoke registered tools;
- reviewer evaluates diff scope, test evidence, policy violations, and unresolved risks;
- deterministic checks override optimistic model claims.

## 6. Repository intelligence

The repository mapper produces a token-bounded repo map containing:

- tracked files and languages;
- symbols, imports, definitions, and line ranges where parsers are available;
- test files and likely source-to-test relationships;
- relevant snippets selected by issue keywords and symbol references;
- repository instructions and configured verification commands.

The standard implementation uses `git ls-files`, ripgrep, and the Python Tree-sitter packages for symbol extraction, with Python AST supplying deeper Python-specific import detail. If a parser cannot load for a language, the system degrades to lexical mapping and records that capability in the trajectory instead of silently pretending semantic indexing succeeded.

## 7. Isolation, policy, and security

### 7.1 Worktree isolation

- Each task receives a dedicated branch and Git worktree under a task-owned directory.
- Tools are bound to that resolved worktree root.
- The source checkout is never edited.
- Cleanup is explicit and refuses to discard uncommitted changes by default.
- Final artifacts include the exact diff and worktree location for human inspection.

### 7.2 Filesystem safety

- All paths are resolved and checked for containment after symlink resolution.
- Absolute paths, `..` escapes, NUL bytes, and paths outside the worktree are rejected.
- Reads and outputs have byte and line budgets.
- The patch tool performs `git apply --check` before mutation and rejects paths outside the worktree.

### 7.3 Command safety

- Commands are argument arrays and run without a shell.
- The policy allowlist is exact by executable and supported subcommand.
- Working directory, environment variables, timeout, and output size are bounded.
- Network-facing or destructive commands are denied by default.
- User-configured test commands are parsed and policy-checked; they are never interpolated into a shell string.

### 7.4 Approval and secrets

- `ask` decisions persist an approval request correlated to the exact operation and arguments digest.
- Changed arguments invalidate an earlier approval.
- Known secret patterns and configured values are redacted from events, API responses, and traces.
- Model prompts and tool outputs are treated as untrusted data; instruction-like repository content cannot change host policy.

### 7.5 MCP boundary

An optional MCP adapter demonstrates standardized tool discovery/calls. Discovered tools remain subject to local allowlists, annotations, approval policy, output budgets, and audit. MCP never bypasses the gateway.

## 8. Reliability and error handling

Failures are typed and handled by policy:

| Failure | Behavior |
|---|---|
| Invalid model/tool schema | Record, return correction context, retry within a small bound |
| Tool timeout or non-zero exit | Record bounded stdout/stderr and let the plan adapt; never retry blindly |
| Transient model/API failure | Exponential backoff with jitter and maximum attempts |
| Context budget exceeded | Compact old tool results while preserving request/result pairs |
| Policy denial | Return explicit denial; repeated denial cannot escalate privilege |
| Approval required | Persist request and pause without holding a process open |
| Process crash | Replay task snapshot/events and continue after the last committed checkpoint |
| Unknown event/schema version | Fail closed with a diagnostic; never guess state |

Recovery replays only pure orchestration. External effects use operation IDs and stored results, preventing a resumed workflow from repeating a committed patch or command.

## 9. Persistence and observability

SQLite is the default local store because it is transactional and requires no service. Repositories abstract persistence so a production deployment can use PostgreSQL without changing orchestration.

Default observability consists of:

- persisted task events;
- one JSONL trajectory per run;
- structured application logs;
- aggregate evaluation JSON and Markdown reports.

The optional OpenTelemetry adapter emits spans for task stages, model calls, tool calls, tests, and graders. Content capture is disabled by default; only redacted metadata, token counts, duration, outcome, and correlation IDs are exported.

## 10. Interfaces and business integration

### 10.1 CLI

Planned commands:

- `repopilot doctor`
- `repopilot run --repo ... --issue ... --test-command ...`
- `repopilot resume TASK_ID`
- `repopilot approve TASK_ID REQUEST_ID`
- `repopilot inspect TASK_ID`
- `repopilot eval`
- `repopilot demo`

### 10.2 REST and SSE

- `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/resume`
- `POST /v1/tasks/{task_id}/approvals/{request_id}`
- `GET /v1/tasks/{task_id}/events` (SSE with sequence cursor)
- `GET /v1/tasks/{task_id}/artifacts`
- `GET /health` and `GET /ready`

Request/response models are versioned and shared through generated OpenAPI. The React UI is a consumer, not a privileged path.

### 10.3 Business adapters

The documentation describes three realistic integrations:

1. GitHub issue triage: webhook creates a task, a maintainer reviews the generated patch and PR draft.
2. Internal developer platform: service catalog submits repositories and receives auditable remediation artifacts.
3. CI repair assistant: a failed pipeline supplies logs and test commands; RepoPilot-R proposes an isolated patch for human approval.

## 11. Evaluation design

### 11.1 Included fixture benchmark

At least three small seeded Python repository tasks exercise different behaviors, such as arithmetic edge handling, normalization/parsing, and configuration validation. Each case contains:

- setup/source repository;
- issue text and constraints;
- deterministic offline action script;
- hidden verification tests;
- expected changed-file scope;
- optional failure-injection point.

### 11.2 Metrics

- task resolved rate and test-pass rate;
- patch scope precision;
- policy violation count and denied-operation count;
- tool steps, retries, duration, and output volume;
- recovery success and duplicate-side-effect count;
- model token/cost metrics when available;
- offline/online runtime label and environment fingerprint.

### 11.3 Ablations

The evaluation interface supports comparisons with repo map, review stage, or recovery disabled. Only executed comparisons appear in the delivered results.

### 11.4 SWE-bench evolution

An optional adapter and runbook explain how to export RepoPilot-R patches for a small SWE-bench Lite/Verified subset in a Docker-capable environment. This is an extension path; the local completion gate does not claim or require a benchmark score that cannot be reproduced on the current machine.

## 12. Testing strategy

- Domain tests: state transitions, value objects, policy decisions, budgets, redaction.
- Persistence tests: atomic append/snapshot, sequence ordering, idempotency, migration/version checks.
- Tool tests: path containment, search/read limits, patch checks, command allowlist, timeout, output truncation.
- Worktree integration tests: create, mutate isolated tree, diff, cleanup safeguards.
- Runtime tests: tool request/result pairing, maximum steps, retries, compaction, final response.
- Recovery test: inject crash after a committed operation and prove resume does not duplicate it.
- API tests: validation, task creation, event cursor/reconnect, approvals, artifact retrieval.
- Evaluation tests: seeded cases, graders, aggregation, reproducible report.
- Frontend tests: API transformation and critical dashboard states; production build must pass.
- End-to-end test: offline issue-to-verified-patch journey through public application interfaces.

Tests must not require network access, a model key, Docker, or external services unless explicitly marked optional.

## 13. Technology choices

- Python >=3.12; local verification uses the available Miniforge Python 3.13.
- Pydantic v2 for domain/API schemas.
- FastAPI + Uvicorn for REST, SSE, and OpenAPI.
- SQLite standard library adapter for durable local state.
- Git CLI and worktrees for task isolation.
- ripgrep plus Python Tree-sitter packages and Python AST for repository mapping.
- OpenAI Agents SDK as an optional online runtime.
- OpenTelemetry as an optional standards-based telemetry exporter.
- React + TypeScript + Vite for the task dashboard.
- pytest, Ruff, mypy, and coverage for backend verification; Vitest and TypeScript checks for frontend verification.
- A standard `venv`/pip quickstart because `uv` is not installed locally; `pyproject.toml` remains compatible with modern package managers.

## 14. Planned repository structure

```text
repopilot-r/
├── pyproject.toml
├── README.md
├── src/repopilot/
│   ├── domain/
│   ├── application/
│   ├── agent/
│   ├── tools/
│   ├── infrastructure/
│   ├── api/
│   ├── cli.py
│   └── evaluation/
├── web/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── benchmarks/fixtures/
├── examples/
├── docs/
│   ├── PROJECT_GUIDE.md
│   ├── CODE_WALKTHROUGH.md
│   ├── PROJECT_BRIEF_AND_RESUME.md
│   ├── BUSINESS_INTEGRATION.md
│   ├── EVALUATION.md
│   ├── SECURITY.md
│   └── superpowers/specs/
├── scripts/
└── .github/workflows/
```

## 15. Documentation contract

The final documentation must be useful to three audiences:

- **Developer:** setup, architecture, commands, API, tests, extension points, troubleshooting.
- **Technical interviewer:** important source files explained line by line or in tight contiguous ranges, with invariants, trade-offs, and failure modes.
- **HR/hiring manager:** a short project summary, business scenario, differentiators, actual metrics, concise Chinese and English resume bullets, STAR interview narrative, and explicit guidance against unsupported claims.

`CODE_WALKTHROUGH.md` will cover at minimum the durable orchestrator, safe tool gateway/policy engine, worktree manager, event store/idempotency, minimal agent loop, and evaluation grader. It will cite exact file paths and line numbers from the final source.

## 16. Delivery and completion audit

The implementation is complete only when all of the following are true:

1. Source and requested Markdown deliverables exist under `/Users/chen/Agent study/project`.
2. The documented offline quickstart is executed successfully from a clean environment or fresh virtual environment.
3. Backend tests/checks, frontend tests/build, end-to-end demo, recovery test, and fixture evaluation have current passing evidence.
4. Generated metrics are copied into documentation without embellishment.
5. Line references in the walkthrough match final files.
6. Placeholder scans find no unfinished markers or empty claims in required deliverables.
7. Security-sensitive behavior is tested and documented.
8. A final requirement-by-requirement audit maps every user deliverable to authoritative evidence.

## 17. Design rationale

This scope deliberately favors a polished vertical slice over a distributed-system façade. Worktree isolation, a durable event journal, idempotent operations, a safe tool gateway, structured evaluation, modern SDK adapters, OTel, API/SSE, and a real dashboard provide enough depth for a strong Agent engineering project. Optional Docker/Temporal/PostgreSQL evolution shows production awareness without making unavailable infrastructure a prerequisite or creating unverified code paths.
