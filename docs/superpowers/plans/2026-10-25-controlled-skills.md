# Controlled Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add explicitly approved Markdown skill authoring and dynamic loading.
**Architecture:** Private directory writer behind existing PolicyEngine/SafeToolGateway;
dynamic SkillCatalog shared by list/read/write tools. Existing stores own approvals.
**Tech Stack:** Python, pathlib/os dir_fd, fcntl, hashlib, PyYAML, pytest; no new dependencies.
**Spec:** ../specs/2026-10-25-controlled-skills.md

## Global Constraints
Disabled by default; per-write ASK; only SKILL.md and references/*.md; 65536 bytes;
subagents read-only; existing budgets/command policies/coverage threshold unchanged.

## Review Focus
Root changes between approval and resume; symlink/hardlink targets; concurrent updates;
duplicate names/read-only roots; hidden escalation through general tools.

### Task 1: Authoring, policy and connected loading (one coupled safety boundary)
Files: tools/skill_write.py (new), tools/skills.py, configuration.py, connected.py,
bootstrap.py, tools/policy.py, worker.py; tests/integration/test_skill_authoring.py.
Interfaces: SkillWriter(root, catalog).preview(request)->str, execute(...)->ToolResult;
SkillCatalog.refresh(), SkillListTool; optional policy preview callback.
- [ ] Write tests for approval/create/reload/update, stale digest, root replacement,
  unsafe paths/types, readonly shadowing, replay, changed approval parameters.
- [ ] Run new tests RED for missing feature before implementation.
- [ ] Implement minimal filesystem writer and fail-closed policy, then wire config/tools.
- [ ] Run focused tests GREEN; full suite, Ruff/mypy, frontend tests/typecheck/build.
- [ ] Independent final security review and regression tests for important findings.

### Task 2: Documentation and Demo RAG design
Files: docs/SKILLS.md, docs/RAG_DESIGN.md, docs/DEBUG_JOURNAL.md, README.md.
- [ ] Document setup/approval/loading, limitations, current results and debug evidence.
- [ ] Describe local bounded hybrid retrieval as future work, evaluation and security gates.
- [ ] Verify links/diff and sync scoped changes to local delivered project with backup.
