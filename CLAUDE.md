# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project identity

**GitHub Actions Performance Optimizer** — A Machine-Learning Based Predictive Scheduler for GitHub Actions CI/CD Pipelines.

B.Tech CSE capstone (BCSE497J), VIT. Akshat Singh (23BKT0005), Aditya Choubey (23BCE2274). Supervisor: Prof. Pushpa Lakshmi R, SCOPE.

One-line description: *an ML-driven, dependency-graph-aware system that predicts GitHub Actions job behaviour and uses those predictions to perform dependency-safe CI/CD scheduling and workflow optimization.*

The research contribution is the chain **Repository → Graph → Features → Predictions → Scheduler → Optimized Workflow**: connecting a heterogeneous dependency graph (source files, packages, workflows, jobs, caches, runners) to historical execution data, predicting cache-miss / failure / duration, and feeding those predictions into a scheduler that respects `needs` constraints and regenerates the workflow YAML. This is an academic, research-oriented system — not a dashboard that displays CI statistics.

## NON-NEGOTIABLE RULES

These govern every session. They exist because this is assessed academic work and an unsupported claim is worse than a missing feature.

1. **Inspect before modifying.** Read the actual implementation. Never assume from this file or from a design document.
2. **Do not rewrite working components** unnecessarily, and **never create duplicate implementations** of an existing concern.
3. **Never introduce fake, mocked or simulated values presented as real output.** No hardcoded numbers dressed up as computed results.
4. **Do not claim a model is trained** unless a real artifact exists and you verified it loads.
5. **Do not claim the GNN produced a prediction** unless you traced the code path proving it. Note: the GNN currently falls back to tabular *silently* — a GNN-labelled result is not evidence the GNN ran. (Status 2026-10-08: the GNN and its `model_backend` switch were removed, so there is no GNN path; this rule applies if one is rebuilt.)
6. **Do not claim performance improvements without real benchmarking.** Always distinguish ESTIMATED from MEASURED — in variable names, API response fields, UI labels and prose. The ~43.9% wall-clock figure is an estimate derived from predicted durations, not a measured execution result, unless proven otherwise.
7. **DAG correctness is sacred.** A generated workflow must never violate a `needs` relationship.
8. **Never silently modify the user's original workflow file.** Emit a proposed file plus a reviewable diff.
9. **Keep the core ML/scheduler architecture independent of any LLM.** If Gemini or another LLM is added, it is an explanation layer only — summaries, workflow-change explanations, natural language. It must never decide the scheduling result, and the system must work fully when that API is unavailable. Validate any structured LLM output application-side.
10. **No scope creep.** No unrelated enterprise features, no unnecessary technologies.
11. **DO NOT CREATE OR MODIFY README.md.** It is written only after the entire project is complete. This holds in every session unless explicitly lifted. Use `docs/` for interim documentation.
12. **Classify status honestly** — IMPLEMENTED / PARTIALLY IMPLEMENTED / STUB / PLANNED / SIMULATED-DEMO-ONLY. Never blur these.
13. **Report what you built, what you verified by running it, and what remains incomplete.** Never claim completion without execution.
14. **Every module must be reachable from a real entry point.** No orphaned code.
15. Avoid vague framing ("AI makes CI/CD faster"). Use precise technical language, and never hide a trade-off — the current configuration reduces wall-clock while *increasing* estimated cost/energy/CO2, and that is a multi-objective trade-off to explain, not conceal.

## Known credibility gaps (as of 2026-10-07, after the real-data phase)

Treat these as open, not resolved. They are the areas most likely to be challenged at a viva. Detail: `docs/REAL_DATA_FOUNDATION.md` (design/status) and `docs/DATASET_REPORT.md` (generated from the DB).

