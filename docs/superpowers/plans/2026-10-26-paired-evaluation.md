# Paired evaluation implementation plan

> Execute inline with executing-plans; user authorized implementation and GitHub sync.

Goal: reproducible Demo RAG on/off comparisons without weakening production security.
Architecture: new paired runner reuses bootstrap and fixtures; retrieval grader uses real tools on fixed sources. CLI exposes offline/explicit-provider runs and append-only human notes.
Tech stack: Python, Pydantic, existing SQLite/FTS5, pytest, Typer.
Spec: ../specs/2026-10-26-paired-evaluation.md

- [x] Add failing tests for paired identity, retained errors/approval waits, citation validation, human notes and overwrite refusal.
- [x] Implement retrieval questions/grading, paired execution and incremental reports; no automatic approvals.
- [x] Add CLI, usage docs and debug history, run actual offline comparison.
- [x] Run targeted/full tests, Ruff/mypy and existing verification; review changes and secrets, commit and push.

Review focus: exception text secrets, incomplete runs, asymmetric budgets, report denominator, symlink/overwrite risks. Report runtime mode explicitly; never infer model benefit from scripted actions. Persist scheduled attempts before execution so interruption cannot erase denominator.
