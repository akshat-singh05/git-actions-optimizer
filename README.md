# GitHub Actions Optimizer

**A machine-learning, dependency-graph-aware scheduler for GitHub Actions CI/CD pipelines.**

[![CI](https://github.com/akshat-singh05/git-actions-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/akshat-singh05/git-actions-optimizer/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

GitHub Actions Optimizer reads the real execution history of a repository's workflows, links it to a dependency graph of the repository
(source files, jobs, steps, caches, runners), predicts how each job will behave, and uses those predictions to propose a re-scheduled
workflow. It never edits your workflow file: it writes a **proposed** file next to it, with a reviewable diff and an explanation of every change.

```
Repository ──► Graph ──► Features ──► Predictions ──► Scheduler ──► Proposed workflow
 (git + API)   files,     history +     failure,        respects       YAML + diff;
               jobs,      graph +       duration,       `needs`        the original file
               caches     time          cache miss                     is never modified
```

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Command reference](#command-reference)
- [Opt-in speed-up levers](#opt-in-speed-up-levers)
- [Dashboard and REST API](#dashboard-and-rest-api)
- [Configuration](#configuration)
- [Docker](#docker)
- [Project layout](#project-layout)
- [Development](#development)
- [Evaluation and current status](#evaluation-and-current-status)
- [Team](#team)

## Features

- **Real data only.** History is ingested from the GitHub REST API (runs, jobs, steps, logs) and from a local git mirror (workflow YAML and diffs at each commit). No synthetic data is used anywhere in the package.
- **Heterogeneous dependency graph.** Source files are parsed with tree-sitter (Python, JavaScript, Java, Go), the workflow is parsed into jobs, steps and cache keys, and five static rules link files to jobs and caches. The graph is built per commit.
- **Three predictions per job.** Failure (regularised logistic regression by default, gradient-boosted tree variants available), duration (gradient-boosted trees on the log ratio to the job's prior mean) and cache miss (report-only).
- **Dependency-safe scheduling.** Jobs are ordered fail-fast over the job DAG while respecting every `needs` edge; the default plan keeps the workflow's structure.
- **Provably safe, opt-in edits.** Removing gate-only `needs` edges, pruning unnecessary dependency installs and adding a pip cache are validated against the original workflow and listed in the proposed file's header.
- **Honest numbers.** Every figure is labelled **ESTIMATED** (derived from predicted durations) or **MEASURED** (real runs). Each score is shown next to a naive baseline, and the tool declines to predict when the ingested history is too thin.
- **Reproducible.** Models are seeded, and the scoring time is an explicit input (`--as-of`), so the same analysis gives the same numbers.
- **Optional LLM summary.** A plain-language summary can be appended after every decision is made. It sends only job ids, runner labels and computed numbers, never affects the plan, and the tool works fully without it.
- **Three ways in.** A command-line tool, a REST API (FastAPI) and a single-page dashboard with per-stage pages and a what-if scorer.

## How it works

| Stage | What it does | Where |
|---|---|---|
| Ingestion | Loads runs, jobs, logs and diffs into a SQL database (SQLite by default); resumable and idempotent | `gha_optimizer/ingestion/` |
| Graph | Builds the source-code graph and the workflow graph and fuses them | `gha_optimizer/graph/` |
| Features | One row per job and run; history uses strictly earlier runs only; unknown stays unknown | `gha_optimizer/features/` |
| Models | Failure, duration and cache-miss models, fitted per repository; persisted only when a data fingerprint matches | `gha_optimizer/models/` |
| Scheduler | DAG levels, fail-fast priority order, runner choice on time, cost and energy | `gha_optimizer/scheduler/` |
| Generator | Writes the proposed workflow, the diff and the explanation | `gha_optimizer/generator/` |

`run_pipeline()` in `gha_optimizer/pipeline.py` is the single entry point used by the CLI, the API, the dashboard and the tests.

## Getting started

**Requirements:** Python 3.10 or newer, Git, and a GitHub personal access token (needed to ingest history because job logs require authentication). Docker is optional.

```bash
git clone https://github.com/akshat-singh05/git-actions-optimizer.git
cd git-actions-optimizer

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -e ".[dev]"              # core pipeline, CLI and test tools
pip install -e ".[server]"           # optional: API server, Celery worker, Postgres and Redis clients
pip install -e ".[ml]"               # optional: XGBoost (used automatically when installed)

cp .env.example .env                 # then set GITHUB_TOKEN (the .env file is git-ignored)
```

### Quick start

```bash
# 1. Ingest the recent history of a repository (resumable; a bare git mirror is cloned under .gha_optimizer/)
gha-optimizer ingest --repo owner/repo --workflow ci.yml --since 40d

# 2. See what was ingested (real SQL row counts)
gha-optimizer ingest-status --repo owner/repo

# 3. Analyse a workflow for a given change set; the proposed file is written next to the original
gha-optimizer analyze path/to/checkout/.github/workflows/ci.yml --repo owner/repo --changed src/app.py,README.md

# 4. Explore everything in the browser (http://localhost:8501)
gha-optimizer dashboard --repo owner/repo
```

`analyze` exits with code 2 and prints *"Declining to predict: insufficient ingested history"* when the history is too thin. It writes the proposed workflow to
`.github/workflows/<name>.optimized.yml` in the checkout and never overwrites the original. Pass `--as-of 2026-10-12T12:00:00Z` for a reproducible score.

## Command reference

Run `gha-optimizer <command> --help` for every option.

| Command | Purpose |
|---|---|
| `ingest`, `ingest-status` | Collect real run history into the database; show row counts |
| `build-dataset`, `dataset-report` | Build per-commit graph features; generate a dataset report from the database |
| `analyze` | Predict, schedule and write a proposed workflow with a diff and an explanation |
| `evaluate` | Ablate the scheduler's time / cost / energy weights (ESTIMATED figures) |
| `dashboard` | The web dashboard (`--warm` pre-computes every ingested repository; `--snapshot-root` keeps exported paths short on Windows) |
| `serve` | The FastAPI server (REST API, webhook endpoint and the dashboard under `/dashboard`) |
| `eval-models` | Rolling-origin comparison of the models against naive baselines, with feature-set ablation and paired bootstrap intervals |
| `eval-calibration` | Brier score, calibration error and reliability of the failure scores |
| `eval-pooled` | Leave-one-repository-out evaluation of a pooled failure model |
| `replay-impact`, `replay-gates` | Counterfactual replays on real history of skipping unreached jobs and of relaxing safe gates |
| `triage` | Classify real failures from recorded outcomes (class, failing step, suggested action) |
| `live-score`, `live-evaluate` | Prospective scoring of live runs into a hash-chained ledger, and evaluation against the real outcomes |
| `llm-check` | One tiny request to verify the optional LLM configuration |

## Opt-in speed-up levers

All levers are **off by default**, so the default plan equals the original workflow.

| Option of `analyze` | Effect | Safety |
|---|---|---|
| `--relax-gates` | Removes `needs` edges that only gate a job (no data dependency, no secrets other than `GITHUB_TOKEN`, no deploy or publish step, no status-function condition) so that jobs run in parallel | Every removal is proven safe by `gha_optimizer/gates.py`, listed in the file header and validated against the original. The job then also runs when its former upstream fails |
| `--prune-installs` | Removes `pip install -r FILE` from jobs that only run stand-alone linters | Strict tested conditions, validated |
| `--add-pip-cache` | Adds `cache: pip` where safe and meaningful | Validated; the saving is deliberately not estimated |
| `--enable-skips` | Writes `if: ${{ false }}` guards for jobs predicted to fail | A correct prediction saves nothing under GitHub's `needs` semantics and a wrong one removes tests; reported as advice only unless enabled |
| `--larger-runners` | Relabels base-tier jobs to larger runners that exist in your organisation | Relies on an assumed speed-up law (`--speedup-exponent`) |
| `--llm-summary` | Appends a plain-language summary (needs `GEMINI_API_KEY` and `GHA_LLM_MODEL`) | Wording only; every number must appear in the computed facts |

## Dashboard and REST API

`gha-optimizer dashboard` serves a single-page dashboard on `http://localhost:8501` with a repository selector and one page per pipeline stage
(repository, source and CI graphs, graph fusion, impact analysis, features, models, scheduling, safety verification, original vs proposed workflow, final summary),
a what-if scorer and evidence pages. Results are cached on disk and reused while the ingested runs, the tip commit and the library versions are unchanged.
The dashboard never renders invented numbers: a repository that the pipeline declines shows the reason instead.

`gha-optimizer serve` starts the API on port 8000:

| Endpoint | Description |
|---|---|
| `POST /api/v1/optimize` | Run the pipeline for a workflow and a change set (returns 422 when the history is insufficient) |
| `GET /health`, `GET /ready` | Liveness and readiness |
| `POST /webhook/github` | GitHub webhook receiver (HMAC-validated; set `GITHUB_WEBHOOK_SECRET`) |
| `GET /api/v1/parsers`, `GET /api/v1/metrics` | Available language parsers; service metrics |
| `/api/v1/graph/...` | Graph queries (hotspots, risk, critical path, quality, provenance, history, diffs) |
| `/api/v1/dashboard/...` | The API behind the dashboard; the page itself is at `/dashboard` |

Interactive API documentation is served by FastAPI at `/docs` when the server is running.

## Configuration

Settings are read from environment variables or a `.env` file found from the working directory upward (real environment variables win).

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | Token used to read runs, jobs, logs and to clone the mirror (never placed in a URL) |
| `GITHUB_REPO` | Default repository (`owner/repo`) |
| `GITHUB_WEBHOOK_SECRET` | Secret for webhook signature validation; without it unsigned webhooks are accepted, so set it in any reachable deployment |
| `DATABASE_URL` | SQLAlchemy URL; falls back to `sqlite:///gha_optimizer.db` |
| `REDIS_URL` | Celery broker (full stack only) |
| `GHA_MODEL_DIR` | Where fingerprint-matched fitted models are stored |
| `GHA_HOST`, `GHA_PORT` | Server bind address and port |
| `GEMINI_API_KEY`, `GHA_LLM_MODEL` | Optional LLM summary |
| `GHA_MAX_RETRIES`, `GHA_NETWORK_PATIENCE_S` | Retry count and patience of the GitHub client for bulk reads |

Never commit `.env`; it is git-ignored. The scheduler's weights and thresholds (`alpha`, `beta`, `gamma`, runner prices, skip threshold) are defaults in `gha_optimizer/config.py`.

## Docker

```bash
docker compose up --build        # Postgres, Redis, the API on :8000 and a Celery worker
```

The images install only the package, so a clean non-editable install is also what runs in the containers. Mirror clones authenticate through git's environment configuration, not through the URL.

## Project layout

```
gha_optimizer/
  ingestion/        GitHub client, log parser, job mapping, git mirror, history loader
  graph/            tree-sitter parsers, workflow graph, fusion rules, graph analytics and storage
  features/         feature extraction, per-commit graph features, dataset report
  models/           failure / duration / cache models, rolling-origin evaluation, calibration, model store
  scheduler/        DAG-aware scheduler and the plan it returns
  generator/        proposed workflow, diff and explanation
  server/           FastAPI app, dashboard API, database models and migrations helpers
  worker/           Celery tasks
  dashboard_ui/     the single-page dashboard
  pipeline.py       run_pipeline(): the orchestration entry point
  cli.py            the `gha-optimizer` command
alembic/            database migrations
scripts/            demo start scripts, live-test supervisor, measurement harness for real before/after runs
tests/              the test suite (fixtures hold real log excerpts and a toy repository)
examples/, demo.py  runnable demonstrations on an ingested repository
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v                                          # the full suite trains models and takes about 15 minutes
ruff check gha_optimizer/ --select E,F,W --ignore E501    # lint, as in CI
mypy gha_optimizer/ --ignore-missing-imports              # type check, as in CI
alembic upgrade head                                      # database migrations (DATABASE_URL, default SQLite)
```

The CI workflow (`.github/workflows/ci.yml`) runs lint, type checking and the tests on Python 3.10.
On Windows, Smart App Control can block native extensions of scikit-learn or tree-sitter intermittently; do not disable it, retry, or run the Linux Docker image instead.

## Evaluation and current status

The system was evaluated on **24,667 workflow runs (590,663 job records) from 19 public repositories**, with rolling-origin folds (the models only ever see the past),
paired bootstrap intervals and a naive baseline next to every metric. Several claims were fixed in writing before the data were read and then re-tested on later runs.
The `eval-*`, `replay-*` and `live-*` commands above regenerate these reports from an ingested database.

- **Prediction is modest.** The models beat the job's own prior-failure-rate baseline in some repositories and not in others. The logistic default was confirmed on fresh data only as a *safe* default. A richer failure model missed its pre-registered bar, and a pooled model was rejected on fresh data. Failure scores are not calibrated, so they are presented as risk scores, never as probabilities.
- **The graph helps only where the workflow states which files affect which job.** Elsewhere its features are uninformative.
- **By default the plan saves nothing** (0.0% ESTIMATED; a real run agreed). Skipping jobs and larger runners are opt-in and not measured savings.
- **Gate relaxation can shorten the critical path, with a cost.** On three real pipelines the **MEASURED** critical path was 15% to 49% shorter with billable time unchanged in green runs. In a failing run the relaxed workflow still runs the downstream jobs and billed 29 s and 97 s more on the two pipelines where this was measured. These pipelines were chosen because the estimated saving was large; mature repositories gain about nothing.
- **Known limits.** One workflow per repository; log retention (about 45-90 days) limits cache-miss labels; GitHub's API refuses some very large runs; the live prospective test is a demonstration, not a significance test.

Status of the components: ingestion, graph engine, features, the three models, the scheduler, the workflow generator with its validators, the CLI, the API and the dashboard are implemented and tested
(480 tests; type checking is clean; a clean non-editable install and the full Docker stack were verified). The optional LLM layer is implemented and was verified against the live service.
A graph neural network and a matrix-reduction module were built, evaluated and removed because they did not hold up.

## Team

Capstone project **BCSE497J (Project I)**, B.Tech, School of Computer Science and Engineering (SCOPE), Vellore Institute of Technology.

- Akshat Singh (23BKT0005)
- Aditya Choubey (23BCE2274)

Supervisor: Dr. Pushpa Lakshmi, SCOPE, VIT.
