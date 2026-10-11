# GitHub Actions Performance Optimizer

A machine-learning-based, dependency-graph-aware scheduler for GitHub Actions CI/CD pipelines.

B.Tech CSE capstone (BCSE497J), VIT. Akshat Singh (23BKT0005), Aditya Choubey (23BCE2274). Supervisor: Prof. Pushpa Lakshmi R, SCOPE.

> **Status: research prototype.** It ingests real GitHub Actions history, builds a dependency graph, predicts job
> failure / duration / cache misses, and proposes a re-scheduled workflow with a reviewable diff. It does **not**
> show a measured speed-up: no optimized workflow has been run, and every improvement figure it prints is labelled
> ESTIMATED. The evaluation (`docs/RESULTS.md`) is deliberately critical; read it before relying on any claim.

## The idea

**Repository -> Graph -> Features -> Predictions -> Scheduler -> Optimized workflow.**

1. A heterogeneous graph links source files, workflow jobs, steps, cache keys and `needs` edges (five static rules, each
   with a provenance and a confidence).
2. Features per (run, job) combine graph structure with history from earlier runs only: whether the diff reaches the job,
   cache-key touch, prior failure rate, prior duration, author / temporal signals.
3. Three models predict job failure, duration and cache misses.
4. A scheduler respects `needs`, orders jobs fail-fast, and selects runners by `alpha*time + beta*cost + gamma*energy`.
5. A generator writes a **proposed** workflow file and a diff; the original file is never modified.

## What the evaluation found (details and caveats: [`docs/RESULTS.md`](docs/RESULTS.md))

* Training data is real: seven repositories, about 4,200 runs and 62,700 (run, job) rows, collected with
  `gha-optimizer ingest`. Two repositories (pydantic, cli/cli) were ingested under a written plan
  ([`docs/HELDOUT_PLAN.md`](docs/HELDOUT_PLAN.md)) so the graph rules could be tested on workflows they were not built on.
* **The models beat naive baselines only in some repositories.** The default failure model (regularised logistic
  regression) beats the "job's prior failure rate" baseline significantly in fastapi (+0.085 PR-AUC) and in the held-out
  pydantic (+0.110), is indistinguishable elsewhere, and is never significantly worse. The raw-seconds duration model is
  worse than the baseline everywhere; the log-ratio duration target beats it in some repositories and loses in others.
* **The graph describes the workflow only where the workflow declares file-to-job relationships** (poetry, fastapi). On
  held-out workflows without file-based gating it is uninformative, and where gating is computed in shell scripts (ruff,
  uv) it points the right way only weakly.
* **The scheduler's default plan equals the baseline** (0.0% estimated saving on real poetry). Its two levers are opt-in
  and unmeasured: skip guards cannot save time under GitHub's `needs` rules, and runner upsizing rests on an assumed
  speed-up law.
* **Skipping jobs the graph says a diff cannot reach** saves 12-17% of billable time in two repositories but would hide
  3-10% of failing runs, and did not generalise to the held-out repository. It is therefore reported as an advisory
  section only; deferring those jobs on pull requests was evaluated under a pre-registered plan and is not supported.
* A GNN and a matrix-reduction engine were built, verified against the data and **removed** (see `docs/RESULTS.md` 6).

## Component status

| component | status | notes |
|---|---|---|
| Ingestion of real history (runs, jobs, logs, git mirror, diffs) | IMPLEMENTED | resumable; log retention limits cache labels |
| Dependency graph and fusion rules | IMPLEMENTED | static approximation; modest predictive value |
| Feature extraction (history strictly from earlier runs) | IMPLEMENTED | |
| Failure / duration / cache-miss models | IMPLEMENTED | mixed results vs baselines; fresh-data check interim |
| Persisted models (fingerprint-checked) | IMPLEMENTED | never loads a stale artifact |
| Scheduler | IMPLEMENTED | default plan = baseline; levers opt-in; figures ESTIMATED |
| Workflow generator (proposed file + diff + explanation) | IMPLEMENTED | never edits the original; never changes build matrices |
| Advisory report (graph-unreached jobs, with measured rates) | IMPLEMENTED | not applied automatically |
| Evaluation tooling (`eval-models`, `replay-impact`, `dataset-report`) | IMPLEMENTED | rolling-origin folds, paired bootstrap |
| LLM explanation layer (opt-in wording only) | IMPLEMENTED, **not verified against the live API** | cannot influence the result |
| FastAPI server, Celery worker and dashboard | IMPLEMENTED (run once end to end) | `docker compose up` was exercised with a signed webhook, a private repo and Postgres (`docs/FULL_STACK_RUN.md`); without `GITHUB_WEBHOOK_SECRET` the webhook accepts unsigned requests |
| Feedback loop (drift detection, retraining) | REMOVED | nothing recorded predictions, so it could never run; see `docs/FULL_STACK_RUN.md` |
| Generated workflow executed on real GitHub Actions | VALIDATED (small scale) | original vs proposed, 12 measured runs: identical behaviour, `needs` respected, no detectable time difference (`docs/VALIDITY_RUN.md`) |
| Measured end-to-end speed-up | **NOT SHOWN** | the default plan is the baseline by design; larger runners (the only lever that shortens the critical path) need paid organisation runners |