- **Training data is real** (ingested GitHub Actions history in the DB; `gha-optimizer ingest`): poetry 596 runs / 3,576 rows, fastapi 682 / 3,975, prometheus 2,163 / 44,507, dubbo 293 / 4,102, quarkus 24 / 504 (its per-commit graph takes ~90 s, so it was not extended). Failure labels cover all of them (pooled out-of-sample positives per repo: poetry 76, fastapi 160, prometheus 808, dubbo 93, quarkus 9); **cache labels exist only for a few hundred runs per repo** (log retention; most older runs were ingested with `--no-logs`, and a log backfill was budget-limited). Only runs whose jobs *and* diff are known are admitted (`load_history`). The only synthetic generator is the test fixture `tests/fixtures/synthetic_history.py`, never importable from `gha_optimizer/`.
- **Graph-derived features describe the workflow correctly only where it states a file→job relationship; their predictive value is modest and confined to those repos.** Version 2 (`GRAPH_FEATURE_VERSION`, `docs/GRAPH_FEATURES.md`) agrees with executed-vs-skipped for poetry (skipped 1% when impacted vs 93% when not) and fastapi (3% vs 37%), is wrong-way for prometheus and quarkus and uninformative for dubbo. In the rolling-origin ablation (`docs/MODEL_EVAL.md`) removing them lowers failure PR-AUC for the simple logistic candidate in poetry (+0.037, interval excludes 0) and fastapi (+0.038), and for fastapi's cache target (+0.028, 123 rows); for the failure model the effect is indistinguishable from 0 everywhere, and in prometheus the logistic candidate is slightly *worse* with them (-0.009). Many comparisons were made (3 failure models x 5 repos x 3 contrasts) and intervals are optimistic, so treat these as leads, not established effects. `dep_graph_distance` has only 2-3 distinct values per repo. Toolchain rules over-approximate; intra-repo imports are unresolved.
- **Models beat the naive baselines only in some repos.** The default failure model is now the regularised logistic regression (`FailurePredictor(variant="logistic")`, chosen 2026-10-08 because it is the only variant never significantly worse than the baseline; the choice was made from the same folds it is judged on, so `docs/FROZEN_CHOICE.md` pre-states a fresh-data test - not yet run - via `eval-models --after 2026-10-07T07:51:04Z`; the `tree` and `regularized_tree` variants remain). Measured with rolling-origin folds and paired bootstrap intervals (`gha-optimizer eval-models`, `docs/MODEL_EVAL.md`). Failure: the original tree model beats the prior-failure-rate baseline only in prometheus (808 positives) and is significantly worse in dubbo and quarkus; two candidates fixed before any fold was seen do better - a regularised tree beats the baseline in poetry, fastapi and prometheus but loses in dubbo and quarkus, and a regularised logistic regression beats it in fastapi and is never significantly worse (the regularised tree is not the default). Duration: the raw-seconds model is significantly worse than the prior-mean baseline everywhere; the `log_ratio` target (default since 2026-10-07) beats it significantly in poetry and prometheus, is indistinguishable in fastapi and worse in dubbo. Cache: small samples (25-543 labelled test rows), mixed - baseline significantly better in prometheus. Always report the base rate/baseline next to a metric.
- **The scheduler's estimate has two levers, neither measured** (`docs/SCHEDULER_ASSUMPTIONS.md`). Skip recommendations (opt-in since 2026-10-08, `--enable-skips`) cannot save time under GitHub's `needs` semantics - a correct prediction saves nothing, a wrong one removes tests - and on held-out runs the flags at the 0.85 threshold were right only 0-25% of the time (`docs/SCHEDULER_ASSUMPTIONS.md` 1b); runner upsizing relies on an ASSUMED speed-up law (`config.scheduler.speedup_exponent`, 0.6) that cannot be estimated because the ingested data has one runner size per OS. A replay of the one assumption-free lever - skipping jobs the dependency graph says a diff cannot reach (`gha-optimizer replay-impact`, `docs/IMPACT_REPLAY.md`, `docs/SCHEDULER_ASSUMPTIONS.md` 1c) - saves roughly 12-17% of billable time in fastapi and prometheus but would make 3-10% of failing runs look green (graph blind spots such as `make`-driven jobs), and nothing extra in poetry (already gated); it was therefore not turned into a generator; it is reported as an advisory section only (`gha_optimizer/advisory.py`, appended to the `analyze` / `/api/v1/optimize` explanation with the repo's replay figures; it never changes the plan or the proposed YAML; deferral to default-branch pushes is not built and not measured). Both scheduler levers are opt-in and off by default, so the default plan equals the baseline (0.0% on real poetry); the old ~46-48% is reported only as a labelled what-if (cost +64-67%) with a sensitivity table. Fail-fast ordering does not affect the estimate and the cache-miss probability is not consumed by the scheduler. All improvement figures are ESTIMATED, never measured; a measured claim needs real runs of both workflows.
- **Persisted models are used only when provably current.** `run_pipeline(model_dir=...)` (CLI `analyze` / API: on by default via `config.model.model_dir`; `--no-model-store` opts out; the library default is off) loads models from `models/store.py` only if a SHA-256 fingerprint of the history, feature contract, graph-feature version, estimator hyper-parameters and scikit-learn version matches; otherwise it refits and saves. Corrupt, partial or stale artifacts are never used. `gha-optimizer train` / the Celery `retrain_models` still write loose joblib files in the model dir root that the pipeline does not read.
- **The GNN was removed (2026-10-08) after verification, not because of a failed experiment.** It had never run (torch was never installed here) and could not have worked on the current graph: `models/graph_utils.py` read node attribute `"type"` and edge attribute `"relation"` while graph v2 uses `node_type` / `edge_type` with different vocabularies; the per-node features `hist_failure_rate`, `hist_mean_duration`, `hist_cache_miss_rate`, `has_cache`, `matrix_size` were set nowhere (constant 0); and `_try_gnn_prediction` trained on one copy of *today's* graph repeated per run with no held-out metric. The project's contribution chain (graph -> features -> predictions -> scheduler) does not need it: the graph already reaches the models through the structural features, and the tabular cache model does not beat its baseline in most repos. Removed: `gnn_cache_predictor.py`, `graph_utils.py`, the `model_backend` switch (pipeline, CLI `--model`, API field, `GHA_MODEL_BACKEND`), the `gnn` extras and the torch pins. Recover from git history if a supervisor requires one; a rebuild needs a new graph-to-tensor adapter, per-commit graph samples and the same rolling-origin evaluation as `eval-models`. **`README.md` still advertises a GNN as the "patent core"** (it is written last, per rule 11) - correct it when the README is written.
- **Matrix reduction was evaluated and removed (2026-10-08).** `models/matrix_reducer.py` was never wired into any entry point (only its own tests called it). Run on real per-cell outcomes (train on the first 70% of runs, test on the last 30%) it did find large reductions (poetry `tests-matrix` 54 -> 9 cells, fastapi `test` 15 -> 2) but its own coverage estimate was 1.00 in every case while on later runs fastapi's kept cells caught only 13 of 17 failing runs (76%) and prometheus `build` caught 0 of 1. It also decides from past failures, yet a cell that never failed still tests a different OS / Python version, so dropping cells silently shrinks CI coverage. The module, its tests and the writer's unused `reduced_matrix` argument were deleted; the workflow writer never modifies build matrices. **`README.md` still lists matrix reduction** (it is written last, per rule 11) - remove those lines when the README is written.

## Commands

A virtualenv lives at `.venv/` (Windows: `.venv\Scripts\python.exe`).

```bash
# Install — both steps are needed (see "Gotchas")
pip install -r requirements.txt
pip install -e ".[dev]"

# Tests (full suite about 1-2 min, 292 tests; the pipeline tests train models)
pytest tests/ -v
pytest tests/test_scheduler.py -v                                   # one file
pytest tests/test_scheduler.py::test_name -v                        # one test
pytest tests/test_end_to_end.py::TestFullPipeline::test_full_pipeline_e2e

# Lint / typecheck — exactly as CI runs them (ruff and mypy are not in requirements.txt)
ruff check gha_optimizer/ --select E,F,W --ignore E501
mypy gha_optimizer/ --ignore-missing-imports     # non-blocking in CI

# Real-data workflow - GITHUB_TOKEN required for ingest (job logs need auth); run from the repo root
gha-optimizer ingest --repo owner/repo --workflow ci.yml --since 40d [--max-runs N] [--log-budget N]   # resumable, idempotent
gha-optimizer ingest-status --repo owner/repo    # real SQL row counts
gha-optimizer build-dataset --repo owner/repo     # per-commit graph features + dataset CSV
gha-optimizer dataset-report --repo owner/repo [--repo ...] --out docs/DATASET_REPORT.md
gha-optimizer eval-models --repo owner/repo [--repo ...] [--target failure|failure_tree|failure_regularized_tree|cache_any_miss|cache_exact_miss|duration|duration_ratio] [--after ISO_TS] --out docs/MODEL_EVAL.md   # rolling-origin folds, feature-set ablation, paired bootstrap
gha-optimizer replay-impact --repo owner/repo [--repo ...] --out docs/IMPACT_REPLAY.md   # counterfactual replay of 'skip jobs the graph says the diff cannot reach' on real history (MEASURED on history)
gha-optimizer train --repo owner/repo             # temporal-holdout metrics; saves joblib models to .gha_optimizer/models
gha-optimizer analyze <checkout>/.github/workflows/ci.yml --repo owner/repo [--changed a,b] [--larger-runners] [--enable-skips] [--speedup-exponent E] [--model-dir D | --no-model-store]   # exits 2 (declines) if history is insufficient; larger-runner relabelling is opt-in
gha-optimizer evaluate <workflow.yml> --repo owner/repo   # scheduler alpha/beta/gamma ablation (ESTIMATED figures)
python examples/demo.py --repo owner/repo         # full pipeline on a real ingested repo
python demo.py --repo owner/repo                  # verbose stage-by-stage walkthrough on real data
python demo_server.py --repo owner/repo           # standalone UI at http://localhost:8501 (no Postgres/Redis/Celery)
gha-optimizer serve [--reload]                    # FastAPI on :8000 (/api/v1/optimize returns 422 on cold start)

# Full stack (Postgres + Redis + API + Celery worker)
docker-compose up --build

# DB migrations (uses DATABASE_URL, falls back to sqlite:///gha_optimizer.db)
alembic upgrade head
alembic revision --autogenerate -m "msg"
```

CI (`.github/workflows/ci.yml`) runs lint → test + typecheck on Python 3.10.

## Architecture

### The pipeline (`gha_optimizer/pipeline.py`)

`run_pipeline()` is the single orchestration entry point used by the CLI, the `/api/v1/optimize` endpoint, the demos and the integration tests. It runs the layers in order:

1. **Ingestion** — `ingestion/workflow_parser.py::parse_workflow` → `WorkflowSpec`. History is a `list[WorkflowRunRecord]` (`ingestion/github_client.py`), one record per (run, workflow job key), loaded from the DB by `ingestion/history.py::load_history` (matrix cells aggregated; NULL labels preserved). `run_pipeline()` calls `require_history` first and raises `InsufficientHistoryError` instead of predicting from too little data.
2. **Graph** — `graph/dependency_graph.py::build_dependency_graph` → fused `nx.MultiDiGraph`.
3. **Features** — `features/feature_extraction.py` builds a training frame (one row per job × historical run, historical aggregates strictly from *earlier* runs, NaN where unknown) and an inference frame (one row per job for the current diff). Structural features come from `features/graph_features.py` (graph of the repo tree at each run's commit, via the git mirror) and are stored in `run_graph_features`. `FEATURE_COLUMNS` is the contract all three tabular models import.
4. **Models** — `CacheMissPredictor` (`target="any_miss"` default, or `"exact_miss"`: exact key not restored, i.e. miss or restore-keys partial hit), `FailurePredictor` (`variant="logistic"` default, or `"tree"` / `"regularized_tree"`), `DurationEstimator` (`target="log_ratio"` default: a median-loss correction to the job's prior mean, or `"seconds"`; NaN-capable `HistGradientBoosting*`; XGBoost is used automatically if importable; per-model metrics are a single temporal holdout with base rate and naive baseline - `models/evaluation.py`; the trustworthy comparison is `models/rolling_eval.py`). Models are re-fit from history unless a fingerprint-matched artifact set is loaded from `models/store.py` (see the credibility gaps).
5. **Scheduler** — `scheduler/optimizer.py::build_schedule` takes a *job-only* `nx.DiGraph` (`pipeline.job_only_dag`, not the dependency graph) plus the three prediction dicts and returns a `SchedulePlan`: fail-fast priority topological order, skip-probability propagation through the DAG, and runner selection minimizing `α·time + β·cost + γ·energy`, restricted to the base tier unless `allow_runner_upsizing` is on; skip candidates are reported but guards are written only if `allow_skip_recommendations` is on. The plan carries the skip-only / larger-runner / combined decomposition and `speedup_sensitivity` reports the larger-runner what-if per assumed exponent. Weights/thresholds default from `config.scheduler`.
6. **Generator** — `generator/workflow_writer.py` emits the optimized YAML, a diff and a markdown explanation. The proposed file carries no non-standard keys (estimates and skip reasons are header comments), skip guards are exactly `${{ false }}` (original condition kept inside), and `runs-on` changes only for a base-tier job when upsizing is on - never a Windows/macOS/matrix job.

### The graph engine (`gha_optimizer/graph/`)

- `GraphBuilder` (`builder.py`) is used twice: once for source files (via the tree-sitter language parsers in `graph/parsers/`, registered by file extension) and once for the workflow (jobs, steps, cache keys, etc.). `GraphFusion.fuse` (`fusion.py`) composes the two and adds cross-domain edges by five static rules (paths-filter gates, path arguments, toolchain scope, `setup-*` action, cache-key globs), each with its own `creation_source` and confidence; a gate supersedes the weaker rules for that job. Rule semantics and limits: `docs/GRAPH_FEATURES.md`. `ci_semantics.py` holds the pure helpers (globs, command classification, implicit-cache defaults); `workflow_expand.py` inlines local reusable workflows / composite actions; step nodes keep their full command in `NodeMetadata.attributes`. Every tree path is a `File` node.
- **Node IDs are `"<NodeType>::<name>"`** (e.g. `WorkflowJob::lint`, `File::src/app.py`); node/edge types and pydantic metadata live in `graph/models.py`. Every node/edge carries provenance + confidence.
- `dependency_graph.py` is the **bridge**: it exposes plain-name helpers (`impacted_jobs_for_diff`, `cache_keys_touched_by`, `graph_distance_to_job`) that translate to/from prefixed IDs. Feature extraction and most tests go through this module rather than the engine classes. `job_graph_features` is the single implementation of the structural features (training, static fallback, inference): impacted is 1 / 0 / NaN (unknown = no file evidence), never a fabricated "all jobs". `impacted_jobs_for_diff` (demos, scheduling) stays conservative: reached jobs plus every unlinked job.
- The pipeline collapses the `MultiDiGraph` to a `DiGraph` before feature extraction.
- Language parsers load inside try/except; a missing tree-sitter grammar disables that language with a warning instead of failing (see `GraphBuilder.get_parser_capabilities`, surfaced at `/api/v1/parsers`).
- The remaining modules (`analytics`, `quality`, `benchmark`, `explain`, `events`, `update`, `versioning`, `storage`) back the graph REST API in `server/api/v1/graph.py` and the Celery tasks; the core pipeline does not use them.

### Server and worker

- `server/app.py` — FastAPI app: `/health`, `/ready`, `/webhook/github` (HMAC-validated, hands off to Celery `process_webhook`), `/api/v1/optimize`, metrics, plus the graph router under `/api/v1/graph`.
- `worker/tasks.py` — Celery tasks (Redis broker) for repo sync, webhook handling, graph builds, training and drift/feedback (`feedback/`). Tasks are meant to be thin wrappers that import service modules lazily.
- Persistence is SQLAlchemy: `server/db/{session,models,crud}.py`, migrated with Alembic (`001_initial`, `002_real_ingestion`, `003_graph_feature_version`; `init_db()` is a dev convenience and does not add columns to existing tables - a DB made by `create_all` needs `alembic stamp <rev>` then `upgrade head`). The `ingestion/` package (`github_client`, `ingest`, `log_parser`, `job_mapping`, `mirror`, `history`, `workspace`) owns real-data collection; Celery `sync_repository` / `ingest_run` wrap it.
- `dashboard/` is a static vanilla-JS page (no build step, no Chart.js). It shows only what `POST /api/v1/optimize` returns (`index.html?repo=...&workflow=<server-side path>[&changed=...][&larger_runners=1]`) or an explicit empty state; the API has no per-job predictions, model metrics or history, so those views say "not available". It used to render invented numbers - never reintroduce demo data. `demo_ui.html` is a separate page served only by `demo_server.py`.

### Configuration

`config.py` exposes a `config` singleton (pydantic-settings, reads `.env`) with `github`, `model`, `scheduler`, `server` sub-configs. Env vars: `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_WEBHOOK_SECRET`, `DATABASE_URL`, `REDIS_URL`, `GHA_MODEL_DIR`, `GHA_HOST`, `GHA_PORT`.

## Gotchas

- **`setup.py` under-declares dependencies.** `pydantic-settings`, SQLAlchemy, Celery, redis, alembic and tree-sitter are only in `requirements.txt`, yet `config.py` (imported by the scheduler) needs `pydantic-settings` even for the core pipeline. `pip install -e .` alone is not enough.
- **Real data needs a token and a local mirror.** `ingest` clones a bare git mirror to `.gha_optimizer/mirrors/` (needed for workflow YAML at each sha, per-commit graphs and git-based PR diffs). Generated data (`gha_optimizer.db`, `.gha_optimizer/`) is gitignored and can be large (GBs).
- **GitHub API quirks measured on 2026-10-07**: log retention differs per repo (about 45-90 days), filtered run listings cap at 1000 and are not reliably ordered (the client windows by day and bisects), `pull_requests[]` is empty for fork PRs and `commits/{sha}/pulls` only returns open PRs (PR diffs fall back to git merge-base), and the shared 5000 req/h budget is hit by parallel ingests (the client sleeps until reset).
- `tests/fixtures/` holds data, not tests: a toy repo (`sample_repo/`, `sample_workflow.yml`), real log excerpts (`real_logs/`) and the explicit SYNTHETIC test generator; it is excluded from collection in `tests/conftest.py`.
- **Two different DB URL defaults.** `server/db/session.py` and `alembic/env.py` read `DATABASE_URL` straight from the environment and fall back to SQLite; `config.database_url` defaults to the docker-compose Postgres host and is not what the session uses.
- `.env.example` lists `API_HOST` / `API_PORT`, but the code reads `GHA_HOST` / `GHA_PORT`.
- The README says the data store is SQLite and the server needs only fastapi/uvicorn; the current server also needs SQLAlchemy and (for webhooks) Celery + Redis. The README also still lists a GNN ("patent core") and matrix reduction, both removed. **Do not fix the README — it is written at the end.**
- Scripts that print emoji reconfigure stdout to UTF-8 for Windows consoles; keep that when adding new entry-point scripts.
- `.gitignore` covers `.gha_optimizer/`, `*.db`, `__pycache__/`, `*.pyc`, `*.egg-info/`, `.pytest_cache/`. The demos write proposed workflows under `.gha_optimizer/snapshots/`, never over a tracked file.
- On Windows, source files whose temporary path exceeds about 240 characters are skipped when building per-commit graphs, and the PowerShell tool blocks commands that combine `Remove-Item` with a `*` anywhere in the command line.

## Working style

When asked to change something: inspect the current implementation, understand the existing architecture, identify the smallest correct change, implement it, run the relevant tests, then report exactly what changed, what you verified, and what remains incomplete.

Development environment is Windows 11 / PowerShell. Use `pathlib` for paths. Prefer dependencies with prebuilt Windows wheels — `torch-geometric` in particular is a known Windows install hazard and a plain-PyTorch GCN (Kipf & Welling, ICLR 2017) is the preferred route if the GNN is rebuilt.
