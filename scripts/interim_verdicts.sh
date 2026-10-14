#!/bin/bash
# Interim runs of the two pre-registered fresh-data tests (docs/FROZEN_CHOICE.md, docs/POOLED_FROZEN.md) on the data that exists now.
# They apply the same procedure and the same rule as the final runs (docs/BACKGROUND_RUNBOOK.md) but are labelled INTERIM:
# the rules require at least 20 fresh failures per judged repository, so a verdict stays open where that is not reached.
# The GitHub token comes from `gh auth token` and lives only in this process.
set -u
cd "$(dirname "$0")/.."
PY=".venv/Scripts/python.exe"
CLI="$PY -u -W ignore -m gha_optimizer.cli"
export GITHUB_TOKEN="$(gh auth token)"
STAMP="${1:-$(date -u +%Y-%m-%d)}"

ingest() { # repo workflow since max_runs
  echo "--- ingest $1 ($(date -u +%T))"
  $CLI ingest --repo "$1" --workflow "$2" --since "$3" --no-logs --max-runs "$4" 2>&1 | tail -n 3 | cut -c1-200
}
dataset() { echo "--- build-dataset $1 ($(date -u +%T))"; $CLI build-dataset --repo "$1" 2>&1 | tail -n 2 | cut -c1-200; }

echo "=== frozen-choice interim ($STAMP)"
ingest python-poetry/poetry tests.yaml 2026-10-07 150
ingest fastapi/fastapi test.yml 2026-10-07 150
ingest prometheus/prometheus ci.yml 2026-10-07 600
ingest apache/dubbo build-and-test-pr.yml 2026-10-07 100
for r in python-poetry/poetry fastapi/fastapi prometheus/prometheus apache/dubbo; do dataset $r; done
$CLI eval-models --repo python-poetry/poetry --repo fastapi/fastapi --repo prometheus/prometheus --repo apache/dubbo \
  --after 2026-10-07T07:51:04Z --target failure --target failure_tree --target failure_regularized_tree \
  --out "docs/MODEL_EVAL_FRESH_INTERIM_$STAMP.md" 2>&1 | tail -n 2 | cut -c1-200

echo "=== pooled interim ($STAMP)"
ingest astral-sh/ruff ci.yaml 2026-10-12 100
ingest astral-sh/uv ci.yml 2026-10-12 100
ingest pydantic/pydantic ci.yml 2026-10-12 80
ingest cli/cli go.yml 2026-10-12 80
ingest quarkusio/quarkus ci-actions-incremental.yml 2026-10-12 40
ingest python-poetry/poetry tests.yaml 2026-10-12 150
ingest fastapi/fastapi test.yml 2026-10-12 150
ingest prometheus/prometheus ci.yml 2026-10-12 600
ingest apache/dubbo build-and-test-pr.yml 2026-10-12 100
# the pooled model excludes the per-commit graph features, so no build-dataset is needed for it
$CLI eval-pooled --repo python-poetry/poetry --repo fastapi/fastapi --repo prometheus/prometheus --repo apache/dubbo \
  --repo quarkusio/quarkus --repo pydantic/pydantic --repo cli/cli --repo astral-sh/ruff --repo astral-sh/uv \
  --after 2026-10-12T14:58:29Z --out "docs/POOLED_EVAL_FRESH_INTERIM_$STAMP.md" 2>&1 | tail -n 2 | cut -c1-200
echo "=== interim runs done ($(date -u +%FT%TZ))"