## Quick start

Python 3.10+. A GitHub token is needed to ingest history (job logs require authentication).

```bash
pip install -e ".[dev]"              # core pipeline, CLI and tests
pip install -e ".[server]"           # optional: API server, Celery worker, Postgres driver

export GITHUB_TOKEN=...              # or put it in .env (see .env.example)

# 1. collect real history for a repository (bounded; resumable)
gha-optimizer ingest --repo owner/repo --workflow ci.yml --since 40d --max-runs 300
gha-optimizer build-dataset --repo owner/repo        # per-commit graph features

# 2. analyze a workflow in a checkout of that repository
gha-optimizer analyze path/to/checkout/.github/workflows/ci.yml --repo owner/repo --changed src/a.py,src/b.py
#    writes ci.optimized.yml next to it and prints the explanation, the diff, the advisory section and ESTIMATED figures;
#    declines (exit 2) when the history is too thin - it never substitutes synthetic data

# 3. judge the claims on your own data
gha-optimizer eval-models   --repo owner/repo --out docs/MODEL_EVAL.md
gha-optimizer replay-impact --repo owner/repo --out docs/IMPACT_REPLAY.md
gha-optimizer dataset-report --repo owner/repo --out docs/DATASET_REPORT.md
```

Useful flags for `analyze`: `--enable-skips`, `--larger-runners`, `--speedup-exponent`, `--llm-summary` (needs
`GEMINI_API_KEY` and `GHA_LLM_MODEL`; sends only job ids and computed numbers), `--no-model-store`.
Check the LLM setup with `gha-optimizer llm-check`, then `python demo.py --repo python-poetry/poetry --llm-summary`.

Server: `gha-optimizer serve` (FastAPI on :8000; `POST /api/v1/optimize` returns 422 when history is missing).
Full stack: `docker-compose up --build` (Postgres, Redis, API, Celery worker).
Standalone demo UI on real ingested data: `python demo_server.py --repo owner/repo`.

## Repository layout

```
gha_optimizer/
  cli.py, pipeline.py, config.py     entry points and orchestration
  ingestion/                         GitHub client, log parsing, git mirror, history loading
  graph/                             graph builder, fusion rules, CI semantics, workflow expansion
  features/                          feature extraction, per-commit graph features, dataset + reports
  models/                            failure / duration / cache models, evaluation, rolling-origin eval, model store
  scheduler/                         fail-fast DAG scheduler and runner selection
  generator/                         proposed workflow + diff + explanation
  advisory.py, replay.py             advisory report; counterfactual replay and deferral evaluation
  llm_summary.py                     optional explanation layer
  server/, worker/                   FastAPI app + DB, Celery tasks (ingest, per-commit graph, benchmark)
dashboard/                           static page that shows only what the API returns
docs/                                design notes, generated reports, pre-registered plans, results
tests/                               335 tests (fixtures are data, not tests)
```

## Documentation

* [`docs/RESULTS.md`](docs/RESULTS.md) - consolidated results and threats to validity
* [`docs/REAL_DATA_FOUNDATION.md`](docs/REAL_DATA_FOUNDATION.md) - how the real dataset is collected and what it contains
* [`docs/GRAPH_FEATURES.md`](docs/GRAPH_FEATURES.md) - graph rules and the structural features
* [`docs/MODEL_EVAL.md`](docs/MODEL_EVAL.md), [`docs/FROZEN_CHOICE.md`](docs/FROZEN_CHOICE.md), [`docs/MODEL_EVAL_FRESH.md`](docs/MODEL_EVAL_FRESH.md) - model comparison, the frozen default and its fresh-data test
* [`docs/SCHEDULER_ASSUMPTIONS.md`](docs/SCHEDULER_ASSUMPTIONS.md) - what the scheduler's estimate rests on
* [`docs/IMPACT_REPLAY.md`](docs/IMPACT_REPLAY.md), [`docs/HELDOUT_PLAN.md`](docs/HELDOUT_PLAN.md), [`docs/HELDOUT_RESULTS.md`](docs/HELDOUT_RESULTS.md), [`docs/DEFERRAL_PLAN.md`](docs/DEFERRAL_PLAN.md) - the skip policy, its held-out test and the deferral evaluation
* [`docs/LLM_EXPLANATION.md`](docs/LLM_EXPLANATION.md) - the optional explanation layer and its guarantees

## Principles the code follows

* No synthetic or default values are presented as results; the only synthetic generator lives in the tests.
* ESTIMATED (from predicted durations) and MEASURED (on recorded runs) are never mixed.
* A generated workflow never violates a `needs` relationship and never overwrites the original file.
* Machine learning and scheduling do not depend on any LLM; the optional LLM layer only words the facts.
* Claims are pre-registered where they could be tuned, and negative results are kept.
