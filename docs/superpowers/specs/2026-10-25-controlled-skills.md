# Controlled Skills Demo

User approved implementation without further confirmation on 2026-10-25.

## Scope and approaches
Choose a separately configured, private writable skills directory rather than editing
skills inside candidate worktrees. Existing roots remain read-only. No marketplace,
script installation, global skill installation, deletion or automatic execution.
Implement in the existing clean publication checkout on a feature branch; preserve
the old development checkout and user runtime. Do not push without a new request.

## Contract
- Optional skills.write_root, disabled by default; existing directory owned by the
  operator with no group/world permissions, outside worktrees and other skill roots.
- skill_list discovers/reloads metadata; skill_read loads Markdown progressively.
- skill_write creates/updates SKILL.md and references/*.md only. Require name,
  resource, content, expected_sha256 (null means absent), and root_id.
- Always ASK through the existing durable gateway, never grant by skill frontmatter.
  Approval binds all arguments including root identity, content and previous hash.
  Show path and diff. Invalid targets are DENY, not approvable exceptions.
- Validate root identity, path components, regular file type, single hard link,
  name/frontmatter consistency and 65536-byte limit. No symlink traversal.
- Approved writes are serialized with a process lock and atomically replace files;
  stale digests fail without overwriting. Committed outcomes replay through the
  existing operation store; indeterminate outcomes stop, not blind retries.
- Dynamic listing and reading make new skills available in the same session and
  after restart. No changes to subagent write/command/MCP restrictions.
- This is a trusted single-user POSIX demo, not OS sandboxing against a hostile
  process with the operator's UID. Same-UID arbitrary commands remain a boundary.

## Verification
Real temporary files + real SQLite gateway approval/resume tests, path/link/type and
stale-content rejection, connected-session wiring, unchanged deny policies, full
backend suite with coverage >=85%, frontend tests/types/build, independent review.
RAG is a follow-up design only, not an implementation or capability claim.
