#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

if [ -x ".venv/bin/python" ]; then
  python_bin=".venv/bin/python"
else
  python_bin="python"
fi

if [ "${REPOPILOT_VERIFY_DIR:-}" ]; then
  verification_parent=$REPOPILOT_VERIFY_DIR
  mkdir -p "$verification_parent"
else
  verification_parent=$(mktemp -d /tmp/repopilot-verify.XXXXXX)
fi
evaluation_workspace="$verification_parent/evaluation"

if [ -e "$evaluation_workspace" ]; then
  echo "verification output already exists: $evaluation_workspace" >&2
  exit 2
fi

echo "[1/7] Ruff"
"$python_bin" -m ruff check src tests

echo "[2/7] mypy"
"$python_bin" -m mypy src

echo "[3/7] pytest + branch coverage"
"$python_bin" -m pytest -q -W error::ResourceWarning \
  --cov=repopilot --cov-branch --cov-report=term --cov-fail-under=85

echo "[4/7] offline benchmark"
"$python_bin" -m repopilot.cli eval --workspace "$evaluation_workspace" --json

echo "[5/7] Vitest"
npm --prefix web test -- --run

echo "[6/7] TypeScript"
npm --prefix web run typecheck

echo "[7/7] production dashboard build"
npm --prefix web run build

echo "verification passed"
echo "evaluation report: $evaluation_workspace/evaluation-results/report.json"
